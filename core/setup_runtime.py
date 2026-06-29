"""
openvurp Core — Runtime Setup

Bootstrap serio del runtime: scaffold memoria, ACL, audit e baseline integrity.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from core.memory import MemoryManager
from core.security.audit import AuditAction, AuditLog
from core.security.integrity import IntegrityChecker
from core.security.rbac import RBAC, Role


@dataclass
class SetupAction:
    name: str
    changed: bool
    detail: str


@dataclass
class SetupReport:
    changed: bool
    actions: list[SetupAction] = field(default_factory=list)

    def render(self) -> str:
        lines = ["## SETUP RUNTIME"]
        for action in self.actions:
            status = "changed" if action.changed else "ok"
            lines.append(f"- {action.name}: ({status}) {action.detail}")
        return "\n".join(lines)


def ensure_runtime_state(
    workspace_dir: str,
    allowed_telegram_users: list[int] | None = None,
    create_integrity_baseline: bool = False,
    force_acl_refresh: bool = False,
) -> SetupReport:
    memory_dir = os.path.join(workspace_dir, "memory")
    actions: list[SetupAction] = []
    changed = False

    MemoryManager(memory_dir)
    for subdir in (
        "audit", "captures", "media", "cache", "sessions",
        "lessons", "projects", "learning", "task_journal",
        "reflections", "agent_state",
    ):
        path = os.path.join(memory_dir, subdir)
        existed = os.path.isdir(path)
        os.makedirs(path, exist_ok=True)
        actions.append(SetupAction(
            name=f"memory/{subdir}",
            changed=not existed,
            detail="cartella pronta",
        ))
        changed = changed or (not existed)

    for filename, default_payload in (
        ("open_loops.json", []),
        ("capability_leases.json", []),
        ("agent_state.json", {}),
    ):
        path = os.path.join(memory_dir, filename)
        existed = os.path.exists(path)
        if not existed:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default_payload, f, indent=2, ensure_ascii=False)
        actions.append(SetupAction(
            name=f"memory/{filename}",
            changed=not existed,
            detail="file pronto",
        ))
        changed = changed or (not existed)

    rbac = RBAC(memory_dir)
    acl_path = os.path.join(memory_dir, RBAC.ACL_FILE)
    acl_existed = os.path.exists(acl_path)
    acl_changed = False
    if force_acl_refresh or not acl_existed:
        for raw_user_id in allowed_telegram_users or []:
            user_id = f"telegram:{raw_user_id}"
            rbac.set_user(user_id, Role.ADMIN, channels=["telegram"])
            acl_changed = True
        if not acl_changed:
            rbac._save()
        if allowed_telegram_users:
            detail = f"seed utenti Telegram: {', '.join(str(x) for x in allowed_telegram_users)}"
        else:
            detail = "ACL inizializzata; CLI owner resta admin implicito"
    else:
        detail = "ACL già presente"
    actions.append(SetupAction(
        name="acl",
        changed=acl_changed or (not acl_existed),
        detail=detail,
    ))
    changed = changed or acl_changed or (not acl_existed)

    audit_dir = os.path.join(memory_dir, "audit")
    audit = AuditLog(audit_dir)
    audit_path = os.path.join(audit_dir, AuditLog.AUDIT_FILE)
    audit_existed = os.path.exists(audit_path)
    if not audit_existed:
        audit.log(
            AuditAction.SESSION_START,
            actor="system",
            target="runtime_setup",
            details="bootstrap iniziale runtime",
            source="system",
        )
    actions.append(SetupAction(
        name="audit",
        changed=not audit_existed,
        detail="audit log pronto",
    ))
    changed = changed or (not audit_existed)

    integrity = IntegrityChecker(workspace_dir)
    baseline_path = os.path.join(workspace_dir, IntegrityChecker.BASELINE_FILE)
    baseline_existed = os.path.exists(baseline_path)
    created_count = 0
    if create_integrity_baseline and not baseline_existed:
        created_count = integrity.create_baseline()
    actions.append(SetupAction(
        name="integrity",
        changed=bool(created_count),
        detail=(
            f"baseline creata su {created_count} file"
            if created_count
            else ("baseline già presente" if baseline_existed else "baseline non ancora creata")
        ),
    ))
    changed = changed or bool(created_count)

    marker_path = os.path.join(memory_dir, ".runtime_setup.json")
    marker_before = os.path.exists(marker_path)
    payload = {
        "updated_at": time.time(),
        "allowed_telegram_users": allowed_telegram_users or [],
        "create_integrity_baseline": create_integrity_baseline,
    }
    with open(marker_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    actions.append(SetupAction(
        name="setup_marker",
        changed=not marker_before,
        detail="stato setup salvato",
    ))
    changed = changed or (not marker_before)

    return SetupReport(changed=changed, actions=actions)
