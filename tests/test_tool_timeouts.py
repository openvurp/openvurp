"""Un tool lento o ostinato non deve poter congelare il turno.

Tre guasti reali, osservati insieme: `find_files` che non tornava piu' su un
mount lento, il timeout provider che scadeva mentre aspettava un nostro tool, e
la stessa chiamata ripetuta all'infinito dopo un rifiuto stabile.
"""

import time

import pytest

from core.executor import Executor
from core.tools import ErrorType, Tool, ToolRegistry, ToolResult
from tools.search import PRUNE_DIRS, _walk_bounded, glob_handler, grep_handler


# ── Ricerca limitata ────────────────────────────────────────────────────

@pytest.fixture
def tree(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "trovami.py").write_text("def bersaglio(): pass\n")
    venv = tmp_path / "envi"                       # virtualenv dal nome insolito
    (venv / "lib").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr\n")
    for i in range(50):
        (venv / "lib" / f"mod{i}.py").write_text("def bersaglio(): pass\n")
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "pacchetto.py").write_text("def bersaglio(): pass\n")
    return tmp_path


def test_virtualenv_is_pruned_by_its_marker_not_its_name(tree):
    """`envi` non e' in nessuna lista di nomi: si riconosce da pyvenv.cfg."""
    assert "envi" not in PRUNE_DIRS
    walked = [root for root, _files, _stop in _walk_bounded(str(tree), time.monotonic() + 30, 10000)]
    assert not any("envi" in root for root in walked)
    assert not any("node_modules" in root for root in walked)
    assert any(root.endswith("core") for root in walked)


def test_find_files_skips_virtualenv_content(tree):
    result = glob_handler("*.py", str(tree))
    assert "trovami.py" in result.output
    assert "mod0.py" not in result.output
    assert "pacchetto.py" not in result.output


def test_search_stops_on_time_budget_and_says_so(tree, monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "SEARCH_TIME_BUDGET_SECONDS", 1, raising=False)
    # Deadline gia' scaduta al primo controllo.
    monkeypatch.setattr("tools.search._search_limits", lambda: (-1.0, 10000))
    result = grep_handler("bersaglio", str(tree))
    assert result.success
    assert "RICERCA TRONCATA" in result.output
    assert "PARZIALI" in result.output


def test_walk_is_breadth_first_so_shallow_code_wins_the_budget(tmp_path):
    """Il match in una dir di primo livello non deve perdersi in un ramo profondo."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "x.py").write_text("ok\n")
    deep = tmp_path / "dati" / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "y.py").write_text("ok\n")

    order = [root for root, _f, _s in _walk_bounded(str(tmp_path), time.monotonic() + 30, 10000)]
    assert order.index(str(tmp_path / "tools")) < order.index(str(deep))


# ── Timeout duro sull'handler ───────────────────────────────────────────

def _executor(tool: Tool) -> Executor:
    registry = ToolRegistry()
    registry.register(tool)
    return Executor(registry)


def test_hanging_tool_is_abandoned_instead_of_freezing_the_turn():
    slow = Tool(
        name="lento",
        description="non torna",
        parameters={"type": "object", "properties": {}},
        timeout=5,
        handler=lambda: time.sleep(30) or ToolResult.ok("mai"),
    )
    started = time.monotonic()
    result = _executor(slow).execute("lento", {})
    elapsed = time.monotonic() - started

    assert not result.success
    assert result.error_type is ErrorType.TIMEOUT
    assert "NON ripetere" in result.error
    assert elapsed < 15, "l'executor ha aspettato l'handler abbandonato"


def test_abandoned_tool_thread_does_not_hold_the_process():
    """I thread di ThreadPoolExecutor non sono daemon: l'interprete li aspetta
    all'uscita, il che sposterebbe il blocco dal turno alla chiusura."""
    import threading

    seen = {}
    def _slow():
        seen["daemon"] = threading.current_thread().daemon
        time.sleep(30)
        return ToolResult.ok("mai")

    slow = Tool(name="lento2", description="x",
                parameters={"type": "object", "properties": {}},
                timeout=5, handler=_slow)
    _executor(slow).execute("lento2", {})
    assert seen.get("daemon") is True


def test_handler_exception_still_propagates_to_retry_logic():
    def _boom():
        raise ValueError("rotto")

    bad = Tool(name="rotto", description="x",
               parameters={"type": "object", "properties": {}}, handler=_boom)
    result = _executor(bad).execute("rotto", {})
    assert not result.success
    assert "rotto" in (result.error or "")


def test_fast_tool_is_unaffected():
    quick = Tool(
        name="veloce", description="ok",
        parameters={"type": "object", "properties": {}},
        handler=lambda: ToolResult.ok("fatto"),
    )
    result = _executor(quick).execute("veloce", {})
    assert result.success and result.output == "fatto"


# ── Guardiano anti-loop ─────────────────────────────────────────────────

def _agent_stub():
    """Agent minimo: solo cio' che serve al guardiano."""
    from core.agent import Agent

    agent = Agent.__new__(Agent)
    agent._failed_calls = {}
    agent.ui = type("UI", (), {"status": lambda self, *a, **k: None})()
    agent.observer = type("Obs", (), {"log_event": lambda self, *a, **k: None})()
    return agent


def test_identical_failure_is_blocked_after_the_limit(monkeypatch):
    """Il caso reale: `remember` ripetuto contro un budget esaurito."""
    import config as cfg
    monkeypatch.setattr(cfg, "TOOL_MAX_IDENTICAL_FAILURES", 3, raising=False)

    agent = _agent_stub()
    args = {"section": "voice", "text": "diretto"}

    for _ in range(3):
        assert agent._loop_guard("remember", args) == ""
        agent._note_call_outcome("remember", args, success=False)

    blocked = agent._loop_guard("remember", args)
    assert "BLOCCATO DAL RUNTIME" in blocked
    assert "Smetti di chiamarlo" in blocked


def test_different_arguments_are_not_blocked():
    agent = _agent_stub()
    for _ in range(5):
        agent._note_call_outcome("remember", {"text": "a"}, success=False)
    assert agent._loop_guard("remember", {"text": "b"}) == ""


def test_a_success_clears_the_counter():
    agent = _agent_stub()
    args = {"pattern": "*.py"}
    for _ in range(5):
        agent._note_call_outcome("find_files", args, success=False)
    agent._note_call_outcome("find_files", args, success=True)
    assert agent._loop_guard("find_files", args) == ""


# ── Budget di iterazioni ────────────────────────────────────────────────

def test_cli_backend_always_gets_room_for_a_second_pass():
    """Regressione: con Codex il budget era 1 giro.

    Un solo giro basta finche' i dynamic tools funzionano e Codex chiude tutto
    dentro un turno. Appena serve un secondo passaggio — la revisione del
    kernel, un tool arrivato come testo, un risultato da rileggere — il turno
    finiva con "limite di iterazioni" senza aver concluso nulla.
    """
    from core.agent import Agent

    for backend in ("codex", "claude_cli"):
        assert Agent.iteration_budget("cli", backend) >= 2
        assert Agent.iteration_budget("cli", backend, "chat") >= 2
        assert Agent.iteration_budget("heartbeat", backend) >= 2


def test_budget_never_drops_below_two_even_if_misconfigured(monkeypatch):
    import config as cfg
    from core.agent import Agent

    for name in ("MAX_ITERATIONS", "CHAT_MAX_ITERATIONS",
                 "CLI_AGENT_MAX_ITERATIONS", "HEARTBEAT_MAX_ITERATIONS"):
        monkeypatch.setattr(cfg, name, 1, raising=False)
    assert Agent.iteration_budget("heartbeat", "codex", "chat") == 2


def test_cli_budget_is_lower_than_the_native_one(monkeypatch):
    """Ogni giro con un CLI in abbonamento e' un turno intero: resta piu' caro."""
    from core.agent import Agent

    assert Agent.iteration_budget("cli", "codex") < Agent.iteration_budget("cli", "ollama")
