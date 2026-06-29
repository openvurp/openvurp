import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.runtime_api import (
    collect_gateway_events,
    collect_runtime_overview,
    collect_session_detail,
    collect_session_list,
    collect_subagent_runs,
)
from core.session import Session
from core.session_routing import SessionRoute
from core.session_store import SessionStore


def test_runtime_api_collectors_read_durable_state():
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = os.path.join(tmp, "memory")
        os.makedirs(os.path.join(memory_dir, "runtime"), exist_ok=True)
        os.makedirs(os.path.join(memory_dir, "subagents", "runs"), exist_ok=True)

        session = Session(session_dir=os.path.join(memory_dir, "sessions"))
        session.add_message("user", "ciao")
        route = SessionRoute.build(source="telegram", sender="mario", actor_id="telegram:1", chat_id="123")
        SessionStore(memory_dir).upsert(route, session, [{"role": "user", "content": "ciao"}], state="idle")

        gateway_log = os.path.join(memory_dir, "runtime", "gateway_events.jsonl")
        with open(gateway_log, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "subagent.completed", "payload": {"id": "abc"}}) + "\n")

        subagent_path = os.path.join(memory_dir, "subagents", "runs", "abc.json")
        with open(subagent_path, "w", encoding="utf-8") as handle:
            json.dump({"id": "abc", "status": "completed"}, handle)

        overview = collect_runtime_overview(tmp)
        sessions = collect_session_list(tmp)
        detail = collect_session_detail(tmp, route.session_key)
        events = collect_gateway_events(tmp, limit=10)
        subagents = collect_subagent_runs(tmp, limit=10)

        assert overview["workspace"] == tmp
        assert overview["sessions"] >= 1
        assert sessions
        assert detail is not None
        assert detail["key"] == route.session_key
        assert events and events[0]["event"] == "subagent.completed"
        assert subagents and subagents[0]["id"] == "abc"
