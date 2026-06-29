"""
openvurp Core — Security Module

Sicurezza avanzata: sandbox, vault, audit, RBAC, integrity checking.
Questo modulo si AGGIUNGE alla safety esistente senza modificarla.
"""

from core.security.sandbox import Sandbox, SandboxConfig
from core.security.vault import Vault
from core.security.audit import AuditLog, AuditEvent
from core.security.rbac import RBAC, Role, Permission
from core.security.integrity import IntegrityChecker

__all__ = [
    "Sandbox", "SandboxConfig",
    "Vault",
    "AuditLog", "AuditEvent",
    "RBAC", "Role", "Permission",
    "IntegrityChecker",
]
