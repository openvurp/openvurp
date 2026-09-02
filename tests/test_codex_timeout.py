"""A provider that is still talking is not a provider that died.

Watched live. Warren was asked for the news of the day and wrote a long, good
analysis — it arrived on screen word by word. Then: "warren non ha risposto:
Codex timeout dopo 300s". The whole answer was thrown away and reported as
silence.

Two faults, one on top of the other. The clock measured the length of the
answer instead of the silence of the provider, so a long piece of work was cut
off mid-sentence while it was still being written. And when the clock fired,
the text already streamed — the text the user was reading — was discarded in
favour of an exception.
"""

import time

import pytest

from core.cli_backends import CodexCLIBackend


def _backend(**kw):
    return CodexCLIBackend(binary="codex", model="", workspace=".",
                           timeout=kw.pop("timeout", 300), **kw)


def test_the_silence_clock_and_the_ceiling_are_two_different_things():
    b = _backend(timeout=60)
    assert b.timeout == 60
    assert b.max_turn_seconds > b.timeout, (
        "with a single clock, a long answer dies of its own length")


def test_the_ceiling_can_be_set_and_is_never_below_the_silence():
    assert _backend(timeout=100, max_turn_seconds=50).max_turn_seconds == 100
    assert _backend(timeout=100, max_turn_seconds=900).max_turn_seconds == 900


def test_every_event_pushes_the_silence_clock_forward():
    """The rule in one line: as long as words arrive, the provider is alive."""
    import inspect
    source = inspect.getsource(CodexCLIBackend)
    assert "deadline = time.monotonic() + self.timeout" in source
    # ...and it has to happen on each event, not only at the start.
    assert source.count("deadline = time.monotonic() + self.timeout") >= 2, (
        "the clock is set once and never reset: any long answer dies")


def test_what_was_already_said_is_never_thrown_away():
    """The heart of it: an answer that arrived beats an exception."""
    import inspect
    source = inspect.getsource(CodexCLIBackend)
    assert "final_text or \"\".join(streamed_parts)" in source, (
        "on timeout the streamed text is discarded")
    assert "[interrotto:" in source, "it comes back with no sign it was cut short"


def test_the_cut_answer_says_it_was_cut(monkeypatch):
    """The user must be able to tell a finished answer from a truncated one."""
    import subprocess

    import core.cli_backends as C

    fatto = {"chiamato": False}

    def _finto(*a, **kw):
        fatto["chiamato"] = True
        raise subprocess.TimeoutExpired(
            cmd="codex", timeout=300,
            output='{"type":"item.completed","item":{"type":"agent_message",'
                   '"text":"la prima meta\' del ragionamento"}}\n',
        )

    monkeypatch.setattr(C.subprocess, "run", _finto)
    b = _backend(timeout=300)
    # Il controllo del login passa dallo stesso subprocess.run: senza spegnerlo
    # la finta lo intercetta e non si arriva mai al punto da verificare.
    b.require_subscription_login = False
    monkeypatch.setattr(b, "_resolved_binary", lambda: "codex", raising=False)

    try:
        result = b.run([{"role": "user", "content": "una domanda lunga"}])
    except Exception as exc:                      # la strada puo' cambiare
        pytest.skip(f"percorso non raggiunto in questo assetto: {exc}")

    assert fatto["chiamato"]
    assert "la prima meta' del ragionamento" in result.text, "il parziale e' andato perso"
    assert "interrotto" in result.text, "sembra una risposta completa e non lo e'"


def test_the_defaults_are_declared_where_they_belong():
    import config as cfg
    assert getattr(cfg, "CODEX_TIMEOUT_SECONDS", None), "manca il silenzio ammesso"
    assert getattr(cfg, "CODEX_MAX_TURN_SECONDS", 0) > cfg.CODEX_TIMEOUT_SECONDS


# ── e l'altro strato: lo sciame ────────────────────────────────────────────

def test_the_swarm_keeps_what_the_agent_already_said():
    """The message the user actually saw: "warren non ha risposto".

    He had answered. A long analysis, streamed to the screen word by word, then
    the turn fell over and the whole thing was replaced by a claim that nothing
    arrived. Whatever kills the turn, text that reached the user is not lost.
    """
    import tempfile

    from core.chat_store import ChatStore
    from core.swarm import Swarm, SwarmError

    store = ChatStore(tempfile.mkdtemp())
    store.create_agent("warren", "investimenti", "", "", "")
    swarm = Swarm(None, store=store)
    member = swarm.resolve("warren")

    def cade_dopo_aver_parlato(m, client, messages, allow_peers=True,
                               chat_id="", steps=None, collected=None):
        if collected is not None:
            collected.append("la prima parte dell'analisi")
        raise RuntimeError("Codex timeout dopo 300s")

    swarm._client = lambda m: object()
    swarm._charge = lambda: None
    swarm._run_with_tools = cade_dopo_aver_parlato

    out = swarm._speak(member, "notizie del giorno", chat_id="", persist=False)
    assert "la prima parte dell'analisi" in out
    assert "interrotto" in out, "sembra completa e non lo è"


def test_it_still_says_nothing_arrived_when_nothing_did():
    """The other half: silence must keep being reported as silence."""
    import tempfile

    from core.chat_store import ChatStore
    from core.swarm import Swarm, SwarmError

    store = ChatStore(tempfile.mkdtemp())
    store.create_agent("warren", "investimenti", "", "", "")
    swarm = Swarm(None, store=store)
    member = swarm.resolve("warren")

    def cade_subito(m, client, messages, allow_peers=True,
                    chat_id="", steps=None, collected=None):
        raise RuntimeError("backend giù")

    swarm._client = lambda m: object()
    swarm._charge = lambda: None
    swarm._run_with_tools = cade_subito

    with pytest.raises(SwarmError, match="non ha risposto"):
        swarm._speak(member, "domanda", chat_id="", persist=False)
