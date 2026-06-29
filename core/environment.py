"""
openvurp Core — Environment Snapshot

Scoperta compatta dell'ambiente runtime e del workspace.
Serve a dare all'agente una vista reale di dove vive senza gonfiare il prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
import platform
import shutil
import socket
import subprocess
import time
from typing import Callable

from core.runtime_shell import resolve_effective_shell


COMMON_COMMANDS = (
    "bash",
    "zsh",
    "sh",
    "pwsh",
    "powershell",
    "python3",
    "python",
    "py",
    "uv",
    "pip",
    "pytest",
    "git",
    "rg",
    "fd",
    "find",
    "node",
    "npm",
    "pnpm",
    "yarn",
    "bun",
    "docker",
    "sqlite3",
    "make",
)

PROJECT_MARKERS = {
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "package.json": "node",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "Makefile": "make",
    "docker-compose.yml": "docker",
    "compose.yml": "docker",
}


@dataclass
class EnvironmentSnapshot:
    timestamp: str
    os_name: str
    runtime_label: str
    hostname: str
    shell_path: str
    shell_name: str
    shell_family: str
    cwd: str
    workspace_dir: str
    is_wsl: bool = False
    repo_root: str = ""
    git_branch: str = ""
    project_types: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    commands: dict[str, str] = field(default_factory=dict)
    preferred: dict[str, str] = field(default_factory=dict)

    def to_memory_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "os": self.os_name,
            "runtime": self.runtime_label,
            "hostname": self.hostname,
            "shell": {
                "path": self.shell_path,
                "name": self.shell_name,
                "family": self.shell_family,
            },
            "cwd": self.cwd,
            "workspace": self.workspace_dir,
            "is_wsl": self.is_wsl,
            "repo": {
                "root": self.repo_root,
                "branch": self.git_branch,
            },
            "project_types": self.project_types,
            "markers": self.markers,
            "commands": self.commands,
            "preferred": self.preferred,
        }


def render_environment_prompt(snapshot: EnvironmentSnapshot) -> str:
    """Rende il contesto ambiente in forma corta e operativa."""
    repo_note = "non è un repo git"
    if snapshot.repo_root:
        repo_note = f"repo git"
        if snapshot.git_branch:
            repo_note += f" su `{snapshot.git_branch}`"

    project_note = ", ".join(snapshot.project_types) if snapshot.project_types else "nessun ecosistema evidente"
    marker_note = ", ".join(snapshot.markers[:6]) if snapshot.markers else "nessun marker forte"
    available_note = ", ".join(sorted(snapshot.commands)) if snapshot.commands else "nessun tool noto"

    preference_lines = []
    preference_labels = {
        "shell": "shell",
        "search_text": "cerca testo",
        "search_files": "cerca file",
        "python": "python",
        "python_tests": "test python",
        "python_install": "installa python",
        "js_package_manager": "package manager js",
    }
    for key in (
        "shell",
        "search_text",
        "search_files",
        "python",
        "python_tests",
        "python_install",
        "js_package_manager",
    ):
        value = snapshot.preferred.get(key)
        if value:
            label = preference_labels.get(key, key)
            preference_lines.append(f"- {label}: `{value}`")

    preferences = "\n".join(preference_lines) if preference_lines else "- usa solo i tool che risultano davvero disponibili"

    return (
        "## DOVE VIVO\n"
        f"- runtime: {snapshot.runtime_label}\n"
        f"- host: `{snapshot.hostname}`\n"
        f"- shell attiva: `{snapshot.shell_name}` ({snapshot.shell_family})\n"
        f"- path shell: `{snapshot.shell_path or 'non rilevato'}`\n"
        f"- workspace: `{snapshot.workspace_dir}`\n"
        f"- progetto: {project_note}\n"
        f"- marker workspace: {marker_note}\n"
        f"- stato repo: {repo_note}\n"
        f"- comandi trovati: {available_note}\n"
        "### Preferenze Operative\n"
        f"{preferences}\n"
        "Resta coerente con il dialetto della shell attiva. "
        "Non mescolare sintassi `cmd`, PowerShell e POSIX nello stesso comando o nello stesso tentativo.\n"
        "Non inventare tool, package manager o shell che qui non esistono. "
        "Quando devi scegliere come fare qualcosa, parti da queste preferenze."
    )


class EnvironmentInspector:
    """Discovery con cache corta e persistenza compatta in memory/environment.json."""

    def __init__(
        self,
        workspace_dir: str,
        ttl_seconds: int = 300,
        which: Callable[[str], str | None] = shutil.which,
    ):
        self.workspace_dir = workspace_dir
        self.ttl_seconds = ttl_seconds
        self._which = which
        self._cached_snapshot: EnvironmentSnapshot | None = None
        self._cached_at = 0.0

    def get_snapshot(self, force: bool = False) -> EnvironmentSnapshot:
        now = time.time()
        if (
            not force
            and self._cached_snapshot is not None
            and (now - self._cached_at) < self.ttl_seconds
        ):
            return self._cached_snapshot

        snapshot = self._detect()
        self._cached_snapshot = snapshot
        self._cached_at = now
        return snapshot

    def refresh_memory(self, memory_manager, force: bool = False) -> EnvironmentSnapshot:
        """Aggiorna memory/environment.json solo se cambia davvero o è stantio."""
        snapshot = self.get_snapshot(force=force)
        new_data = snapshot.to_memory_dict()
        current = memory_manager.get_environment()

        if self._should_persist(current, new_data):
            memory_manager.set_environment(new_data)

        return snapshot

    def _detect(self) -> EnvironmentSnapshot:
        try:
            import config as cfg
            configured_shell = getattr(cfg, "SHELL", "")
        except Exception:
            configured_shell = ""
        resolved_shell = resolve_effective_shell(
            configured_shell=configured_shell,
            which=self._which,
        )
        shell_path = resolved_shell.path
        shell_name = resolved_shell.name or "unknown"
        shell_family = resolved_shell.family
        hostname = socket.gethostname()
        cwd = os.getcwd()
        is_wsl = self._is_wsl()
        os_name = platform.system() or "Unknown"
        runtime_label = os_name
        if is_wsl:
            runtime_label += " (WSL)"
        elif platform.release():
            runtime_label += f" {platform.release()}"

        commands = self._detect_commands()
        markers, project_types = self._detect_workspace_markers()
        repo_root, git_branch = self._detect_git_state(commands)
        preferred = self._build_preferences(
            shell_name=shell_name,
            commands=commands,
            project_types=project_types,
            has_tests=os.path.isdir(os.path.join(self.workspace_dir, "tests")),
        )

        return EnvironmentSnapshot(
            timestamp=datetime.now(timezone.utc).isoformat(),
            os_name=os_name,
            runtime_label=runtime_label,
            hostname=hostname,
            shell_path=shell_path,
            shell_name=shell_name,
            shell_family=shell_family,
            cwd=cwd,
            workspace_dir=self.workspace_dir,
            is_wsl=is_wsl,
            repo_root=repo_root,
            git_branch=git_branch,
            project_types=project_types,
            markers=markers,
            commands=commands,
            preferred=preferred,
        )

    def _detect_commands(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for command in COMMON_COMMANDS:
            path = self._which(command)
            if path:
                found[command] = path

        comspec = os.environ.get("COMSPEC")
        if comspec and "cmd" not in found:
            found["cmd"] = comspec

        return found

    def _detect_workspace_markers(self) -> tuple[list[str], list[str]]:
        markers = []
        project_types = []

        for marker, project_type in PROJECT_MARKERS.items():
            path = os.path.join(self.workspace_dir, marker)
            if os.path.exists(path):
                markers.append(marker)
                if project_type not in project_types:
                    project_types.append(project_type)

        if os.path.isdir(os.path.join(self.workspace_dir, "tests")):
            markers.append("tests/")
        if os.path.isdir(os.path.join(self.workspace_dir, ".git")):
            markers.append(".git/")

        return markers, project_types

    def _detect_git_state(self, commands: dict[str, str]) -> tuple[str, str]:
        if "git" not in commands:
            return "", ""

        repo_root = self._safe_run(["git", "rev-parse", "--show-toplevel"])
        if not repo_root:
            return "", ""
        git_branch = self._safe_run(["git", "branch", "--show-current"])
        return repo_root, git_branch

    def _build_preferences(
        self,
        shell_name: str,
        commands: dict[str, str],
        project_types: list[str],
        has_tests: bool,
    ) -> dict[str, str]:
        preferred: dict[str, str] = {}

        if shell_name:
            preferred["shell"] = shell_name

        if "rg" in commands:
            preferred["search_text"] = "rg"
            preferred["search_files"] = "rg --files"
        elif "grep" in commands:
            preferred["search_text"] = "grep -R"
            if "find" in commands:
                preferred["search_files"] = "find . -type f"

        python_cmd = self._first_available(commands, ("python3", "python", "py"))
        if python_cmd:
            preferred["python"] = python_cmd
            if "uv" in commands:
                preferred["python_install"] = "uv"
            elif "pip" in commands:
                preferred["python_install"] = f"{python_cmd} -m pip"

            if has_tests:
                if "pytest" in commands:
                    preferred["python_tests"] = f"{python_cmd} -m pytest"
                else:
                    preferred["python_tests"] = f"{python_cmd} tests/<file>.py"

        if "node" in commands or "node" in project_types:
            js_pm = self._first_available(commands, ("pnpm", "bun", "yarn", "npm"))
            if js_pm:
                preferred["js_package_manager"] = js_pm

        return preferred

    def _first_available(self, commands: dict[str, str], candidates: tuple[str, ...]) -> str:
        for candidate in candidates:
            if candidate in commands:
                return candidate
        return ""

    def _safe_run(self, cmd: list[str]) -> str:
        try:
            completed = subprocess.run(
                cmd,
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                timeout=2,
                encoding="utf-8",
                errors="replace",
            )
        except Exception:
            return ""

        if completed.returncode != 0:
            return ""
        return (completed.stdout or "").strip()

    def _is_wsl(self) -> bool:
        if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
            return True
        try:
            with open("/proc/version", "r", encoding="utf-8") as fh:
                return "microsoft" in fh.read().lower()
        except Exception:
            return False

    def _should_persist(self, current: dict, new_data: dict) -> bool:
        if self._normalized(current) != self._normalized(new_data):
            return True

        timestamp = current.get("timestamp", "")
        if not timestamp:
            return True

        try:
            written_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except Exception:
            return True

        age_seconds = (datetime.now(timezone.utc) - written_at).total_seconds()
        return age_seconds > 6 * 3600

    def _normalized(self, data: dict) -> dict:
        if not isinstance(data, dict):
            return {}
        result = dict(data)
        result.pop("timestamp", None)
        return result
