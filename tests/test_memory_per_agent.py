"""What an agent remembers belongs to that agent — and it gets it back.

There was one store. `remember` wrote into it whoever called, so what amanda
learned about prices sat next to what dev knew about the code. And no agent
ever read any of it: retrieval happened only for the platform, so an agent's
memory was a drawer you threw things into and never opened.

Both halves matter. Separating the stores without giving agents their memories
back would only make the drawer smaller.
"""

import tempfile

import pytest

from core.memory import MemoryManager
from core.scope import agent_home, current_scope, reset_scope, set_scope


@pytest.fixture
def base():
    return tempfile.mkdtemp()


def test_two_agents_do_not_share_a_store(base):
    amanda = MemoryManager(base, scope="ag_amanda")
    ciccio = MemoryManager(base, scope="ag_ciccio")
    assert amanda.base_dir != ciccio.base_dir
    assert amanda.base_dir == agent_home(base, "ag_amanda")


def test_the_platform_stays_where_it_was(base):
    """No scope means the terminal: what is already on disk keeps being read."""
    piatta = MemoryManager(base)
    assert piatta.base_dir == base
    assert "agents" not in piatta.base_dir


def test_what_one_remembers_the_other_does_not_find(base):
    amanda = MemoryManager(base, scope="ag_amanda")
    if not amanda.remember("Il Crucial P3 Plus da 1TB costa 74 euro",
                           category="prezzi"):
        pytest.skip("memoria semantica non disponibile in questo ambiente")
    # La domanda contiene solo UNA delle parole del ricordo: e' il caso reale,
    # e prima tornava vuoto.
    ciccio = MemoryManager(base, scope="ag_ciccio")
    assert "Crucial" in amanda.get_relevant("SSD Crucial prezzo", session_type="main")
    assert "Crucial" not in ciccio.get_relevant("SSD Crucial prezzo", session_type="main")


def test_remember_writes_into_the_store_of_whoever_calls():
    """The tool has no idea who is calling: the scope is how it is told."""
    import inspect

    from core.agent import Agent
    source = inspect.getsource(Agent._remember_handler)
    assert "current_scope()" in source, "il ricordo finisce nell'archivio di tutti"
    assert "memory_for" in source


def test_an_agent_gets_its_own_memories_back():
    """The half that was missing entirely: retrieval, for the agent."""
    import inspect

    from core import swarm
    source = inspect.getsource(swarm.Swarm)
    assert "_memories(" in source, "nessuno rilegge niente"
    assert "get_relevant" in source
    # ...and they must be its own, keyed by id.
    assert "recupera(member.id)" in source


def test_the_scope_is_shared_by_lessons_and_memory():
    """One notion of "who is working", not two that can drift apart."""
    from core import learning
    token = set_scope("ag_x")
    try:
        assert current_scope() == "ag_x"
        assert learning.current_scope() == "ag_x"
    finally:
        reset_scope(token)
    assert current_scope() == ""


# ── e il difetto che la separazione ha fatto emergere ───────────────────────

def test_a_question_finds_the_memory_even_without_all_its_words():
    """FTS5 puts words in AND, and that made recall fail almost always.

    `MATCH 'Crucial SSD prezzo'` demanded all three words. A memory saying
    "Il Crucial P3 Plus da 1TB costa 74 euro" has neither "SSD" nor "prezzo",
    so it came back empty — while `remember` kept answering "saved". Nobody
    noticed because no agent ever read memory back.
    """
    from core.vector_memory import VectorMemory

    with tempfile.TemporaryDirectory() as tmp:
        import os
        v = VectorMemory(os.path.join(tmp, "m.db"))
        v.add("Il Crucial P3 Plus da 1TB costa 74 euro", category="prezzi")
        assert v.search("Crucial SSD prezzo", top_k=5, min_score=0.0), (
            "una parola in comune deve bastare")


def test_an_apostrophe_does_not_silently_empty_the_search():
    """The other half: broken FTS syntax raised, the raise was swallowed, and
    the result was zero memories. In Italian an apostrophe turns up constantly.
    """
    from core.vector_memory import VectorMemory

    with tempfile.TemporaryDirectory() as tmp:
        import os
        v = VectorMemory(os.path.join(tmp, "m.db"))
        v.add("L'SSD Crucial costa 74 euro", category="prezzi")
        for domanda in ("quanto costa l'SSD?", 'dimmi "il prezzo" del Crucial',
                        "SSD: quale?"):
            assert v.search(domanda, top_k=5, min_score=0.0), domanda


def test_the_query_becomes_an_or_of_its_words():
    from core.vector_memory import VectorMemory
    q = VectorMemory._fts_query("quanto costa il Crucial?")
    assert " OR " in q
    assert '"Crucial"' in q
    assert VectorMemory._fts_query("?! ...") == "", "senza parole non si cerca"
