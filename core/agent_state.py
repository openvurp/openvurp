"""
openvurp Core - Agent operational state.

This is the lightweight autonomy layer: it tracks the current goal, phase,
plan, observations, blockers, and final result across tool loops and turns.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
import hashlib
import json
import os
import time
from typing import Any

from core.security.audit import redact


class AgentPhase(Enum):
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    OBSERVING = "observing"
    REVISING = "revising"
    WAITING_USER = "waiting_user"
    BLOCKED = "blocked"
    REFLECTING = "reflecting"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


ACTIVE_PHASES = {
    AgentPhase.PLANNING.value,
    AgentPhase.EXECUTING.value,
    AgentPhase.OBSERVING.value,
    AgentPhase.REVISING.value,
    AgentPhase.WAITING_USER.value,
    AgentPhase.BLOCKED.value,
}


@dataclass
class ActiveTask:
    task_id: str
    goal: str
    state: str = AgentPhase.IDLE.value
    source: str = "cli"
    actor: str = "agent"
    session_key: str = ""
    thinking_level: str = "normal"
    plan: list[str] = field(default_factory=list)
    current_step: int = 0
    observations: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    iterations: int = 0
    created_at: str = field(default_factory=lambda: _now())
    updated_at: str = field(default_factory=lambda: _now())
    finished_at: str = ""
    final_result: str = ""

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_PHASES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentStateMachine:
    STATE_FILE = "agent_state.json"
    EVENTS_DIR = "agent_state"

    def __init__(self, memory_dir: str, scope_key: str = ""):
        self.memory_dir = memory_dir
        scope = str(scope_key or "").strip()
        if scope:
            digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:20]
            route_root = os.path.join(memory_dir, "route_state", digest)
            self.path = os.path.join(route_root, self.STATE_FILE)
            self.events_dir = os.path.join(route_root, self.EVENTS_DIR)
        else:
            self.path = os.path.join(memory_dir, self.STATE_FILE)
            self.events_dir = os.path.join(memory_dir, self.EVENTS_DIR)
        os.makedirs(memory_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        os.makedirs(self.events_dir, exist_ok=True)
        self.current: ActiveTask | None = self._load()

    def begin_turn(
        self,
        goal: str,
        source: str = "cli",
        actor: str = "agent",
        session_key: str = "",
        thinking_level: str = "normal",
        plan: list[str] | None = None,
    ) -> ActiveTask:
        goal = self._clean(goal, 1000)
        if self._should_continue(goal):
            task = self.current
            assert task is not None
            task.state = AgentPhase.PLANNING.value
            task.source = source or task.source
            task.actor = actor or task.actor
            task.session_key = session_key or task.session_key
            task.thinking_level = thinking_level or task.thinking_level
            if plan:
                task.plan = [self._clean(step, 300) for step in plan[:12]]
                task.current_step = min(task.current_step, max(len(task.plan) - 1, 0))
            task.observations.append(self._clean(f"User asked to continue: {goal}", 300))
            task.observations = task.observations[-12:]
        else:
            task = ActiveTask(
                task_id=self._new_id("task", goal),
                goal=goal,
                state=AgentPhase.PLANNING.value,
                source=source or "cli",
                actor=actor or "agent",
                session_key=session_key or "",
                thinking_level=thinking_level or "normal",
                plan=(
                    [self._clean(step, 300) for step in plan[:12]]
                    if plan else self._default_plan(goal, thinking_level)
                ),
            )
            self.current = task

        self._touch(task)
        self._save(task)
        self._event("turn_begin", {"task": task.to_dict()})
        return task

    def transition(self, phase: AgentPhase | str, note: str = "") -> ActiveTask | None:
        task = self.current
        if not task:
            return None
        value = phase.value if isinstance(phase, AgentPhase) else str(phase)
        task.state = value
        if note:
            task.observations.append(self._clean(note, 300))
            task.observations = task.observations[-12:]
        self._touch(task)
        self._save(task)
        self._event("transition", {"state": value, "note": self._clean(note, 300)})
        return task

    def mark_execution(self, tool_names: list[str], iteration: int = 0) -> None:
        task = self.current
        if not task:
            return
        task.state = AgentPhase.EXECUTING.value
        task.iterations = max(task.iterations, int(iteration or 0))
        for name in tool_names:
            if name and name not in task.tools_used:
                task.tools_used.append(name)
        task.tools_used = task.tools_used[-30:]
        self._touch(task)
        self._save(task)
        self._event("execute", {"tools": tool_names, "iteration": iteration})

    def record_observation(
        self,
        tool_history: list[dict] | None = None,
        outputs: list[str] | None = None,
    ) -> None:
        task = self.current
        if not task:
            return
        tool_history = tool_history or []
        outputs = outputs or []
        parts = []
        for item in tool_history:
            name = str(item.get("tool") or "tool")
            success = bool(item.get("success", False))
            status = "ok" if success else f"failed:{item.get('error_type') or 'error'}"
            parts.append(f"{name}={status}")
            if name and name not in task.tools_used:
                task.tools_used.append(name)
            if not success:
                task.blockers.append(f"{name}: {item.get('error_type') or 'error'}")
        for output in outputs[:3]:
            preview = self._clean(output, 220)
            if preview:
                parts.append(f"output: {preview}")

        had_success = any(bool(item.get("success", False)) for item in tool_history)
        had_failure = any(not bool(item.get("success", True)) for item in tool_history)
        if had_success and not had_failure and task.plan:
            task.current_step = min(task.current_step + 1, max(len(task.plan) - 1, 0))

        if parts:
            task.observations.append("; ".join(parts)[:500])
            task.observations = task.observations[-12:]
        task.blockers = task.blockers[-12:]
        task.tools_used = task.tools_used[-30:]
        task.state = AgentPhase.REVISING.value
        self._touch(task)
        self._save(task)
        self._event("observe", {"summary": parts[:8]})

    def finish(self, result: str = "", waiting_user: bool = False) -> ActiveTask | None:
        task = self.current
        if not task:
            return None
        task.state = AgentPhase.WAITING_USER.value if waiting_user else AgentPhase.COMPLETED.value
        task.final_result = self._clean(result, 800)
        task.finished_at = _now() if not waiting_user else ""
        self._touch(task)
        self._save(task)
        self._event("finish", {
            "state": task.state,
            "result": task.final_result,
        })
        return task

    def fail(self, error: str, phase: AgentPhase = AgentPhase.FAILED) -> ActiveTask | None:
        task = self.current
        if not task:
            return None
        task.state = phase.value
        task.blockers.append(self._clean(error, 500))
        task.blockers = task.blockers[-12:]
        task.final_result = self._clean(error, 800)
        if phase in (AgentPhase.FAILED, AgentPhase.INTERRUPTED):
            task.finished_at = _now()
        self._touch(task)
        self._save(task)
        self._event("fail", {"state": task.state, "error": self._clean(error, 500)})
        return task

    def add_note(self, note: str) -> ActiveTask | None:
        task = self.current
        if not task:
            return None
        task.observations.append(self._clean(note, 500))
        task.observations = task.observations[-12:]
        self._touch(task)
        self._save(task)
        self._event("note", {"note": self._clean(note, 500)})
        return task

    def clear(self) -> None:
        self.current = None
        self._save_empty()
        self._event("clear", {})

    def status(self) -> dict[str, Any]:
        if not self.current:
            return {"active": False, "state": AgentPhase.IDLE.value}
        data = self.current.to_dict()
        data["active"] = self.current.active
        return data

    def prompt_section(self) -> str:
        task = self.current
        if not task or not task.active:
            return (
                "## AUTONOMY LOOP\n"
                "For non-trivial work, operate as: goal -> plan -> act -> observe -> revise -> finish. "
                "Use tools directly, observe their output, revise the plan, and stop only when the user's goal is handled or genuinely blocked."
            )

        lines = [
            "## AUTONOMY STATE",
            f"State: {task.state}",
            f"Goal: {task.goal}",
            f"Thinking: {task.thinking_level}",
            "Loop: goal -> plan -> act -> observe -> revise -> finish.",
        ]
        if task.plan:
            lines.append("Plan:")
            for idx, step in enumerate(task.plan[:8], start=1):
                marker = "current" if idx - 1 == task.current_step else "next"
                lines.append(f"{idx}. [{marker}] {step}")
        if task.observations:
            lines.append("Recent observations:")
            for obs in task.observations[-5:]:
                lines.append(f"- {obs}")
        if task.blockers:
            lines.append("Known blockers:")
            for blocker in task.blockers[-5:]:
                lines.append(f"- {blocker}")
        if task.tools_used:
            lines.append(f"Tools used: {', '.join(task.tools_used[-12:])}")
        lines.append(
            "Instruction: pick the next concrete step, use the right tool, observe the result, "
            "then revise or finish. If blocked, state the blocker and the smallest useful next request."
        )
        return "\n".join(lines)

    def _should_continue(self, goal: str) -> bool:
        if not self.current or not self.current.active:
            return False
        text = (goal or "").strip().lower()
        if not text:
            return False
        continuation_markers = {
            "vai",
            "continua",
            "ok vai",
            "ok continua",
            "procedi",
            "andiamo avanti",
            "avanti",
            "fallo",
            "prosegui",
        }
        if text in continuation_markers:
            return True
        return text.startswith(("continua", "vai ", "procedi", "prosegui"))

    def _default_plan(self, goal: str, thinking_level: str) -> list[str]:
        level = (thinking_level or "normal").lower()
        if level == "quick":
            return [
                "Identify the single requested outcome.",
                "Answer directly or use one tool if needed.",
                "Stop when the request is handled.",
            ]
        if level == "deep":
            return [
                "Define the concrete outcome and constraints.",
                "Inspect relevant local context before changing anything.",
                "Choose the smallest safe next action.",
                "Execute using the most specific tool available.",
                "Observe tool output and update the plan.",
                "Verify the result.",
                "Finish with outcome, tests, and remaining gaps.",
            ]
        return [
            "Identify intent and success criteria.",
            "Gather only the context needed.",
            "Act with the right tool.",
            "Observe the result and revise if needed.",
            "Finish or ask for the missing decision.",
        ]

    def _load(self) -> ActiveTask | None:
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception:
            return None
        if not isinstance(raw, dict) or not raw:
            return None
        try:
            return ActiveTask(**raw)
        except TypeError:
            return None

    def _save(self, task: ActiveTask) -> None:
        os.makedirs(self.memory_dir, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(task.to_dict(), handle, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _save_empty(self) -> None:
        os.makedirs(self.memory_dir, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({}, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _event(self, event_type: str, payload: dict[str, Any]) -> None:
        item = {
            "type": event_type,
            "timestamp": _now(),
            "task_id": self.current.task_id if self.current else "",
            "payload": self._redact(payload),
        }
        path = os.path.join(self.events_dir, f"{datetime.now().date().isoformat()}.jsonl")
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
        except Exception:
            pass

    def _new_id(self, prefix: str, text: str) -> str:
        digest = hashlib.sha1(f"{time.time()}:{text}".encode("utf-8")).hexdigest()[:10]
        return f"{prefix}_{digest}"

    def _touch(self, task: ActiveTask) -> None:
        task.updated_at = _now()

    def _clean(self, text: str, max_chars: int) -> str:
        compact = " ".join(str(text or "").split())
        return redact(compact)[:max_chars]

    def _redact(self, value):
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            return redact(value)
        return value


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
