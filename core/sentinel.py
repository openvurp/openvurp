"""
openvurp Core — Sentinella

L'agente deve accorgersi quando il mondo intorno a lui cambia: internet che
cade e poi torna, Ollama che muore, Telegram che si stacca. La sentinella è
un watchdog leggero che sonda questi servizi a intervalli regolari, rileva le
TRANSIZIONI (su→giù, giù→su) e reagisce:

- avvisa l'owner (canale disponibile, con coda se la consegna fallisce);
- tenta il recupero automatico dove può (es. riattacca Telegram);
- segnala il ritorno al heartbeat, così l'agente riprende il lavoro sospeso.

Niente LLM qui dentro: solo probe meccanici, debounce e stato persistito in
memory/sentinel.json (leggibile dall'agente per rispondere "come va la rete?").
La logica è pura e testabile: il clock e i probe si iniettano.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


# ── Probe standard ───────────────────────────────────────────────────────

def check_internet(timeout: float = 3.0) -> bool:
    """Connettività reale via TCP/443 verso più host pubblici (no DNS
    obbligatorio). True se ALMENO uno risponde."""
    import socket
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 443),
                       ("github.com", 443), ("www.google.com", 443)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def make_ollama_check(base_url: str, timeout: float = 3.0) -> Callable[[], bool]:
    """Probe per Ollama: GET /api/tags risponde ⇒ il server è vivo."""
    url = f"{(base_url or 'http://localhost:11434').rstrip('/')}/api/tags"

    def _check() -> bool:
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return 200 <= resp.status < 500
        except Exception:
            return False

    return _check


# ── Messaggi all'owner (brevi, umani, in transizione) ────────────────────

_DOWN_NOTICES = {
    "internet": "🌐 Internet non risponde. Continuo a lavorare in locale e ti avviso appena torna.",
    "ollama": "🧠 Ollama non risponde: senza di lui non posso pensare. Lo tengo d'occhio e ti avviso quando torna.",
    "telegram": "📡 Telegram si è staccato: provo a riagganciarlo da solo.",
}

_UP_NOTICES = {
    "internet": "🌐 Internet è tornato ({downtime} senza rete). Riprendo quello che era rimasto in sospeso.",
    "ollama": "🧠 Ollama è tornato ({downtime} di blackout). Sono di nuovo operativo.",
    "telegram": "📡 Telegram riagganciato ({downtime} di distacco). Eccomi.",
}


def format_downtime(seconds: float) -> str:
    """Durata leggibile: 45s, 12 min, 1h 05min."""
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rem = divmod(minutes, 60)
    return f"{hours}h {rem:02d}min"


# ── Stato ────────────────────────────────────────────────────────────────

@dataclass
class Probe:
    name: str
    check: Callable[[], bool]
    recover: Optional[Callable[[], bool]] = None
    wake_agent: bool = True          # al ritorno, sveglia il heartbeat
    label: str = ""
    # Stato runtime
    status: str = "unknown"          # unknown | up | down
    fails: int = 0
    oks: int = 0
    since: float = 0.0               # da quando è nello stato attuale
    down_at: float = 0.0             # inizio dell'ultimo periodo giù
    recover_attempts: int = 0
    next_recover_at: float = 0.0


@dataclass
class Transition:
    name: str
    status: str                      # "up" | "down"
    at: float
    downtime_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "at": self.at,
            "downtime_seconds": round(self.downtime_seconds, 1),
        }


class Sentinel:
    """Watchdog dei servizi vitali. `tick()` è sincrono e testabile;
    `start()` lo fa girare in un thread di background."""

    def __init__(
        self,
        workspace_dir: str,
        interval_seconds: int = 30,
        down_interval_seconds: int = 10,
        fails_to_down: int = 2,
        clock: Callable[[], float] = time.time,
    ):
        self.workspace_dir = workspace_dir
        self.interval_seconds = interval_seconds
        # Quando qualcosa è giù si sonda più spesso: il ritorno va colto presto.
        self.down_interval_seconds = down_interval_seconds
        self.fails_to_down = fails_to_down
        self._clock = clock

        self._probes: dict[str, Probe] = {}
        self._transitions: list[Transition] = []
        self._pending_notices: list[str] = []
        self._notify: Optional[Callable[[str], bool]] = None
        self._heartbeat = None

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()
        self._lock = threading.Lock()

    # ── Configurazione ──────────────────────────────────────────────────

    def add_probe(self, name: str, check: Callable[[], bool],
                  recover: Optional[Callable[[], bool]] = None,
                  wake_agent: bool = True, label: str = ""):
        self._probes[name] = Probe(
            name=name, check=check, recover=recover,
            wake_agent=wake_agent, label=label or name,
        )

    def set_notifier(self, fn: Callable[[str], bool]):
        """fn(testo) → True se consegnato all'owner. Se False, il messaggio
        resta in coda e viene ritentato al tick successivo (es. Telegram giù:
        l'avviso arriva appena si riattacca)."""
        self._notify = fn

    def attach_heartbeat(self, heartbeat):
        """Ogni transizione diventa un evento del heartbeat; al ritorno di un
        servizio con wake_agent il heartbeat scatta subito, così l'agente
        riprende il lavoro sospeso senza aspettare il prossimo intervallo."""
        self._heartbeat = heartbeat

    # ── Ciclo di vita ───────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="sentinel"
        )
        self._thread.start()

    def stop(self):
        self._running = False
        self._wake.set()

    def check_now(self):
        """Sonda subito, senza aspettare l'intervallo (es. l'agente ha appena
        visto Ollama irraggiungibile). Non blocca il chiamante."""
        if self._running:
            self._wake.set()
        else:
            threading.Thread(target=self.tick, daemon=True,
                             name="sentinel-now").start()

    def _loop(self):
        while self._running:
            try:
                self.tick()
            except Exception:
                pass
            interval = self.interval_seconds
            if any(p.status == "down" for p in self._probes.values()):
                interval = self.down_interval_seconds
            self._wake.wait(timeout=interval)
            self._wake.clear()

    # ── Cuore: un giro di sonde ─────────────────────────────────────────

    def tick(self) -> list[Transition]:
        """Sonda tutti i servizi, applica il debounce, gestisce transizioni,
        recuperi e notifiche. Ritorna le transizioni di questo giro."""
        with self._lock:
            now = self._clock()
            happened: list[Transition] = []

            for probe in self._probes.values():
                try:
                    ok = bool(probe.check())
                except Exception:
                    ok = False

                if ok:
                    probe.oks += 1
                    probe.fails = 0
                    if probe.status != "up":
                        happened.append(self._mark_up(probe, now))
                else:
                    probe.fails += 1
                    probe.oks = 0
                    if probe.status == "up" and probe.fails >= self.fails_to_down:
                        happened.append(self._mark_down(probe, now))
                    elif probe.status == "unknown" and probe.fails >= self.fails_to_down:
                        # Anche partire già senza rete va segnalato.
                        happened.append(self._mark_down(probe, now))

                # Recupero automatico con backoff mentre è giù
                if probe.status == "down" and probe.recover and now >= probe.next_recover_at:
                    probe.recover_attempts += 1
                    probe.next_recover_at = now + min(
                        15 * (2 ** probe.recover_attempts), 300
                    )
                    try:
                        probe.recover()
                    except Exception:
                        pass

            for tr in happened:
                self._on_transition(tr)

            self._flush_notices()
            if happened:
                self._persist()
            return happened

    def _mark_up(self, probe: Probe, now: float) -> Transition:
        was_down = probe.status == "down"
        downtime = (now - probe.down_at) if (was_down and probe.down_at) else 0.0
        first_sight = probe.status == "unknown"
        probe.status = "up"
        probe.since = now
        probe.recover_attempts = 0
        probe.next_recover_at = 0.0
        tr = Transition(probe.name, "up", now, downtime)
        # Il primo avvistamento all'avvio non è una notizia.
        tr.downtime_seconds = downtime
        if first_sight:
            tr.downtime_seconds = -1.0  # marcatore interno: silenzioso
        return tr

    def _mark_down(self, probe: Probe, now: float) -> Transition:
        probe.status = "down"
        probe.since = now
        probe.down_at = now
        probe.next_recover_at = 0.0  # primo tentativo di recupero subito
        return Transition(probe.name, "down", now, 0.0)

    def _on_transition(self, tr: Transition):
        # Avvio silenzioso: unknown→up non è un evento da raccontare.
        silent_first_up = tr.status == "up" and tr.downtime_seconds < 0
        if silent_first_up:
            return

        probe = self._probes.get(tr.name)
        label = probe.label if probe else tr.name

        if tr.status == "down":
            notice = _DOWN_NOTICES.get(
                tr.name, f"⚠️ {label} non risponde. Lo tengo d'occhio.")
            event = f"{label} è caduto: non risponde ai probe della sentinella."
        else:
            downtime = format_downtime(tr.downtime_seconds)
            notice = _UP_NOTICES.get(
                tr.name, f"✅ {label} è tornato ({downtime} di assenza)."
            ).format(downtime=downtime)
            event = (f"{label} è tornato dopo {downtime} di assenza: se c'era "
                     f"lavoro sospeso per la sua mancanza, ora si può riprendere.")

        self._pending_notices.append(notice)

        if self._heartbeat is not None:
            try:
                self._heartbeat.add_event(event)
                if tr.status == "up" and probe is not None and probe.wake_agent:
                    self._heartbeat.trigger_now(reason=f"{tr.name}_back")
            except Exception:
                pass

        self._transitions.append(tr)
        if len(self._transitions) > 50:
            self._transitions = self._transitions[-50:]

    def _flush_notices(self):
        if not self._pending_notices or not self._notify:
            return
        remaining: list[str] = []
        for notice in self._pending_notices:
            delivered = False
            try:
                delivered = bool(self._notify(notice))
            except Exception:
                delivered = False
            if not delivered:
                remaining.append(notice)
        self._pending_notices = remaining

    # ── Stato leggibile ─────────────────────────────────────────────────

    def status(self) -> dict:
        now = self._clock()
        return {
            "checked_at": now,
            "services": {
                name: {
                    "label": p.label,
                    "status": p.status,
                    "for_seconds": round(now - p.since, 1) if p.since else 0,
                }
                for name, p in self._probes.items()
            },
            "recent_transitions": [t.to_dict() for t in self._transitions[-10:]],
        }

    def _persist(self):
        try:
            memory_dir = os.path.join(self.workspace_dir, "memory")
            os.makedirs(memory_dir, exist_ok=True)
            path = os.path.join(memory_dir, "sentinel.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.status(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass
