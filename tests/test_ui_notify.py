"""Output asincrono prompt-safe: mentre sei sul prompt va in coda, poi si svuota."""

from agent import UI


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
