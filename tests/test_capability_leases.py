"""Tests for dry-run execution and temporary capability leases."""

from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.executor import Executor
from core.safety import SafetyGuard
from core.security.capability_lease import CapabilityLeaseManager
from core.tools import ErrorType, Tool, ToolRegistry, ToolResult
from tools.file_ops import WRITE_FILE_TOOL


def _shell_registry(calls: list[str]) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Tool(
        name="shell",
        description="shell",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["command"],
        },
        handler=lambda command, dry_run=False: (
            calls.append(command) or ToolResult.ok(f"ran: {command}")
        ),
    ))
    return registry


def test_dry_run_high_risk_shell_does_not_prompt_or_execute():
    calls: list[str] = []
    executor = Executor(_shell_registry(calls), safety=SafetyGuard())

    result = executor.execute("shell", {"command": "rm file.txt", "dry_run": True})

    assert result.success
    assert "DRY RUN" in result.output
    assert "rm file.txt" in result.output
    assert calls == []


def test_dry_run_still_blocks_critical_shell():
    calls: list[str] = []
    executor = Executor(_shell_registry(calls), safety=SafetyGuard())

    result = executor.execute("shell", {"command": "rm -rf /", "dry_run": True})

    assert not result.success
    assert result.error_type == ErrorType.PERMISSION
    assert calls == []


def test_dry_run_write_file_does_not_create_file_without_ui():
    workspace = tempfile.mkdtemp(prefix="openvurp-dry-run-write-")
    target = os.path.join(workspace, "new.txt")
    registry = ToolRegistry()
    registry.register(WRITE_FILE_TOOL)
    executor = Executor(registry, safety=SafetyGuard(openvurp_dir=workspace))

    result = executor.execute(
        "write_file",
        {"path": target, "content": "hello", "dry_run": True},
    )

    assert result.success
    assert "DRY RUN" in result.output
    assert not os.path.exists(target)


def test_capability_lease_allows_matching_high_risk_action_once():
    calls: list[str] = []
    memory_dir = tempfile.mkdtemp(prefix="openvurp-lease-memory-")
    leases = CapabilityLeaseManager(memory_dir)
    leases.grant(
        actor="tester",
        source="tests",
        tool_name="shell",
        risk="high",
        ttl_seconds=60,
        max_uses=1,
        command_prefix="rm ",
        reason="test lease",
    )
    executor = Executor(
        _shell_registry(calls),
        safety=SafetyGuard(),
        lease_manager=leases,
    )

    allowed = executor.execute(
        "shell",
        {"command": "rm file.txt"},
        actor="tester",
        source="tests",
    )
    blocked = executor.execute(
        "shell",
        {"command": "rm file.txt"},
        actor="tester",
        source="tests",
    )

    assert allowed.success
    assert calls == ["rm file.txt"]
    assert not blocked.success
    assert blocked.error_type == ErrorType.PERMISSION
    stored = leases.list_leases(include_expired=True)[0]
    assert stored.uses == 1
    assert stored.exhausted


def test_expired_capability_lease_is_not_used():
    calls: list[str] = []
    memory_dir = tempfile.mkdtemp(prefix="openvurp-expired-lease-")
    leases = CapabilityLeaseManager(memory_dir)
    lease = leases.grant(
        actor="tester",
        source="tests",
        tool_name="shell",
        risk="high",
        ttl_seconds=60,
        max_uses=1,
        command_prefix="rm ",
    )
    lease.expires_at = time.time() - 1
    leases._save([lease])
    executor = Executor(
        _shell_registry(calls),
        safety=SafetyGuard(),
        lease_manager=leases,
    )

    result = executor.execute(
        "shell",
        {"command": "rm file.txt"},
        actor="tester",
        source="tests",
    )

    assert not result.success
    assert result.error_type == ErrorType.PERMISSION
    assert calls == []


def test_path_prefix_lease_matches_windows_and_wsl_paths():
    memory_dir = tempfile.mkdtemp(prefix="openvurp-path-lease-")
    leases = CapabilityLeaseManager(memory_dir)
    leases.grant(
        actor="tester",
        source="tests",
        tool_name="write_file",
        risk="moderate",
        ttl_seconds=60,
        max_uses=1,
        path_prefix=r"C:\Users\alice\Desktop\openvurp",
    )

    found = leases.find_valid(
        actor="tester",
        source="tests",
        tool_name="write_file",
        args={"path": "/mnt/c/Users/alice/Desktop/openvurp/core/agent.py"},
        risk="moderate",
    )

    assert found is not None


if __name__ == "__main__":
    test_dry_run_high_risk_shell_does_not_prompt_or_execute()
    test_dry_run_still_blocks_critical_shell()
    test_dry_run_write_file_does_not_create_file_without_ui()
    test_capability_lease_allows_matching_high_risk_action_once()
    test_expired_capability_lease_is_not_used()
    test_path_prefix_lease_matches_windows_and_wsl_paths()
    print("Capability lease tests passed.")
