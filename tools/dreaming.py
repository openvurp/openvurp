"""
openvurp Tool — Dreaming

Consolida memoria giornaliera in MEMORY.md.
"""

from __future__ import annotations

import os

from core.dreaming import consolidate_memory
from core.tools import Tool, ToolResult


OPENVURP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def memory_consolidate_handler(days: int = 7, max_lines_per_file: int = 5) -> ToolResult:
    report = consolidate_memory(
        OPENVURP_DIR,
        days=days,
        max_lines_per_file=max_lines_per_file,
    )
    return ToolResult.ok(report.render())


MEMORY_CONSOLIDATE_TOOL = Tool(
    name="memory_consolidate",
    description="Consolida note giornaliere recenti da memory/YYYY-MM-DD.md in MEMORY.md.",
    parameters={
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Quanti file giornalieri recenti considerare.",
            },
            "max_lines_per_file": {
                "type": "integer",
                "description": "Massime righe utili da estrarre per ogni file.",
            },
        },
    },
    handler=memory_consolidate_handler,
    timeout=30,
)
