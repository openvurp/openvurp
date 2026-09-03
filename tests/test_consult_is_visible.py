"""When an agent consults a colleague, you must see the colleague working.

What actually happened: dev asked amanda; two avatars walked over to each
other; then nothing moved for minutes. The owner thought it was stuck. Amanda
was running commands the whole time — published into HER conversation, not
into dev's, where the owner was looking. After five minutes without an event
the page's sweeper cleared the scene altogether.
"""

import re

from core.swarm import Swarm
from tests.test_swarm import _Parent


class _WorkingPeer:
    """ciccio asks meteo; meteo runs a command before answering."""

    backend = "openai"
    supports_function_calling = True
    supports_tool_transport = True

    def __init__(self, **_kw):
        self.max_tokens = 0
        self.temperature = 0.0

    def call_with_tools(self, messages, schema):
        from core.llm import LLMResponse, ToolCall

        who = messages[0]["content"].split("'")[1]
        done = any(m.get("role") == "tool_result" for m in messages)
        if who == "meteo":
            if not done:
                return LLMResponse(text="", tool_calls=[ToolCall(
                    id="m1", name="shell", args={"command": "curl wttr.in"})])
            return LLMResponse(text="Domani sereno.")
        if not done:
            return LLMResponse(text="", tool_calls=[ToolCall(
                id="1", name="ask_peer",
                args={"name": "meteo", "question": "domani piove?"})])
        return LLMResponse(text="Ho chiesto a meteo: sereno.")

    def call(self, messages, **_kw):
        return "(senza tool)"


class _Recording(_Parent):
    routes: list = []

    def __init__(self):
        super().__init__()
        self._active_route = None
        self._active_channel = "cli"

    def _execute_tool(self, name, args, source):
        route = self._active_route
        type(self).routes.append((name, getattr(route, "chat_id", ""),
                                  getattr(route, "agent_id", "")))
        return super()._execute_tool(name, args, source)


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr("core.llm.create_llm_client", lambda **kw: _WorkingPeer())
    monkeypatch.setattr("core.chat_store.DEFAULT_AGENTS", (), raising=False)
    _Recording.routes = []
    swarm = Swarm(_Recording(), memory_dir=str(tmp_path))
    swarm.spawn("ciccio", "bollette")
    swarm.spawn("meteo", "previsioni del tempo")
    return swarm


def test_the_colleague_works_in_the_conversation_you_are_looking_at(tmp_path, monkeypatch):
    swarm = _setup(tmp_path, monkeypatch)
    swarm.ask("ciccio", "devo uscire domani?")

    ciccio = swarm.store.direct_chat_for_agent(swarm.resolve("ciccio").id)
    meteo = swarm.resolve("meteo")
    assert _Recording.routes == [("shell", ciccio["id"], meteo.id)], (
        "meteo's command went into the wrong conversation, or nobody knows it was meteo")


def test_what_the_colleague_did_is_saved_with_his_answer(tmp_path, monkeypatch):
    swarm = _setup(tmp_path, monkeypatch)
    swarm.ask("ciccio", "devo uscire domani?")

    chat = swarm.store.direct_chat_for_agent(swarm.resolve("ciccio").id)
    answer = [m for m in swarm.store.list_messages(chat["id"])
              if (m.get("metadata") or {}).get("direction") == "answer"][0]
    steps = answer["metadata"]["steps"]
    assert steps and steps[0]["tool"] == "shell" and "wttr" in steps[0]["args"]


def test_the_page_is_told_which_agent_is_acting(tmp_path, monkeypatch):
    from core import activity

    swarm = _setup(tmp_path, monkeypatch)
    queue, _ = activity.subscribe()
    try:
        swarm.ask("ciccio", "devo uscire domani?")
    finally:
        activity.unsubscribe(queue)
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    chat = swarm.store.direct_chat_for_agent(swarm.resolve("ciccio").id)["id"]
    meteo = swarm.resolve("meteo").id
    here = [e for e in events if e.get("chat_id") == chat]
    kinds = [e["kind"] for e in here]
    assert kinds.index("peer") < kinds.index("peer_done")
    spoken = [e for e in here if e["kind"] == "token" and e.get("agent_id") == meteo]
    assert spoken and "sereno" in spoken[0]["text"]
    # meteo's closing must be marked as his: the page must not take it for ciccio's.
    ends = [e.get("agent_id") for e in here if e["kind"] == "assistant_end"]
    assert meteo in ends


def test_openvurp_itself_stamps_the_acting_agent_on_every_event():
    from types import SimpleNamespace

    from core import activity
    from core.agent import Agent
    from core.swarm import _Route

    fake = SimpleNamespace(_active_route=_Route("c1", "agent_x"),
                           _active_channel="dashboard", _active_actor_id="owner")
    queue, _ = activity.subscribe()
    try:
        Agent._emit(fake, "step", step="shell", text="ls")
    finally:
        activity.unsubscribe(queue)
    evt = queue.get_nowait()
    assert evt["chat_id"] == "c1" and evt["agent_id"] == "agent_x"


def test_the_page_puts_the_colleagues_work_in_his_box():
    from tests.test_dashboard_page import _page, _script

    js = _script(_page())
    # A step or token from the consulted agent goes to the consultation box.
    assert "p.to_id===e.agent_id&&p.answer==null" in js
    assert "guest.steps.push(" in js
    # His closing does not close the asker's turn.
    assert 'else if(e.kind!=="assistant_end")return;' in js
    # The box shows commands, text as it arrives, and the seconds passing.
    assert 'stepsBlock(p.steps,"peer:"+p.key)' in js
    assert "since:Date.now()" in js and ".psecs" in js
    # Saved consultations keep the colleague's steps.
    assert "nm.steps" in js


def test_the_page_shows_the_rooms_notices():
    import dashboard as D
    from tests.test_dashboard_page import _page, _script

    js = _script(_page())
    assert 'e.kind==="room_note"' in js and "function roomNote" in js
    assert 'm.author_type==="system"' in js
    # The settings the notices point to exist, on the page and on the server.
    for key in ("MULTIPLAYER_MAX_AGENTS", "MULTIPLAYER_DAILY_CALL_BUDGET"):
        assert key in D.DashboardHandler.SETTABLE
        assert f'"{key}"' in js
    assert "Discussion contributions per day" in js
    assert re.search(r'error:"the discussion broke off', js)
