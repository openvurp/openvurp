"""
openvurp Core — Subagent Manager

Runtime subagent production-grade:
- orchestration non bloccante
- worker process-based con kill/timeout reali
- stato persistito su disco
- announce automatico al requester
- policy di profondità e concorrenza
- routing automatico cloud/local/executor
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.model_router import route_subagent
from core.session_routing import SessionRoute, build_subagent_session_key
from core.subagent_runtime import run_subagent_job, write_state_file


class SubagentStatus(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    KILLED = "killed"


@dataclass
class SubagentRun:
    id: str
    task: str
    deliverable: str = ""
    requested_by: str = ""
    request_route: SessionRoute = field(default_factory=SessionRoute)
    parent_session_key: str = ""
    child_session_key: str = ""
    depth: int = 0
    backend: str = ""
    model: str = ""
    thinking: str = ""
    mode: str = "text"
    strategy: str = ""
    routing_reason: str = ""
    status: SubagentStatus = SubagentStatus.QUEUED
    result: str = ""
    error: str = ""
    announce_back: bool = True
    timeout_seconds: int = 180
    started_at: float = field(default_factory=time.time)
    running_at: float = 0
    finished_at: float = 0
    pid: int = 0
    thread: Optional[threading.Thread] = None
    process: Optional[subprocess.Popen] = None
    job_path: str = ""
    state_path: str = ""

    @property
    def elapsed_ms(self) -> int:
        end = self.finished_at or time.time()
        return int((end - self.started_at) * 1000)

    def to_dict(self, include_full: bool = False) -> dict:
        data = {
            "id": self.id,
            "task": self.task if include_full else self.task[:200],
            "deliverable": self.deliverable if include_full else self.deliverable[:200],
            "requested_by": self.requested_by,
            "request_route": {
                "source": self.request_route.source,
                "sender": self.request_route.sender,
                "actor_id": self.request_route.actor_id,
                "chat_id": self.request_route.chat_id,
                "thread_id": self.request_route.thread_id,
                "session_key": self.request_route.session_key,
                "parent_session_key": self.request_route.parent_session_key,
            },
            "parent_session_key": self.parent_session_key,
            "child_session_key": self.child_session_key,
            "depth": self.depth,
            "backend": self.backend,
            "model": self.model,
            "thinking": self.thinking,
            "mode": self.mode,
            "strategy": self.strategy,
            "routing_reason": self.routing_reason,
            "status": self.status.value,
            "result": self.result if include_full else self.result[:200],
            "error": self.error,
            "announce_back": self.announce_back,
            "timeout_seconds": self.timeout_seconds,
            "started_at": self.started_at,
            "running_at": self.running_at,
            "finished_at": self.finished_at,
            "elapsed_ms": self.elapsed_ms,
            "pid": self.pid,
            "job_path": self.job_path,
            "state_path": self.state_path,
        }
        return data


class SubagentManager:
    def __init__(self, parent_agent):
        import config as cfg

        self.parent = parent_agent
        self.children: dict[str, SubagentRun] = {}
        self.max_depth = int(getattr(cfg, "SUBAGENT_MAX_DEPTH", 3) or 3)
        self.max_children = int(getattr(cfg, "SUBAGENT_MAX_CONCURRENT", 4) or 4)
        self.default_timeout = int(getattr(cfg, "SUBAGENT_TIMEOUT_SECONDS", 180) or 180)
        self.default_backend = str(getattr(cfg, "SUBAGENT_DEFAULT_BACKEND", "") or "").strip()
        self.default_model = str(getattr(cfg, "SUBAGENT_DEFAULT_MODEL", "") or "").strip()
        self.default_thinking = str(getattr(cfg, "SUBAGENT_DEFAULT_THINKING", "off") or "off").strip()
        self.default_mode = str(getattr(cfg, "SUBAGENT_DEFAULT_MODE", "auto") or "auto").strip()
        self.auto_announce = bool(getattr(cfg, "SUBAGENT_AUTO_ANNOUNCE", True))
        self.runtime_mode = str(getattr(cfg, "SUBAGENT_RUNTIME", "process") or "process").strip().lower()
        self.kill_grace_seconds = int(getattr(cfg, "SUBAGENT_KILL_GRACE_SECONDS", 3) or 3)
        self._depth = getattr(parent_agent, "_subagent_depth", 0)
        self._semaphore = threading.Semaphore(max(1, self.max_children))
        self._lock = threading.Lock()
        self._root_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "memory",
            "subagents",
        )
        self._jobs_dir = os.path.join(self._root_dir, "jobs")
        self._runs_dir = os.path.join(self._root_dir, "runs")
        os.makedirs(self._jobs_dir, exist_ok=True)
        os.makedirs(self._runs_dir, exist_ok=True)
        self._restore_runs()

    def _restore_runs(self) -> None:
        for name in sorted(os.listdir(self._runs_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self._runs_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except Exception:
                continue
            route_data = data.get("request_route", {}) or {}
            route = SessionRoute.build(
                source=route_data.get("source", "cli"),
                sender=route_data.get("sender", "user"),
                actor_id=route_data.get("actor_id", "cli_owner"),
                chat_id=route_data.get("chat_id", ""),
                thread_id=route_data.get("thread_id", ""),
                session_key=route_data.get("session_key", ""),
                parent_session_key=route_data.get("parent_session_key", ""),
            )
            status_text = str(data.get("status", "failed") or "failed")
            if status_text in {"queued", "running"}:
                status_text = "failed"
                data["error"] = data.get("error") or "Runtime riavviato durante l'esecuzione del subagent."
            try:
                status = SubagentStatus(status_text)
            except Exception:
                status = SubagentStatus.FAILED
            run = SubagentRun(
                id=str(data.get("id", os.path.splitext(name)[0]) or os.path.splitext(name)[0]),
                task=str(data.get("task", "") or ""),
                deliverable=str(data.get("deliverable", "") or ""),
                requested_by=str(data.get("requested_by", "") or ""),
                request_route=route,
                parent_session_key=str(data.get("parent_session_key", "") or ""),
                child_session_key=str(data.get("child_session_key", "") or ""),
                depth=int(data.get("depth", 0) or 0),
                backend=str(data.get("backend", "") or ""),
                model=str(data.get("model", "") or ""),
                thinking=str(data.get("thinking", "") or ""),
                mode=str(data.get("mode", "text") or "text"),
                strategy=str(data.get("strategy", "") or ""),
                routing_reason=str(data.get("routing_reason", "") or ""),
                status=status,
                result=str(data.get("result", "") or ""),
                error=str(data.get("error", "") or ""),
                announce_back=bool(data.get("announce_back", True)),
                timeout_seconds=int(data.get("timeout_seconds", self.default_timeout) or self.default_timeout),
                started_at=float(data.get("started_at", time.time()) or time.time()),
                running_at=float(data.get("running_at", 0) or 0),
                finished_at=float(data.get("finished_at", 0) or 0),
                pid=int(data.get("pid", 0) or 0),
                job_path=str(data.get("job_path", os.path.join(self._jobs_dir, f"{os.path.splitext(name)[0]}.json"))),
                state_path=path,
            )
            self.children[run.id] = run

    def spawn(
        self,
        task: str,
        model: str = None,
        thinking: str = None,
        deliverable: str = "",
        requested_by: str = "",
        backend: str = None,
        timeout_seconds: int = 0,
        mode: str = "",
        announce_back: bool | None = None,
        request_route: SessionRoute | None = None,
    ) -> SubagentRun:
        with self._lock:
            active = [
                run for run in self.children.values()
                if run.status in {SubagentStatus.QUEUED, SubagentStatus.RUNNING}
            ]
            if len(active) >= self.max_children:
                return SubagentRun(
                    id="error",
                    task=task,
                    status=SubagentStatus.FAILED,
                    error=f"Limite sub-agenti raggiunto ({self.max_children})",
                )

        if self._depth >= self.max_depth:
            return SubagentRun(
                id="error",
                task=task,
                status=SubagentStatus.FAILED,
                error=f"Profondità massima raggiunta ({self.max_depth})",
            )

        route = request_route or getattr(self.parent, "_active_route", SessionRoute())
        child_id = str(uuid.uuid4())[:8]
        child_session_key = build_subagent_session_key(route.session_key, child_id)
        choice = route_subagent(
            parent_backend=(backend or self.default_backend or getattr(self.parent.llm, "backend", "")),
            parent_model=(model or self.default_model or getattr(self.parent.llm, "model", "")),
            task=task,
            deliverable=deliverable or "",
            requested_backend=backend or "",
            requested_model=model or "",
            requested_thinking=thinking or "",
            requested_mode=mode or self.default_mode,
        )
        run = SubagentRun(
            id=child_id,
            task=task,
            deliverable=deliverable or "",
            requested_by=requested_by or getattr(route, "actor_id", "") or "",
            request_route=route,
            parent_session_key=route.session_key,
            child_session_key=child_session_key,
            depth=self._depth + 1,
            backend=choice.backend,
            model=choice.model,
            thinking=choice.thinking or self.default_thinking or "off",
            mode=choice.mode or "text",
            strategy=choice.strategy,
            routing_reason=choice.reason,
            announce_back=self.auto_announce if announce_back is None else bool(announce_back),
            timeout_seconds=max(1, int(timeout_seconds or self.default_timeout)),
            job_path=os.path.join(self._jobs_dir, f"{child_id}.json"),
            state_path=os.path.join(self._runs_dir, f"{child_id}.json"),
        )
        self._write_job(run)
        self._write_state(run)

        with self._lock:
            self.children[child_id] = run

        thread = threading.Thread(
            target=self._run_controller,
            args=(run,),
            daemon=True,
            name=f"subagent-controller-{child_id}",
        )
        run.thread = thread
        thread.start()
        self._emit("spawned", run)
        return run

    def _emit(self, event_name: str, run: SubagentRun) -> None:
        gateway = getattr(self.parent, "gateway", None)
        if gateway:
            gateway.emit(
                f"subagent.{event_name}",
                {
                    "id": run.id,
                    "status": run.status.value,
                    "mode": run.mode,
                    "backend": run.backend,
                    "model": run.model,
                    "strategy": run.strategy,
                    "routing_reason": run.routing_reason,
                    "parent_session_key": run.parent_session_key,
                    "child_session_key": run.child_session_key,
                    "pid": run.pid,
                },
            )

    def _announce(self, run: SubagentRun, message: str) -> None:
        if not run.announce_back or not message.strip():
            return
        gateway = getattr(self.parent, "gateway", None)
        if not gateway:
            return
        gateway.announce(run.request_route, message.strip())

    def _build_job_payload(self, run: SubagentRun) -> dict:
        return {
            **run.to_dict(include_full=True),
            "tool_names": list(getattr(self.parent.tools, "names", lambda: [])()),
        }

    def _write_job(self, run: SubagentRun) -> None:
        write_state_file(run.job_path, self._build_job_payload(run))

    def _write_state(self, run: SubagentRun) -> None:
        write_state_file(run.state_path, run.to_dict(include_full=True))

    def _refresh_from_state(self, run: SubagentRun) -> None:
        if not run.state_path or not os.path.exists(run.state_path):
            return
        try:
            with open(run.state_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return
        status_text = str(data.get("status", run.status.value) or run.status.value)
        try:
            run.status = SubagentStatus(status_text)
        except Exception:
            pass
        run.result = str(data.get("result", run.result) or run.result)
        run.error = str(data.get("error", run.error) or run.error)
        run.running_at = float(data.get("running_at", run.running_at) or run.running_at or 0)
        run.finished_at = float(data.get("finished_at", run.finished_at) or run.finished_at or 0)
        run.pid = int(data.get("pid", run.pid) or run.pid or 0)

    def _launch_worker_process(self, run: SubagentRun) -> subprocess.Popen:
        worker_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "subagent_worker.py",
        )
        command = [sys.executable, worker_path, "--job", run.job_path, "--state", run.state_path]
        return subprocess.Popen(
            command,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _run_inline_job(self, run: SubagentRun) -> dict:
        return run_subagent_job(self._build_job_payload(run))

    def _terminate_process(self, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=self.kill_grace_seconds)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass

    def _run_controller(self, run: SubagentRun) -> None:
        acquired = False
        try:
            self._semaphore.acquire()
            acquired = True
            if run.status == SubagentStatus.KILLED:
                run.finished_at = time.time()
                self._write_state(run)
                return

            run.status = SubagentStatus.RUNNING
            run.running_at = time.time()
            self._write_state(run)
            self._emit("running", run)

            if self.runtime_mode == "inline":
                holder: dict[str, dict] = {}

                def _inline_worker():
                    holder["outcome"] = self._run_inline_job(run)

                worker = threading.Thread(target=_inline_worker, daemon=True, name=f"subagent-inline-{run.id}")
                worker.start()
                worker.join(timeout=run.timeout_seconds)
                if worker.is_alive():
                    run.status = SubagentStatus.TIMED_OUT
                    run.error = f"Timeout dopo {run.timeout_seconds}s"
                    run.finished_at = time.time()
                    self._write_state(run)
                    self._emit("timed_out", run)
                    self._announce(
                        run,
                        (
                            f"[subagent {run.id}] timeout\n"
                            f"task: {run.task[:160]}\n"
                            f"sessione child: {run.child_session_key}"
                        ),
                    )
                    return
                outcome = holder.get("outcome", {}) or {}
                run.pid = os.getpid()
                try:
                    run.status = SubagentStatus(str(outcome.get("status", "failed")))
                except Exception:
                    run.status = SubagentStatus.FAILED
                run.result = str(outcome.get("result", "") or "")
                run.error = str(outcome.get("error", "") or "")
                run.finished_at = float(outcome.get("finished_at", time.time()) or time.time())
                self._write_state(run)
            else:
                process = self._launch_worker_process(run)
                run.process = process
                run.pid = int(process.pid or 0)
                self._write_state(run)
                try:
                    process.wait(timeout=run.timeout_seconds)
                except subprocess.TimeoutExpired:
                    self._terminate_process(process)
                    run.status = SubagentStatus.TIMED_OUT
                    run.error = f"Timeout dopo {run.timeout_seconds}s"
                    run.finished_at = time.time()
                    self._write_state(run)
                    self._emit("timed_out", run)
                    self._announce(
                        run,
                        (
                            f"[subagent {run.id}] timeout\n"
                            f"task: {run.task[:160]}\n"
                            f"sessione child: {run.child_session_key}"
                        ),
                    )
                    return
                self._refresh_from_state(run)

            if run.status == SubagentStatus.COMPLETED:
                self._emit("completed", run)
                self._announce(
                    run,
                    (
                        f"[subagent {run.id}] completato\n"
                        f"route: {run.strategy or 'manual'}\n"
                        f"sessione child: {run.child_session_key}\n\n"
                        f"{run.result[:3000]}"
                    ),
                )
            elif run.status == SubagentStatus.KILLED:
                self._emit("killed", run)
            elif run.status == SubagentStatus.TIMED_OUT:
                self._emit("timed_out", run)
            else:
                if run.status != SubagentStatus.FAILED:
                    run.status = SubagentStatus.FAILED
                run.finished_at = run.finished_at or time.time()
                self._write_state(run)
                self._emit("failed", run)
                self._announce(run, f"[subagent {run.id}] errore\n{run.error[:500]}")
        except Exception as exc:
            run.status = SubagentStatus.FAILED
            run.error = str(exc)
            run.finished_at = time.time()
            self._write_state(run)
            self._emit("failed", run)
            self._announce(run, f"[subagent {run.id}] errore\n{run.error[:500]}")
        finally:
            if acquired:
                self._semaphore.release()

    def list_children(self) -> list[dict]:
        return [run.to_dict() for run in self.children.values()]

    def kill(self, child_id: str) -> bool:
        run = self.children.get(child_id)
        if not run:
            return False
        if run.status not in {SubagentStatus.QUEUED, SubagentStatus.RUNNING}:
            return True
        if run.process is not None:
            self._terminate_process(run.process)
        run.status = SubagentStatus.KILLED
        run.finished_at = time.time()
        self._write_state(run)
        self._emit("killed", run)
        return True

    def wait(self, child_id: str, timeout: int = 120) -> str:
        run = self.children.get(child_id)
        if not run:
            return f"Sub-agente {child_id} non trovato"

        if run.thread and run.thread.is_alive():
            run.thread.join(timeout=timeout)
        self._refresh_from_state(run)

        if run.status == SubagentStatus.COMPLETED:
            return run.result
        if run.status == SubagentStatus.FAILED:
            return f"[ERRORE] {run.error}"
        if run.status == SubagentStatus.TIMED_OUT:
            return f"[TIMEOUT] {run.error}"
        if run.status == SubagentStatus.KILLED:
            return "[TERMINATO]"
        if run.status == SubagentStatus.QUEUED:
            return "[IN CODA]"
        return "[IN ESECUZIONE]"

    def wait_all(self, timeout: int = 300) -> dict[str, str]:
        results = {}
        for child_id in list(self.children.keys()):
            results[child_id] = self.wait(child_id, timeout=timeout)
        return results
