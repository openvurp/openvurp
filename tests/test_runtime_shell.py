"""Test per la risoluzione coerente della shell cross-platform."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.runtime_shell import (
    build_shell_command,
    default_allowed_env_vars,
    resolve_effective_shell,
    split_command_tokens,
)


def test_resolve_effective_shell_ignores_invalid_posix_config_on_windows():
    env = {"COMSPEC": r"C:\Windows\System32\cmd.exe"}
    shell = resolve_effective_shell(
        configured_shell="/bin/bash",
        env=env,
        platform_name="win32",
        which=lambda name: None,
    )

    assert shell.family == "cmd"
    assert shell.path.lower().endswith("cmd.exe")
    assert shell.source in {"env:COMSPEC", "fallback"}


def test_resolve_effective_shell_prefers_valid_configured_shell():
    shell = resolve_effective_shell(
        configured_shell="/bin/bash",
        env={},
        platform_name="linux",
        which=lambda name: "/bin/bash" if name == "bash" else None,
    )

    assert shell.family == "posix"
    assert shell.path == "/bin/bash"
    assert shell.source == "configured"


def test_build_shell_command_uses_platform_specific_flags():
    windows_shell = resolve_effective_shell(
        configured_shell="",
        env={"COMSPEC": r"C:\Windows\System32\cmd.exe"},
        platform_name="win32",
        which=lambda name: None,
    )
    assert build_shell_command("echo hi", windows_shell) == [
        r"C:\Windows\System32\cmd.exe",
        "/d",
        "/c",
        "echo hi",
    ]

    posix_shell = resolve_effective_shell(
        configured_shell="/bin/bash",
        env={},
        platform_name="linux",
        which=lambda name: "/bin/bash" if name == "bash" else None,
    )
    assert build_shell_command("echo hi", posix_shell) == ["/bin/bash", "-lc", "echo hi"]


def test_default_allowed_env_vars_include_windows_runtime_keys():
    keys = default_allowed_env_vars("win32")
    assert "COMSPEC" in keys
    assert "SystemRoot" in keys
    assert "PATHEXT" in keys
    assert "USERPROFILE" in keys


def test_split_command_tokens_uses_non_posix_mode_for_windows_shells():
    tokens = split_command_tokens(
        'where powershell "C:\\Program Files"',
        shell_family="cmd",
        platform_name="win32",
    )

    assert tokens[0] == "where"
    assert tokens[-1] == '"C:\\Program Files"'


if __name__ == "__main__":
    test_resolve_effective_shell_ignores_invalid_posix_config_on_windows()
    test_resolve_effective_shell_prefers_valid_configured_shell()
    test_build_shell_command_uses_platform_specific_flags()
    test_default_allowed_env_vars_include_windows_runtime_keys()
    test_split_command_tokens_uses_non_posix_mode_for_windows_shells()
    print("Tutti i test runtime shell passati!")
