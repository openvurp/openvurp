"""What one agent learns belongs to that agent.

There was a single store. `memory/learning` and `memory/lessons` were written
by whichever agent called `learning_feedback`, so a correction given to the one
who hunts deals landed in the luggage of the one who writes code — and neither
lesson could be checked afterwards, because nobody knew whose it was. The
mirror then measured every agent against everybody's mistakes.

Now each agent has its own, keyed by id: the owner can rename an agent, and
what it learned stays with it.
"""

import tempfile

import pytest

from core.learning import (
    LearningLoop, current_scope, reset_scope, scoped_dir, set_scope,
)
from core.mirror import Mirror


@pytest.fixture
def base():
    return tempfile.mkdtemp()


def test_two_agents_do_not_write_in_the_same_place(base):
    amanda = LearningLoop(base, scope="agent_aaa")
    ciccio = LearningLoop(base, scope="agent_bbb")
    assert amanda.learning_dir != ciccio.learning_dir
    assert amanda.lessons_dir != ciccio.lessons_dir


def test_the_platform_keeps_the_place_it_had(base):
    """No scope means the terminal: what is already on disk keeps being read."""
    piatta = LearningLoop(base)
    assert piatta.learning_dir == scoped_dir(base, "learning")
    assert piatta.learning_dir.endswith("learning")
    assert "agents" not in piatta.learning_dir


def test_a_lesson_of_one_is_not_read_by_the_other(base):
    amanda = LearningLoop(base, scope="agent_aaa")
    amanda.record_user_signal("hai sbagliato: quel prezzo è con la spedizione",
                              actor="owner", source="dashboard")
    ciccio = LearningLoop(base, scope="agent_bbb")

    def righe(loop):
        import os
        path = loop.events_path
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [l for l in f if l.strip()]

    assert righe(amanda), "the signal was not recorded at all"
    assert not righe(ciccio), "it ended up in the other agent's store"
    assert not righe(LearningLoop(base)), "it ended up in the platform's store"


def test_the_mirror_replays_only_its_own_corrections(base):
    amanda = LearningLoop(base, scope="agent_aaa")
    amanda.record_user_signal("hai sbagliato, non fare così", actor="owner",
                              source="dashboard")

    mia = Mirror(base, scope="agent_aaa")
    altrui = Mirror(base, scope="agent_bbb")
    assert mia.dir != altrui.dir
    presi = mia.harvest()
    assert altrui.harvest() == 0, "it harvested somebody else's corrections"
    assert presi >= 0


def test_the_scope_is_a_context_not_a_shared_attribute():
    """Agents run in parallel (broadcast uses a thread pool). An attribute on
    a shared object would be overwritten by whoever starts a moment later."""
    token = set_scope("agent_aaa")
    try:
        assert current_scope() == "agent_aaa"
    finally:
        reset_scope(token)
    assert current_scope() == ""


def test_each_thread_keeps_its_own_scope():
    import threading
    visti = {}

    def lavora(nome):
        token = set_scope(nome)
        import time
        time.sleep(0.02)              # lascia partire l'altro nel frattempo
        visti[nome] = current_scope()
        reset_scope(token)

    fili = [threading.Thread(target=lavora, args=(n,)) for n in ("aaa", "bbb", "ccc")]
    for f in fili:
        f.start()
    for f in fili:
        f.join()
    assert visti == {"aaa": "aaa", "bbb": "bbb", "ccc": "ccc"}


def test_the_tool_writes_where_the_caller_is(base, monkeypatch):
    """The tools are plain functions with no idea who called them: the scope is
    how the swarm tells them, and it is what keeps the stores apart."""
    import tools.learning as T
    monkeypatch.setattr(T, "MEMORY_DIR", base)
    token = set_scope("agent_aaa")
    try:
        assert T._learning().learning_dir == scoped_dir(base, "learning", "agent_aaa")
    finally:
        reset_scope(token)
    assert T._learning().learning_dir == scoped_dir(base, "learning")


def test_the_swarm_declares_who_is_learning():
    """The wiring itself: without this line every agent writes as the platform."""
    import inspect
    from core import swarm
    source = inspect.getsource(swarm)
    assert "set_scope(member.id)" in source
    assert "reset_scope(token)" in source, "the scope is never given back"
