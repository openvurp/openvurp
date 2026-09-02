"""
openvurp Security — RBAC (Role-Based Access Control)

Controllo accessi granulare per utenti e canali.
"""

from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Permission(Enum):
    """Permessi disponibili."""
    # Chat
    CHAT = "chat"                    # Può parlare con openvurp

    # Tool
    TOOL_SHELL = "tool.shell"        # Può eseguire comandi shell
    TOOL_FILE_READ = "tool.file.read"    # Può leggere file
    TOOL_FILE_WRITE = "tool.file.write"  # Può scrivere file
    TOOL_WEB = "tool.web"            # Può usare tool web
    TOOL_BROWSER = "tool.browser"    # Può usare browser
    TOOL_PROCESS = "tool.process"    # Può gestire processi
    TOOL_MCP = "tool.mcp"           # Può usare tool MCP
    TOOL_SUBAGENT = "tool.subagent"  # Può delegare a sub-agenti

    # Admin
    ADMIN_CONFIG = "admin.config"    # Può modificare configurazione
    ADMIN_PLUGINS = "admin.plugins"  # Può gestire plugin
    ADMIN_VAULT = "admin.vault"      # Può accedere al vault
    ADMIN_AUDIT = "admin.audit"      # Può leggere audit log
    ADMIN_USERS = "admin.users"      # Può gestire utenti e ruoli


class Role(Enum):
    """Ruoli predefiniti."""
    ADMIN = "admin"       # Tutto
    USER = "user"         # Chat + tool base (no shell, no admin)
    POWER = "power"       # Chat + tutti i tool (no admin)
    READER = "reader"     # Solo chat + lettura file
    GUEST = "guest"       # Solo chat


# Permessi per ruolo
ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.ADMIN: set(Permission),  # Tutti i permessi
    Role.POWER: {
        Permission.CHAT,
        Permission.TOOL_SHELL,
        Permission.TOOL_FILE_READ,
        Permission.TOOL_FILE_WRITE,
        Permission.TOOL_WEB,
        Permission.TOOL_BROWSER,
        Permission.TOOL_PROCESS,
        Permission.TOOL_MCP,
        Permission.TOOL_SUBAGENT,
    },
    Role.USER: {
        Permission.CHAT,
        Permission.TOOL_FILE_READ,
        Permission.TOOL_FILE_WRITE,
        Permission.TOOL_WEB,
        Permission.TOOL_BROWSER,
    },
    Role.READER: {
        Permission.CHAT,
        Permission.TOOL_FILE_READ,
    },
    Role.GUEST: {
        Permission.CHAT,
    },
}

# Mapping tool name → permesso richiesto
TOOL_PERMISSIONS: dict[str, Permission] = {
    "shell": Permission.TOOL_SHELL,
    "read_file": Permission.TOOL_FILE_READ,
    "write_file": Permission.TOOL_FILE_WRITE,
    "edit_file": Permission.TOOL_FILE_WRITE,
    "edit_lines": Permission.TOOL_FILE_WRITE,
    "append_file": Permission.TOOL_FILE_WRITE,
    "grep": Permission.TOOL_FILE_READ,
    "glob": Permission.TOOL_FILE_READ,
    "web_fetch": Permission.TOOL_WEB,
    "web_search": Permission.TOOL_WEB,
    "vurpub_search": Permission.TOOL_WEB,
    "vurpub_pull": Permission.TOOL_WEB,
    "vurpub_candidates": Permission.TOOL_WEB,
    "vurpub_reject": Permission.TOOL_WEB,
    "vurpub_approve": Permission.ADMIN_CONFIG,
    "vurpub_share": Permission.ADMIN_CONFIG,
    "browser": Permission.TOOL_BROWSER,
    "browser_devtools": Permission.TOOL_BROWSER,
    "process_list": Permission.TOOL_PROCESS,
    "process_sessions": Permission.TOOL_PROCESS,
    "process_start": Permission.TOOL_PROCESS,
    "process_read": Permission.TOOL_PROCESS,
    "process_write": Permission.TOOL_PROCESS,
    "process_stop": Permission.TOOL_PROCESS,
    "process_kill": Permission.TOOL_PROCESS,
    "desktop_screenshot": Permission.TOOL_BROWSER,
    "browser_setup": Permission.ADMIN_CONFIG,
    "scaffold_plugin": Permission.ADMIN_PLUGINS,
    "reload_plugins": Permission.ADMIN_PLUGINS,
    "list_plugins": Permission.ADMIN_PLUGINS,
    "doctor": Permission.ADMIN_CONFIG,
    "doctor_fix": Permission.ADMIN_CONFIG,
    "memory_consolidate": Permission.ADMIN_CONFIG,
    "learning_feedback": Permission.ADMIN_CONFIG,
    "learning_review": Permission.ADMIN_CONFIG,
    "learning_promote": Permission.ADMIN_CONFIG,
    "learning_rollback": Permission.ADMIN_CONFIG,
    "pact": Permission.ADMIN_CONFIG,
    "project": Permission.ADMIN_CONFIG,
    "forge": Permission.ADMIN_PLUGINS,
    "sense": Permission.ADMIN_CONFIG,
    "second_opinion": Permission.ADMIN_CONFIG,
    "task_journal": Permission.ADMIN_CONFIG,
    "reflection_note": Permission.ADMIN_CONFIG,
    "open_loop": Permission.ADMIN_CONFIG,
    "capability_lease": Permission.ADMIN_CONFIG,
    "agent_state": Permission.ADMIN_CONFIG,
    "request_restart": Permission.ADMIN_CONFIG,
    "notify_file": Permission.ADMIN_CONFIG,
    "notify_photo": Permission.ADMIN_CONFIG,
    "subagent_spawn": Permission.TOOL_SUBAGENT,
    "subagent_list": Permission.TOOL_SUBAGENT,
    "subagent_wait": Permission.TOOL_SUBAGENT,
    "subagent_wait_all": Permission.TOOL_SUBAGENT,
    "subagent_kill": Permission.TOOL_SUBAGENT,
}


@dataclass
class UserACL:
    """Access Control per un singolo utente."""
    user_id: str
    role: Role = Role.USER
    extra_permissions: set[Permission] = field(default_factory=set)
    denied_permissions: set[Permission] = field(default_factory=set)
    channels: list[str] = field(default_factory=list)  # Canali autorizzati (vuoto = tutti)

    def has_permission(self, perm: Permission) -> bool:
        """Controlla se l'utente ha un permesso specifico."""
        if perm in self.denied_permissions:
            return False
        if perm in self.extra_permissions:
            return True
        return perm in ROLE_PERMISSIONS.get(self.role, set())

    def can_use_tool(self, tool_name: str) -> bool:
        """Controlla se l'utente può usare un tool specifico."""
        # Tool MCP
        if tool_name.startswith("mcp_"):
            return self.has_permission(Permission.TOOL_MCP)

        perm = TOOL_PERMISSIONS.get(tool_name)
        if perm is None:
            # Tool sconosciuto — default a CHAT (permesso base)
            return self.has_permission(Permission.CHAT)
        return self.has_permission(perm)

    def can_access_channel(self, channel: str) -> bool:
        """Controlla se l'utente può accedere a un canale."""
        if not self.channels:
            return True  # Vuoto = tutti
        return channel in self.channels


class RBAC:
    """
    Gestore RBAC. Carica/salva ACL da file JSON.

    Il CLI owner è sempre ADMIN.
    """

    ACL_FILE = "acl.json"

    def __init__(self, config_dir: str):
        self.config_dir = config_dir
        self._acl_path = os.path.join(config_dir, self.ACL_FILE)
        self._users: dict[str, UserACL] = {}
        self._cli_owner: str = "cli_owner"

        os.makedirs(config_dir, exist_ok=True)
        self._load()

    def get_user(self, user_id: str) -> UserACL:
        """Ottieni ACL per utente. Se non esiste, ritorna il ruolo default.

        Eccezione: gli ID Telegram in TELEGRAM_ALLOWED_USERS sono i
        dispositivi dell'owner — sono ADMIN senza bisogno di setup.
        Un ACL esplicito in acl.json ha comunque precedenza.
        """
        if user_id == self._cli_owner:
            return UserACL(user_id=self._cli_owner, role=Role.ADMIN)
        if user_id in self._users:
            return self._users[user_id]
        if self._is_owner_telegram_actor(user_id):
            return UserACL(user_id=user_id, role=Role.ADMIN)
        return UserACL(user_id=user_id, role=self._default_role())

    @staticmethod
    def _is_owner_telegram_actor(user_id: str) -> bool:
        if not user_id.startswith("telegram:"):
            return False
        try:
            import config as cfg
            allowed = getattr(cfg, "TELEGRAM_ALLOWED_USERS", []) or []
        except Exception:
            return False
        raw_id = user_id.split(":", 1)[1].strip()
        return any(str(item).strip() == raw_id for item in allowed)

    @staticmethod
    def _default_role() -> "Role":
        """Ruolo di default per attori sconosciuti (RBAC_DEFAULT_ROLE)."""
        try:
            import config as cfg
            name = str(getattr(cfg, "RBAC_DEFAULT_ROLE", "guest") or "guest").lower()
            return Role(name)
        except Exception:
            return Role.GUEST

    def set_user(self, user_id: str, role: Role,
                 extra_permissions: set[Permission] = None,
                 denied_permissions: set[Permission] = None,
                 channels: list[str] = None):
        """Imposta ruolo e permessi per un utente."""
        acl = UserACL(
            user_id=user_id,
            role=role,
            extra_permissions=extra_permissions or set(),
            denied_permissions=denied_permissions or set(),
            channels=channels or [],
        )
        self._users[user_id] = acl
        self._save()

    def remove_user(self, user_id: str) -> bool:
        """Rimuovi utente (torna a GUEST)."""
        if user_id in self._users:
            del self._users[user_id]
            self._save()
            return True
        return False

    def check_tool(self, user_id: str, tool_name: str) -> tuple[bool, str]:
        """
        Controlla se un utente può usare un tool.
        Returns: (allowed, reason)
        """
        user = self.get_user(user_id)

        if not user.can_use_tool(tool_name):
            return False, (
                f"Permesso negato: {user_id} (ruolo={user.role.value}) "
                f"non può usare tool '{tool_name}'"
            )
        return True, ""

    def check_channel(self, user_id: str, channel: str) -> tuple[bool, str]:
        """Controlla se un utente può accedere a un canale."""
        user = self.get_user(user_id)

        if not user.has_permission(Permission.CHAT):
            return False, f"Permesso negato: {user_id} non ha permesso CHAT"

        if not user.can_access_channel(channel):
            return False, f"Canale non autorizzato: {channel} per {user_id}"

        return True, ""

    def list_users(self) -> list[dict]:
        """Lista tutti gli utenti con ruoli."""
        result = []
        for uid, acl in self._users.items():
            result.append({
                "user_id": uid,
                "role": acl.role.value,
                "channels": acl.channels,
                "extra": [p.value for p in acl.extra_permissions],
                "denied": [p.value for p in acl.denied_permissions],
            })
        return result

    # ── Persistence ──

    def _save(self):
        """Salva ACL su disco."""
        data = {}
        for uid, acl in self._users.items():
            data[uid] = {
                "role": acl.role.value,
                "extra_permissions": [p.value for p in acl.extra_permissions],
                "denied_permissions": [p.value for p in acl.denied_permissions],
                "channels": acl.channels,
            }

        try:
            with open(self._acl_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.chmod(self._acl_path, 0o600)
        except OSError:
            pass

    def _load(self):
        """Carica ACL da disco."""
        if not os.path.exists(self._acl_path):
            return

        try:
            with open(self._acl_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for uid, info in data.items():
                role = Role(info.get("role", "guest"))
                extra = {Permission(p) for p in info.get("extra_permissions", [])}
                denied = {Permission(p) for p in info.get("denied_permissions", [])}
                channels = info.get("channels", [])

                self._users[uid] = UserACL(
                    user_id=uid,
                    role=role,
                    extra_permissions=extra,
                    denied_permissions=denied,
                    channels=channels,
                )
        except Exception:
            pass
