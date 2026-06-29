"""Test per il wiring base di Chrome DevTools MCP."""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.browser_devtools import ChromeDevToolsMCP


def test_candidate_chrome_paths_use_windows_native_layout():
    with patch("tools.browser_devtools.ChromeDevToolsMCP._load_cached_browser_path", return_value=""):
        paths = ChromeDevToolsMCP._candidate_chrome_paths(
            system="Windows",
            env={
                "ProgramFiles": r"C:\Program Files",
                "ProgramFiles(x86)": r"C:\Program Files (x86)",
                "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
            },
        )

    assert any(path.startswith(r"C:\Program Files") for path in paths)
    assert all(not path.startswith("/mnt/c/") for path in paths)


def test_candidate_chrome_paths_prefers_explicit_override():
    with patch("tools.browser_devtools.ChromeDevToolsMCP._load_cached_browser_path", return_value=""):
        paths = ChromeDevToolsMCP._candidate_chrome_paths(
            system="Windows",
            env={
                "OPENVURP_BROWSER_PATH": r"D:\Browsers\Chrome\chrome.exe",
                "ProgramFiles": r"C:\Program Files",
            },
        )

    assert paths[0] == r"D:\Browsers\Chrome\chrome.exe"


def test_build_mcp_command_uses_yes_flag():
    with patch("tools.browser_devtools.shutil.which", return_value=None):
        command = ChromeDevToolsMCP._build_mcp_command(
            npx_path=r"C:\Program Files\nodejs\npx.cmd",
            auto_connect=True,
            channel="beta",
        )

    assert command[:3] == [r"C:\Program Files\nodejs\npx.cmd", "-y", "chrome-devtools-mcp@latest"]
    assert "--autoConnect" in command
    assert "--channel=beta" in command


def test_build_mcp_command_prefers_browser_url_over_autoconnect():
    with patch("tools.browser_devtools.shutil.which", return_value=None):
        command = ChromeDevToolsMCP._build_mcp_command(
            npx_path="npx",
            auto_connect=True,
            browser_url="http://127.0.0.1:9222",
        )

    assert "--browser-url=http://127.0.0.1:9222" in command
    assert "--autoConnect" not in command


if __name__ == "__main__":
    test_candidate_chrome_paths_use_windows_native_layout()
    test_candidate_chrome_paths_prefers_explicit_override()
    test_build_mcp_command_uses_yes_flag()
    test_build_mcp_command_prefers_browser_url_over_autoconnect()
    print("Tutti i test browser devtools passati!")
