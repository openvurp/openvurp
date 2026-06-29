"""Test per il self-update: classificazione revisioni e ciclo sentinel."""

from pathlib import Path

import core.updater as up


def test_classify_revisions():
    assert up.classify_revisions("a", "a", "a") == "up_to_date"
    # locale == base, remote avanti → si può fast-forward
    assert up.classify_revisions("base", "remote", "base") == "behind"
    # remote == base, locale avanti → abbiamo commit non pushati
    assert up.classify_revisions("local", "base", "base") == "ahead"
    # nessuno coincide con base → divergenza
    assert up.classify_revisions("local", "remote", "base") == "diverged"
    # input mancante
    assert up.classify_revisions("", "x", "y") == "unknown"


def test_restart_sentinel_roundtrip(tmp_path, monkeypatch):
    sentinel = tmp_path / "memory" / ".restart"
    monkeypatch.setattr(up, "RESTART_SENTINEL", sentinel)

    assert up.restart_pending() is False
    up.request_restart("motivo di prova")
    assert up.restart_pending() is True

    reason = up.consume_restart()
    assert reason == "motivo di prova"
    # consumato → niente più sentinel
    assert up.restart_pending() is False
    assert up.consume_restart() == ""


def test_consume_restart_defaults_when_no_reason(tmp_path, monkeypatch):
    sentinel = tmp_path / "memory" / ".restart"
    monkeypatch.setattr(up, "RESTART_SENTINEL", sentinel)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("1234567.0\n", encoding="utf-8")  # timestamp ma niente reason
    assert up.consume_restart() == "restart"
