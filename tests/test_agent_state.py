"""Tests for the autonomy state machine."""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.agent_state import AgentPhase, AgentStateMachine


def test_agent_state_tracks_goal_plan_observation_and_finish():
    with tempfile.TemporaryDirectory() as tmp:
        state = AgentStateMachine(tmp)
        task = state.begin_turn(
            "Implement autonomy loop",
            source="cli",
            actor="tester",
            session_key="cli:main",
            thinking_level="deep",
        )

        assert task.state == AgentPhase.PLANNING.value
        assert task.plan

        state.mark_execution(["read_file", "edit_file"], iteration=1)
        state.record_observation([
            {"tool": "read_file", "success": True},
            {"tool": "edit_file", "success": False, "error_type": "validation"},
        ], outputs=["Need more context"])

        status = state.status()
        assert status["state"] == AgentPhase.REVISING.value
        assert "read_file" in status["tools_used"]
        assert status["blockers"]

        state.finish("Done")
        assert state.status()["state"] == AgentPhase.COMPLETED.value

        with open(os.path.join(tmp, "agent_state.json"), "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        assert raw["final_result"] == "Done"


def test_agent_state_continues_active_task_for_short_continue_prompt():
    with tempfile.TemporaryDirectory() as tmp:
        state = AgentStateMachine(tmp)
        first = state.begin_turn("Build the runtime loop", thinking_level="deep")
        second = state.begin_turn("continua", thinking_level="quick")

        assert second.task_id == first.task_id
        assert second.goal == first.goal
        assert any("continue" in item.lower() for item in second.observations)


def test_agent_state_accepts_kernel_plan_and_advances_after_successful_observation():
    with tempfile.TemporaryDirectory() as tmp:
        state = AgentStateMachine(tmp)
        task = state.begin_turn(
            "Implement kernel",
            thinking_level="deep",
            plan=["Inspect files", "Edit code", "Run tests"],
        )

        assert task.plan == ["Inspect files", "Edit code", "Run tests"]
        assert task.current_step == 0

        state.record_observation([
            {"tool": "read_file", "success": True},
        ], outputs=["inspected"])

        assert state.status()["current_step"] == 1


def test_agent_state_prompt_redacts_secrets():
    with tempfile.TemporaryDirectory() as tmp:
        state = AgentStateMachine(tmp)
        state.begin_turn("Use token=abcd", thinking_level="normal")
        state.add_note("password=supersecret")

        text = state.prompt_section()

        assert "supersecret" not in text
        assert "password=***REDACTED***" in text


if __name__ == "__main__":
    test_agent_state_tracks_goal_plan_observation_and_finish()
    test_agent_state_continues_active_task_for_short_continue_prompt()
    test_agent_state_accepts_kernel_plan_and_advances_after_successful_observation()
    test_agent_state_prompt_redacts_secrets()
    print("Agent state tests passed.")
