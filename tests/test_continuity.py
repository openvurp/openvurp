"""Tests for active task continuity prompt."""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.agent_state import AgentStateMachine
from core.continuity import ContinuityPromptBuilder
from core.memory import MemoryManager
from core.task_journal import TaskJournal


def test_continuity_prompt_combines_state_open_loops_and_reflections():
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = os.path.join(tmp, "memory")
        state = AgentStateMachine(memory_dir)
        journal = TaskJournal(memory_dir)
        state.begin_turn("Improve autonomy loop", thinking_level="deep")
        state.record_observation(outputs=["Implemented state machine"])
        journal.add_open_loop(
            "Add evaluation harness",
            "Behavior tests for autonomy loop.",
            tags=["autonomy", "tests"],
        )
        turn_id = journal.start_turn("work on autonomy", actor="cli_owner")
        journal.finish_turn(
            turn_id,
            user_input="work on autonomy",
            assistant_text="State machine added.",
            tool_history=[{"tool": "shell", "success": True}],
        )

        text = ContinuityPromptBuilder(state, journal).build(
            "continua autonomy",
            session_type="main",
        )

        assert "## AUTONOMY STATE" in text
        assert "Improve autonomy loop" in text
        assert "## OPEN LOOPS" in text
        assert "Add evaluation harness" in text
        assert "## RECENT REFLECTIONS" in text
        assert "State machine added" in text


def test_continuity_prompt_is_private_to_main_session():
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = os.path.join(tmp, "memory")
        state = AgentStateMachine(memory_dir)
        journal = TaskJournal(memory_dir)
        state.begin_turn("Private task", thinking_level="normal")
        journal.add_open_loop("Private follow-up")

        text = ContinuityPromptBuilder(state, journal).build(
            "private",
            session_type="group",
        )

        assert text == ""


def test_memory_retrieval_skips_private_journal_for_group_session():
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = os.path.join(tmp, "memory")
        memory = MemoryManager(memory_dir)
        state = AgentStateMachine(memory_dir)
        journal = TaskJournal(memory_dir)
        state.begin_turn("private agent state")
        journal.add_open_loop("private open loop")
        journal.record_note("private deployment note", kind="decision")

        text = memory.get_relevant("deployment", session_type="group")

        assert "private deployment note" not in text
        assert "private open loop" not in text
        assert "private agent state" not in text
        assert text == "(nessun ricordo ancora)"


if __name__ == "__main__":
    test_continuity_prompt_combines_state_open_loops_and_reflections()
    test_continuity_prompt_is_private_to_main_session()
    test_memory_retrieval_skips_private_journal_for_group_session()
    print("Continuity tests passed.")
