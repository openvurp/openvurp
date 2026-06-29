"""
openvurp Security — Audit Log

Log immutabile di tutte le azioni sensibili.
Hash chain (come blockchain) per garantire integrità.
"""

from __future__ import annotations

import os
import json
import time
import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum


class AuditAction(Enum):
    """Tipi di azione loggati."""
    SHELL_EXEC = "shell_exec"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    TOOL_CALL = "tool_call"
    LLM_CALL = "llm_call"
    AUTH_SUCCESS = "auth_success"
    AUTH_FAILURE = "auth_failure"
    PERMISSION_DENIED = "permission_denied"
    PERMISSION_GRANTED = "permission_granted"
    CONFIG_CHANGE = "config_change"
    MCP_CONNECT = "mcp_connect"
    MCP_TOOL_CALL = "mcp_tool_call"
    PLUGIN_LOAD = "plugin_load"
    VAULT_ACCESS = "vault_access"
    SESSION_START = "session_start"
    SESSION_END = "session_end"


@dataclass
class AuditEvent:
    """Singolo evento di audit."""
    timestamp: float
    action: str
    actor: str           # chi ha causato l'azione (user, agent, plugin, channel:telegram)
    target: str          # su cosa (file path, tool name, command)
    details: str = ""    # dettagli aggiuntivi (redacted)
    risk_level: str = "low"  # low, medium, high, critical
    success: bool = True
    source: str = "cli"  # canale di origine
    hash: str = ""       # hash di questo evento
    prev_hash: str = ""  # hash evento precedente (chain)


# Pattern per redazione automatica di secrets
_REDACT_PATTERNS = [
    (re.compile(r'(sk-[a-zA-Z0-9]{20,})'), r'sk-***REDACTED***'),
    (re.compile(r'(sk-ant-[a-zA-Z0-9]{20,})'), r'sk-ant-***REDACTED***'),
    (re.compile(r'(gsk_[a-zA-Z0-9]{20,})'), r'gsk_***REDACTED***'),
    (re.compile(r'(ghp_[a-zA-Z0-9]{20,})'), r'ghp_***REDACTED***'),
    (re.compile(r'(xoxb-[a-zA-Z0-9\-]+)'), r'xoxb-***REDACTED***'),
    (re.compile(r'(\d{6,}:[A-Za-z0-9_\-]{30,})'), r'***TELEGRAM_TOKEN***'),
    (re.compile(r'(eyJ[a-zA-Z0-9_\-]{50,})'), r'***JWT_REDACTED***'),
    (re.compile(r'(password|passwd|pwd|secret|token|key|credential)\s*[=:]\s*\S+',
                re.IGNORECASE), r'\1=***REDACTED***'),
    (re.compile(r'(Authorization:\s*Bearer\s+)\S+', re.IGNORECASE), r'\1***REDACTED***'),
]


def redact(text: str) -> str:
    """Rimuove secrets da una stringa."""
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class AuditLog:
    """
    Audit log append-only con hash chain.

    Ogni evento contiene l'hash dell'evento precedente, formando
    una catena verificabile. Se qualcuno modifica un evento passato,
    la catena si rompe.
    """

    AUDIT_FILE = "audit.jsonl"
    MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB, poi ruota

    def __init__(self, audit_dir: str):
        self.audit_dir = audit_dir
        self._audit_path = os.path.join(audit_dir, self.AUDIT_FILE)
        self._last_hash = "genesis"
        self._event_count = 0

        os.makedirs(audit_dir, exist_ok=True)

        # Recupera ultimo hash dalla catena esistente
        self._recover_chain()

        # Permessi restrittivi
        try:
            if os.path.exists(self._audit_path):
                os.chmod(self._audit_path, 0o600)
        except OSError:
            pass

    def log(self, action: AuditAction, actor: str, target: str,
            details: str = "", risk_level: str = "low",
            success: bool = True, source: str = "cli"):
        """Logga un evento di audit."""
        # Redazione automatica
        target = redact(target)
        details = redact(details)

        event = AuditEvent(
            timestamp=time.time(),
            action=action.value,
            actor=actor,
            target=target,
            details=details[:500],  # Limita dettagli
            risk_level=risk_level,
            success=success,
            source=source,
            prev_hash=self._last_hash,
        )

        # Calcola hash dell'evento
        event.hash = self._hash_event(event)
        self._last_hash = event.hash
        self._event_count += 1

        # Append al file
        self._append(event)

        # Rotazione se necessario
        self._maybe_rotate()

    def log_shell(self, command: str, actor: str = "agent",
                  success: bool = True, source: str = "cli"):
        """Shortcut per log di comandi shell."""
        # Determina risk level dal comando
        risk = "low"
        cmd_lower = command.lower()
        if any(w in cmd_lower for w in ("sudo", "rm ", "kill", "docker")):
            risk = "high"
        elif any(w in cmd_lower for w in ("pip", "npm", "git push", "mv ")):
            risk = "medium"

        self.log(
            AuditAction.SHELL_EXEC,
            actor=actor,
            target=command[:200],
            risk_level=risk,
            success=success,
            source=source,
        )

    def log_tool(self, tool_name: str, args_summary: str = "",
                 actor: str = "agent", success: bool = True, source: str = "cli"):
        """Shortcut per log di tool call."""
        self.log(
            AuditAction.TOOL_CALL,
            actor=actor,
            target=tool_name,
            details=args_summary[:200],
            success=success,
            source=source,
        )

    def log_auth(self, user_id: str, success: bool, source: str = "cli"):
        """Shortcut per log di autenticazione."""
        action = AuditAction.AUTH_SUCCESS if success else AuditAction.AUTH_FAILURE
        risk = "low" if success else "high"
        self.log(action, actor=user_id, target="auth", risk_level=risk, source=source)

    def verify_chain(self) -> tuple[bool, int, str]:
        """
        Verifica integrità della hash chain.
        Returns: (valid, num_events, message)
        """
        if not os.path.exists(self._audit_path):
            return True, 0, "Nessun audit log."

        prev_hash = "genesis"
        count = 0

        try:
            with open(self._audit_path, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue

                    event_data = json.loads(line)
                    event = AuditEvent(**event_data)
                    count += 1

                    # Verifica catena
                    if event.prev_hash != prev_hash:
                        return False, count, (
                            f"Chain rotta alla riga {line_num}: "
                            f"prev_hash atteso={prev_hash}, trovato={event.prev_hash}"
                        )

                    # Verifica hash dell'evento
                    computed = self._hash_event(event)
                    if computed != event.hash:
                        return False, count, (
                            f"Hash evento corrotto alla riga {line_num}: "
                            f"calcolato={computed}, registrato={event.hash}"
                        )

                    prev_hash = event.hash

            return True, count, f"Chain valida: {count} eventi verificati."

        except Exception as e:
            return False, count, f"Errore verifica: {e}"

    def get_recent(self, n: int = 50) -> list[dict]:
        """Ultimi N eventi."""
        if not os.path.exists(self._audit_path):
            return []

        events = []
        try:
            with open(self._audit_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
        except Exception:
            pass

        return events[-n:]

    def get_by_action(self, action: AuditAction, n: int = 50) -> list[dict]:
        """Eventi filtrati per tipo di azione."""
        all_events = self.get_recent(n=1000)
        return [e for e in all_events if e.get("action") == action.value][-n:]

    def get_failures(self, n: int = 50) -> list[dict]:
        """Eventi falliti (potenziali attacchi o errori)."""
        all_events = self.get_recent(n=1000)
        return [e for e in all_events if not e.get("success", True)][-n:]

    # ── Internals ──

    def _hash_event(self, event: AuditEvent) -> str:
        """Calcola SHA256 dell'evento (escluso il campo hash)."""
        data = {
            "timestamp": event.timestamp,
            "action": event.action,
            "actor": event.actor,
            "target": event.target,
            "details": event.details,
            "risk_level": event.risk_level,
            "success": event.success,
            "source": event.source,
            "prev_hash": event.prev_hash,
        }
        raw = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _append(self, event: AuditEvent):
        """Append evento al file."""
        try:
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _recover_chain(self):
        """Recupera ultimo hash dalla catena esistente."""
        if not os.path.exists(self._audit_path):
            return

        try:
            last_line = ""
            with open(self._audit_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        last_line = line
                        self._event_count += 1

            if last_line:
                event_data = json.loads(last_line)
                self._last_hash = event_data.get("hash", "genesis")
        except Exception:
            pass

    def _maybe_rotate(self):
        """Ruota il file se troppo grande."""
        try:
            if os.path.exists(self._audit_path):
                size = os.path.getsize(self._audit_path)
                if size > self.MAX_FILE_SIZE:
                    # Rinomina con timestamp
                    rotated = self._audit_path + f".{int(time.time())}"
                    os.rename(self._audit_path, rotated)
                    self._last_hash = "rotated"
                    try:
                        os.chmod(rotated, 0o400)  # Read-only
                    except OSError:
                        pass
        except Exception:
            pass
