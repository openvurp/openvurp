"""
openvurp Core — Subagent Runtime

Runner condiviso tra processo worker e fallback inline.
"""

from __future__ import annotations

import json
import os
import time

from core.environment import EnvironmentInspector, render_environment_prompt
from core.llm import create_llm_client
from core.method import build_operating_method
from core.session_routing import SessionRoute


SAFE_EXECUTOR_TOOLS = {
    "read_file",
    "grep",
    "glob",
    "web_fetch",
    "web_search",
    "browser",
    "browser_devtools",
    "browser_setup",
    "process_list",
    "process_sessions",
    "process_read",
    "doctor",
}


class SubagentBridgeUI:
    def __init__(self):
        self.response_text = ""
        self.events: list[str] = []

    def start_spinner(self, _text: str = "Thinking...") -> None:
        return None

    def stop_spinner(self) -> None:
        return None

    def start_response(self) -> None:
        self.response_text = ""

    def stream_text(self, text: str) -> None:
        self.response_text += str(text or "")

    def end_response(self) -> None:
        return None

    def openvurp_say(self, text: str) -> None:
        self.response_text += str(text or "")

    def status(self, text: str) -> None:
        if text:
            self.events.append(str(text))

    def error(self, text: str) -> None:
        if text:
            self.events.append(f"[error] {text}")

    def show_cmd(self, cmd: str) -> None:
        if cmd:
            self.events.append(f"$ {cmd}")

    def show_output(self, output: str, is_error: bool = False, max_lines: int = 999) -> None:
        if not output:
            return
        lines = str(output).splitlines()
        preview = "\n".join(lines[:max_lines])
        self.events.append(f"[tool error]\n{preview}" if is_error else preview)

    def confirm(self, _question: str) -> bool:
        return False

    def prompt(self, context_pct: int = 0) -> str:
        return ""

    def welcome(self, model: str = "", backend: str = "") -> None:
        return None

    def goodbye(self) -> None:
        return None

    def show_memory_table(self) -> None:
        return None

    def show_skills_table(self) -> None:
        return None

    def show_self_panel(self) -> None:
        return None

    def show_trace(self, trace: str) -> None:
        if trace:
            self.events.append(trace)

    def show_doctor(self, report: str) -> None:
        if report:
            self.events.append(report)

    def show_evolve(self) -> None:
        return None


def _workspace_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _restrict_child_tools(child, allowed: set[str] | None) -> None:
    protected = {"subagent_spawn", "subagent_list", "subagent_wait", "subagent_wait_all", "subagent_kill"}
    for name in list(child.tools.names()):
        if name in protected:
            child.tools.unregister(name)
            continue
        if allowed is not None and name not in allowed:
            child.tools.unregister(name)
    child._build_system_prompt()


def run_text_job(job: dict, tool_names: list[str] | None = None) -> str:
    workspace_dir = _workspace_dir()
    llm = create_llm_client(backend=job.get("backend", ""), model=job.get("model", ""))

    snapshot = EnvironmentInspector(workspace_dir).get_snapshot()
    method = build_operating_method(snapshot, tool_names or [])
    environment = render_environment_prompt(snapshot)

    system_prompt = (
        f"{environment}\n\n"
        f"{method}\n\n"
        f"## SUBAGENT MODE\n"
        f"Sei un sub-agente con un compito specifico.\n"
        f"Rispondi in modo conciso ma strutturato.\n"
        f"NON usare tool — rispondi solo con testo.\n"
        f"Deliverable richiesto: {job.get('deliverable', '') or 'non specificato'}\n"
        f"Sessione child: {job.get('child_session_key', '')}\n"
        f"Formato obbligatorio:\n"
        f"RESULT:\n"
        f"- risposta principale\n"
        f"RISKS:\n"
        f"- rischi o incertezze\n"
        f"NEXT_FOR_PARENT:\n"
        f"- prossimo passo consigliato al parent\n"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": job.get("task", "")},
    ]
    return llm.call(messages, thinking_level=job.get("thinking", "off") or "off")


def run_executor_job(job: dict) -> tuple[str, list[str]]:
    from core.agent import Agent

    ui = SubagentBridgeUI()
    child = Agent(ui=ui)
    child._subagent_depth = int(job.get("depth", 0) or 0) + 1
    child._build_system_prompt()

    mode = str(job.get("mode", "text") or "text")
    if mode == "safe_executor":
        _restrict_child_tools(child, set(SAFE_EXECUTOR_TOOLS))
    else:
        _restrict_child_tools(child, None)

    child.llm = create_llm_client(
        backend=job.get("backend", "") or "",
        model=job.get("model", "") or "",
    )
    child._build_system_prompt()
    child.run(
        job.get("task", ""),
        source="subagent",
        sender=f"subagent:{job.get('id', '')}",
        actor_id=job.get("requested_by", "") or "cli_owner",
        session_key=job.get("child_session_key", "") or "",
        parent_session_key=job.get("parent_session_key", "") or "",
    )
    try:
        child.save_session()
    except Exception:
        pass

    body = ui.response_text.strip()
    if not body:
        messages = child._get_session_messages(
            SessionRoute.build(
                source="subagent",
                sender=f"subagent:{job.get('id', '')}",
                actor_id=job.get("requested_by", "") or "cli_owner",
                session_key=job.get("child_session_key", "") or "",
                parent_session_key=job.get("parent_session_key", "") or "",
            )
        )
        for message in reversed(messages):
            if message.get("role") == "assistant" and message.get("content"):
                body = str(message["content"]).strip()
                break
    return body.strip(), ui.events[-40:]


def run_subagent_job(job: dict) -> dict:
    started = time.time()
    mode = str(job.get("mode", "text") or "text")
    try:
        if mode in {"safe_executor", "inherit_executor"}:
            body, events = run_executor_job(job)
            result = f"{body}\n\nTOOL_LOG:\n" + "\n".join(events).strip() if events else body
        else:
            result = run_text_job(job, tool_names=job.get("tool_names", []) or [])
        return {
            "status": "completed",
            "result": (result or "").strip(),
            "error": "",
            "finished_at": time.time(),
            "duration_ms": int((time.time() - started) * 1000),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "result": "",
            "error": str(exc),
            "finished_at": time.time(),
            "duration_ms": int((time.time() - started) * 1000),
        }


def write_state_file(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
