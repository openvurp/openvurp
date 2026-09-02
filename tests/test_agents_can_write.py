"""An agent's own limit is not a fact about the user's machine.

Twice in one day, dev told the owner the workspace was read-only and it could
not produce the file. It was false both times. The disk was writable, the
folder was writable, and dev had `write_file` in its own toolset.

What was read-only was the sandbox of its engine — Codex runs with
CODEX_SANDBOX=read-only, on purpose, so that every change goes through
openvurp and gets approved and logged. dev tried to write with the engine's own
shell, was refused, and reported that refusal as a property of the machine.

The user waited through three rounds for a file that could have been made in
the first.
"""

import tempfile

import pytest

from core.chat_store import ChatStore


@pytest.fixture
def swarm():
    from core.agent import Agent
    from core.swarm import Swarm

    class _UI:
        def __getattr__(self, name):
            return lambda *a, **k: None

    store = ChatStore(tempfile.mkdtemp())
    store.create_agent("dev", "scrive codice", "", "", "")
    return Swarm(Agent(_UI()), store=store)


def test_an_agent_can_write_files(swarm):
    """The premise of the whole story: it always could."""
    names = swarm.tool_names()
    assert "write_file" in names
    assert "edit_file" in names


def test_an_agent_can_read_the_documents_it_is_asked_about(swarm):
    names = swarm.tool_names()
    assert "read_file" in names
    assert "pdf_read" in names


def test_the_tool_itself_says_the_engine_sandbox_is_not_the_machine():
    """The message belongs where the model decides, not in a preamble.

    It started life in the system prompt and broke the length guard there —
    rightly: a long rulebook makes the model orbit the instructions instead of
    the work. It reads better anyway on `write_file`, which is exactly what it
    is looking at when it wants to write.
    """
    from tools.file_ops import WRITE_FILE_TOOL
    descrizione = WRITE_FILE_TOOL.description
    assert "sola lettura" in descrizione
    assert "limite suo e non della macchina" in descrizione


def test_the_prompt_keeps_the_general_lesson(swarm):
    """Short enough to survive the budget, general enough to outlive the bug."""
    prompt = swarm._system_prompt(swarm.resolve("dev"), [])
    assert "Never report your own limit" in prompt
    assert len(prompt) < 900, f"prompt di {len(prompt)}: sta ringonfiando"


def test_the_main_agent_is_told_the_same_thing():
    from core.environment import EnvironmentInspector
    from core.method import build_operating_method

    snapshot = EnvironmentInspector(tempfile.mkdtemp()).get_snapshot()
    method = build_operating_method(snapshot, ["edit_file", "write_file", "read_file"])
    assert "sola lettura" in method
    assert "Non riportare mai un tuo limite" in method


def test_writing_inside_the_workspace_really_works():
    """Not an opinion about permissions: the actual write, where it counts.

    `memory/uploads` is where the documents people send arrive, and it is
    inside the workspace. This is the write dev said was impossible.
    """
    import os

    from config import OPENVURP_DIR
    from tools.file_ops import write_file_handler

    cartella = os.path.join(OPENVURP_DIR, "memory", "uploads")
    os.makedirs(cartella, exist_ok=True)
    percorso = os.path.join(cartella, ".prova_scrittura_test.txt")
    try:
        result = write_file_handler(percorso, "contenuto")
        assert result.success, result.error
        assert os.path.exists(percorso)
    finally:
        if os.path.exists(percorso):
            os.remove(percorso)


def test_outside_the_workspace_it_is_refused_and_that_is_correct():
    """The other half of the story, and dev was right about this one.

    Asked for a way around, dev tried /tmp and reported it was refused. True:
    the sandbox confines file tools to the workspace, on purpose. What was
    false was the first claim — that the workspace itself was read-only.
    """
    import os

    from tools.file_ops import write_file_handler

    fuori = os.path.join(tempfile.mkdtemp(), "prova.txt")
    result = write_file_handler(fuori, "contenuto")
    assert not result.success, "la sandbox non sta confinando niente"
    assert "sandbox" in (result.error or "").lower()


# ── una capacità che non si sa di avere non è una capacità ─────────────────

def test_an_agent_is_told_which_skills_exist(swarm):
    """`load_skill` asks for an exact name. Nobody was giving the names.

    The tool's own description says to look at "the SKILLS index in the system
    prompt". The main agent has that index. An agent in the roster never did:
    it could load a skill only by guessing its name. Thirteen written
    procedures, unusable.
    """
    indice = swarm._skills_index()
    assert indice, "nessun indice: le skill restano invisibili"
    assert "load_skill" in indice, "non dice nemmeno come si caricano"
    # Il nome esatto e' quello che il tool pretende.
    assert "- coding:" in indice or "- coding" in indice


def test_the_index_is_an_index_not_the_procedures(swarm):
    """The whole point of loading on demand: the prompt must not carry them all."""
    indice = swarm._skills_index()
    assert len(indice) < 4000, f"{len(indice)} battute: sono le procedure, non l'indice"
    for riga in indice.splitlines()[1:]:
        assert len(riga) < 140, f"riga troppo lunga, e' contenuto: {riga[:60]}"


def test_the_index_travels_beside_the_prompt_not_inside_it(swarm):
    """It is data, not rulebook: the 900-char guard protects the rules.

    Putting it inside would either blow that budget or force the rules out,
    and the guard exists for a good reason — a long rulebook makes the model
    orbit the instructions instead of the work.
    """
    import inspect
    source = inspect.getsource(swarm.__class__._speak)
    assert "_skills_index()" in source
    assert 'role": "system"' in source
    assert len(swarm._system_prompt(swarm.resolve("dev"), [])) < 900
