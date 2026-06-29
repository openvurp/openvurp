"""
openvurp Tools — Browser Setup

Bootstrap dell'ambiente browser nel Python corrente:
- installa l'extra browser del progetto
- installa i runtime Playwright richiesti
"""

from __future__ import annotations

import os
import subprocess
import sys

from core.browser_manager import PLAYWRIGHT_VENDOR_DIR, PLAYWRIGHT_BROWSERS_DIR, PROBE_CACHE_PATH
from core.tools import Tool, ToolResult, ErrorType, RetryPolicy


OPENVURP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(command: list[str], env: dict | None = None) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            cwd=OPENVURP_DIR,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
            env=env,
        )
    except Exception as exc:
        return False, str(exc)
    output = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    combined = output
    if error:
        combined = combined + ("\n" if combined else "") + error
    return result.returncode == 0, combined.strip()


def browser_setup_handler(
    install_package: bool = True,
    browsers: str = "chromium firefox webkit",
    channels: str = "",
    with_deps: bool = False,
) -> ToolResult:
    browsers = " ".join(str(browsers or "").split()) or "chromium firefox webkit"
    channels = " ".join(str(channels or "").split())
    steps: list[str] = []
    os.makedirs(PLAYWRIGHT_VENDOR_DIR, exist_ok=True)
    os.makedirs(PLAYWRIGHT_BROWSERS_DIR, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = PLAYWRIGHT_VENDOR_DIR + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PLAYWRIGHT_BROWSERS_PATH"] = PLAYWRIGHT_BROWSERS_DIR
    env["PLAYWRIGHT_SKIP_BROWSER_GC"] = "1"

    if install_package:
        ok, output = _run(
            [sys.executable, "-m", "pip", "install", "--target", PLAYWRIGHT_VENDOR_DIR, "--upgrade", "playwright"],
            env=env,
        )
        steps.append(f"## {sys.executable} -m pip install --target {PLAYWRIGHT_VENDOR_DIR} --upgrade playwright")
        steps.append(output or "(no output)")
        if not ok:
            return ToolResult.fail(
                "Installazione pacchetto browser fallita.",
                error_type=ErrorType.DEPENDENCY,
                output="\n\n".join(steps),
            )

    command = [sys.executable, "-m", "playwright", "install"]
    if with_deps:
        command.append("--with-deps")
    command.extend(browsers.split())
    ok, output = _run(command, env=env)
    steps.append(f"## {' '.join(command)}")
    steps.append(output or "(no output)")
    if not ok:
        return ToolResult.fail(
            "Installazione runtime Playwright fallita.",
            error_type=ErrorType.DEPENDENCY,
            output="\n\n".join(steps),
        )

    if channels:
        branded = [sys.executable, "-m", "playwright", "install", *channels.split()]
        ok, output = _run(branded, env=env)
        steps.append(f"## {' '.join(branded)}")
        steps.append(output or "(no output)")
        if not ok:
            return ToolResult.fail(
                "Installazione browser branded Playwright fallita.",
                error_type=ErrorType.DEPENDENCY,
                output="\n\n".join(steps),
            )

    try:
        if os.path.exists(PROBE_CACHE_PATH):
            os.remove(PROBE_CACHE_PATH)
    except OSError:
        pass

    return ToolResult.ok("\n\n".join(steps))


BROWSER_SETUP_TOOL = Tool(
    name="browser_setup",
    description=(
        "Installa Playwright e i runtime browser in una runtime cache locale al workspace. "
        "Usalo quando il doctor segnala che il browser layer non è pronto."
    ),
    parameters={
        "type": "object",
        "properties": {
            "install_package": {
                "type": "boolean",
                "description": "Se true, installa/aggiorna Playwright nella cache locale del workspace",
            },
            "browsers": {
                "type": "string",
                "description": "Lista runtime Playwright da installare, es. `chromium firefox webkit`",
            },
            "channels": {
                "type": "string",
                "description": (
                    "Browser branded opzionali da installare globalmente via Playwright, "
                    "es. `chrome msedge` o `chrome-beta msedge-dev`"
                ),
            },
            "with_deps": {
                "type": "boolean",
                "description": "Su Linux installa anche le dipendenze di sistema con `playwright install --with-deps`",
            },
        },
    },
    requires_approval=True,
    handler=browser_setup_handler,
    timeout=1800,
    retry_policy=RetryPolicy(max_retries=0),
)
