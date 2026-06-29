import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.runtime_gateway import RuntimeGateway
from core.session_routing import SessionRoute
from core.subagent import SubagentManager, SubagentStatus


class _DummyLLM:
    backend = "ollama"
    model = "glm-5.1:cloud"


class _DummyTools:
    def names(self):
        return ["read_file", "grep", "subagent_spawn"]


class _DummyParent:
    def __init__(self):
        self._subagent_depth = 0
        self.gateway = RuntimeGateway()
        self.llm = _DummyLLM()
        self.tools = _DummyTools()


def test_subagent_spawn_announces_result_to_requester():
    parent = _DummyParent()
    manager = SubagentManager(parent)
    manager.runtime_mode = "inline"
    manager._run_inline_job = lambda run: {
        "status": "completed",
        "result": "RESULT:\n- done\nRISKS:\n- none\nNEXT_FOR_PARENT:\n- continue",
        "error": "",
        "finished_at": time.time(),
    }

    announced = []
    parent.gateway.register_announcer("telegram", lambda route, text: announced.append((route.chat_id, text)))

    route = SessionRoute.build(
        source="telegram",
        sender="mario",
        actor_id="telegram:1",
        chat_id="123",
        thread_id="77",
    )
    run = manager.spawn(task="test", request_route=route, requested_by="telegram:1")
    result = manager.wait(run.id, timeout=5)

    assert "RESULT:" in result
    assert run.status == SubagentStatus.COMPLETED
    assert announced
    assert announced[0][0] == "123"
    assert run.child_session_key.endswith(f":subagent:{run.id}")


def test_subagent_timeout_marks_run_and_returns_timeout():
    parent = _DummyParent()
    manager = SubagentManager(parent)
    manager.runtime_mode = "inline"

    def _slow(_run):
        time.sleep(1.2)
        return {
            "status": "completed",
            "result": "late",
            "error": "",
            "finished_at": time.time(),
        }

    manager._run_inline_job = _slow
    run = manager.spawn(task="slow", timeout_seconds=1, announce_back=False)
    result = manager.wait(run.id, timeout=3)

    assert "[TIMEOUT]" in result
    assert run.status == SubagentStatus.TIMED_OUT
