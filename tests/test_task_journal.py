"""Test per task journal, reflection e open-loop tracker."""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.task_journal import TaskJournal


def test_task_journal_records_turn_reflection_and_redacts():
    with tempfile.TemporaryDirectory() as tmp:
        journal = TaskJournal(os.path.join(tmp, "memory"))
        fake_token = "123456789:" + ("A" * 35)
        turn_id = journal.start_turn(
            f"fix this token {fake_token}",
            source="cli",
            actor="cli_owner",
            session_key="cli:main",
        )
        reflection = journal.finish_turn(
            turn_id=turn_id,
            user_input=f"fix this token {fake_token}",
            assistant_text="Done. Follow-up: add CI before release.",
            tool_history=[
                {
                    "tool": "shell",
                    "args": {"command": "bad"},
                    "success": False,
                    "error_type": "runtime",
                }
            ],
            source="cli",
            actor="cli_owner",
            session_key="cli:main",
        )

        assert reflection.failures == ["shell:runtime"]
        assert reflection.open_loop_hints
        event_text = open(journal._events_path(), "r", encoding="utf-8").read()
        reflection_text = open(journal._reflections_path(), "r", encoding="utf-8").read()
        assert fake_token not in event_text
        assert fake_token not in reflection_text
        assert "***TELEGRAM_TOKEN***" in event_text


def test_open_loop_add_list_close():
    with tempfile.TemporaryDirectory() as tmp:
        journal = TaskJournal(os.path.join(tmp, "memory"))
        loop = journal.add_open_loop(
            "Add CI",
            description="Run tests on pull requests.",
            tags=["github", "release"],
        )

        open_loops = journal.list_open_loops()
        assert len(open_loops) == 1
        assert open_loops[0].title == "Add CI"

        closed = journal.close_open_loop(loop.id, resolution="CI added.")
        assert closed is not None
        assert closed.status == "closed"
        assert journal.list_open_loops() == []

        raw = json.load(open(journal.open_loops_path, "r", encoding="utf-8"))
        assert raw[0]["resolution"] == "CI added."


if __name__ == "__main__":
    test_task_journal_records_turn_reflection_and_redacts()
    test_open_loop_add_list_close()
    print("Tutti i test task journal passati!")
