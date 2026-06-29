"""Tests for the runtime agent kernel."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.agent_kernel import AgentKernel, GateAction, KernelMode


def test_kernel_prepares_implementation_plan_with_verification():
    kernel = AgentKernel()

    plan = kernel.prepare(
        "implementa il planner runtime nel progetto",
        thinking_level="deep",
        tools_available=["read_file", "edit_file", "shell"],
    )

    assert plan.mode == KernelMode.IMPLEMENT.value
    assert plan.requires_tools
    assert plan.requires_verification
    assert any("Verify" in step or "verification" in step.lower() for step in plan.steps)


def test_kernel_blocks_actionable_final_without_tools():
    kernel = AgentKernel()
    plan = kernel.prepare(
        "analizza il progetto e dimmi cosa non va",
        thinking_level="deep",
        tools_available=["grep", "read_file"],
    )

    gate = kernel.review_final(plan, "Secondo me va bene.", [])

    assert gate.action == GateAction.CONTINUE.value
    assert "No tool observation" in gate.reason
    assert "[KERNEL CHECK]" in gate.prompt


def test_kernel_blocks_code_completion_without_verification():
    kernel = AgentKernel()
    plan = kernel.prepare(
        "aggiungi il nuovo agent kernel",
        thinking_level="deep",
        tools_available=["edit_file", "shell"],
    )

    gate = kernel.review_final(
        plan,
        "Fatto, ho implementato tutto.",
        [{"tool": "edit_file", "args": {"path": "core/agent.py"}, "success": True}],
    )

    assert gate.action == GateAction.CONTINUE.value
    assert "verification" in gate.reason.lower()


def test_kernel_allows_verified_code_completion():
    kernel = AgentKernel()
    plan = kernel.prepare(
        "aggiungi il nuovo agent kernel",
        thinking_level="deep",
        tools_available=["edit_file", "shell"],
    )

    gate = kernel.review_final(
        plan,
        "Fatto, test passati.",
        [
            {"tool": "edit_file", "args": {"path": "core/agent.py"}, "success": True},
            {"tool": "shell", "args": {"command": "python3 tests/test_agent_kernel.py"}, "success": True},
        ],
    )

    assert gate.action == GateAction.ALLOW.value


def test_kernel_tracks_waiting_user_open_loop_only_for_active_tasks():
    kernel = AgentKernel()
    active = kernel.prepare("implementa una feature", thinking_level="deep")
    chat = kernel.prepare("grazie", thinking_level="quick")

    assert kernel.should_track_open_loop(active, "Mi serve una scelta.", True)
    assert not kernel.should_track_open_loop(chat, "Prego.", False)


def test_kernel_treats_goodnight_as_chat_not_investigate():
    """'Buonanotte' is a social closing, not an investigation task.

    Regression: previously classified as INVESTIGATE because it contains
    no marker words at all but somehow still went through inspect gates.
    The real cause was the absence of an early-return for social patterns.
    """
    kernel = AgentKernel()
    plan = kernel.prepare("buonanotte Pico", thinking_level="normal")

    assert plan.mode == KernelMode.CHAT.value
    assert not plan.active


def test_kernel_treats_presence_complaint_as_chat():
    """'Perché non rispondi?' is a complaint about the agent, not a task.

    Regression: this exact message was swallowed in INVESTIGATE because it
    matched the 'perché' question pattern, then the gate blocked the answer
    for lack of inspection tools.
    """
    kernel = AgentKernel()
    plan = kernel.prepare("perché non rispondi alla buonanotte?", thinking_level="normal")

    assert plan.mode == KernelMode.CHAT.value
    assert not plan.active


def test_kernel_still_classifies_real_why_questions_as_investigate():
    """Real 'perché' questions about the project must still investigate.

    The social-pattern early-return must NOT swallow actionable questions
    that mention project terms.
    """
    kernel = AgentKernel()
    plan = kernel.prepare(
        "perché il file core/agent_kernel.py non si carica?",
        thinking_level="deep",
    )

    assert plan.mode == KernelMode.INVESTIGATE.value


def test_kernel_treats_thanks_and_greeting_as_chat():
    """Quick acks and greetings never become actionable tasks."""
    kernel = AgentKernel()
    for text in ("ciao", "grazie!", "a dopo", "ok."):
        plan = kernel.prepare(text, thinking_level="normal")
        assert plan.mode == KernelMode.CHAT.value, f"failed for: {text!r}"


def test_kernel_unaddressed_message_forces_quiet_chat():
    """When the runtime says we're not addressed, the kernel must NOT
    fabricate an investigation. Even 'perché X' must fall back to CHAT.

    Regression: in the night of 2026-06-21 a "perché non rispondi alla
    buonanotte?" arrived addressed but the runtime did not pass that flag
    down to the kernel, so INVESTIGATE blocked the answer silently.
    """
    kernel = AgentKernel()
    plan = kernel.prepare(
        "perché non rispondi alla buonanotte?",
        thinking_level="deep",
        is_addressed=False,
    )

    assert plan.mode == KernelMode.CHAT.value
    assert not plan.active
    assert not plan.requires_tools
    assert not plan.requires_verification


def test_kernel_addressed_message_still_classifies_normally():
    """When addressed=True (default) the classifier keeps its power.
    'perché il file X non si carica' must still be INVESTIGATE.
    """
    kernel = AgentKernel()
    plan = kernel.prepare(
        "perché il file core/agent.py non si carica?",
        thinking_level="deep",
        is_addressed=True,
    )

    assert plan.mode == KernelMode.INVESTIGATE.value


if __name__ == "__main__":
    test_kernel_prepares_implementation_plan_with_verification()
    test_kernel_blocks_actionable_final_without_tools()
    test_kernel_blocks_code_completion_without_verification()
    test_kernel_allows_verified_code_completion()
    test_kernel_tracks_waiting_user_open_loop_only_for_active_tasks()
    test_kernel_treats_goodnight_as_chat_not_investigate()
    test_kernel_treats_presence_complaint_as_chat()
    test_kernel_still_classifies_real_why_questions_as_investigate()
    test_kernel_treats_thanks_and_greeting_as_chat()
    test_kernel_unaddressed_message_forces_quiet_chat()
    test_kernel_addressed_message_still_classifies_normally()
    print("Agent kernel tests passed.")
