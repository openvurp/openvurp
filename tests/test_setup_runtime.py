"""Test per bootstrap runtime serio."""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.setup_runtime import ensure_runtime_state
from core.security.integrity import IntegrityChecker
from core.security.rbac import RBAC


def test_ensure_runtime_state_bootstraps_acl_audit_and_integrity():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "core"), exist_ok=True)
        with open(os.path.join(tmp, "main.py"), "w", encoding="utf-8") as f:
            f.write("print('ok')\n")
        with open(os.path.join(tmp, "config.py"), "w", encoding="utf-8") as f:
            f.write("LLM_MODEL='test'\n")

        report = ensure_runtime_state(
            tmp,
            allowed_telegram_users=[123],
            create_integrity_baseline=True,
            force_acl_refresh=True,
        )

        assert report.changed
        assert os.path.exists(os.path.join(tmp, "memory", "audit", "audit.jsonl"))
        assert os.path.exists(os.path.join(tmp, IntegrityChecker.BASELINE_FILE))
        assert os.path.exists(os.path.join(tmp, "memory", ".runtime_setup.json"))

        rbac = RBAC(os.path.join(tmp, "memory"))
        acl = rbac.get_user("telegram:123")
        assert acl.role.value == "admin"
        assert acl.channels == ["telegram"]


if __name__ == "__main__":
    test_ensure_runtime_state_bootstraps_acl_audit_and_integrity()
    print("Tutti i test setup runtime passati!")
