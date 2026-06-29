import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.session_routing import build_session_key, build_subagent_session_key, SessionRoute


def test_build_session_key_includes_chat_and_thread():
    key = build_session_key(
        source="telegram",
        sender="mario",
        chat_id="12345",
        thread_id="77",
    )
    assert key == "telegram:chat:12345:thread:77"


def test_build_subagent_session_key_binds_to_parent():
    key = build_subagent_session_key("telegram:chat:12345:thread:77", "abcd1234")
    assert key.endswith(":subagent:abcd1234")
    assert key.startswith("telegram:chat:12345:thread:77")


def test_session_route_build_uses_explicit_session_key():
    route = SessionRoute.build(
        source="subagent",
        sender="child",
        actor_id="cli_owner",
        session_key="agent:otto:subagent:test",
        parent_session_key="cli:main",
    )
    assert route.session_key == "agent:otto:subagent:test"
    assert route.parent_session_key == "cli:main"

