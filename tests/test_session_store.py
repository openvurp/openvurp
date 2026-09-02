import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.session import Session
from core.session_routing import SessionRoute
from core.session_store import SessionStore


def test_session_store_upsert_and_get():
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(tmp)
        route = SessionRoute.build(source="telegram", sender="mario", actor_id="telegram:1", chat_id="123", thread_id="45")
        session = Session(session_dir=os.path.join(tmp, "sessions"))
        session.add_message("user", "ciao")
        session.add_message("assistant", "ciao mario")
        session.tokens.add_call(100, 50)

        path = store.upsert(route, session, [
            {"role": "system", "content": "x"},
            {"role": "user", "content": "ciao"},
            {"role": "assistant", "content": "ciao mario"},
        ], state="idle")

        assert os.path.exists(path)
        detail = store.get_snapshot(route.session_key)
        assert detail is not None
        assert detail["key"] == route.session_key
        assert detail["chat_id"] == "123"
        assert detail["thread_id"] == "45"
        assert detail["tokens_total"] == 150
        assert len(detail["recent_messages"]) == 2


def test_session_store_list_sorted():
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(tmp)
        session = Session(session_dir=os.path.join(tmp, "sessions"))
        route_a = SessionRoute.build(source="cli", sender="user")
        route_b = SessionRoute.build(source="telegram", sender="mario", chat_id="777")
        store.upsert(route_a, session, [{"role": "user", "content": "a"}], state="idle")
        store.upsert(route_b, session, [{"role": "user", "content": "b"}], state="running")
        snapshots = store.list_snapshots()
        keys = [item["key"] for item in snapshots]
        assert route_a.session_key in keys
        assert route_b.session_key in keys


def test_session_history_persists_compact_conversation_only():
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(tmp)
        key = "dashboard:chat:abc"
        store.save_messages(key, [
            {"role": "system", "content": "large prompt"},
            {"role": "user", "content": "ciao"},
            {"role": "assistant", "content": "uso tool", "tool_calls": [{"id": "1"}]},
            {"role": "tool_result", "content": "x" * 10000},
            {"role": "assistant", "content": "risposta finale"},
        ])
        assert store.load_messages(key) == [
            {"role": "user", "content": "ciao"},
            {"role": "assistant", "content": "risposta finale"},
        ]
