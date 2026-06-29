"""Test per BrowserManager e tool browser alto livello."""

import os
import sys
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.browser_manager import SUPPORTED_CHANNELS, choose_browser_mode
from core.tools import ErrorType
from tools.browser import browser_handler


def test_choose_browser_mode_prefers_shared_for_running_browser():
    assert choose_browser_mode("auto", "navigate", False, True, engine="chromium") == "shared"


def test_choose_browser_mode_uses_isolated_without_live_browser():
    assert choose_browser_mode("auto", "navigate", False, False, engine="chromium") == "isolated"


def test_choose_browser_mode_forces_isolated_for_firefox():
    assert choose_browser_mode("auto", "navigate", True, True, engine="firefox") == "isolated"


def test_supported_channels_include_branded_variants():
    expected = {
        "chromium",
        "chrome",
        "chrome-beta",
        "chrome-dev",
        "chrome-canary",
        "msedge",
        "msedge-beta",
        "msedge-dev",
        "msedge-canary",
    }
    assert expected.issubset(set(SUPPORTED_CHANNELS))


def test_browser_handler_routes_to_manager():
    manager = Mock()
    manager.read.return_value = "ok"

    with patch("tools.browser.get_browser_manager", return_value=manager):
        result = browser_handler(action="read", mode="auto")

    assert result.success is True
    assert result.output == "ok"


def test_browser_handler_marks_playwright_issue_as_dependency():
    manager = Mock()
    manager.read.side_effect = RuntimeError("Playwright non installato")

    with patch("tools.browser.get_browser_manager", return_value=manager):
        result = browser_handler(action="read", mode="auto")

    assert result.success is False
    assert result.error_type == ErrorType.DEPENDENCY


if __name__ == "__main__":
    test_choose_browser_mode_prefers_shared_for_running_browser()
    test_choose_browser_mode_uses_isolated_without_live_browser()
    test_choose_browser_mode_forces_isolated_for_firefox()
    test_supported_channels_include_branded_variants()
    test_browser_handler_routes_to_manager()
    test_browser_handler_marks_playwright_issue_as_dependency()
    print("Tutti i test browser manager passati!")
