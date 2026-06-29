"""Test per il learning loop verificabile."""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.learning import LearningLoop
from core.tools import ErrorType, ToolResult


def test_user_signal_is_recorded_and_redacted():
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = os.path.join(tmp, "memory")
        loop = LearningLoop(memory_dir)

        fake_token = "123456789:" + ("A" * 35)
        event = loop.record_user_signal(
            f"ricorda che non devi mai mostrare token {fake_token}",
            actor="cli_owner",
            source="cli",
        )

        assert event is not None
        serialized_events = open(loop.events_path, "r", encoding="utf-8").read()
        daily_files = [
            name for name in os.listdir(memory_dir)
            if name.endswith(".md")
        ]
        assert daily_files
        daily_note = open(
            os.path.join(memory_dir, daily_files[0]),
            "r",
            encoding="utf-8",
        ).read()
        assert fake_token not in serialized_events
        assert fake_token not in daily_note
        assert "***TELEGRAM_TOKEN***" in serialized_events
        assert "***TELEGRAM_TOKEN***" in daily_note


def test_repeated_tool_failures_create_promotable_candidate():
    with tempfile.TemporaryDirectory() as tmp:
        loop = LearningLoop(os.path.join(tmp, "memory"))
        result = ToolResult.fail(
            "command failed",
            error_type=ErrorType.RUNTIME,
            tool_name="shell",
        )

        loop.record_tool_failure("shell", {"command": "bad"}, result)
        loop.record_tool_failure("shell", {"command": "bad"}, result)
        report = loop.review(max_events=10, min_repeats=2)

        assert report.candidates
        assert os.path.exists(loop.candidates_path)
        saved = json.load(open(loop.candidates_path, "r", encoding="utf-8"))
        assert saved[0]["kind"] == "tool_reliability"

        promoted = loop.promote_candidate(report.candidates[0].id)
        assert promoted.startswith("[OK]")
        assert os.listdir(os.path.join(tmp, "memory", "lessons"))


if __name__ == "__main__":
    test_user_signal_is_recorded_and_redacted()
    test_repeated_tool_failures_create_promotable_candidate()
    print("Tutti i test learning loop passati!")
