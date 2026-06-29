"""
openvurp Tool — Desktop

Cattura screenshot del desktop reale.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

from core.tools import Tool, ToolResult, ErrorType


OPENVURP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTURES_DIR = os.path.join(OPENVURP_DIR, "memory", "captures")


def _default_capture_path() -> str:
    os.makedirs(CAPTURES_DIR, exist_ok=True)
    ts = int(time.time())
    return os.path.join(CAPTURES_DIR, f"desktop_{ts}.png")


def _wsl_to_windows_path(path: str) -> str:
    result = subprocess.run(
        ["wslpath", "-w", path],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "wslpath fallito")
    return result.stdout.strip()


def _capture_with_powershell(path: str):
    target = path
    if os.name != "nt":
        target = _wsl_to_windows_path(path)

    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "Add-Type -AssemblyName System.Drawing; "
        "$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
        "$bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height; "
        "$graphics = [System.Drawing.Graphics]::FromImage($bitmap); "
        "$graphics.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size); "
        f"$bitmap.Save('{target}', [System.Drawing.Imaging.ImageFormat]::Png); "
        "$graphics.Dispose(); "
        "$bitmap.Dispose();"
    )

    exe = "powershell" if os.name == "nt" else "powershell.exe"
    result = subprocess.run(
        [exe, "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "PowerShell screenshot fallito")


def _capture_with_pillow(path: str):
    from PIL import ImageGrab

    image = ImageGrab.grab(all_screens=True)
    image.save(path, "PNG")


def desktop_screenshot_handler(path: str = "") -> ToolResult:
    target_path = os.path.abspath(path or _default_capture_path())
    os.makedirs(os.path.dirname(target_path), exist_ok=True)

    try:
        if os.name == "nt" or shutil.which("powershell.exe"):
            _capture_with_powershell(target_path)
        else:
            _capture_with_pillow(target_path)
    except ImportError:
        return ToolResult.fail(
            "Per screenshot desktop senza PowerShell serve Pillow (pip install pillow).",
            error_type=ErrorType.DEPENDENCY,
        )
    except Exception as e:
        return ToolResult.fail(str(e), error_type=ErrorType.RUNTIME)

    if not os.path.exists(target_path):
        return ToolResult.fail("Screenshot non creato.", error_type=ErrorType.RUNTIME)

    return ToolResult.ok(f"Screenshot salvato in: {target_path}")


DESKTOP_SCREENSHOT_TOOL = Tool(
    name="desktop_screenshot",
    description="Cattura uno screenshot del desktop reale e salva un file PNG nel workspace.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Percorso output opzionale. Default: memory/captures/desktop_<timestamp>.png",
            },
        },
    },
    handler=desktop_screenshot_handler,
    timeout=45,
)
