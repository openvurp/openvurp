"""Test per environment snapshot e prompt habitat."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.environment import EnvironmentInspector, EnvironmentSnapshot, render_environment_prompt
from core.memory import MemoryManager


def test_render_environment_prompt_includes_real_preferences():
    snapshot = EnvironmentSnapshot(
        timestamp="2026-03-18T10:00:00+00:00",
        os_name="Linux",
        runtime_label="Linux (WSL)",
        hostname="devbox",
        shell_path="/bin/bash",
        shell_name="bash",
        shell_family="posix",
        cwd="/workspace",
        workspace_dir="/workspace",
        is_wsl=True,
        repo_root="/workspace",
        git_branch="main",
        project_types=["python", "node"],
        markers=["pyproject.toml", "package.json", "tests/"],
        commands={"python3": "/usr/bin/python3", "rg": "/usr/bin/rg", "git": "/usr/bin/git", "npm": "/usr/bin/npm"},
        preferred={
            "shell": "bash",
            "search_text": "rg",
            "search_files": "rg --files",
            "python": "python3",
            "python_tests": "python3 -m pytest",
            "js_package_manager": "npm",
        },
    )

    prompt = render_environment_prompt(snapshot)
    assert "Linux (WSL)" in prompt
    assert "`bash` (posix)" in prompt
    assert "/bin/bash" in prompt
    assert "`rg --files`" in prompt
    assert "`python3 -m pytest`" in prompt
    assert "repo git" in prompt
    assert "Non mescolare sintassi `cmd`, PowerShell e POSIX" in prompt


def test_environment_inspector_refreshes_memory_with_detected_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        memory = MemoryManager(tmp)
        inspector = EnvironmentInspector(tmp)

        snapshot = EnvironmentSnapshot(
            timestamp="2026-03-18T10:00:00+00:00",
            os_name="Linux",
            runtime_label="Linux",
            hostname="box",
            shell_path="/bin/bash",
            shell_name="bash",
            shell_family="posix",
            cwd=tmp,
            workspace_dir=tmp,
            commands={"python3": "/usr/bin/python3", "rg": "/usr/bin/rg"},
            preferred={"shell": "bash", "search_text": "rg", "python": "python3"},
        )

        inspector.get_snapshot = lambda force=False: snapshot
        inspector.refresh_memory(memory)
        stored = memory.get_environment()

        assert stored["shell"]["name"] == "bash"
        assert stored["preferred"]["search_text"] == "rg"
        assert stored["commands"]["python3"] == "/usr/bin/python3"


if __name__ == "__main__":
    test_render_environment_prompt_includes_real_preferences()
    test_environment_inspector_refreshes_memory_with_detected_snapshot()
    print("Tutti i test environment passati!")
