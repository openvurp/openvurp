import json

from core.agent import Agent
from core.tool_router import ToolRouter
from core.tools import ToolRegistry


def _registry():
    agent = Agent.__new__(Agent)
    agent.tools = ToolRegistry()
    agent._register_tools()
    return agent.tools


def test_core_schema_is_much_smaller_than_full_schema():
    tools = _registry()
    router = ToolRouter()
    full = json.dumps(tools.to_openai_schema(), ensure_ascii=False)
    core = json.dumps(tools.to_openai_schema(router.selection.names), ensure_ascii=False)
    assert len(core) < len(full) * 0.25
    assert "load_toolset" in core


def test_router_selects_relevant_packs_and_can_expand():
    tools = _registry()
    router = ToolRouter()
    selection = router.select("Modifica il progetto e poi cerca la documentazione web")
    assert {"core", "files", "web"}.issubset(selection.packs)
    names, unknown = router.activate(["communication"], set(tools.names()))
    assert not unknown
    assert "notify" in names


def test_registry_filters_anthropic_and_openai_schemas():
    tools = _registry()
    names = {"read_file", "grep"}
    assert {item["function"]["name"] for item in tools.to_openai_schema(names)} == names
    assert {item["name"] for item in tools.to_anthropic_schema(names)} == names


def test_codex_receives_selected_openvurp_tool_schemas():
    tools = _registry()
    agent = Agent.__new__(Agent)
    agent.tools = tools
    agent._active_tool_names = {"web_search", "web_fetch"}
    agent._active_llm = type("Codex", (), {"backend": "codex"})()
    schema = agent._get_tools_schema()
    assert {item["function"]["name"] for item in schema} == {
        "web_search", "web_fetch",
    }


def test_codex_tool_result_is_compacted_without_skipping_openvurp_executor(monkeypatch):
    agent = Agent.__new__(Agent)
    calls = []
    monkeypatch.setattr(
        agent, "_execute_tool",
        lambda name, args, source="cli": calls.append((name, args, source)) or "A" * 9000,
    )
    monkeypatch.setattr("config.CODEX_TOOL_RESULT_MAX_CHARS", 2000)

    result = agent._execute_codex_tool("web_search", {"query": "test"}, "web")

    assert calls == [("web_search", {"query": "test"}, "web")]
    assert len(result) <= 2000
    assert "CONTENUTO ESTERNO NON FIDATO" in result
    assert "risultato tool compattato" in result


def test_default_mode_exposes_every_tool(monkeypatch):
    """Regressione: la selezione per keyword rendeva l'agente incapace.

    Con `TOOL_ROUTER_MODE=off` (default) un messaggio qualsiasi deve lasciare
    esposti tutti i tool registrati: senza `web_search` nello schema, "che
    tempo fa" diventa una risposta inventata invece di una ricerca.
    """
    import config as cfg
    monkeypatch.setattr(cfg, "TOOL_ROUTER_MODE", "off", raising=False)
    tools = _registry()
    available = set(tools.names())
    selection = ToolRouter().select("che tempo fa?", available=available)
    assert selection.everything
    assert selection.names == available
    assert {"web_search", "notify", "remember"}.issubset(selection.names)


def test_wide_mode_keeps_everyday_packs_without_keywords(monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "TOOL_ROUTER_MODE", "wide", raising=False)
    selection = ToolRouter().select("che tempo fa?")
    assert not selection.everything
    assert {"web_search", "notify", "remember"}.issubset(selection.names)


def test_strict_mode_still_narrows_to_core(monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "TOOL_ROUTER_MODE", "strict", raising=False)
    selection = ToolRouter().select("che tempo fa?")
    assert selection.packs == {"core"}
    assert "web_search" not in selection.names


def test_every_registered_tool_belongs_to_a_pack():
    """Un tool fuori da ogni pack è irraggiungibile in modalità strict."""
    from core.tool_router import PACKS

    packed = set().union(*PACKS.values())
    missing = set(_registry().names()) - packed
    assert not missing, f"tool senza pack: {sorted(missing)}"


def test_cli_backends_get_a_textual_tool_index_as_fallback():
    """Se i dynamic tools di Codex non partono, deve restare un modo di agire."""
    tools = _registry()
    assert tools.prompt_section(native_tools=True) == ""
    index = tools.compact_index(names={"web_search", "read_file"})
    assert "```TOOL:" in index
    assert "`web_search`" in index and "`read_file`" in index
