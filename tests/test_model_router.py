import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.model_router import route_subagent


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
