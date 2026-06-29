"""Test per policy engine ed executor approval."""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.executor import Executor
from core.security.audit import AuditAction, AuditLog
from core.security.rbac import Permission, RBAC, Role
from core.safety import SafetyGuard
from core.tools import Tool, ToolRegistry, ToolResult, ErrorType


class FakeUI:
    def __init__(self, approved: bool):
        self.approved = approved
        self.prompts = []

    def confirm(self, prompt: str) -> bool:
        self.prompts.append(prompt)
        return self.approved


def _approval_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(
        name="dangerous_tool",
        description="Tool fittizio sensibile",
        parameters={"type": "object", "properties": {}},
        requires_approval=True,
        handler=lambda: ToolResult.ok("executed"),
    ))
    return registry


def test_requires_approval_without_ui_is_blocked():
    executor = Executor(_approval_registry(), safety=SafetyGuard())
    result = executor.execute("dangerous_tool", {})
    assert not result.success
    assert result.error_type == ErrorType.PERMISSION
    assert "Richiede approvazione" in (result.error or "")


def test_requires_approval_denied_by_user():
    ui = FakeUI(approved=False)
    executor = Executor(_approval_registry(), safety=SafetyGuard())
    result = executor.execute("dangerous_tool", {}, ui=ui)
    assert not result.success
    assert result.error_type == ErrorType.PERMISSION
    assert ui.prompts


def test_requires_approval_approved_by_user_executes():
    ui = FakeUI(approved=True)
    executor = Executor(_approval_registry(), safety=SafetyGuard())
    result = executor.execute("dangerous_tool", {}, ui=ui)
    assert result.success
    assert result.output == "executed"
    assert ui.prompts


def test_high_risk_shell_requires_approval_in_executor():
    registry = ToolRegistry()
    registry.register(Tool(
        name="shell",
        description="shell",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}},
        handler=lambda command: ToolResult.ok(f"ran: {command}"),
    ))
    executor = Executor(registry, safety=SafetyGuard())

    blocked = executor.execute("shell", {"command": "rm file.txt"})
    assert not blocked.success
    assert blocked.error_type == ErrorType.PERMISSION

    approved = executor.execute(
        "shell",
        {"command": "rm file.txt"},
        ui=FakeUI(approved=True),
    )
    assert approved.success


def test_file_read_outside_workspace_is_blocked():
    workspace = tempfile.mkdtemp(prefix="openvurp-policy-workspace-")
    outside = tempfile.NamedTemporaryFile(delete=False)
    outside.close()
    try:
        safety = SafetyGuard(openvurp_dir=workspace)
        ok, reason = safety.check_tool("read_file", {"path": outside.name})
        assert not ok
        assert "fuori dal workspace" in reason
    finally:
        try:
            os.unlink(outside.name)
        except OSError:
            pass


def test_relative_critical_file_write_requires_policy_attention():
    workspace = tempfile.mkdtemp(prefix="openvurp-policy-critical-")
    config_path = os.path.join(workspace, "config.py")
    with open(config_path, "w", encoding="utf-8") as handle:
        handle.write("# config\n")

    safety = SafetyGuard(openvurp_dir=workspace)
    ok, reason = safety.check_tool("write_file", {"path": "config.py"})
    assert not ok
    assert "file critico" in reason


def test_rbac_covers_file_edit_helpers():
    tmp = tempfile.mkdtemp(prefix="openvurp-rbac-")
    rbac = RBAC(tmp)
    rbac.set_user("reader", Role.READER)
    rbac.set_user("writer", Role.USER)

    assert not rbac.check_tool("reader", "edit_lines")[0]
    assert not rbac.check_tool("reader", "append_file")[0]
    assert rbac.check_tool("writer", "edit_lines")[0]
    assert rbac.check_tool("writer", "append_file")[0]


def test_policy_denial_is_audited():
    tmp = tempfile.mkdtemp(prefix="openvurp-policy-audit-deny-")
    audit = AuditLog(os.path.join(tmp, "audit"))
    executor = Executor(_approval_registry(), safety=SafetyGuard(), audit_log=audit)

    result = executor.execute(
        "dangerous_tool",
        {},
        actor="tester",
        source="tests",
    )

    assert not result.success
    events = audit.get_recent()
    assert any(
        event["action"] == AuditAction.PERMISSION_DENIED.value
        and event["actor"] == "tester"
        and event["source"] == "tests"
        and event["target"] == "dangerous_tool"
        for event in events
    )
    assert audit.verify_chain()[0]


def test_approval_and_tool_call_are_audited():
    tmp = tempfile.mkdtemp(prefix="openvurp-policy-audit-allow-")
    audit = AuditLog(os.path.join(tmp, "audit"))
    executor = Executor(_approval_registry(), safety=SafetyGuard(), audit_log=audit)

    result = executor.execute(
        "dangerous_tool",
        {},
        ui=FakeUI(approved=True),
        actor="tester",
        source="tests",
    )

    assert result.success
    events = audit.get_recent()
    actions = [event["action"] for event in events]
    assert AuditAction.PERMISSION_GRANTED.value in actions
    assert AuditAction.TOOL_CALL.value in actions
    assert audit.verify_chain()[0]


def test_audit_redacts_sensitive_args():
    registry = ToolRegistry()
    registry.register(Tool(
        name="shell",
        description="shell",
        parameters={"type": "object", "properties": {"command": {"type": "string"}}},
        handler=lambda command: ToolResult.ok("ok"),
    ))
    tmp = tempfile.mkdtemp(prefix="openvurp-policy-audit-redact-")
    audit = AuditLog(os.path.join(tmp, "audit"))
    executor = Executor(registry, safety=SafetyGuard(), audit_log=audit)

    fake_token = "123456789:" + ("A" * 35)
    result = executor.execute(
        "shell",
        {"command": f"echo {fake_token}"},
        actor="tester",
        source="tests",
    )

    assert result.success
    serialized_events = json.dumps(audit.get_recent(), ensure_ascii=False)
    assert fake_token not in serialized_events
    assert "***TELEGRAM_TOKEN***" in serialized_events
    assert audit.verify_chain()[0]


if __name__ == "__main__":
    test_requires_approval_without_ui_is_blocked()
    test_requires_approval_denied_by_user()
    test_requires_approval_approved_by_user_executes()
    test_high_risk_shell_requires_approval_in_executor()
    test_file_read_outside_workspace_is_blocked()
    test_relative_critical_file_write_requires_policy_attention()
    test_rbac_covers_file_edit_helpers()
    test_policy_denial_is_audited()
    test_approval_and_tool_call_are_audited()
    test_audit_redacts_sensitive_args()
    print("Tutti i test policy passati!")
