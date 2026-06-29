"""Test per browser_setup."""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tools import ErrorType
from tools.browser_setup import browser_setup_handler


def test_browser_setup_runs_install_steps():
    calls = []

    def fake_run(command, env=None):
        calls.append((command, env))
        return True, "ok"

    with patch("tools.browser_setup._run", side_effect=fake_run):
        result = browser_setup_handler()

    assert result.success is True
    assert any("pip" in " ".join(cmd) for cmd, _env in calls)
    assert any("playwright" in " ".join(cmd) for cmd, _env in calls)


def test_browser_setup_marks_dependency_failure():
    with patch("tools.browser_setup._run", return_value=(False, "boom")):
        result = browser_setup_handler()

    assert result.success is False
    assert result.error_type == ErrorType.DEPENDENCY


def test_browser_setup_can_request_deps_and_channels():
    calls = []

    def fake_run(command, env=None):
        calls.append(command)
        return True, "ok"

    with patch("tools.browser_setup._run", side_effect=fake_run):
        result = browser_setup_handler(
            install_package=False,
            browsers="chromium webkit",
            channels="chrome msedge",
            with_deps=True,
        )

    assert result.success is True
    assert any("--with-deps" in call for call in calls)
    assert any("chrome" in call and "msedge" in call for call in calls)


if __name__ == "__main__":
    test_browser_setup_runs_install_steps()
    test_browser_setup_marks_dependency_failure()
    test_browser_setup_can_request_deps_and_channels()
    print("Tutti i test browser setup passati!")
