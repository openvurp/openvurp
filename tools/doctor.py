"""
openvurp Tool — Doctor

Diagnostica strutturale del runtime e del workspace.
"""

from __future__ import annotations

import os

from core.doctor import build_doctor_report
from core.tools import Tool, ToolResult


OPENVURP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def doctor_handler(tool_names: list[str] | None = None) -> ToolResult:
    report = build_doctor_report(OPENVURP_DIR, tool_names or [])
    return ToolResult.ok(report.render())


DOCTOR_TOOL = Tool(
    name="doctor",
    description="Esegue una diagnosi rapida del runtime, del workspace e dei controlli di sicurezza.",
    parameters={
        "type": "object",
        "properties": {
            "tool_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Lista opzionale dei tool attivi per una diagnosi più precisa.",
            },
        },
    },
    handler=doctor_handler,
)
