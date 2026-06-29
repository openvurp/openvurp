"""
openvurp Core — La Fucina

Auto-estensione delle capacità: quando l'agente incontra una lacuna che
i suoi tool non coprono, non si arrende — si forgia uno strumento nuovo.

Ciclo di vita, applicato dal runtime (non dal prompt):

    proposed → drafted → tested → adopted
                            ↘ rejected / retired

- propose:  dichiara la lacuna (cosa manca e perché vale)
- draft:    l'agente scrive il plugin (scaffold_plugin + write_file)
- test:     il selftest del plugin gira in un SUBPROCESS isolato con
            timeout; senza selftest verde non si adotta nulla
- adopt:    consentito solo se il test è passato E il codice non è
            cambiato dal test (hash-binding). Vietato nei cicli
            autonomi: di notte la fucina prepara, l'adozione avviene
            con l'owner sveglio.
- retire:   disattiva il plugin (manifest enabled=false)

Ogni capacità adottata porta la sua provenienza: da quale bisogno è
nata, quando è stata testata, quando è entrata in servizio.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime

FORGE_FILE = "forge.json"
SELFTEST_TIMEOUT = 60
MAX_OPEN_ENTRIES = 10

AUTONOMOUS_SOURCES = ("heartbeat", "cron", "subagent")


@dataclass
class ForgeEntry:
    id: str
    plugin_id: str
    need: str                 # la lacuna: cosa manca
    why: str = ""             # perché vale la pena
    status: str = "proposed"  # proposed | drafted | tested | adopted | rejected | retired
    origin: str = "owner"     # owner | agent | heartbeat
    created: str = ""
    tested_at: str = ""
    test_report: str = ""
    code_hash: str = ""       # hash del codice al momento del test verde
    adopted_at: str = ""
    retired_at: str = ""
    history: list = field(default_factory=list)   # [{ts, event}]

    def log(self, event: str):
        self.history.append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event[:200],
        })
        if len(self.history) > 30:
            self.history = self.history[-30:]


class ForgeError(Exception):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean(text: str, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


class Forge:
    def __init__(self, memory_dir: str, openvurp_dir: str):
        self.memory_dir = memory_dir
        self.openvurp_dir = openvurp_dir
        self.path = os.path.join(memory_dir, FORGE_FILE)
        self._entries: list[ForgeEntry] = []
        self._mtime: float = -1.0
        self._load()

    # ── Persistenza ──

    def _load(self):
        try:
            stat = os.stat(self.path)
        except OSError:
            self._entries = []
            self._mtime = -1.0
            return
        if stat.st_mtime == self._mtime:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._entries = [ForgeEntry(**e) for e in json.load(f)]
            self._mtime = stat.st_mtime
        except Exception:
            self._entries = []

    def _save(self):
        os.makedirs(self.memory_dir, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([asdict(e) for e in self._entries], f,
                      indent=2, ensure_ascii=False)
        try:
            self._mtime = os.stat(self.path).st_mtime
        except OSError:
            pass

    def _growth(self, note: str):
        try:
            from core.growth import record_growth_event
            record_growth_event(self.memory_dir, "forge", note)
        except Exception:
            pass

    # ── Helpers ──

    def get(self, forge_id: str) -> ForgeEntry:
        self._load()
        for e in self._entries:
            if e.id == forge_id:
                return e
        raise ForgeError(f"Voce di fucina non trovata: {forge_id}")

    def _plugin_dir(self, plugin_id: str) -> str:
        return os.path.join(self.openvurp_dir, "plugins", plugin_id)

    def _hash_plugin(self, plugin_id: str) -> str:
        """Hash del codice del plugin (init + manifest): lega il test al codice."""
        h = hashlib.sha256()
        plugin_dir = self._plugin_dir(plugin_id)
        for name in ("__init__.py", "manifest.json"):
            fpath = os.path.join(plugin_dir, name)
            try:
                with open(fpath, "rb") as f:
                    h.update(f.read())
            except OSError:
                h.update(b"missing:" + name.encode())
        return h.hexdigest()[:16]

    # ── Ciclo di vita ──

    def propose(self, plugin_id: str, need: str, why: str = "",
                origin: str = "owner") -> ForgeEntry:
        self._load()
        plugin_id = _clean(plugin_id, 60)
        need = _clean(need, 300)
        if not plugin_id.isidentifier():
            raise ForgeError("plugin_id non valido: usa un identificatore Python semplice.")
        if len(need) < 15:
            raise ForgeError(
                "Descrivi la lacuna sul serio: cosa non riesci a fare oggi, "
                "in quale situazione ti è mancato?"
            )
        open_entries = [e for e in self._entries
                        if e.status in ("proposed", "drafted", "tested")]
        if len(open_entries) >= MAX_OPEN_ENTRIES:
            raise ForgeError(
                f"Too many capabilities in progress ({MAX_OPEN_ENTRIES}): "
                "finish or reject something first."
            )
        for e in self._entries:
            if e.plugin_id == plugin_id and e.status not in ("rejected", "retired"):
                raise ForgeError(
                    f"C'è già una voce attiva per il plugin {plugin_id} [{e.id}]."
                )
        fid = hashlib.sha1(f"{plugin_id}:{need}".lower().encode()).hexdigest()[:8]
        entry = ForgeEntry(
            id=fid, plugin_id=plugin_id, need=need,
            why=_clean(why, 300),
            origin=origin if origin in ("owner", "agent", "heartbeat") else "agent",
            created=_now(),
        )
        entry.log(f"proposta: {need[:120]}")
        self._entries.append(entry)
        self._save()
        return entry

    def mark_drafted(self, forge_id: str) -> ForgeEntry:
        entry = self.get(forge_id)
        if entry.status not in ("proposed", "drafted", "tested"):
            raise ForgeError(f"Stato {entry.status}: non si può tornare in bozza.")
        init_path = os.path.join(self._plugin_dir(entry.plugin_id), "__init__.py")
        if not os.path.exists(init_path):
            raise ForgeError(
                f"Non esiste plugins/{entry.plugin_id}/__init__.py: "
                "scrivi prima il codice (scaffold_plugin + write_file)."
            )
        entry.status = "drafted"
        entry.log("bozza scritta")
        self._save()
        return entry

    def test(self, forge_id: str) -> ForgeEntry:
        """Esegue selftest() del plugin in un subprocess isolato.

        Convenzione: il modulo del plugin DEVE definire selftest() che
        ritorna True (o solleva) — è la prova che la capacità funziona.
        """
        entry = self.get(forge_id)
        init_path = os.path.join(self._plugin_dir(entry.plugin_id), "__init__.py")
        if not os.path.exists(init_path):
            raise ForgeError(f"Codice mancante: plugins/{entry.plugin_id}/__init__.py")

        probe = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('forge_probe', {init_path!r})\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "if not hasattr(mod, 'selftest'):\n"
            "    print('FORGE_FAIL: il plugin non definisce selftest()')\n"
            "    sys.exit(1)\n"
            "result = mod.selftest()\n"
            "if result is False:\n"
            "    print('FORGE_FAIL: selftest() ha ritornato False')\n"
            "    sys.exit(1)\n"
            "print('FORGE_PASS')\n"
        )
        # Il selftest deve poter importare core.tools anche se openvurp_dir
        # non è la dir del runtime (es. nei test): PYTHONPATH esplicito.
        runtime_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (self.openvurp_dir, runtime_dir, env.get("PYTHONPATH", "")) if p
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True, text=True,
                timeout=SELFTEST_TIMEOUT, cwd=self.openvurp_dir, env=env,
            )
            output = (proc.stdout + proc.stderr).strip()
            passed = proc.returncode == 0 and "FORGE_PASS" in proc.stdout
        except subprocess.TimeoutExpired:
            output = f"timeout dopo {SELFTEST_TIMEOUT}s"
            passed = False
        except Exception as e:
            output = f"errore esecuzione: {e}"
            passed = False

        entry.test_report = output[-1500:]
        entry.tested_at = _now()
        if passed:
            entry.status = "tested"
            entry.code_hash = self._hash_plugin(entry.plugin_id)
            entry.log("selftest PASSATO")
        else:
            entry.status = "drafted"
            entry.code_hash = ""
            entry.log(f"selftest fallito: {output[:120]}")
        self._save()
        if not passed:
            raise ForgeError(f"Selftest fallito:\n{output[-800:]}")
        return entry

    def adopt(self, forge_id: str, source: str = "cli") -> ForgeEntry:
        entry = self.get(forge_id)
        if source in AUTONOMOUS_SOURCES:
            raise ForgeError(
                "Adopting a new capability does not happen in autonomous "
                "cycles: the forge prepares and tests, but the go-ahead "
                "comes when the owner is present."
            )
        if entry.status != "tested":
            raise ForgeError(
                f"Status {entry.status}: adopt only after a green "
                f"selftest (action=test)."
            )
        current = self._hash_plugin(entry.plugin_id)
        if current != entry.code_hash:
            entry.status = "drafted"
            entry.log("code changed after the test: retest required")
            self._save()
            raise ForgeError(
                "The plugin code changed after the selftest: "
                "rerun the test before adopting."
            )
        entry.status = "adopted"
        entry.adopted_at = _now()
        entry.log("adopted: capability in service")
        self._save()
        self._growth(f"capability forged: {entry.plugin_id} — {entry.need[:60]}")
        return entry

    def reject(self, forge_id: str, reason: str = "") -> ForgeEntry:
        entry = self.get(forge_id)
        if entry.status in ("adopted",):
            raise ForgeError("È già in servizio: usa retire, non reject.")
        entry.status = "rejected"
        entry.log(f"rifiutata: {reason[:150]}" if reason else "rifiutata")
        self._save()
        return entry

    def retire(self, forge_id: str, reason: str = "") -> ForgeEntry:
        entry = self.get(forge_id)
        if entry.status != "adopted":
            raise ForgeError(f"Stato {entry.status}: si ritira solo ciò che è in servizio.")
        manifest_path = os.path.join(self._plugin_dir(entry.plugin_id), "manifest.json")
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            manifest["enabled"] = False
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, ensure_ascii=False)
        except Exception as e:
            raise ForgeError(f"Impossibile disattivare il manifest: {e}")
        entry.status = "retired"
        entry.retired_at = _now()
        entry.log(f"ritirata: {reason[:150]}" if reason else "ritirata")
        self._save()
        self._growth(f"capacità ritirata: {entry.plugin_id}")
        return entry

    # ── Rendering ──

    def adopted(self) -> list[ForgeEntry]:
        self._load()
        return [e for e in self._entries if e.status == "adopted"]

    def in_progress(self) -> list[ForgeEntry]:
        self._load()
        return [e for e in self._entries
                if e.status in ("proposed", "drafted", "tested")]

    def heartbeat_state(self) -> str:
        """Righe per lo stato vivo dell'heartbeat: capacità pronte da
        proporre all'owner, bozze da completare."""
        pending = self.in_progress()
        if not pending:
            return ""
        lines = [f"Fucina — capacità in lavorazione ({len(pending)}):"]
        for e in pending[:3]:
            stato = {
                "proposed": "da scrivere",
                "drafted": "da testare",
                "tested": "PRONTA: chiedi all'owner se adottarla",
            }.get(e.status, e.status)
            lines.append(f"- [{e.id}] {e.plugin_id}: {e.need[:80]} ({stato})")
        return "\n".join(lines)

    def render_status(self) -> str:
        self._load()
        if not self._entries:
            return (
                "The forge is cold. When the agent lacks a capability "
                "(a tool it doesn't have), it declares it here, writes the plugin, "
                "tests it in isolation and — with your go-ahead — adopts it. "
                "Capabilities aren't downloaded: they're forged."
            )
        adopted = self.adopted()
        pending = self.in_progress()
        closed = [e for e in self._entries if e.status in ("rejected", "retired")]
        lines = [f"{len(adopted)} in service · {len(pending)} in progress · {len(closed)} closed", ""]
        if adopted:
            lines.append("In service:")
            for e in adopted:
                lines.append(f"  [{e.id}] {e.plugin_id} — born from: {e.need[:80]}")
                lines.append(f"        tested {e.tested_at[:10]} · adopted {e.adopted_at[:10]}")
        if pending:
            lines.append("")
            lines.append("In progress:")
            for e in pending:
                lines.append(f"  [{e.id}] {e.plugin_id} ({e.status}) — {e.need[:80]}")
                if e.status == "drafted" and e.test_report:
                    lines.append(f"        last test: {e.test_report.splitlines()[-1][:100]}")
        if closed:
            lines.append("")
            for e in closed[-3:]:
                lines.append(f"  [{e.id}] {e.plugin_id} ({e.status})")
        return "\n".join(lines)
