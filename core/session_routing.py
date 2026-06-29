"""
openvurp Core — Session Routing

Session key stabili per CLI, canali e subagent.
"""

from __future__ import annotations

from dataclasses import dataclass


def _clean(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    safe = []
    for ch in text:
        if ch.isalnum() or ch in ("-", "_", ".", ":"):
            safe.append(ch)
        else:
            safe.append("_")
    return "".join(safe)


def build_session_key(
    source: str = "cli",
    sender: str = "user",
    chat_id: str = "",
    thread_id: str = "",
    explicit: str = "",
) -> str:
    if explicit:
        return _clean(explicit)
    source_key = _clean(source or "cli") or "cli"
    if source_key == "cli":
        return "cli:main"
    parts = [source_key]
    if chat_id:
        parts.append(f"chat:{_clean(chat_id)}")
    elif sender:
        parts.append(f"sender:{_clean(sender)}")
    if thread_id:
        parts.append(f"thread:{_clean(thread_id)}")
    return ":".join(parts)


def build_subagent_session_key(parent_session_key: str, subagent_id: str) -> str:
    parent = _clean(parent_session_key) or "cli:main"
    child = _clean(subagent_id) or "child"
    return f"{parent}:subagent:{child}"


@dataclass(frozen=True)
class SessionRoute:
    source: str = "cli"
    sender: str = "user"
    actor_id: str = "cli_owner"
    chat_id: str = ""
    thread_id: str = ""
    session_key: str = "cli:main"
    parent_session_key: str = ""

    @classmethod
    def build(
        cls,
        source: str = "cli",
        sender: str = "user",
        actor_id: str = "cli_owner",
        chat_id: str = "",
        thread_id: str = "",
        session_key: str = "",
        parent_session_key: str = "",
    ) -> "SessionRoute":
        return cls(
            source=str(source or "cli"),
            sender=str(sender or "user"),
            actor_id=str(actor_id or "cli_owner"),
            chat_id=str(chat_id or ""),
            thread_id=str(thread_id or ""),
            session_key=build_session_key(
                source=source,
                sender=sender,
                chat_id=chat_id,
                thread_id=thread_id,
                explicit=session_key,
            ),
            parent_session_key=str(parent_session_key or ""),
        )

