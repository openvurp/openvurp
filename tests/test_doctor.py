"""Test per il doctor runtime/workspace."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.doctor import build_doctor_report


def test_doctor_report_renders_core_sections():
    workspace_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    report = build_doctor_report(
        workspace_dir,
        ["doctor", "process_start", "subagent_spawn", "subagent_wait"],
    )
    text = report.render()

    assert "## DOCTOR" in text
    assert "### Runtime" in text
    assert "### Workspace" in text
    assert "### Security" in text
    assert "### Plugin" in text
    assert "### Capability" in text
    assert "- doctor: yes" in text
    assert "- subagent_spawn: yes" in text


if __name__ == "__main__":
    test_doctor_report_renders_core_sections()
    print("Tutti i test doctor passati!")
