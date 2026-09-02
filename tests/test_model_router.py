import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from unittest.mock import patch

from core.model_router import route_chat_prompt, route_subagent


def test_economic_chat_router_uses_luna_without_an_llm_classifier():
    with patch("core.cli_backends.codex_login_status", return_value=(True, "ChatGPT")):
        choice = route_chat_prompt("ciao, come stai?")
    assert (choice.backend, choice.model, choice.tier) == (
        "codex", "gpt-5.6-luna", "fast",
    )


def test_economic_chat_router_reserves_terra_for_complex_work():
    with patch("core.cli_backends.codex_login_status", return_value=(True, "ChatGPT")):
        choice = route_chat_prompt(
            "Analizza il progetto e proponi una migrazione completa dell'architettura"
        )
    assert (choice.backend, choice.model, choice.tier) == (
        "codex", "gpt-5.6-terra", "deep",
    )


def test_economic_chat_router_falls_back_to_claude_subscription():
    with patch("core.cli_backends.codex_login_status", return_value=(False, "no")), \
            patch("core.cli_backends.claude_login_status", return_value=(True, "Claude.ai")):
        choice = route_chat_prompt("scrivi una risposta")
    assert choice.backend == "claude_cli"
    assert choice.model == "sonnet"


def test_explicit_route_is_preserved():
    choice = route_subagent(
        parent_backend="ollama",
        parent_model="glm-5.1:cloud",
        task="analizza",
        requested_backend="openai",
        requested_model="gpt-4o-mini",
        requested_mode="text",
    )
    assert choice.backend == "openai"
    assert choice.model == "gpt-4o-mini"
    assert choice.strategy == "explicit"


def test_auto_executor_mode_is_selected_for_tool_like_tasks():
    choice = route_subagent(
        parent_backend="ollama",
        parent_model="glm-5.1:cloud",
        task="usa browser e process_read per controllare lo stato del servizio",
        requested_mode="auto",
    )
    assert choice.mode in {"safe_executor", "inherit_executor"}
