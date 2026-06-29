"""
openvurp Tool — Shell

Esecuzione comandi nel terminale reale dell'ambiente con timeout e output cap.
"""

from __future__ import annotations

import os
import subprocess

from core.tools import Tool, ToolResult, ErrorType, RetryPolicy
from core.security.sandbox import Sandbox, SandboxConfig, SandboxMode
from core.runtime_shell import default_allowed_env_vars, resolve_effective_shell


def _build_sandbox(workdir: str | None) -> Sandbox:
    import config as cfg

    cwd = os.path.abspath(workdir or os.getcwd())
    raw_mode = str(getattr(cfg, "SANDBOX_MODE", "restricted") or "restricted").lower()
    try:
        mode = SandboxMode(raw_mode)
    except ValueError:
        mode = SandboxMode.RESTRICTED

    allowed_paths = list(getattr(cfg, "SANDBOX_ALLOWED_PATHS", []) or [])
    if not allowed_paths:
        allowed_paths = [os.getcwd()]

    resolved_shell = resolve_effective_shell(getattr(cfg, "SHELL", ""))
    sandbox_cfg = SandboxConfig(
        mode=mode,
        shell_executable=resolved_shell.path,
        allowed_paths=[os.path.abspath(path) for path in allowed_paths],
        docker_image=getattr(cfg, "SANDBOX_DOCKER_IMAGE", "python:3.12-slim"),
        docker_memory=getattr(cfg, "SANDBOX_DOCKER_MEMORY", "512m"),
        docker_cpus=getattr(cfg, "SANDBOX_DOCKER_CPUS", "1"),
        docker_network=getattr(cfg, "SANDBOX_DOCKER_NETWORK", "none"),
        docker_timeout=int(getattr(cfg, "SANDBOX_TIMEOUT", 120) or 120),
        allowed_env_vars=default_allowed_env_vars(),
    )
    return Sandbox(config=sandbox_cfg, working_dir=cwd)


def _truncate_output(out: str) -> str:
    if len(out) <= 20000:
        return out
    return out[:10000] + "\n[...TRONCATO...]\n" + out[-5000:]


def shell_handler(command: str, timeout: int = 120, workdir: str = None,
                  dry_run: bool = False) -> ToolResult:
    """Esegue un comando nella shell del sistema."""
    if dry_run:
        cwd = os.path.abspath(workdir or os.getcwd())
        return ToolResult.ok(
            f"DRY RUN\nwould: run command `{command}` in `{cwd}`\n"
            "effect: no command was executed"
        )

    try:
        sandbox = _build_sandbox(workdir)
        ok, reason = sandbox.check_path(sandbox.working_dir)
        if not ok:
            return ToolResult.fail(
                reason,
                error_type=ErrorType.PERMISSION,
                retryable=False,
            )

        out, return_code = sandbox.execute(command, timeout=timeout)
        out = _truncate_output((out or "").strip()) or "(no output)"

        if return_code != 0:
            out += f"\n[exit code: {return_code}]"
            error_type = ErrorType.TIMEOUT if return_code == 124 else ErrorType.RUNTIME
            return ToolResult.fail(
                error=f"Exit code {return_code}",
                error_type=error_type,
                output=out,
                retryable=(return_code == 124),
            )

        return ToolResult.ok(out)

    except subprocess.TimeoutExpired:
        return ToolResult.fail(
            f"Timeout dopo {timeout}s",
            error_type=ErrorType.TIMEOUT,
            retryable=True
        )
    except Exception as e:
        return ToolResult.fail(str(e), error_type=ErrorType.RUNTIME)


SHELL_TOOL = Tool(
    name="shell",
    description="Esegue un comando nella shell del sistema passando dalla sandbox configurata (restricted/docker/nsjail/none).",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Il comando da eseguire"
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in secondi (default: 120)"
            },
            "workdir": {
                "type": "string",
                "description": "Directory di lavoro (opzionale)"
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true, preview the action without executing the command"
            }
        },
        "required": ["command"]
    },
    requires_approval=False,
    timeout=120,
    retry_policy=RetryPolicy(max_retries=0),
    handler=shell_handler
)
