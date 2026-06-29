"""
openvurp Tools - Task Journal, Reflection, Open Loops
"""

from __future__ import annotations

import os

from core.task_journal import TaskJournal
from core.tools import Tool, ToolResult


OPENVURP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_DIR = os.path.join(OPENVURP_DIR, "memory")


def _journal() -> TaskJournal:
    return TaskJournal(MEMORY_DIR)


def task_journal_handler(action: str = "review", note: str = "",
                         kind: str = "note",
                         tags: list[str] | None = None) -> ToolResult:
    journal = _journal()
    normalized = (action or "review").strip().lower()
    if normalized == "record":
        if not note.strip():
            return ToolResult.fail("Parametro note obbligatorio per action=record.")
        note_id = journal.record_note(note=note, kind=kind or "note", source="tool", tags=tags or [])
        return ToolResult.ok(f"Journal note recorded: {note_id}")
    if normalized == "review":
        return ToolResult.ok(journal.review().render())
    return ToolResult.fail("Azione non supportata. Usa: record, review.")


def reflection_note_handler(note: str = "", tags: list[str] | None = None) -> ToolResult:
    if not note.strip():
        return ToolResult.fail("Parametro note obbligatorio.")
    note_id = _journal().record_note(
        note=note,
        kind="reflection",
        source="tool",
        tags=tags or [],
    )
    return ToolResult.ok(f"Reflection note recorded: {note_id}")


def open_loop_handler(action: str = "list", title: str = "",
                      description: str = "", loop_id: str = "",
                      resolution: str = "", due: str = "",
                      tags: list[str] | None = None,
                      include_closed: bool = False) -> ToolResult:
    journal = _journal()
    normalized = (action or "list").strip().lower()

    if normalized == "add":
        if not title.strip():
            return ToolResult.fail("Parametro title obbligatorio per action=add.")
        loop = journal.add_open_loop(
            title=title,
            description=description,
            source="tool",
            due=due,
            tags=tags or [],
        )
        return ToolResult.ok(f"Open loop added: [{loop.id}] {loop.title}")

    if normalized == "close":
        if not loop_id.strip():
            return ToolResult.fail("Parametro loop_id obbligatorio per action=close.")
        loop = journal.close_open_loop(loop_id=loop_id, resolution=resolution, source="tool")
        if not loop:
            return ToolResult.fail(f"Open loop non trovato: {loop_id}")
        return ToolResult.ok(f"Open loop closed: [{loop.id}] {loop.title}")

    if normalized == "list":
        loops = journal.list_open_loops(include_closed=include_closed)
        if not loops:
            return ToolResult.ok("No open loops.")
        lines = []
        for loop in loops:
            due_text = f" due={loop.due}" if loop.due else ""
            lines.append(f"- [{loop.id}] {loop.status}: {loop.title}{due_text}")
            if loop.description:
                lines.append(f"  {loop.description}")
        return ToolResult.ok("\n".join(lines))

    return ToolResult.fail("Azione non supportata. Usa: add, list, close.")


TASK_JOURNAL_TOOL = Tool(
    name="task_journal",
    description="Record or review durable task journal notes for continuity.",
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["record", "review"],
                "description": "Journal action.",
            },
            "note": {
                "type": "string",
                "description": "Note content for action=record.",
            },
            "kind": {
                "type": "string",
                "description": "Note kind, such as decision, progress, risk, or lesson.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short tags.",
            },
        },
    },
    handler=task_journal_handler,
)

REFLECTION_NOTE_TOOL = Tool(
    name="reflection_note",
    description="Record a durable reflection about what changed, failed, or should improve.",
    parameters={
        "type": "object",
        "properties": {
            "note": {
                "type": "string",
                "description": "Reflection text.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short tags.",
            },
        },
        "required": ["note"],
    },
    handler=reflection_note_handler,
)

OPEN_LOOP_TOOL = Tool(
    name="open_loop",
    description="Add, list, or close durable open loops that the agent should carry forward.",
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "list", "close"],
                "description": "Open-loop action.",
            },
            "title": {
                "type": "string",
                "description": "Short title for action=add.",
            },
            "description": {
                "type": "string",
                "description": "Details for action=add.",
            },
            "loop_id": {
                "type": "string",
                "description": "Loop ID for action=close.",
            },
            "resolution": {
                "type": "string",
                "description": "Resolution note for action=close.",
            },
            "due": {
                "type": "string",
                "description": "Optional due date or reminder hint.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short tags.",
            },
            "include_closed": {
                "type": "boolean",
                "description": "Include closed loops when listing.",
            },
        },
    },
    handler=open_loop_handler,
)
