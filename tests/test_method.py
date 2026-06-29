"""Test per il metodo operativo derivato dal runtime."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.environment import EnvironmentSnapshot
from core.method import build_operating_method


def test_operating_method_prefers_structured_tools_and_verification():
    snapshot = EnvironmentSnapshot(
        timestamp="2026-03-18T12:00:00+00:00",
        os_name="Linux",
        runtime_label="Linux (WSL)",
        hostname="box",
        shell_path="/bin/bash",
        shell_name="bash",
        shell_family="posix",
        cwd="/workspace",
        workspace_dir="/workspace",
        project_types=["python"],
        commands={"python3": "/usr/bin/python3", "rg": "/usr/bin/rg"},
        preferred={
            "shell": "bash",
            "python_tests": "python3 -m pytest",
        },
    )

    text = build_operating_method(
        snapshot,
        ["find_files", "grep", "read_file", "edit_file", "write_file", "shell", "process_list", "process_kill"],
    )

    assert "find_files" in text
    assert "grep" in text
    assert "read_file" in text
    assert "edit_file" in text
    assert "shell" in text
    assert "python3 -m pytest" in text


def test_operating_method_mentions_media_tools_when_present():
    snapshot = EnvironmentSnapshot(
        timestamp="2026-03-18T12:00:00+00:00",
        os_name="Linux",
        runtime_label="Linux (WSL)",
        hostname="box",
        shell_path="/bin/bash",
        shell_name="bash",
        shell_family="posix",
        cwd="/workspace",
        workspace_dir="/workspace",
        project_types=["python"],
        commands={"python3": "/usr/bin/python3"},
        preferred={},
    )

    text = build_operating_method(
        snapshot,
        ["image_analyze", "audio_transcribe", "pdf_read"],
    )

    assert "image_analyze" in text
    assert "audio_transcribe" in text
    assert "pdf_read" in text


def test_operating_method_mentions_background_process_flow():
    snapshot = EnvironmentSnapshot(
        timestamp="2026-03-18T12:00:00+00:00",
        os_name="Linux",
        runtime_label="Linux",
        hostname="box",
        shell_path="/bin/bash",
        shell_name="bash",
        shell_family="posix",
        cwd="/workspace",
        workspace_dir="/workspace",
        project_types=["python"],
        commands={"python3": "/usr/bin/python3"},
        preferred={"shell": "bash"},
    )

    text = build_operating_method(
        snapshot,
        ["shell", "process_start", "process_read", "process_stop"],
    )

    assert "process_start" in text
    assert "process_read" in text
    assert "process_stop" in text
    assert "Non bloccare il turno con `shell`" in text


def test_operating_method_mentions_notify_for_meaningful_proactive_messages():
    snapshot = EnvironmentSnapshot(
        timestamp="2026-03-18T12:00:00+00:00",
        os_name="Linux",
        runtime_label="Linux",
        hostname="box",
        shell_path="/bin/bash",
        shell_name="bash",
        shell_family="posix",
        cwd="/workspace",
        workspace_dir="/workspace",
        project_types=["python"],
        commands={"python3": "/usr/bin/python3"},
        preferred={},
    )

    text = build_operating_method(snapshot, ["notify"])

    assert "notify" in text
    assert "fuori turno" in text


def test_operating_method_mentions_plugin_growth_flow():
    snapshot = EnvironmentSnapshot(
        timestamp="2026-03-18T12:00:00+00:00",
        os_name="Linux",
        runtime_label="Linux",
        hostname="box",
        shell_path="/bin/bash",
        shell_name="bash",
        shell_family="posix",
        cwd="/workspace",
        workspace_dir="/workspace",
        project_types=["python"],
        commands={"python3": "/usr/bin/python3"},
        preferred={},
    )

    text = build_operating_method(
        snapshot,
        ["scaffold_plugin", "reload_plugins", "request_restart"],
    )

    assert "scaffold_plugin" in text
    assert "reload_plugins" in text
    assert "request_restart" in text


def test_operating_method_mentions_subagents_and_doctor():
    snapshot = EnvironmentSnapshot(
        timestamp="2026-03-18T12:00:00+00:00",
        os_name="Linux",
        runtime_label="Linux",
        hostname="box",
        shell_path="/bin/bash",
        shell_name="bash",
        shell_family="posix",
        cwd="/workspace",
        workspace_dir="/workspace",
        project_types=["python"],
        commands={"python3": "/usr/bin/python3"},
        preferred={},
    )

    text = build_operating_method(
        snapshot,
        ["subagent_spawn", "subagent_wait", "doctor"],
    )

    assert "subagent_spawn" in text
    assert "subagent_wait" in text
    assert "doctor" in text


def test_operating_method_mentions_setup_desktop_and_memory_consolidation():
    snapshot = EnvironmentSnapshot(
        timestamp="2026-03-18T12:00:00+00:00",
        os_name="Linux",
        runtime_label="Linux",
        hostname="box",
        shell_path="/bin/bash",
        shell_name="bash",
        shell_family="posix",
        cwd="/workspace",
        workspace_dir="/workspace",
        project_types=["python"],
        commands={"python3": "/usr/bin/python3"},
        preferred={},
    )

    text = build_operating_method(
        snapshot,
        ["doctor", "doctor_fix", "desktop_screenshot", "notify_file", "memory_consolidate"],
    )

    assert "doctor_fix" in text
    assert "desktop_screenshot" in text
    assert "notify_file" in text
    assert "memory_consolidate" in text


def test_operating_method_prefers_browser_devtools_for_chrome_tasks():
    snapshot = EnvironmentSnapshot(
        timestamp="2026-03-18T12:00:00+00:00",
        os_name="Linux",
        runtime_label="Linux",
        hostname="box",
        shell_path="/bin/bash",
        shell_name="bash",
        shell_family="posix",
        cwd="/workspace",
        workspace_dir="/workspace",
        project_types=["python"],
        commands={"python3": "/usr/bin/python3"},
        preferred={},
    )

    text = build_operating_method(
        snapshot,
        ["browser_devtools", "desktop_screenshot"],
    )

    assert "browser_devtools" in text
    assert "Chrome" in text
    assert "desktop_screenshot" in text
    assert "debugging" in text


def test_operating_method_prefers_browser_tool_when_present():
    snapshot = EnvironmentSnapshot(
        timestamp="2026-03-18T12:00:00+00:00",
        os_name="Linux",
        runtime_label="Linux",
        hostname="box",
        shell_path="/bin/bash",
        shell_name="bash",
        shell_family="posix",
        cwd="/workspace",
        workspace_dir="/workspace",
        project_types=["python"],
        commands={"python3": "/usr/bin/python3"},
        preferred={},
    )

    text = build_operating_method(
        snapshot,
        ["browser", "browser_devtools", "desktop_screenshot"],
    )

    assert "browser" in text
    assert "mode=\"shared\"" in text
    assert "mode=\"isolated\"" in text
    assert "action=\"relaunch\"" in text
    assert "firefox" in text
    assert "webkit" in text


def test_operating_method_adds_windows_shell_guidance():
    snapshot = EnvironmentSnapshot(
        timestamp="2026-03-18T12:00:00+00:00",
        os_name="Windows",
        runtime_label="Windows 11",
        hostname="box",
        shell_path=r"C:\Windows\System32\cmd.exe",
        shell_name="cmd.exe",
        shell_family="cmd",
        cwd=r"C:\workspace",
        workspace_dir=r"C:\workspace",
        project_types=["python"],
        commands={"python": r"C:\Python\python.exe"},
        preferred={"shell": "cmd.exe"},
    )

    text = build_operating_method(snapshot, ["shell"])

    assert "cmd" in text
    assert "powershell -NoProfile -Command" in text
    assert "Non mischiare sintassi di shell diverse" in text


if __name__ == "__main__":
    test_operating_method_prefers_structured_tools_and_verification()
    test_operating_method_mentions_media_tools_when_present()
    test_operating_method_mentions_background_process_flow()
    test_operating_method_mentions_notify_for_meaningful_proactive_messages()
    test_operating_method_mentions_plugin_growth_flow()
    test_operating_method_mentions_subagents_and_doctor()
    test_operating_method_mentions_setup_desktop_and_memory_consolidation()
    test_operating_method_prefers_browser_devtools_for_chrome_tasks()
    test_operating_method_prefers_browser_tool_when_present()
    test_operating_method_adds_windows_shell_guidance()
    print("Tutti i test method passati!")
