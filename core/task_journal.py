"""
openvurp Core - Task Journal, Reflection, Open Loops

Durable local records that make the agent behave less like a stateless chat
model and more like a personal operator with continuity.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from core.security.audit import redact


@dataclass
class OpenLoop:
    id: str
    title: str
    description: str = ""
    status: str = "open"
    created_at: str = ""
    updated_at: str = ""
    source: str = "cli"
    actor: str = "agent"
    due: str = ""
    tags: list[str] = field(default_factory=list)
    resolution: str = ""


@dataclass
class Reflection:
    turn_id: str
    timestamp: str
    status: str
    user_intent: str
    result: str
    tools_used: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    memory_touched: bool = False
    open_loop_hints: list[str] = field(default_factory=list)


@dataclass
class JournalReport:
    events_path: str
    reflections_path: str
    open_loops_path: str
    open_loops: list[OpenLoop]
    recent_reflections: list[dict]

    def render(self) -> str:
        lines = [
            f"Task journal: {self.events_path}",
            f"Reflections: {self.reflections_path}",
            f"Open loops: {len([item for item in self.open_loops if item.status == 'open'])} open",
        ]
        for item in self.open_loops[:8]:
            if item.status != "open":
                continue
            due = f" due={item.due}" if item.due else ""
            lines.append(f"- [{item.id}] {item.title}{due}")
        return "\n".join(lines)


class TaskJournal:
    EVENTS_DIR = "task_journal"
    REFLECTIONS_DIR = "reflections"
    OPEN_LOOPS_FILE = "open_loops.json"

    def __init__(self, memory_dir: str):
        self.memory_dir = memory_dir
        self.events_dir = os.path.join(memory_dir, self.EVENTS_DIR)
        self.reflections_dir = os.path.join(memory_dir, self.REFLECTIONS_DIR)
        self.open_loops_path = os.path.join(memory_dir, self.OPEN_LOOPS_FILE)
        os.makedirs(self.events_dir, exist_ok=True)
        os.makedirs(self.reflections_dir, exist_ok=True)
        os.makedirs(self.memory_dir, exist_ok=True)

    def start_turn(self, user_input: str, source: str = "cli",
                   actor: str = "agent", session_key: str = "") -> str:
        turn_id = self._new_id("turn", user_input)
        self._append_event({
            "type": "turn_start",
            "turn_id": turn_id,
            "timestamp": self._now(),
            "source": source or "cli",
            "actor": actor or "agent",
            "session_key": session_key,
            "user_input": self._clean(user_input, 1000),
        })
        return turn_id

    def finish_turn(self, turn_id: str, user_input: str, assistant_text: str,
                    tool_history: list[dict] | None = None,
                    status: str = "completed", source: str = "cli",
                    actor: str = "agent", session_key: str = "") -> Reflection:
        tool_history = tool_history or []
        reflection = self._build_reflection(
            turn_id=turn_id,
            user_input=user_input,
            assistant_text=assistant_text,
            tool_history=tool_history,
            status=status,
        )
        event = {
            "type": "turn_finish",
            "turn_id": turn_id,
            "timestamp": reflection.timestamp,
            "source": source or "cli",
            "actor": actor or "agent",
            "session_key": session_key,
            "status": status,
            "reflection": asdict(reflection),
        }
        self._append_event(event)
        self._append_reflection(asdict(reflection))
        return reflection

    def record_note(self, note: str, kind: str = "note", source: str = "cli",
                    actor: str = "agent", tags: list[str] | None = None) -> str:
        note_id = self._new_id(kind, note)
        self._append_event({
            "type": "note",
            "id": note_id,
            "kind": kind or "note",
            "timestamp": self._now(),
            "source": source or "cli",
            "actor": actor or "agent",
            "note": self._clean(note, 1200),
            "tags": tags or [],
        })
        return note_id

    def add_open_loop(self, title: str, description: str = "",
                      source: str = "cli", actor: str = "agent",
                      due: str = "", tags: list[str] | None = None) -> OpenLoop:
        now = self._now()
        loop = OpenLoop(
            id=self._new_id("loop", title),
            title=self._clean(title, 180),
            description=self._clean(description, 1000),
            status="open",
            created_at=now,
            updated_at=now,
            source=source or "cli",
            actor=actor or "agent",
            due=self._clean(due, 80),
            tags=(tags or [])[:12],
        )
        loops = self.list_open_loops(include_closed=True)
        loops.append(loop)
        self._save_open_loops(loops)
        self._append_event({
            "type": "open_loop_add",
            "timestamp": now,
            "open_loop": asdict(loop),
        })
        return loop

    def close_open_loop(self, loop_id: str, resolution: str = "",
                        actor: str = "agent", source: str = "cli") -> OpenLoop | None:
        loops = self.list_open_loops(include_closed=True)
        target = None
        now = self._now()
        for loop in loops:
            if loop.id == loop_id:
                loop.status = "closed"
                loop.updated_at = now
                loop.resolution = self._clean(resolution, 1000)
                loop.actor = actor or loop.actor
                loop.source = source or loop.source
                target = loop
                break
        if not target:
            return None
        self._save_open_loops(loops)
        self._append_event({
            "type": "open_loop_close",
            "timestamp": now,
            "open_loop": asdict(target),
        })
        return target

    def list_open_loops(self, include_closed: bool = False) -> list[OpenLoop]:
        if not os.path.exists(self.open_loops_path):
            return []
        try:
            with open(self.open_loops_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception:
            return []
        loops = []
        for item in raw if isinstance(raw, list) else []:
            try:
                loops.append(OpenLoop(**item))
            except TypeError:
                continue
        if include_closed:
            return loops
        return [loop for loop in loops if loop.status == "open"]

    def review(self, max_reflections: int = 10) -> JournalReport:
        return JournalReport(
            events_path=self._events_path(),
            reflections_path=self._reflections_path(),
            open_loops_path=self.open_loops_path,
            open_loops=self.list_open_loops(include_closed=True),
            recent_reflections=self._read_recent_jsonl(self._reflections_path(), max_reflections),
        )

    def _build_reflection(self, turn_id: str, user_input: str,
                          assistant_text: str,
                          tool_history: list[dict],
                          status: str) -> Reflection:
        tools_used = []
        failures = []
        memory_touched = False
        for item in tool_history:
            name = str(item.get("tool") or "")
            if name and name not in tools_used:
                tools_used.append(name)
            if not item.get("success", True):
                failures.append(
                    f"{name or 'tool'}:{item.get('error_type') or 'error'}"
                )
            args = item.get("args") or {}
            if name in {"learning_feedback", "learning_promote", "memory_consolidate"}:
                memory_touched = True
            if isinstance(args, dict) and any(
                "memory/" in str(value) or "MEMORY.md" in str(value)
                for value in args.values()
            ):
                memory_touched = True

        open_loop_hints = self._extract_open_loop_hints(user_input + "\n" + assistant_text)

        return Reflection(
            turn_id=turn_id,
            timestamp=self._now(),
            status=status,
            user_intent=self._summarize(user_input, 240),
            result=self._summarize(assistant_text, 360),
            tools_used=tools_used[:20],
            failures=failures[:20],
            memory_touched=memory_touched,
            open_loop_hints=open_loop_hints[:10],
        )

    def _extract_open_loop_hints(self, text: str) -> list[str]:
        hints = []
        for line in (text or "").splitlines():
            compact = " ".join(line.strip().split())
            if not compact:
                continue
            lowered = compact.lower()
            if any(marker in lowered for marker in (
                "todo:", "follow-up:", "open loop:", "next:", "prossimo:",
                "da fare:", "rimane", "manca ancora",
            )):
                hints.append(self._clean(compact, 220))
        return hints

    def _save_open_loops(self, loops: list[OpenLoop]) -> None:
        tmp = f"{self.open_loops_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump([asdict(loop) for loop in loops], handle, indent=2, ensure_ascii=False)
        os.replace(tmp, self.open_loops_path)

    def _append_event(self, item: dict[str, Any]) -> None:
        self._append_jsonl(self._events_path(), self._redact_item(item))

    def _append_reflection(self, item: dict[str, Any]) -> None:
        self._append_jsonl(self._reflections_path(), self._redact_item(item))

    def _append_jsonl(self, path: str, item: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")

    def _read_recent_jsonl(self, path: str, limit: int) -> list[dict]:
        if not os.path.exists(path):
            return []
        rows = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except Exception:
            return rows[-limit:]
        return rows[-limit:]

    def _events_path(self) -> str:
        return os.path.join(self.events_dir, f"{self._today()}.jsonl")

    def _reflections_path(self) -> str:
        return os.path.join(self.reflections_dir, f"{self._today()}.jsonl")

    def _new_id(self, prefix: str, text: str) -> str:
        raw = f"{prefix}:{time.time()}:{text}"
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
        return f"{prefix}_{digest}"

    def _summarize(self, text: str, max_chars: int) -> str:
        return self._clean(text, max_chars)

    def _clean(self, text: str, max_chars: int) -> str:
        compact = " ".join(str(text or "").split())
        return redact(compact)[:max_chars]

    def _redact_item(self, value):
        if isinstance(value, dict):
            return {key: self._redact_item(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_item(item) for item in value]
        if isinstance(value, str):
            return redact(value)
        return value

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _today(self) -> str:
        return datetime.now().date().isoformat()
