"""Output asincrono prompt-safe e rendering Markdown della CLI."""

import io

from agent import UI
from rich.console import Console


def test_notify_queues_while_at_prompt():
    ui = UI()
    ui._at_prompt = True
    ui.notify("telegram msg")
    # in coda, NON stampato (box intatto)
    assert ui._pending_notes == ["telegram msg"]


def test_notify_prints_immediately_when_not_at_prompt():
    ui = UI()
    ui._at_prompt = False
    ui.notify("subito")
    # niente in coda: è stato stampato direttamente
    assert ui._pending_notes == []


def test_flush_notes_empties_queue():
    ui = UI()
    ui._at_prompt = True
    ui.notify("a")
    ui.notify("b")
    assert ui._pending_notes == ["a", "b"]
    ui.flush_notes()
    assert ui._pending_notes == []


def test_streaming_response_renders_markdown_without_raw_markers():
    ui = UI()
    output = io.StringIO()
    ui.console = Console(file=output, width=80, force_terminal=False)
    ui.start_response()
    ui.stream_token("**Mint ")
    ui.stream_token("Cucina Fresca**\n\n```python\nprint('ok')\n```")
    ui.end_response()
    rendered = output.getvalue()
    assert "Mint Cucina Fresca" in rendered
    assert "print('ok')" in rendered
    assert "**" not in rendered
    assert "```" not in rendered
