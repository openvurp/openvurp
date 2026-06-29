"""
openvurp Core — Legame

Il lato umano dell'iniziativa: non solo "ho un motivo operativo per
scriverti", ma una relazione che vive nel tempo.

Tre pezzi:

1. **Fili** — quando l'owner racconta qualcosa che ha un dopo ("domani ho
   il colloquio", "stasera la partita"), l'agente lega un filo: cosa, quando
   richiedere, perché conta. Al momento giusto scrive LUI: "com'è andata?"

2. **Consapevolezza del silenzio** — l'agente sa da quanto l'owner non
   scrive. Un silenzio lungo non è un allarme: è contesto. Se ha un filo
   maturo o qualcosa di vero da dire, il silenzio è un buon momento per
   farsi vivo. Mai per riempire il vuoto.

3. **Ritmo rispettoso** — i messaggi spontanei hanno un budget (pochi al
   giorno, distanziati) e si adattano: se l'owner risponde, il ritmo va
   bene; se li ignora, l'agente si fa più discreto da solo. Un amico vero
   non è appiccicoso.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime

BONDS_FILE = "bonds.json"
MAX_OPEN_THREADS = 15

# Ritmo dei messaggi spontanei
MAX_SPONTANEOUS_PER_DAY = 2
MIN_GAP_SECONDS = 3 * 3600          # almeno 3h tra spontanei
SHY_GAP_SECONDS = 24 * 3600         # se ignorato spesso: max 1 al giorno
REPLY_WINDOW_SECONDS = 12 * 3600    # risposta entro 12h = "ha gradito"
IGNORED_STREAK_TO_BACK_OFF = 3


@dataclass
class LifeThread:
    """Un filo: qualcosa nella vita dell'owner che merita un 'com'è andata?'"""
    id: str
    what: str            # "colloquio di lavoro da X"
    due: str             # ISO datetime: quando ha senso chiedere
    why: str = ""        # perché conta per lui
    status: str = "waiting"   # waiting | asked | closed
    created: str = ""
    asked_at: str = ""
    outcome: str = ""


class BondError(Exception):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean(text: str, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


def _parse_due(due: str) -> datetime:
    due = (due or "").strip()
    try:
        return datetime.fromisoformat(due)
    except ValueError:
        raise BondError(
            f"Data non valida: {due!r}. Usa ISO, es. 2026-06-13 o 2026-06-13T18:00."
        )


class Bonds:
    def __init__(self, memory_dir: str):
        self.memory_dir = memory_dir
        self.path = os.path.join(memory_dir, BONDS_FILE)
        self._threads: list[LifeThread] = []
        self._spontaneous: dict = {}
        self._mtime: float = -1.0
        self._load()

    # ── Persistenza ──

    def _load(self):
        try:
            stat = os.stat(self.path)
        except OSError:
            self._threads, self._spontaneous = [], {}
            self._mtime = -1.0
            return
        if stat.st_mtime == self._mtime:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._threads = [LifeThread(**t) for t in data.get("threads", [])]
            self._spontaneous = data.get("spontaneous", {}) or {}
            self._mtime = stat.st_mtime
        except Exception:
            self._threads, self._spontaneous = [], {}

    def _save(self):
        os.makedirs(self.memory_dir, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({
                "threads": [asdict(t) for t in self._threads],
                "spontaneous": self._spontaneous,
            }, f, indent=2, ensure_ascii=False)
        try:
            self._mtime = os.stat(self.path).st_mtime
        except OSError:
            pass

    # ── Fili ──

    def open_threads(self) -> list[LifeThread]:
        self._load()
        return [t for t in self._threads if t.status in ("waiting", "asked")]

    def add_thread(self, what: str, due: str, why: str = "") -> LifeThread:
        self._load()
        what = _clean(what, 200)
        if len(what) < 8:
            raise BondError("Descrivi il filo: cosa succede nella vita dell'owner?")
        due_dt = _parse_due(due)
        if len(self.open_threads()) >= MAX_OPEN_THREADS:
            raise BondError(f"Troppi fili aperti ({MAX_OPEN_THREADS}).")
        tid = hashlib.sha1(f"{what}:{due}".lower().encode()).hexdigest()[:8]
        for t in self._threads:
            if t.id == tid and t.status != "closed":
                raise BondError("Questo filo esiste già.")
        thread = LifeThread(
            id=tid, what=what,
            due=due_dt.isoformat(timespec="seconds"),
            why=_clean(why, 200), created=_now(),
        )
        self._threads.append(thread)
        self._save()
        return thread

    def due_threads(self, now: datetime | None = None) -> list[LifeThread]:
        """Fili maturi: è arrivato il momento di chiedere com'è andata."""
        now = now or datetime.now()
        ready = []
        for t in self.open_threads():
            if t.status != "waiting":
                continue
            try:
                if datetime.fromisoformat(t.due) <= now:
                    ready.append(t)
            except ValueError:
                continue
        return ready

    def mark_asked(self, thread_id: str) -> LifeThread:
        self._load()
        for t in self._threads:
            if t.id == thread_id and t.status == "waiting":
                t.status = "asked"
                t.asked_at = _now()
                self._save()
                return t
        raise BondError(f"Filo in attesa non trovato: {thread_id}")

    def close_thread(self, thread_id: str, outcome: str = "") -> LifeThread:
        self._load()
        for t in self._threads:
            if t.id == thread_id and t.status != "closed":
                t.status = "closed"
                t.outcome = _clean(outcome, 300)
                self._save()
                try:
                    from core.growth import record_growth_event
                    record_growth_event(
                        self.memory_dir, "bonds",
                        f"filo chiuso: {t.what[:60]}"
                        + (f" — {t.outcome[:60]}" if t.outcome else ""),
                    )
                except Exception:
                    pass
                return t
        raise BondError(f"Filo aperto non trovato: {thread_id}")

    # ── Ritmo dei messaggi spontanei ──

    def record_spontaneous(self, delivered: bool = True):
        """L'agente ha appena scritto di sua iniziativa.

        delivered=True  → messaggio realmente consegnato all'owner (mangia budget)
        delivered=False → tick silenzioso del runtime (NON mangia budget)
        """
        self._load()
        today = datetime.now().date().isoformat()
        s = self._spontaneous
        if s.get("day") != today:
            s["day"] = today
            s["count_today"] = 0
        if delivered:
            s["count_today"] = int(s.get("count_today", 0)) + 1
            s["last_sent_at"] = time.time()
            s["last_replied"] = False
        else:
            # Tick silenzioso: aggiorna solo il timestamp di ultima attività,
            # NON tocca il budget dei messaggi veri.
            s["last_tick_at"] = time.time()
        self._spontaneous = s
        self._save()

    def record_owner_reply(self):
        """L'owner ha scritto: se c'era uno spontaneo recente, l'ha gradito."""
        self._load()
        s = self._spontaneous
        last_sent = float(s.get("last_sent_at", 0) or 0)
        if not last_sent or s.get("last_replied"):
            return
        if time.time() - last_sent <= REPLY_WINDOW_SECONDS:
            s["last_replied"] = True
            s["ignored_streak"] = 0
        else:
            s["ignored_streak"] = int(s.get("ignored_streak", 0)) + 1
        self._spontaneous = s
        self._save()

    def can_write_spontaneous(self) -> tuple[bool, str]:
        """Il runtime dice se c'è budget per un messaggio spontaneo oggi."""
        self._load()
        s = self._spontaneous
        today = datetime.now().date().isoformat()
        count_today = int(s.get("count_today", 0)) if s.get("day") == today else 0
        last_sent = float(s.get("last_sent_at", 0) or 0)
        ignored = int(s.get("ignored_streak", 0))
        # Se uno spontaneo è rimasto senza risposta troppo a lungo, conta
        # come ignorato (senza aspettare il prossimo messaggio dell'owner)
        if (last_sent and not s.get("last_replied")
                and time.time() - last_sent > REPLY_WINDOW_SECONDS):
            ignored = max(ignored, 1)

        gap = SHY_GAP_SECONDS if ignored >= IGNORED_STREAK_TO_BACK_OFF else MIN_GAP_SECONDS
        max_per_day = 1 if ignored >= IGNORED_STREAK_TO_BACK_OFF else MAX_SPONTANEOUS_PER_DAY

        if count_today >= max_per_day:
            return False, f"già {count_today} messaggi spontanei oggi"
        if last_sent and time.time() - last_sent < gap:
            hours = gap // 3600
            return False, f"ultimo spontaneo troppo recente (aspetta ~{hours}h)"
        return True, ""

    # ── Silenzio ──

    def silence_seconds(self) -> float:
        """Da quanto l'owner non scrive su NESSUN canale (0 = mai visto)."""
        try:
            from core.presence import Presence
            presence = Presence(self.memory_dir)
            last = max(
                (presence.last_seen(ch) for ch in ("cli", "telegram", "discord",
                                                   "slack", "signal")),
                default=0.0,
            )
            if not last:
                return 0.0
            return max(0.0, time.time() - last)
        except Exception:
            return 0.0

    # ── Rendering ──

    def heartbeat_state(self) -> str:
        """Blocco per lo stato vivo dell'heartbeat."""
        lines: list[str] = []
        due = self.due_threads()
        if due:
            lines.append(f"Fili maturi ({len(due)}) — è il momento di chiedere com'è andata:")
            for t in due[:3]:
                lines.append(f"- [{t.id}] {t.what}" + (f" (perché: {t.why})" if t.why else ""))

        silence = self.silence_seconds()
        if silence >= 36 * 3600:
            days = int(silence // 86400)
            hours = int((silence % 86400) // 3600)
            span = f"{days}g {hours}h" if days else f"{hours}h"
            lines.append(
                f"L'owner non scrive da {span}. Non è un allarme: se hai un "
                f"filo maturo o qualcosa di vero da dirgli, è un buon momento "
                f"per farti vivo con calore. Mai messaggi di solo 'ci sono?'."
            )

        ok, reason = self.can_write_spontaneous()
        if not ok:
            lines.append(f"Niente messaggi spontanei adesso ({reason}).")
        return "\n".join(lines)

    def render_status(self) -> str:
        self._load()
        open_t = self.open_threads()
        closed = [t for t in self._threads if t.status == "closed"]
        if not self._threads:
            return (
                "No threads yet. When you tell the agent something that "
                "has an aftermath (an interview, an exam, a match), it ties a "
                "thread — and at the right moment IT messages YOU to ask how it "
                "went. Like a friend would."
            )
        lines = [f"{len(open_t)} open threads · {len(closed)} closed", ""]
        for t in open_t:
            stato = "waiting" if t.status == "waiting" else "asked, awaiting reply"
            lines.append(f"[{t.id}] {t.what} — {t.due[:16].replace('T', ' ')} ({stato})")
            if t.why:
                lines.append(f"    why: {t.why}")
        for t in closed[-3:]:
            lines.append(f"[{t.id}] {t.what} ✓"
                         + (f" — {t.outcome[:80]}" if t.outcome else ""))
        silence = self.silence_seconds()
        if silence >= 3600:
            lines.append("")
            lines.append(f"Your last message: {int(silence // 3600)}h ago.")
        return "\n".join(lines)
