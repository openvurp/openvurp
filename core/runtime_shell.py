"""
openvurp Core — Runtime Shell

Risoluzione coerente della shell effettiva e dei suoi vincoli cross-platform.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import shlex
import shutil
import sys
from typing import Callable, Mapping


POSIX_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TERM",
    "SHELL",
    "TMPDIR",
)

WINDOWS_ENV_KEYS = (
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "SystemRoot",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOME",
    "APPDATA",
    "LOCALAPPDATA",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramW6432",
    "USERNAME",
    "TERM",
    "LANG",
    "LC_ALL",
    "SHELL",
)


@dataclass(frozen=True)
class ResolvedShell:
    path: str
    name: str
    family: str
    source: str
    platform: str

    @property
    def is_windows(self) -> bool:
        return is_windows_platform(self.platform)


def is_windows_platform(platform_name: str | None = None) -> bool:
    value = (platform_name or sys.platform or "").lower()
    return value.startswith("win")


def shell_basename(path: str) -> str:
    raw = (path or "").strip().strip('"')
    if not raw:
        return ""
    normalized = raw.replace("\\", "/")
    return normalized.rsplit("/", 1)[-1]


def infer_shell_family(shell_path_or_name: str) -> str:
    name = shell_basename(shell_path_or_name).lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name in {"pwsh", "powershell"}:
        return "powershell"
    if name == "cmd":
        return "cmd"
    if name in {"bash", "zsh", "sh", "fish", "dash", "ksh"}:
        return "posix"
    return "unknown"


def default_allowed_env_vars(platform_name: str | None = None) -> list[str]:
    keys = WINDOWS_ENV_KEYS if is_windows_platform(platform_name) else POSIX_ENV_KEYS
    seen: set[str] = set()
    ordered: list[str] = []
    for key in keys:
        if key and key not in seen:
            ordered.append(key)
            seen.add(key)
    return ordered


def split_command_tokens(
    command: str,
    shell_family: str = "",
    platform_name: str | None = None,
) -> list[str]:
    posix = True
    if shell_family in {"cmd", "powershell"}:
        posix = False
    elif shell_family == "posix":
        posix = True
    else:
        posix = not is_windows_platform(platform_name)

    try:
        return shlex.split(command, posix=posix)
    except ValueError:
        return command.split()


def resolve_effective_shell(
    configured_shell: str = "",
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    platform_name: str | None = None,
) -> ResolvedShell:
    env_map = dict(os.environ if env is None else env)
    platform_value = platform_name or sys.platform
    windows = is_windows_platform(platform_value)

    candidates: list[tuple[str, str]] = []
    if configured_shell:
        candidates.append(("configured", configured_shell))

    env_shell = (env_map.get("SHELL") or "").strip()
    if env_shell:
        candidates.append(("env:SHELL", env_shell))

    comspec = (env_map.get("COMSPEC") or "").strip()
    if windows and comspec:
        candidates.append(("env:COMSPEC", comspec))

    if windows:
        candidates.extend([
            ("which:cmd", "cmd"),
            ("which:pwsh", "pwsh"),
            ("which:powershell", "powershell"),
            ("which:bash", "bash"),
            ("which:sh", "sh"),
        ])
    else:
        candidates.extend([
            ("which:bash", "bash"),
            ("which:zsh", "zsh"),
            ("which:sh", "sh"),
            ("which:pwsh", "pwsh"),
            ("which:powershell", "powershell"),
        ])

    for source, candidate in candidates:
        resolved = _resolve_candidate_path(candidate, which=which)
        if not resolved:
            continue
        family = infer_shell_family(resolved)
        if family == "unknown":
            continue
        if windows and family == "posix" and resolved.startswith("/"):
            continue
        if windows and source.startswith("which:") and family == "cmd":
            # COMSPEC/cmd devono restare il fallback nativo su Windows.
            return ResolvedShell(
                path=resolved,
                name=shell_basename(resolved),
                family=family,
                source=source,
                platform=platform_value,
            )
        if source == "configured":
            return ResolvedShell(
                path=resolved,
                name=shell_basename(resolved),
                family=family,
                source=source,
                platform=platform_value,
            )
        if windows and source == "env:SHELL" and family not in {"posix", "powershell", "cmd"}:
            continue
        return ResolvedShell(
            path=resolved,
            name=shell_basename(resolved),
            family=family,
            source=source,
            platform=platform_value,
        )

    if windows:
        fallback = comspec or "cmd.exe"
        return ResolvedShell(
            path=fallback,
            name=shell_basename(fallback) or "cmd.exe",
            family="cmd",
            source="fallback",
            platform=platform_value,
        )

    fallback = "/bin/sh"
    return ResolvedShell(
        path=fallback,
        name="sh",
        family="posix",
        source="fallback",
        platform=platform_value,
    )


def build_shell_command(command: str, shell: ResolvedShell) -> list[str]:
    path = shell.path or ("cmd.exe" if shell.is_windows else "/bin/sh")

    if shell.family == "cmd":
        return [path, "/d", "/c", command]
    if shell.family == "powershell":
        return [path, "-NoProfile", "-Command", command]
    return [path, "-lc", command]


def _resolve_candidate_path(candidate: str, which: Callable[[str], str | None]) -> str:
    raw = (candidate or "").strip().strip('"')
    if not raw:
        return ""

    expanded = os.path.expandvars(raw)
    if _looks_like_path(expanded):
        if os.path.exists(expanded):
            return os.path.abspath(expanded)
        base = shell_basename(expanded)
        if base:
            resolved = which(base)
            if resolved:
                return resolved
        return ""

    resolved = which(expanded)
    return resolved or ""


def _looks_like_path(value: str) -> bool:
    if not value:
        return False
    if any(sep in value for sep in ("/", "\\")):
        return True
    return len(value) > 1 and value[1] == ":"
