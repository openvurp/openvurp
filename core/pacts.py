"""
openvurp Core — Patti

Accordi espliciti tra owner e agente, applicati dal runtime.

Un limite scritto nel prompt il modello *dovrebbe* rispettarlo.
Un patto è diverso: negoziato in conversazione, registrato
con data e motivo, e fatto rispettare da `_execute_tool` anche se il
modello ha un giorno storto. Le promesse si mantengono per costruzione.

Tipi di patto:
- protected_path: nessuna scrittura/shell che tocchi quel path
- confirm_external: conferma esplicita prima di ogni azione esterna
  (notify, invii); nei cicli autonomi le azioni esterne sono bloccate
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime

PACTS_FILE = "pacts.json"

PACT_TYPES = ("protected_path", "confirm_external")

# Tool che scrivono su file (per protected_path)
WRITE_TOOLS = {"write_file", "edit_file", "edit_lines", "append_file"}

# Tool che parlano col mondo esterno (per confirm_external)
EXTERNAL_TOOLS = {
    "notify", "notify_voice", "notify_photo", "notify_file",
    "schedule_notify",
}


@dataclass
class Pact:
    id: str
    pact_type: str
    description: str
    pattern: str = ""        # per protected_path: il path protetto
    reason: str = ""
    created: str = ""
    active: bool = True
    history: list = field(default_factory=list)


class PactError(Exception):
    pass


class Pacts:
    def __init__(self, memory_dir: str):
        self.memory_dir = memory_dir
        self.path = os.path.join(memory_dir, PACTS_FILE)
        self._pacts: list[Pact] = []
        self._mtime: float = -1.0
        self._load()

    def _load(self):
        try:
            stat = os.stat(self.path)
        except OSError:
            self._pacts = []
            self._mtime = -1.0
            return
        if stat.st_mtime == self._mtime:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._pacts = [Pact(**p) for p in data]
            self._mtime = stat.st_mtime
        except Exception:
            self._pacts = []

    def _save(self):
        os.makedirs(self.memory_dir, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([asdict(p) for p in self._pacts], f, indent=2, ensure_ascii=False)
        try:
            self._mtime = os.stat(self.path).st_mtime
        except OSError:
            pass

    def active_pacts(self) -> list[Pact]:
        self._load()
        return [p for p in self._pacts if p.active]

    def find(self, pact_id: str) -> Pact | None:
        self._load()
        for p in self._pacts:
            if p.id == pact_id:
                return p
        return None

    # ── Gestione ──

    def add(self, pact_type: str, description: str, pattern: str = "",
            reason: str = "") -> Pact:
        self._load()
        pact_type = (pact_type or "").strip().lower()
        if pact_type not in PACT_TYPES:
            raise PactError(
                f"Tipo di patto sconosciuto: {pact_type}. "
                f"Disponibili: {', '.join(PACT_TYPES)}"
            )
        description = " ".join((description or "").split())
        if len(description) < 8:
            raise PactError("Descrizione troppo corta: un patto va detto chiaramente.")
        pattern = (pattern or "").strip()
        if pact_type == "protected_path" and not pattern:
            raise PactError("protected_path richiede pattern: il path da proteggere.")

        now = datetime.now().isoformat(timespec="seconds")
        pact = Pact(
            id=hashlib.sha1(f"{pact_type}:{pattern}:{description}".encode()).hexdigest()[:8],
            pact_type=pact_type,
            description=description[:300],
            pattern=pattern,
            reason=(reason or "")[:200],
            created=now,
        )
        if any(p.id == pact.id and p.active for p in self._pacts):
            raise PactError("Questo patto esiste già.")
        self._pacts.append(pact)
        self._save()
        return pact

    def retire(self, pact_id: str, reason: str = "") -> Pact:
        pact = self.find(pact_id)
        if pact is None or not pact.active:
            raise PactError(f"Patto non trovato o già sciolto: {pact_id}")
        pact.active = False
        pact.history.append({
            "date": datetime.now().isoformat(timespec="seconds"),
            "event": "retired",
            "reason": (reason or "")[:200],
        })
        self._save()
        return pact

    # ── Enforcement (chiamato da _execute_tool) ──

    @staticmethod
    def _normalize(path: str) -> str:
        return path.replace("\\", "/").lower().strip()

    def check_tool_call(self, tool_name: str, tool_args: dict,
                        source: str = "cli") -> tuple[bool, str, bool]:
        """Verifica una tool call contro i patti attivi.

        Returns (allowed, reason, needs_confirm):
        - allowed False → blocco assoluto con motivazione
        - needs_confirm True → serve conferma dell'owner anche in auto mode
        """
        pacts = self.active_pacts()
        if not pacts:
            return True, "", False

        args = tool_args if isinstance(tool_args, dict) else {}

        for pact in pacts:
            if pact.pact_type == "protected_path":
                pattern = self._normalize(pact.pattern)
                if not pattern:
                    continue
                if tool_name in WRITE_TOOLS:
                    target = self._normalize(str(args.get("path", "")))
                    if pattern in target:
                        return False, (
                            f"[PATTO {pact.id}] '{pact.description}' — "
                            f"scrittura su '{args.get('path')}' bloccata dal runtime."
                        ), False
                if tool_name in ("shell", "", "process_start", "process_write"):
                    command = self._normalize(str(
                        args.get("command", "") or args.get("text", "")
                    ))
                    if pattern in command:
                        return False, (
                            f"[PATTO {pact.id}] '{pact.description}' — "
                            f"comando che tocca '{pact.pattern}' bloccato dal runtime."
                        ), False

            elif pact.pact_type == "confirm_external":
                if tool_name in EXTERNAL_TOOLS:
                    if source in ("heartbeat", "cron", "subagent"):
                        return False, (
                            f"[PATTO {pact.id}] '{pact.description}' — "
                            f"azione esterna in ciclo autonomo bloccata: "
                            f"serve l'owner presente."
                        ), False
                    return True, pact.description, True

        return True, "", False

    # ── Vista umana (/patti) ──

    def render_status(self) -> str:
        self._load()
        active = [p for p in self._pacts if p.active]
        retired = [p for p in self._pacts if not p.active]
        if not active and not retired:
            return (
                "No pacts made.\n"
                "A pact is a promise the runtime enforces: "
                "agree on it in chat and the agent records it with the `pact` tool.\n"
                "Types: protected_path (never touch a path), "
                "confirm_external (never external actions without confirmation)."
            )
        lines = [f"{len(active)} active pacts"
                 + (f" · {len(retired)} dissolved" if retired else ""), ""]
        for p in active:
            lines.append(f"[{p.id}] {p.pact_type}: {p.description}")
            if p.pattern:
                lines.append(f"         pattern: {p.pattern}")
            lines.append(f"         since {p.created[:10]}"
                         + (f" — {p.reason}" if p.reason else ""))
        return "\n".join(lines)
