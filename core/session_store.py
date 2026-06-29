"""
openvurp Core — Session Store

Snapshot durevoli delle sessioni route-bound per gateway esterni e recovery.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os

from core.session_routing import SessionRoute


def _safe_name(value: str) -> str:
    text = str(value or "").strip() or "session"
    chars = []
    for ch in text:
        if ch.isalnum() or ch in ("-", "_", "."):
            chars.append(ch)
        else:
            chars.append("_")
    return "".join(chars)


def _preview_messages(messages: list[dict], limit: int = 10, max_chars: int = 240) -> list[dict]:
    result = []
    for msg in messages:
        role = msg.get("role", "")
        if role in {"system", "tool_result"}:
            continue
        content = str(msg.get("content", "") or "").strip()
        if not content:
            continue
        compact = " ".join(content.split())
        if len(compact) > max_chars:
            compact = compact[: max_chars - 1] + "…"
        result.append({"role": role, "preview": compact})
    return result[-limit:]


@dataclass
class SessionSnapshot:
    key: str
    source: str
    sender: str
    actor_id: str
    chat_id: str
    thread_id: str
    parent_session_key: str
    started_at: str
    updated_at: str
    state: str
    turns: int
    llm_calls: int
    tool_calls: int
    errors: int
    tokens_total: int
    estimated_cost: str
    duration: str
    recent_messages: list[dict]


class SessionStore:
    def __init__(self, memory_dir: str):
        self.root_dir = os.path.join(memory_dir, "session_store")
        os.makedirs(self.root_dir, exist_ok=True)

    def path_for(self, key: str) -> str:
        return os.path.join(self.root_dir, f"{_safe_name(key)}.json")

    def upsert(self, route: SessionRoute, runtime_session, messages: list[dict], state: str = "idle") -> str:
        summary = runtime_session.summary()
        snapshot = SessionSnapshot(
            key=route.session_key,
            source=route.source,
            sender=route.sender,
            actor_id=route.actor_id,
            chat_id=route.chat_id,
            thread_id=route.thread_id,
            parent_session_key=route.parent_session_key,
            started_at=str(runtime_session.started_at.isoformat()),
            updated_at=datetime.now().isoformat(),
            state=str(state or "idle"),
            turns=int(summary.get("turns", 0) or 0),
            llm_calls=int(summary.get("llm_calls", 0) or 0),
            tool_calls=int(summary.get("tool_calls", 0) or 0),
            errors=int(summary.get("errors", 0) or 0),
            tokens_total=int(summary.get("tokens_total", 0) or 0),
            estimated_cost=str(summary.get("estimated_cost", "$0")),
            duration=str(summary.get("duration", "0:00:00")),
            recent_messages=_preview_messages(messages),
        )
        path = self.path_for(route.session_key)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(asdict(snapshot), handle, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        return path

    def list_snapshots(self) -> list[dict]:
        snapshots = []
        for name in sorted(os.listdir(self.root_dir), reverse=True):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.root_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    snapshots.append(json.load(handle))
            except Exception:
                continue
        snapshots.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return snapshots

    def get_snapshot(self, key: str) -> dict | None:
        path = self.path_for(key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return None
