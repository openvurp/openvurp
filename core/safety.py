"""
openvurp Core — Safety & Guardrails

Classificazione rischio comandi, permission system, pattern blocking.
"""

from __future__ import annotations

import re
import os
from enum import Enum
from typing import Optional


class ActionRisk(Enum):
    SAFE = "safe"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


# Pattern critici — sempre bloccati senza conferma
CRITICAL_PATTERNS = [
    r'rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/',       # rm -rf /
    r'rm\s+-[a-zA-Z]*r[a-zA-Z]*\s+/',            # rm -r /
    r'dd\s+.*of=/dev/',                            # dd to device
    r'mkfs\.',                                      # format disk
    r'>\s*/dev/sd',                                 # overwrite disk
    r':\(\)\s*\{\s*:\|:&\s*\}\s*;',               # fork bomb
    r'chmod\s+(-R\s+)?777\s+/',                    # chmod 777 /
    r'chown\s+.*\s+/',                             # chown /
    r'>\s*/etc/',                                   # overwrite system config
    r'format\s+[a-zA-Z]:',                         # Windows format
    r'shutdown|reboot|poweroff|init\s+[06]',       # system shutdown
    r'git\s+push\s+.*--force\s+.*main',            # force push main
    r'git\s+push\s+.*--force\s+.*master',          # force push master
]

# Pattern che richiedono approvazione
APPROVAL_PATTERNS = [
    r'sudo\s+',                                     # sudo
    r'apt\s+(install|remove|purge)',                 # apt install/remove
    r'apt-get\s+(install|remove|purge)',             # apt-get
    r'pip\s+install\s+(?!-e\s+\.)',                 # pip install (non locale)
    r'npm\s+install\s+-g',                          # npm global install
    r'git\s+push',                                   # git push
    r'git\s+reset\s+--hard',                        # git reset hard
    r'docker\s+(rm|rmi|system\s+prune)',            # docker cleanup
    r'curl\s+.*\|\s*(ba)?sh',                       # pipe to shell
    r'wget\s+.*\|\s*(ba)?sh',                       # pipe to shell
    r'systemctl\s+(start|stop|restart|enable|disable)', # service management
    r'service\s+\w+\s+(start|stop|restart)',        # service management
    r'\brm\s+',                                      # rm (qualsiasi)
    r'\brmdir\s+',                                   # rmdir
    # nota: `mv` non richiede approvazione — spostare/rinominare nel
    # workspace è lavoro quotidiano; i path concordati sono protetti dai patti
    r'del\s+',                                       # del (Windows)
    r'rd\s+',                                        # rd (Windows)
    r'truncate\s+',                                  # truncate file
    r'shred\s+',                                     # shred file
    r'git\s+branch\s+-[dD]',                         # delete branch
    r'git\s+checkout\s+--\s+\.',                     # discard changes
    r'git\s+clean\s+-[a-zA-Z]*f',                   # git clean -f
    r'kill\s+',                                      # kill process
    r'killall\s+',                                   # killall
    r'pkill\s+',                                     # pkill
]

# Comandi safe — nessun rischio
SAFE_PATTERNS = [
    r'^ls\b', r'^dir\b', r'^pwd\b', r'^echo\b',
    r'^cat\b', r'^head\b', r'^tail\b', r'^less\b', r'^more\b',
    r'^grep\b', r'^rg\b', r'^find\b', r'^which\b', r'^where\b',
    r'^wc\b', r'^sort\b', r'^uniq\b', r'^diff\b',
    r'^date\b', r'^whoami\b', r'^hostname\b', r'^uname\b',
    r'^env\b', r'^printenv\b', r'^set\b',
    r'^file\b', r'^stat\b', r'^du\b', r'^df\b',
    r'^python\s+-c\b', r'^python3\s+-c\b',
    r'^git\s+(status|log|diff|branch|show|remote)',
    r'^git\s+add\b', r'^git\s+commit\b',
    r'^ps\b', r'^top\b', r'^htop\b',
    r'^curl\s+(?!.*\|\s*(ba)?sh)',                   # curl senza pipe a sh
    r'^wget\s+(?!.*\|\s*(ba)?sh)',                   # wget senza pipe a sh
]


# File e cartelle che i tool dell'agente non devono leggere né scrivere:
# segreti e superfici di sicurezza. Anche se è dentro il workspace, l'agente
# non ci accede — così un prompt injection non ha un bersaglio pronto.
SECRET_FILE_NAMES = {
    ".env", ".env.local", ".env.production",
    "acl.json", ".integrity_baseline.json",
}
SECRET_DIR_NAMES = {
    "audit", "vault", ".backups", ".reset_baseline",
}


class SafetyGuard:
    def __init__(self, openvurp_dir: str = ""):
        self.openvurp_dir = openvurp_dir
        self.critical_files = []
        if openvurp_dir:
            self.critical_files = [
                os.path.join(openvurp_dir, f) for f in ('agent.py', 'main.py', 'config.py')
            ]

    def classify(self, command: str) -> ActionRisk:
        """Classifica il rischio di un comando."""
        cmd = command.strip()

        # Check critical first
        for pattern in CRITICAL_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                return ActionRisk.CRITICAL

        # Check approval
        for pattern in APPROVAL_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                return ActionRisk.HIGH

        # Check safe
        for pattern in SAFE_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                return ActionRisk.SAFE

        # Check for file modifications to critical files
        if self.critical_files:
            for cf in self.critical_files:
                if cf in cmd or os.path.basename(cf) in cmd:
                    # Writing to critical file
                    if any(op in cmd for op in ['>', '>>', 'tee ', 'mv ', 'cp ']):
                        return ActionRisk.HIGH

        # Check for write operations
        if any(op in cmd for op in ['>', '>>', 'rm ', 'mv ', 'cp ']):
            return ActionRisk.MODERATE

        # Default
        return ActionRisk.MODERATE

    def command_touches_secret(self, command: str) -> bool:
        """True se un comando shell nomina un file/cartella di segreti."""
        lowered = " " + command.lower().replace("\\", "/") + " "
        for name in SECRET_FILE_NAMES:
            if name == ".env":
                # .env sì, ma non .env.example (è un template safe)
                import re as _re
                if _re.search(r'(^|[\s=/\'"])\.env($|[\s\'";|&>])', lowered):
                    return True
            elif name in lowered:
                return True
        for name in SECRET_DIR_NAMES:
            if f"/{name}/" in lowered or f" {name}/" in lowered:
                return True
        return False

    def check(self, command: str) -> tuple[bool, str]:
        """
        Controlla se un comando è sicuro.
        Returns: (allowed, reason)
        """
        risk = self.classify(command)

        if risk == ActionRisk.CRITICAL:
            return False, f"Comando critico bloccato: {command[:80]}"

        if self.command_touches_secret(command):
            return False, (
                "Comando bloccato: tocca file di segreti/sicurezza "
                "(.env, audit, vault…). Questi sono off-limits per i tool."
            )

        if risk == ActionRisk.SAFE:
            return True, ""

        # MODERATE e HIGH passano ma con info
        return True, f"risk={risk.value}"

    def check_tool(self, tool_name: str, args: dict) -> tuple[bool, str]:
        """Check sicurezza per tool strutturati."""
        if tool_name == "shell":
            return self.check(args.get("command", ""))

        if tool_name in ("read_file", "write_file", "edit_file", "edit_lines", "append_file"):
            path = args.get("path", "")
            if self.openvurp_dir and not self._path_inside_workspace(path):
                return False, f"Path fuori dal workspace: {path}"
            if self._is_secret_path(path):
                return False, f"File protetto (segreti/sicurezza): {path}"
            # Check critical files
            if tool_name in ("write_file", "edit_file", "edit_lines", "append_file") and self.critical_files:
                resolved_path = self._resolve_workspace_path(path)
                for cf in self.critical_files:
                    if os.path.abspath(resolved_path) == os.path.abspath(cf):
                        return False, f"Modifica a file critico: {path}"
            return True, ""

        if tool_name == "process_kill":
            return False, "Richiede approvazione"

        if tool_name in ("process_start", "process_write"):
            text = args.get("command", "") or args.get("text", "")
            risk = self.classify(text)
            if risk == ActionRisk.CRITICAL:
                return False, f"Input critico bloccato: {text[:80]}"
            return True, ""

        if tool_name in ("browser", "browser_devtools"):
            action = str(args.get("action", "") or "").strip().lower()
            if action == "relaunch":
                return False, "Richiede approvazione"
            return True, ""

        if tool_name == "browser_setup":
            return False, "Richiede approvazione"

        if tool_name == "capability_lease":
            action = str(args.get("action", "list") or "list").strip().lower()
            if action in ("grant", "add", "create", "revoke", "delete"):
                return False, "Richiede approvazione"
            return True, ""

        # Default: allow
        return True, ""

    def _is_secret_path(self, path: str) -> bool:
        """True se il path tocca un file/cartella di segreti o sicurezza."""
        if not path:
            return False
        resolved = self._resolve_workspace_path(path).replace("\\", "/")
        parts = [p for p in resolved.split("/") if p]
        if not parts:
            return False
        if parts[-1] in SECRET_FILE_NAMES:
            return True
        return any(p in SECRET_DIR_NAMES for p in parts)

    def is_critical_file(self, path: str) -> bool:
        """Check se un percorso è un file critico."""
        if not self.critical_files:
            return False
        abs_path = os.path.abspath(path)
        return any(os.path.abspath(cf) == abs_path for cf in self.critical_files)

    def _path_inside_workspace(self, path: str) -> bool:
        """Ritorna True se il path risolve dentro il workspace openvurp."""
        if not path:
            return True
        try:
            candidate = self._resolve_workspace_path(path)
            root = os.path.realpath(os.path.abspath(self.openvurp_dir))
            resolved = os.path.realpath(os.path.abspath(candidate))
            return resolved == root or resolved.startswith(root + os.sep)
        except Exception:
            return False

    def _resolve_workspace_path(self, path: str) -> str:
        candidate = (path or "").strip().strip('"').strip("'")
        if len(candidate) >= 3 and candidate[1] == ":" and candidate[2] in ("/", "\\"):
            drive = candidate[0].lower()
            rest = candidate[3:].replace("\\", "/")
            candidate = f"/mnt/{drive}/{rest}"
        if self.openvurp_dir and not os.path.isabs(candidate):
            candidate = os.path.join(self.openvurp_dir, candidate)
        return candidate
