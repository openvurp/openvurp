"""The room must say who is missing, instead of letting the others wait.

What actually happened: a room with eight agents, a cap of three. The owner
wrote "dev, from now on…" — dev was fourth in the roster and never got the
message. Nothing said so. Amanda, ciccio and colibri spent two rounds urging
dev to answer, and ciccio finally wrote a line beginning with "dev:" in his
name. The closing said "dev has not answered yet: we have not decided".
"""

import pytest

import config as cfg
import core.multiplayer as M
from core.chat_store import ChatStore


class _Stub:
    script: dict = {}     # name -> text | callable(messages) | None (= silence)
    prompts: list = []

    def __init__(self, **_kw):
        self.max_tokens = 0
        self.temperature = 0.0

    def call_with_timing(self, messages, **_kw):
        type(self).prompts.append(messages)
        who = messages[0]["content"].split(",")[0].replace("Sei ", "")
        what = type(self).script.get(who)
        if callable(what):
            what = what(messages)
        return (what if what is not None else M.NOTHING), 1, 1, 1


@pytest.fixture
def room(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "create_llm_client", lambda **kw: _Stub(**kw))
    monkeypatch.setattr("core.chat_store.DEFAULT_AGENTS", (), raising=False)
    monkeypatch.setattr(cfg, "MULTIPLAYER_MAX_AGENTS", 3, raising=False)
    monkeypatch.setattr(cfg, "MULTIPLAYER_MAX_ROUNDS", 4, raising=False)
    _Stub.script, _Stub.prompts = {}, []
    store = ChatStore(str(tmp_path))
    ids = [store.create_agent(n, n + " role", "", "", "")["id"]
           for n in ("amanda", "ciccio", "colibri", "dev")]
    chat = store.create_chat("room", mode="team")
    store.set_chat_agents(chat["id"], ids)
    rows: list = []
    coord = M.MultiplayerCoordinator(store)

    def run(request):
        return coord.collaborate(chat["id"], request, on_message=rows.append)

    return run, rows, store


def _agents(rows):
    return [(r["author_name"], r["content"]) for r in rows
            if r.get("author_type") == "agent"]


def _notes(rows):
    return [r["content"] for r in rows if r.get("author_type") == "system"]


def test_whoever_is_called_by_name_is_in_and_speaks_first(room):
    run, rows, _ = room
    _Stub.script = {n: f"{n} here" for n in ("amanda", "ciccio", "colibri", "dev")}
    result = run("dev, from now on keep a history of your commands")

    assert _agents(rows)[0][0] == "dev", "the one you called did not speak first"
    spoke = {name for name, _ in _agents(rows)}
    assert "dev" in spoke
    # Three in, one out: and the one out is named, with where to fix it.
    assert len(result.notes) == 1
    assert "colibri" in result.notes[0] and "3" in result.notes[0]
    assert "Impostazioni" in result.notes[0]
    assert _notes(rows) == result.notes, "the notice is not stored in the room"


def test_the_notice_comes_before_anyone_speaks(room):
    run, rows, _ = room
    _Stub.script = {n: f"{n} here" for n in ("amanda", "ciccio", "colibri", "dev")}
    run("dev, keep a history")
    assert rows[0]["author_type"] == "system", "who is out must be said first"


def test_who_cannot_answer_is_said_when_it_happens_and_only_once(room):
    run, rows, _ = room
    turn = {"n": 0}

    def fresh(_messages):
        turn["n"] += 1
        return f"new point number {turn['n']}" if turn["n"] <= 4 else None

    def broken(_messages):
        raise RuntimeError("timeout")

    _Stub.script = {"amanda": fresh, "ciccio": fresh, "dev": broken}
    result = run("dev, keep a history")

    assert result.rounds >= 2
    notes = [n for n in _notes(rows) if "dev non ha risposto" in n]
    assert notes == ["dev non ha risposto: timeout"], notes
    # Said before amanda's first contribution, not in a line at the end.
    first_agent = next(i for i, r in enumerate(rows) if r.get("author_type") == "agent")
    assert any("dev non ha risposto" in r["content"] for r in rows[:first_agent])
    assert result.errors == ["dev: timeout"], "not retried in round two"


def test_the_others_are_told_not_to_wait_for_him(room):
    run, _rows, _ = room

    def broken(_messages):
        raise RuntimeError("timeout")

    _Stub.script = {"amanda": "my point", "ciccio": "another point", "dev": broken}
    run("dev, keep a history")

    seen_by_amanda = [p for p in _Stub.prompts if p[0]["content"].startswith("Sei amanda")]
    floor = seen_by_amanda[0][1]["content"]
    assert "[openvurp]" in floor and "dev non ha risposto" in floor
    assert "non sollecitateli" in floor
    # And nobody is asked to solicit anyone.
    assert "non sollecitarlo" in seen_by_amanda[0][0]["content"]


def test_nobody_speaks_in_a_colleagues_name(room):
    run, rows, _ = room
    _Stub.script = {
        "dev": "dev here",
        "ciccio": "dev: you're right, I'll answer now.\nMy own point: a format.",
        "amanda": "**dev**: I agree.",     # nothing of her own: silence
    }
    run("dev, keep a history")

    said = dict(_agents(rows))
    assert said["ciccio"] == "My own point: a format."
    assert "amanda" not in said


def test_addressing_someone_is_not_speaking_for_them():
    me, other = {"id": "1", "name": "ciccio"}, {"id": "2", "name": "dev"}
    keep = "dev, this one is yours.\n@dev: can you answer?\nDev is the programmer: it's his."
    assert M.spoken_for_others(me, [me, other], keep) == keep
    assert M.spoken_for_others(me, [me, other], "[dev · programmer] done") == ""


def test_a_pass_from_the_one_you_called_is_visible(room):
    run, rows, _ = room
    _Stub.script = {"amanda": "my point", "ciccio": "another", "dev": None}
    result = run("dev, keep a history")
    assert any("dev ha letto e non ha aggiunto niente" in n for n in result.notes)
    assert any("dev ha letto" in n for n in _notes(rows))


def test_a_name_is_only_a_name_as_a_whole_word():
    roster = [{"id": "1", "name": "dev"}, {"id": "2", "name": "notizie del giorno"}]
    assert M.named_in("devo uscire domani", roster) == []
    assert [w["name"] for w in M.named_in("@dev ciao", roster)] == ["dev"]
    assert [w["name"] for w in M.named_in("Notizie del giorno, che c'e'?", roster)] \
        == ["notizie del giorno"]


def test_the_one_you_called_is_told_it_is_his(room):
    run, _rows, _ = room
    _Stub.script = {"dev": "dev here", "amanda": "mine"}
    run("dev, keep a history")
    dev_rules = [p[0]["content"] for p in _Stub.prompts if p[0]["content"].startswith("Sei dev")]
    assert "ti chiama per nome" in dev_rules[0]
    amanda_rules = [p[0]["content"] for p in _Stub.prompts if p[0]["content"].startswith("Sei amanda")]
    assert "ti chiama per nome" not in amanda_rules[0]
