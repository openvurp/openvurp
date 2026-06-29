#!/usr/bin/env python3
"""
Experimental full-screen TUI.

This file is intentionally separate from the existing CLI so the user can
evaluate the layout and interaction model without changing the current UI.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import random
import re
import shutil
import subprocess
import textwrap
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable, Optional

try:
    import curses  # type: ignore
except ModuleNotFoundError:
    from core import win_curses as curses  # type: ignore

import config
from agent import MEMORY_DIR, OPENVURP_DIR
from core.agent import Agent
from core.doctor import build_doctor_report, fix_runtime_issues
from core.session import Session
from core.setup_runtime import ensure_runtime_state


SPINNER_FRAMES = ["|", "/", "-", "\\"]
TOOL_PREVIEW_LINES = 12
MAX_CHAT_ENTRIES = 240
MODAL_DESCRIPTION_MIN_WIDTH = 48
MAX_INPUT_LINES = 8
ALT_ENTER_KEY = -1001
CHAT_SCROLL_STEP = 10
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
WAITING_PHRASES = [
    "flibbertigibbeting",
    "kerfuffling",
    "dillydallying",
    "twiddling thumbs",
    "noodling",
    "bamboozling",
    "moseying",
    "hobnobbing",
    "pondering",
    "conjuring",
]

SLASH_COMMANDS: tuple[tuple[str, str], ...] = (
    ("help", "show slash command help"),
    ("commands", "show slash command help"),
    ("status", "show runtime status summary"),
    ("gateway", "show runtime gateway status"),
    ("doctor", "run doctor diagnostics"),
    ("setup", "apply runtime setup fixes"),
    ("settings", "open settings"),
    ("agent", "switch agent or open picker"),
    ("agents", "open agent picker"),
    ("session", "switch session or open picker"),
    ("sessions", "open recent sessions"),
    ("model", "set model or open model picker"),
    ("models", "open model picker"),
    ("think", "show or hide thinking traces"),
    ("verbose", "set tool visibility"),
    ("reasoning", "toggle reasoning wrapper"),
    ("usage", "toggle response usage line"),
    ("new", "start a fresh session"),
    ("reset", "start a fresh session"),
    ("restart", "restart the runtime in place (keeps the terminal)"),
    ("update", "check for updates, apply and restart"),
    ("dashboard", "start the local web dashboard (chat from the browser)"),
    ("trace", "show current session trace"),
    ("memory", "show memory files summary"),
    ("skills", "show local skills summary"),
    ("self", "show local agent summary"),
    ("exit", "exit the TUI"),
    ("quit", "exit the TUI"),
    ("q", "exit the TUI"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Experimental full-screen TUI")
    parser.add_argument("--model", "-m", help="Temporary model override for this TUI session")
    parser.add_argument("--backend", "-b", help="Temporary backend override for this TUI session")
    parser.add_argument("--fresh", action="store_true", help="Start without restoring last session")
    parser.add_argument("--message", help="Auto-send one message after startup")
    parser.add_argument("--setup", action="store_true", help="Run the guided setup wizard, then start")
    return parser.parse_args()


def read_identity_name() -> str:
    path = os.path.join(OPENVURP_DIR, "IDENTITY.md")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                marker = "- **Name:**"
                marker_it = "- **Nome:**"
                if line.lower().startswith(marker.lower()):
                    return line[len(marker):].strip()
                if line.lower().startswith(marker_it.lower()):
                    return line[len(marker_it):].strip()
    except OSError:
        return ""
    return ""


def wrap_text_block(text: str, width: int, prefix: str = "") -> list[str]:
    width = max(12, width)
    wrapper = textwrap.TextWrapper(
        width=max(8, width - len(prefix)),
        replace_whitespace=False,
        drop_whitespace=False,
        break_long_words=True,
        break_on_hyphens=False,
    )
    lines: list[str] = []
    for raw_line in str(text).splitlines() or [""]:
        if not raw_line.strip():
            lines.append(prefix.rstrip())
            continue
        wrapped = wrapper.wrap(raw_line)
        if not wrapped:
            lines.append(prefix.rstrip())
            continue
        for item in wrapped:
            lines.append(prefix + item)
    return lines


def parse_ollama_models(output: str, current_model: str) -> list[str]:
    models: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("name"):
            continue
        name = line.split()[0]
        if name and name not in models:
            models.append(name)
    if current_model and current_model not in models:
        models.insert(0, current_model)
    return models


def pick_waiting_phrase(tick: int, phrases: list[str] | None = None) -> str:
    pool = phrases or WAITING_PHRASES
    if not pool:
        return "waiting"
    idx = (tick // 10) % len(pool)
    return pool[idx]


def choose_waiting_phrase(
    current: str | None,
    phrases: list[str] | None = None,
    rng: random.Random | None = None,
) -> str:
    if current:
        return current
    pool = list(phrases or WAITING_PHRASES)
    if not pool:
        return "waiting"
    chooser = rng or random
    return chooser.choice(pool)


def format_runtime_estimates(session: dict, budget: dict, ctx_pct: int) -> tuple[str, str]:
    session_tokens = int(session.get("tokens_total", 0) or 0)
    llm_calls = int(session.get("llm_calls", 0) or 0)
    turns = int(session.get("turns", 0) or 0)
    tool_calls = int(session.get("tool_calls", 0) or 0)
    budget_total = int(budget.get("total_tokens", 0) or 0)
    budget_max = int(budget.get("budget_tokens", 0) or 0)

    summary = (
        f"ctx stima {ctx_pct}% | "
        f"session tok~ {session_tokens} | "
        f"llm {llm_calls} | turns {turns} | tools {tool_calls}"
    )
    footer = (
        f"ctx tok~ {budget_total}/{budget_max} | "
        f"session tok~ {session_tokens} | "
        f"llm {llm_calls} | turns {turns} | tools {tool_calls}"
    )
    return summary, footer


def normalize_status_event(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    lowered = text.lower()
    if any(word in lowered for word in ("thinking", "elaboro", "compattazione", "overflow")):
        return text, "thinking"
    if any(word in lowered for word in ("bloccato", "permesso negato", "errore", "error")):
        return text, "error"
    return text, "event"


def sanitize_renderable_text(text: str) -> str:
    clean = ANSI_RE.sub("", str(text or "")).replace("\r", "")
    clean = clean.replace("\x00", "")
    return "\n".join(line.rstrip() for line in clean.splitlines()).strip()


def format_token_count(value: int | float | None) -> str:
    if value is None:
        return "0"
    try:
        safe = max(0.0, float(value))
    except (TypeError, ValueError):
        return "0"
    if safe >= 1_000_000:
        return f"{safe / 1_000_000:.1f}m"
    if safe >= 1_000:
        return f"{safe / 1_000:.0f}k" if safe >= 10_000 else f"{safe / 1_000:.1f}k"
    return str(int(round(safe)))


def format_tokens_compact(total: int | None, context: int | None) -> str:
    if total is None and context is None:
        return "tokens ?"
    total_label = "?" if total is None else f"~{format_token_count(total)}"
    if context is None:
        return f"tokens {total_label}"
    pct = None
    if isinstance(total, int) and context and context > 0:
        pct = min(999, round((total / context) * 100))
    suffix = f" ({pct}%)" if pct is not None else ""
    return f"tokens {total_label}/{format_token_count(context)}{suffix}"


def format_response_usage_line(before: dict, after: dict, elapsed_seconds: int, mode: str) -> str:
    delta_tokens = max(0, int(after.get("tokens_total", 0) or 0) - int(before.get("tokens_total", 0) or 0))
    delta_tools = max(0, int(after.get("tool_calls", 0) or 0) - int(before.get("tool_calls", 0) or 0))
    delta_llm = max(0, int(after.get("llm_calls", 0) or 0) - int(before.get("llm_calls", 0) or 0))
    if mode == "tokens":
        return f"usage {format_tokens_compact(delta_tokens, None)} | llm {delta_llm}"
    return (
        f"usage {format_tokens_compact(delta_tokens, None)} | "
        f"llm {delta_llm} | tools {delta_tools} | elapsed {elapsed_seconds}s"
    )


def probe_runtime_gateway(host: str | None = None, port: int | None = None, timeout: float = 1.0) -> dict:
    safe_host = str(host or getattr(config, "GATEWAY_HOST", "127.0.0.1") or "127.0.0.1")
    safe_port = int(port or getattr(config, "GATEWAY_PORT", 8421) or 8421)
    url = f"http://{safe_host}:{safe_port}/api/runtime"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        return {
            "ok": True,
            "host": safe_host,
            "port": safe_port,
            "payload": payload,
        }
    except Exception as exc:
        return {
            "ok": False,
            "host": safe_host,
            "port": safe_port,
            "error": str(exc),
        }


def format_gateway_summary(probe: dict) -> str:
    if not probe.get("ok"):
        return f"gateway offline | {probe.get('host')}:{probe.get('port')} | {probe.get('error', 'unreachable')}"
    payload = probe.get("payload", {}) or {}
    return (
        f"gateway online | {probe.get('host')}:{probe.get('port')} | "
        f"snapshots {payload.get('sessions', 0)} | events {payload.get('event_count_estimate', 0)}"
    )


def tool_display(tool_name: str) -> tuple[str, str]:
    mapping = {
        "shell": ("⚙", "Shell"),
        "browser": ("🌐", "Browser"),
        "browser_devtools": ("🌐", "Browser DevTools"),
        "browser_setup": ("🧰", "Browser Setup"),
        "read_file": ("📄", "Read File"),
        "write_file": ("✍", "Write File"),
        "edit_file": ("✍", "Edit File"),
        "process_start": ("▶", "Process Start"),
        "process_read": ("📟", "Process Read"),
        "process_write": ("⌨", "Process Write"),
        "process_stop": ("■", "Process Stop"),
        "desktop_screenshot": ("🖼", "Desktop Screenshot"),
        "image_analyze": ("👁", "Image Analyze"),
        "audio_transcribe": ("🎧", "Audio Transcribe"),
        "pdf_read": ("📚", "PDF Read"),
    }
    if tool_name in mapping:
        return mapping[tool_name]
    label = " ".join(part.capitalize() for part in tool_name.replace("-", "_").split("_") if part) or "Tool"
    return "🛠", label


def score_modal_item(item: "ModalItem", query: str) -> Optional[tuple[int, int, str]]:
    q = query.strip().lower()
    if not q:
        return (0, 0, item.label.lower())

    label = item.label.lower()
    desc = item.description.lower()
    extra = item.search_text.lower()

    label_idx = label.find(q)
    if label_idx != -1:
        return (0, label_idx, label)

    boundary_words = re.split(r"[\s/_\-.:]+", label)
    for idx, word in enumerate(boundary_words):
        if word.startswith(q):
            return (1, idx, label)

    desc_idx = desc.find(q)
    if desc_idx != -1:
        return (2, desc_idx, label)

    haystack = " ".join(part for part in [label, desc, extra] if part)
    pos = -1
    score = 0
    for ch in q:
        next_pos = haystack.find(ch, pos + 1)
        if next_pos == -1:
            return None
        score += next_pos
        pos = next_pos
    return (3, score, label)


def compute_slash_suggestions(
    text: str,
    commands: tuple[tuple[str, str], ...] | None = None,
) -> list["ModalItem"]:
    raw = str(text or "")
    if not raw.startswith("/"):
        return []

    specs = commands or SLASH_COMMANDS
    query = raw[1:].strip().lower()
    if not query:
        return [
            ModalItem(
                value=f"/{name}",
                label=f"/{name}",
                description=description,
                search_text=f"{name} {description}",
            )
            for name, description in specs
        ]

    scored: list[tuple[tuple[int, int, str], ModalItem]] = []
    for name, description in specs:
        item = ModalItem(
            value=f"/{name}",
            label=f"/{name}",
            description=description,
            search_text=f"{name} {description}",
        )
        score = score_modal_item(item, query)
        if score is None:
            continue
        scored.append((score, item))

    scored.sort(key=lambda row: row[0])
    return [item for _score, item in scored]


def decode_paste_codes(codes: list[int]) -> str:
    """Converte i codici-tasto di un blocco incollato (bracketed paste) in testo.

    I newline (CR/LF, anche CRLF) diventano un singolo '\\n' così il testo
    multilinea incollato entra nell'input invece di essere interpretato come
    tanti Invio (che lo spezzerebbero e invierebbero a metà)."""
    out: list[str] = []
    prev = None
    for code in codes:
        if code == 13:
            out.append("\n")
        elif code == 10:
            if prev != 13:  # collassa CRLF in un solo newline
                out.append("\n")
        elif code == 9:
            out.append("\t")
        elif 32 <= code <= 126:
            out.append(chr(code))
        prev = code
    return "".join(out)


def layout_editor_text(text: str, cursor_pos: int, width: int) -> tuple[list[str], int, int]:
    max_width = max(1, int(width or 1))
    raw = str(text or "")
    cursor_pos = max(0, min(int(cursor_pos), len(raw)))

    lines = [""]
    row = 0
    col = 0
    cursor_row = 0
    cursor_col = 0

    for idx, ch in enumerate(raw):
        if idx == cursor_pos:
            cursor_row = row
            cursor_col = col

        if ch == "\n":
            lines.append("")
            row += 1
            col = 0
            continue

        if col >= max_width:
            lines.append("")
            row += 1
            col = 0

        lines[row] += ch
        col += 1

    if cursor_pos == len(raw):
        cursor_row = row
        cursor_col = col

    if not lines:
        lines = [""]

    return lines, cursor_row, cursor_col


def load_saved_sessions(session_dir: str, limit: int = 12) -> list[dict]:
    if not os.path.isdir(session_dir):
        return []

    rows = []
    for name in os.listdir(session_dir):
        if not name.endswith(".json"):
            continue
        path = os.path.join(session_dir, name)
        try:
            stat = os.stat(path)
            data = Session.load(path) or {}
        except OSError:
            continue
        label = data.get("last_user_message") or data.get("id") or name[:-5]
        label = " ".join(str(label).split())[:72] or name[:-5]
        desc_parts = []
        turns = data.get("turns")
        if isinstance(turns, int):
            desc_parts.append(f"{turns} turns")
        tool_calls = data.get("tool_calls")
        if isinstance(tool_calls, int):
            desc_parts.append(f"{tool_calls} tools")
        ended_at = data.get("ended_at", "")
        if isinstance(ended_at, str) and ended_at:
            desc_parts.append(ended_at.replace("T", " ")[:16])
        rows.append(
            {
                "path": path,
                "label": label,
                "description": " | ".join(desc_parts),
                "mtime": stat.st_mtime,
            }
        )
    rows.sort(key=lambda row: row["mtime"], reverse=True)
    return rows[:limit]


def load_conversation_from_session_file(path: str) -> list[dict]:
    data = Session.load(path) or {}
    conversation = data.get("conversation")
    if not isinstance(conversation, list):
        return []
    messages: list[dict] = []
    for item in conversation:
        role = item.get("role", "")
        text = item.get("text", "")
        if role in ("user", "assistant") and text:
            messages.append({"role": role, "content": text})
    return messages


@dataclass
class ChatEntry:
    kind: str
    title: str = ""
    body: str = ""
    state: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class ConfirmRequest:
    question: str
    response: "queue.Queue[bool]" = field(default_factory=lambda: queue.Queue(maxsize=1))


@dataclass
class ModalItem:
    value: str
    label: str
    description: str = ""
    search_text: str = ""


@dataclass
class ModalState:
    kind: str
    title: str
    items: list[ModalItem] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    selected: int = 0
    filter_text: str = ""
    search_enabled: bool = False
    max_visible: int = 9
    footer_hint: str = ""
    on_select: Optional[Callable[[str], None]] = None
    on_cancel: Optional[Callable[[], None]] = None
    confirm_request: Optional[ConfirmRequest] = None


class TuiBridgeUI:
    def __init__(self, event_queue: "queue.Queue[tuple[str, object]]"):
        self._events = event_queue

    def _emit(self, event: str, payload: object = None) -> None:
        self._events.put((event, payload))

    def start_spinner(self, text: str = "Thinking...") -> None:
        self._emit("spinner_start", text)

    def stop_spinner(self) -> None:
        self._emit("spinner_stop", None)

    def start_response(self) -> None:
        self._emit("assistant_start", None)

    def stream_text(self, text: str) -> None:
        self._emit("assistant_chunk", text)

    def stream_token(self, text: str) -> None:
        # Streaming live token-per-token: la TUI consuma già assistant_chunk
        self._emit("assistant_chunk", text)

    def end_response(self) -> None:
        self._emit("assistant_end", None)

    def openvurp_say(self, text: str) -> None:
        self._emit("assistant_full", text)

    def status(self, text: str) -> None:
        self._emit("status", text)

    def error(self, text: str) -> None:
        self._emit("error", text)

    def show_cmd(self, cmd: str) -> None:
        self._emit("cmd", cmd)

    def show_tool(self, tool_name: str, tool_args: dict | None = None) -> None:
        """Tool call strutturata: ⏺ Label(arg principale), come la CLI."""
        _, label = tool_display(tool_name)
        preview = ""
        if isinstance(tool_args, dict) and tool_args:
            for key in ("path", "file", "query", "url", "name", "command", "pattern", "content", "task"):
                if tool_args.get(key):
                    preview = str(tool_args[key])
                    break
            else:
                preview = str(next(iter(tool_args.values())))
        preview = " ".join(preview.split())
        if len(preview) > 100:
            preview = preview[:97] + "..."
        self._emit("tool_call", {"label": label, "preview": preview})

    def show_output(self, output: str, is_error: bool = False, max_lines: int = 999) -> None:
        self._emit(
            "output",
            {
                "output": output,
                "is_error": is_error,
                "max_lines": max_lines,
            },
        )

    def confirm(self, question: str) -> bool:
        request = ConfirmRequest(question=question)
        self._emit("confirm", request)
        try:
            return bool(request.response.get())
        except Exception:
            return False

    def welcome(self, model: str = "", backend: str = "") -> None:
        self._emit("status", f"[ready: {backend}/{model}]")

    def goodbye(self) -> None:
        self._emit("status", "[goodbye]")

    def prompt(self, context_pct: int = 0) -> str:
        return ""

    def show_memory_table(self) -> None:
        self._emit("status", "[memory panel not supported in TUI]")

    def show_skills_table(self) -> None:
        self._emit("status", "[skills panel not supported in TUI]")

    def show_self_panel(self) -> None:
        self._emit("status", "[self panel not supported in TUI]")

    def show_trace(self, trace: str) -> None:
        self._emit("system_text", trace)

    def show_doctor(self, report: str) -> None:
        self._emit("system_text", report)

    def show_evolve(self) -> None:
        self._emit("status", "[evolve panel not supported in TUI]")


class ExperimentalTUI:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.identity_name = read_identity_name() or "assistant"
        self.session_label = "main"
        self.current_agent_id = "default"
        self.event_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.task_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.bridge = TuiBridgeUI(self.event_queue)

        if args.backend:
            config.LLM_BACKEND = args.backend
        if args.model:
            config.LLM_MODEL = args.model

        ensure_runtime_state(
            OPENVURP_DIR,
            allowed_telegram_users=list(getattr(config, "TELEGRAM_ALLOWED_USERS", []) or []),
            create_integrity_baseline=False,
            force_acl_refresh=False,
        )

        self.agent = Agent(ui=self.bridge)
        if hasattr(self.agent, "gateway"):
            self.agent.gateway.register_announcer(
                "cli",
                lambda _route, text: self.event_queue.put(("subagent_announce", text)),
            )
            self.agent.gateway.register_event_listener(
                lambda event_name, payload: self.event_queue.put(("runtime_event", {"event": event_name, "payload": payload}))
            )
        self.saved_session_dir = os.path.join(MEMORY_DIR, "sessions")

        self.entries: list[ChatEntry] = []
        self.streaming_index: Optional[int] = None
        self.active_tool_index: Optional[int] = None

        self.connection_status = "connected"
        self.activity_status = "idle"
        self.activity_started_at: Optional[float] = None
        self.status_text = "local runtime ready"
        self.flash_until = 0.0
        self.last_ctrl_c_at = 0.0
        self.waiting_phrase: Optional[str] = None

        self.input_buffer = ""
        self.cursor_pos = 0
        self.input_scroll = 0
        self.history: list[str] = []
        self.history_index: Optional[int] = None
        self.scroll_offset = 0
        self.slash_selected = 0

        self.tools_expanded = True
        self.show_thinking = True
        self.local_shell_enabled = False
        self.pending_shell_command = ""
        self.verbose_level = "full"
        self.reasoning_level = "on"
        self.usage_mode = "off"
        self.pending_usage_snapshot: Optional[dict] = None
        self._cached_model_items: Optional[list[ModalItem]] = None
        self._cached_model_items_at = 0.0
        self._cached_session_items: Optional[list[ModalItem]] = None
        self._cached_session_items_at = 0.0

        self.modal: Optional[ModalState] = None
        self.confirm_queue: list[ConfirmRequest] = []

        self.running = True
        self.worker_busy = False

        # Restart/auto-update: impostati per chiedere a main() di rilanciare il
        # processo (stesso terminale) caricando il codice aggiornato.
        self.restart_mode: Optional[str] = None  # None | "restart" | "update"
        self.restart_reason = ""
        self._last_update_check = 0.0

        # Serializza l'accesso all'agente tra worker della TUI e chat dashboard.
        self.agent_lock = threading.Lock()
        self._dashboard_server = None

        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

        if getattr(config, "AUTO_UPDATE", False):
            threading.Thread(target=self._auto_update_loop, daemon=True).start()

        if getattr(config, "DASHBOARD_ENABLED", False):
            self._start_dashboard(announce=False)

        if not args.fresh:
            self._restore_last_conversation()

        self.add_system(
            f"{self.identity_name} ready\n"
            f"backend {self.agent.llm.backend} | model {self.agent.llm.model}\n"
            "events visible | thinking visible | tool output full\n"
            "up/down scroll when input is empty | pgup/pgdn jump | end bottom\n"
            "ctrl+l model | ctrl+g agent | ctrl+p session | ctrl+o tools | ctrl+t thinking | tab autocomplete | /help"
        )

        if args.message:
            self.submit_text(args.message)

    def add_entry(self, entry: ChatEntry) -> None:
        self.entries.append(entry)
        if len(self.entries) > MAX_CHAT_ENTRIES:
            self.entries = self.entries[-MAX_CHAT_ENTRIES:]
        if self.scroll_offset == 0:
            self.scroll_offset = 0

    def add_system(self, text: str, kind: str = "system") -> None:
        self.add_entry(ChatEntry(kind=kind, body=text))

    def add_user(self, text: str) -> None:
        self.add_entry(ChatEntry(kind="user", body=text))

    def add_assistant(self, text: str) -> None:
        self.add_entry(ChatEntry(kind="assistant", body=text))

    def _scroll_chat(self, delta: int) -> None:
        if delta > 0:
            self.scroll_offset += delta
            self.history_index = None
            return
        self.scroll_offset = max(0, self.scroll_offset + delta)
        if self.scroll_offset == 0:
            self.history_index = None

    def set_flash(self, text: str, ttl: float = 3.0) -> None:
        self.status_text = text
        self.flash_until = time.time() + ttl

    def set_activity(self, text: str) -> None:
        previous = self.activity_status
        self.activity_status = text
        if text == "idle":
            self.activity_started_at = None
            self.waiting_phrase = None
            if time.time() > self.flash_until:
                self.status_text = f"{self.connection_status} | idle"
        else:
            if text == "waiting":
                self.waiting_phrase = choose_waiting_phrase(self.waiting_phrase)
            elif previous == "waiting":
                self.waiting_phrase = None
            if self.activity_started_at is None or previous != text:
                self.activity_started_at = time.time()

    def _worker_loop(self) -> None:
        while True:
            item = self.task_queue.get()
            if item is None:
                return
            kind, payload = item
            try:
                if kind == "agent":
                    self._run_agent_request(str(payload))
                    self.agent.save_session()
                elif kind == "local_shell":
                    self._run_local_shell(str(payload))
            finally:
                self.event_queue.put(("task_done", {"kind": kind}))

    def _run_agent_request(self, text: str) -> None:
        with self.agent_lock:
            if self.reasoning_level != "off":
                self.agent.run(text, source="cli", sender="user", actor_id="cli_owner")
                return

            original_classify = getattr(self.agent.reasoner, "classify", None)
            if not callable(original_classify):
                self.agent.run(text, source="cli", sender="user", actor_id="cli_owner")
                return

            self.agent.reasoner.classify = lambda _prompt: SimpleNamespace(value="quick")
            try:
                self.agent.run(text, source="cli", sender="user", actor_id="cli_owner")
            finally:
                self.agent.reasoner.classify = original_classify

    def _run_local_shell(self, command: str) -> None:
        self.event_queue.put(("cmd", f"[local] {command}"))
        process = subprocess.Popen(
            command,
            cwd=OPENVURP_DIR,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate()
        combined = stdout or ""
        if stderr:
            combined = combined + ("\n" if combined else "") + stderr
        combined = combined.strip()
        self.event_queue.put(
            (
                "output",
                {
                    "output": combined or "(no output)",
                    "is_error": process.returncode != 0,
                    "max_lines": 999,
                },
            )
        )

    def _restore_last_conversation(self) -> None:
        self.agent.restore_conversation()
        restored = []
        for message in self.agent.messages:
            role = message.get("role")
            content = message.get("content", "")
            if role == "user" and content:
                restored.append(ChatEntry(kind="user", body=content))
            elif role == "assistant" and content:
                restored.append(ChatEntry(kind="assistant", body=content))
        if restored:
            self.entries.extend(restored[-80:])
            self.set_flash("restored last conversation", ttl=4.0)

    def _replace_chat_with_messages(self, messages: list[dict], label: str) -> None:
        self.entries.clear()
        self.streaming_index = None
        self.active_tool_index = None
        self._cached_session_items = None
        self._cached_session_items_at = 0.0
        self.agent.messages = []
        self.agent._channel_sessions.clear()
        self.agent._build_system_prompt()
        if self.agent.messages and self.agent.messages[0].get("role") == "system":
            base = [dict(self.agent.messages[0])]
        else:
            base = []
        self.agent.messages = base + [dict(message) for message in messages]
        self.session_label = label
        for message in messages:
            role = message.get("role")
            content = message.get("content", "")
            if role == "user" and content:
                self.add_user(content)
            elif role == "assistant" and content:
                self.add_assistant(content)
        self.set_flash(f"loaded session {label}", ttl=4.0)

    def _new_session(self) -> None:
        try:
            self.agent.save_session()
        except Exception:
            pass
        self._cached_session_items = None
        self._cached_session_items_at = 0.0
        self.agent.session = Session(session_dir=self.saved_session_dir)
        self.agent.messages = []
        self.agent._channel_sessions.clear()
        self.agent._build_system_prompt()
        self.entries.clear()
        self.streaming_index = None
        self.active_tool_index = None
        self.session_label = "main"
        self.set_flash("new session started", ttl=4.0)
        self.add_system("fresh session ready")

    def _discover_model_items(self) -> list[ModalItem]:
        if self._cached_model_items is not None and (time.time() - self._cached_model_items_at) < 5.0:
            return list(self._cached_model_items)

        items: list[ModalItem] = []
        current = self.agent.llm.model
        seen = set()
        if current:
            items.append(
                ModalItem(
                    value=current,
                    label=current,
                    description="current",
                    search_text=f"{self.agent.llm.backend} current",
                )
            )
            seen.add(current)
        if self.agent.llm.backend == "ollama" and shutil.which("ollama"):
            try:
                result = subprocess.run(
                    ["ollama", "list"],
                    cwd=OPENVURP_DIR,
                    capture_output=True,
                    text=True,
                    timeout=6,
                    check=False,
                )
                for model in parse_ollama_models(result.stdout, current):
                    if model in seen:
                        continue
                    seen.add(model)
                    items.append(
                        ModalItem(
                            value=model,
                            label=model,
                            description=self.agent.llm.backend,
                            search_text=f"{self.agent.llm.backend} {model}",
                        )
                    )
            except Exception:
                pass
        if not items:
            items.append(
                ModalItem(
                    value=current or "unknown",
                    label=current or "unknown",
                    description=self.agent.llm.backend,
                )
            )
        items.append(
            ModalItem(
                value="__info__",
                label="manual override via /model <id>",
                description=f"backend {self.agent.llm.backend}",
                search_text=f"manual custom {self.agent.llm.backend}",
            )
        )
        self._cached_model_items = list(items)
        self._cached_model_items_at = time.time()
        return items

    def _open_modal(self, modal: ModalState) -> None:
        self.modal = modal

    def _searchable_modal(
        self,
        title: str,
        items: list[ModalItem],
        on_select: Callable[[str], None],
        lines: Optional[list[str]] = None,
        footer_hint: str = "",
        max_visible: int = 9,
    ) -> None:
        self._open_modal(
            ModalState(
                kind="menu",
                title=title,
                items=items,
                lines=lines or [],
                search_enabled=True,
                footer_hint=footer_hint,
                max_visible=max_visible,
                on_select=on_select,
            )
        )

    def _close_modal(self) -> None:
        modal = self.modal
        self.modal = None
        if modal and modal.on_cancel:
            modal.on_cancel()

    def _filtered_modal_items(self) -> list[ModalItem]:
        if not self.modal:
            return []
        if not self.modal.search_enabled:
            return list(self.modal.items)

        query = self.modal.filter_text.strip()
        if not query:
            return list(self.modal.items)

        scored: list[tuple[tuple[int, int, str], ModalItem]] = []
        for item in self.modal.items:
            score = score_modal_item(item, query)
            if score is None:
                continue
            scored.append((score, item))
        scored.sort(key=lambda row: row[0])
        return [item for _score, item in scored]

    def _ensure_modal_selection(self) -> None:
        if not self.modal:
            return
        items = self._filtered_modal_items()
        if not items:
            self.modal.selected = 0
            return
        if self.modal.selected < 0:
            self.modal.selected = 0
        if self.modal.selected >= len(items):
            self.modal.selected = len(items) - 1

    def _open_help_modal(self) -> None:
        self._open_modal(
            ModalState(
                kind="text",
                title="Help",
                lines=[
                    "/help /status /doctor /setup /settings /model <id> /new /reset /exit",
                    "/models opens the model selector, /sessions opens saved sessions",
                    "/think <on|off> /verbose <off|on|full> /reasoning <on|off>",
                    "/usage <off|tokens|full>",
                    "!<command> runs a local shell command after one explicit approval",
                    "Ctrl+O tool output, Ctrl+L model, Ctrl+G agent, Ctrl+P sessions, Ctrl+T thinking",
                    "Type / to open live slash suggestions, use Up/Down to pick, Tab to autocomplete",
                    "Alt+Enter inserts newline when the terminal exposes it; Ctrl+V inserts newline as fallback",
                    "Empty input: Up/Down scroll chat line by line",
                    "PgUp/PgDn scroll chat faster, End jumps back to the bottom",
                ],
            )
        )

    def _open_settings_modal(self) -> None:
        items = [
            ModalItem(
                value="tools",
                label=f"Tool output: {'expanded' if self.tools_expanded else 'collapsed'}",
                search_text="tools output expand collapse",
            ),
            ModalItem(
                value="thinking",
                label=f"Show thinking: {'on' if self.show_thinking else 'off'}",
                search_text="thinking visibility",
            ),
            ModalItem(
                value="verbose",
                label=f"Verbose tool events: {self.verbose_level}",
                search_text="verbose tool events off on full",
            ),
            ModalItem(
                value="reasoning",
                label=f"Reasoning wrapper: {self.reasoning_level}",
                search_text="reasoning wrapper on off",
            ),
            ModalItem(
                value="usage",
                label=f"Usage line: {self.usage_mode}",
                search_text="usage line off tokens full",
            ),
            ModalItem(value="save", label="Save session now", search_text="persist store"),
            ModalItem(value="close", label="Close", search_text="dismiss"),
        ]

        def on_select(value: str) -> None:
            if value == "tools":
                self.tools_expanded = not self.tools_expanded
                self.set_flash(
                    f"tool output {'expanded' if self.tools_expanded else 'collapsed'}",
                    ttl=3.0,
                )
                self._open_settings_modal()
                return
            if value == "thinking":
                self._set_thinking_mode("off" if self.show_thinking else "on", announce=False)
                self._open_settings_modal()
                return
            if value == "verbose":
                next_value = {"off": "on", "on": "full", "full": "off"}[self.verbose_level]
                self._set_verbose_level(next_value, announce=False)
                self._open_settings_modal()
                return
            if value == "reasoning":
                self._set_reasoning_level("off" if self.reasoning_level == "on" else "on", announce=False)
                self._open_settings_modal()
                return
            if value == "usage":
                next_value = {"off": "tokens", "tokens": "full", "full": "off"}[self.usage_mode]
                self._set_usage_mode(next_value, announce=False)
                self._open_settings_modal()
                return
            if value == "save":
                self.agent.save_session()
                self.set_flash("session saved", ttl=3.0)
            self.modal = None

        self._open_modal(
            ModalState(
                kind="menu",
                title="Settings",
                items=items,
                footer_hint="enter select | esc close",
                on_select=on_select,
            )
        )

    def _open_agent_modal(self) -> None:
        def on_select(value: str) -> None:
            self.current_agent_id = value or "default"
            self.set_flash(f"agent set to {self.current_agent_id}", ttl=3.0)

        self._searchable_modal(
            title="Agents",
            items=[
                ModalItem(
                    value="default",
                    label="default",
                    description="local agent",
                    search_text="default local single agent",
                )
            ],
            on_select=on_select,
            footer_hint="search | enter select | esc close",
            max_visible=7,
        )

    def _open_model_modal(self) -> None:
        items = self._discover_model_items()

        def on_select(value: str) -> None:
            self.modal = None
            if value == "__info__":
                self.set_flash("use /model <id> for a manual override", ttl=4.0)
                return
            self.agent.llm.model = value
            self._cached_model_items = None
            self._cached_model_items_at = 0.0
            self.set_flash(f"model set to {value}", ttl=4.0)
            self.add_system(f"model set to {value}")

        self._searchable_modal(
            title="Models",
            items=items,
            on_select=on_select,
            footer_hint="search | enter select | esc clears filter/close",
            max_visible=9,
        )

    def _open_sessions_modal(self) -> None:
        items = [
            ModalItem(
                value="__new__",
                label="New session",
                description="clear current chat",
                search_text="new reset clear",
            )
        ]
        saved = load_saved_sessions(self.saved_session_dir)
        for row in saved:
            items.append(
                ModalItem(
                    value=row["path"],
                    label=row["label"],
                    description=row["description"],
                    search_text=row["path"],
                )
            )

        def on_select(value: str) -> None:
            self.modal = None
            if value == "__new__":
                self._new_session()
                return
            messages = load_conversation_from_session_file(value)
            label = os.path.basename(value).rsplit(".", 1)[0]
            self._replace_chat_with_messages(messages, label=label)

        self._searchable_modal(
            title="Sessions",
            items=items,
            on_select=on_select,
            footer_hint="search recent sessions | enter load | esc clears filter/close",
            max_visible=10,
        )

    def _open_confirm_modal(
        self,
        question: str,
        on_accept: Callable[[], None],
        on_decline: Optional[Callable[[], None]] = None,
    ) -> None:
        def handler(value: str) -> None:
            self.modal = None
            if value == "yes":
                on_accept()
                return
            if on_decline:
                on_decline()

        self._open_modal(
            ModalState(
                kind="menu",
                title="Confirm",
                lines=wrap_text_block(question, 64),
                items=[
                    ModalItem(value="yes", label="Yes"),
                    ModalItem(value="no", label="No"),
                ],
                footer_hint="enter select | y/n quick answer | esc cancel",
                on_select=handler,
            )
        )

    def _process_event(self, event: str, payload: object) -> None:
        if event == "spinner_start":
            label = str(payload or "thinking").strip()
            lowered = label.lower()
            if "elaboro" in lowered or "thinking" in lowered:
                self.set_activity("waiting")
            else:
                self.set_activity(lowered)
            self.status_text = label
            return

        if event == "spinner_stop":
            if not self.worker_busy:
                self.set_activity("idle")
            return

        if event == "assistant_start":
            self.add_entry(ChatEntry(kind="assistant", body=""))
            self.streaming_index = len(self.entries) - 1
            self.set_activity("streaming")
            return

        if event == "assistant_chunk":
            if self.streaming_index is None:
                self.add_entry(ChatEntry(kind="assistant", body=str(payload or "")))
                self.streaming_index = len(self.entries) - 1
            else:
                self.entries[self.streaming_index].body += str(payload or "")
            return

        if event == "assistant_end":
            self.streaming_index = None
            if not self.worker_busy:
                self.set_activity("idle")
            return

        if event == "assistant_full":
            self.add_assistant(str(payload or ""))
            if not self.worker_busy:
                self.set_activity("idle")
            return

        if event == "status":
            raw = str(payload or "")
            self.status_text = raw.strip("[]") if raw.startswith("[") and raw.endswith("]") else raw
            lowered = self.status_text.lower()
            if raw.startswith("[tool:") and raw.endswith("]"):
                tool_name = raw[6:-1].strip()
                self.add_entry(ChatEntry(kind="tool", title=tool_name, state="running"))
                self.active_tool_index = len(self.entries) - 1
            elif raw.startswith("[") and raw.endswith("]"):
                text, kind = normalize_status_event(raw)
                if self.verbose_level in {"on", "full"} or kind == "error":
                    self.add_entry(ChatEntry(kind=kind, body=text))
            elif "thinking" in lowered or "elaboro" in lowered or "compattazione" in lowered:
                self.set_activity("waiting")
            elif lowered in ("bloccato", "permesso negato"):
                self.set_activity("error")
            if "thinking" in lowered or "elaboro" in lowered or "compattazione" in lowered:
                self.set_activity("waiting")
            elif lowered in ("bloccato", "permesso negato"):
                self.set_activity("error")
            return

        if event == "cmd":
            command = str(payload or "")
            title = "local shell" if command.startswith("[local] ") else "shell"
            body = command[8:] if command.startswith("[local] ") else command
            self.add_entry(ChatEntry(kind="tool", title=title, body=body, state="running"))
            self.active_tool_index = len(self.entries) - 1
            self.set_activity("running")
            return

        if event == "tool_call":
            data = payload if isinstance(payload, dict) else {}
            label = str(data.get("label") or "Tool")
            preview = str(data.get("preview") or "")
            self.add_entry(ChatEntry(kind="tool", title=label, body=preview, state="running"))
            self.active_tool_index = len(self.entries) - 1
            self.set_activity("running")
            return

        if event == "output":
            data = payload if isinstance(payload, dict) else {}
            output = sanitize_renderable_text(str(data.get("output", "")))
            is_error = bool(data.get("is_error"))
            if self.active_tool_index is not None and self.active_tool_index < len(self.entries):
                entry = self.entries[self.active_tool_index]
                entry.body = (entry.body + "\n\n" + output).strip()
                entry.state = "error" if is_error else "done"
                self.active_tool_index = None
            else:
                self.add_system(output, kind="system" if not is_error else "error")
            if not self.worker_busy:
                self.set_activity("idle")
            return

        if event == "error":
            self.add_entry(ChatEntry(kind="error", body=str(payload or "")))
            self.set_activity("error")
            self.set_flash(str(payload or ""), ttl=5.0)
            return

        if event == "confirm":
            request = payload if isinstance(payload, ConfirmRequest) else None
            if request:
                self.confirm_queue.append(request)
            return

        if event == "system_text":
            self.add_system(str(payload or ""))
            return

        if event == "subagent_announce":
            self.add_system(str(payload or ""))
            return

        if event == "runtime_event":
            data = payload if isinstance(payload, dict) else {}
            event_name = str(data.get("event", "") or "")
            payload_data = data.get("payload", {}) if isinstance(data.get("payload", {}), dict) else {}
            if event_name.startswith("subagent."):
                label = event_name.split(".", 1)[1]
                subagent_id = payload_data.get("id", "?")
                status = payload_data.get("status", "")
                self.add_entry(ChatEntry(kind="event", body=f"subagent {subagent_id}: {label} ({status})"))
            return

        if event == "task_done":
            data = payload if isinstance(payload, dict) else {}
            task_kind = str(data.get("kind", "") or "")
            if task_kind == "agent" and self.pending_usage_snapshot and self.usage_mode != "off":
                snapshot = self.pending_usage_snapshot if isinstance(self.pending_usage_snapshot, dict) else {}
                before = snapshot.get("summary", {}) if isinstance(snapshot.get("summary", {}), dict) else {}
                started_at = float(snapshot.get("started_at", time.time()) or time.time())
                after = dict(self.agent.session.summary())
                elapsed = max(0, int(time.time() - started_at))
                self.add_system(format_response_usage_line(before, after, elapsed, self.usage_mode))
            self.pending_usage_snapshot = None
            self.worker_busy = False
            self.streaming_index = None
            self.active_tool_index = None
            self.waiting_phrase = None
            if self.activity_status != "error":
                self.set_activity("idle")
            return

    def _process_events(self) -> None:
        while True:
            try:
                event, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break
            self._process_event(event, payload)
        if self.modal is None and self.confirm_queue:
            request = self.confirm_queue.pop(0)

            def decline() -> None:
                request.response.put(False)
                self.set_flash("action blocked", ttl=3.0)

            def accept() -> None:
                request.response.put(True)
                self.set_flash("action approved", ttl=3.0)

            self._open_confirm_modal(request.question, on_accept=accept, on_decline=decline)

    def _parse_slash_buffer(self) -> tuple[str, str, bool]:
        raw = self.input_buffer
        if not raw.startswith("/"):
            return "", "", False
        body = raw[1:]
        match = re.match(r"^(\S+)(?:\s+(.*))?$", body)
        if not match:
            return "", "", False
        name = (match.group(1) or "").lower()
        arg = match.group(2) or ""
        has_arg_section = raw.endswith(" ") or match.group(2) is not None
        return name, arg, has_arg_section

    def _prefixed_command_items(
        self,
        command_name: str,
        items: list[ModalItem],
        arg_prefix: str = "",
    ) -> list[ModalItem]:
        query = (arg_prefix or "").strip().lower()
        scored: list[tuple[tuple[int, int, str], ModalItem]] = []
        for item in items:
            command_value = f"/{command_name} {item.value}".rstrip()
            command_label = f"/{command_name} {item.label}".rstrip()
            candidate = ModalItem(
                value=command_value,
                label=command_label,
                description=item.description,
                search_text=f"{item.search_text} {item.value} {item.label}",
            )
            score = score_modal_item(candidate, query) if query else (0, 0, command_label.lower())
            if score is None:
                continue
            scored.append((score, candidate))
        scored.sort(key=lambda row: row[0])
        return [item for _score, item in scored]

    def _session_completion_items(self) -> list[ModalItem]:
        if self._cached_session_items is not None and (time.time() - self._cached_session_items_at) < 2.0:
            return list(self._cached_session_items)

        items = [
            ModalItem(
                value="new",
                label="new",
                description="start a fresh session",
                search_text="new fresh reset clear",
            )
        ]
        for row in load_saved_sessions(self.saved_session_dir):
            stem = os.path.basename(row["path"]).rsplit(".", 1)[0]
            description = row["label"]
            if row.get("description"):
                description = f"{description} | {row['description']}"
            items.append(
                ModalItem(
                    value=stem,
                    label=stem,
                    description=description,
                    search_text=f"{row['label']} {row['description']} {row['path']}",
                )
            )
        self._cached_session_items = list(items)
        self._cached_session_items_at = time.time()
        return items

    def _toggle_completion_items(self, name: str) -> list[ModalItem]:
        descriptions = {
            "think": {
                "on": "show thinking traces in chat",
                "off": "hide thinking traces in chat",
            },
            "verbose": {
                "off": "hide tool cards in chat",
                "on": "show tool cards without full output",
                "full": "show tool cards and tool output",
            },
            "reasoning": {
                "on": "use the normal reasoning wrapper",
                "off": "force quick mode for this TUI",
            },
            "usage": {
                "off": "hide per-response usage line",
                "tokens": "show compact usage line",
                "full": "show detailed usage line",
            },
        }
        items: list[ModalItem] = []
        values = ("on", "off")
        if name == "verbose":
            values = ("off", "on", "full")
        elif name == "usage":
            values = ("off", "tokens", "full")
        for value in values:
            items.append(
                ModalItem(
                    value=value,
                    label=value,
                    description=descriptions.get(name, {}).get(value, ""),
                    search_text=f"{name} {value}",
                )
            )
        return items

    def _set_thinking_mode(self, value: str, announce: bool = True) -> bool:
        normalized = (value or "").strip().lower()
        if normalized not in {"on", "off"}:
            return False
        self.show_thinking = normalized == "on"
        self.set_flash(f"thinking {normalized}", ttl=3.0)
        if announce:
            self.add_system(f"thinking {normalized}")
        return True

    def _set_verbose_level(self, value: str, announce: bool = True) -> bool:
        normalized = (value or "").strip().lower()
        if normalized not in {"off", "on", "full"}:
            return False
        self.verbose_level = normalized
        self.set_flash(f"verbose {normalized}", ttl=3.0)
        if announce:
            self.add_system(f"verbose {normalized}")
        return True

    def _set_usage_mode(self, value: str, announce: bool = True) -> bool:
        normalized = (value or "").strip().lower()
        if normalized not in {"off", "tokens", "full"}:
            return False
        self.usage_mode = normalized
        self.set_flash(f"usage {normalized}", ttl=3.0)
        if announce:
            self.add_system(f"usage {normalized}")
        return True

    def _set_reasoning_level(self, value: str, announce: bool = True) -> bool:
        normalized = (value or "").strip().lower()
        if normalized not in {"on", "off"}:
            return False
        self.reasoning_level = normalized
        self.set_flash(f"reasoning {normalized}", ttl=3.0)
        if announce:
            self.add_system(f"reasoning {normalized}")
        return True

    def _dynamic_slash_suggestions(self) -> list[ModalItem]:
        raw = self.input_buffer
        if not raw.startswith("/"):
            return []

        name, arg, has_arg_section = self._parse_slash_buffer()
        if not name or not has_arg_section:
            return compute_slash_suggestions(raw)

        if name in ("model", "models"):
            items = [item for item in self._discover_model_items() if item.value != "__info__"]
            return self._prefixed_command_items("model", items, arg)

        if name in ("agent", "agents"):
            items = [
                ModalItem(
                    value="default",
                    label="default",
                    description="local agent",
                    search_text="default local single agent",
                )
            ]
            return self._prefixed_command_items("agent", items, arg)

        if name in ("session", "sessions"):
            return self._prefixed_command_items("session", self._session_completion_items(), arg)

        if name in ("think", "verbose", "reasoning", "usage"):
            return self._prefixed_command_items(name, self._toggle_completion_items(name), arg)

        return []

    def _slash_suggestions(self) -> list[ModalItem]:
        items = self._dynamic_slash_suggestions()
        if not items:
            self.slash_selected = 0
            return []
        if self.slash_selected < 0:
            self.slash_selected = 0
        if self.slash_selected >= len(items):
            self.slash_selected = len(items) - 1
        return items

    def _accept_slash_suggestion(self) -> bool:
        items = self._slash_suggestions()
        if not items:
            return False
        item = items[self.slash_selected]
        replacement = item.value
        command_name = replacement[1:].split(None, 1)[0]
        if command_name in {"model", "agent", "session", "think", "verbose", "reasoning", "usage"} and not replacement.endswith(" "):
            replacement += " "
        self.input_buffer = replacement
        self.cursor_pos = len(self.input_buffer)
        self.input_scroll = 0
        return True

    def submit_text(self, text: str) -> None:
        raw = text.rstrip("\n")
        value = raw.strip()
        self.input_buffer = ""
        self.cursor_pos = 0
        self.input_scroll = 0
        self.history_index = None

        if not value:
            return

        if raw.startswith("!") and raw != "!":
            if self.worker_busy:
                self.set_flash("busy; wait before starting local shell", ttl=3.0)
                return
            if not self.local_shell_enabled:
                self.pending_shell_command = raw[1:]

                def allow() -> None:
                    self.local_shell_enabled = True
                    self.set_flash("local shell enabled for this session", ttl=4.0)
                    self._start_local_shell(self.pending_shell_command)
                    self.pending_shell_command = ""

                def deny() -> None:
                    self.pending_shell_command = ""
                    self.add_system("local shell not enabled")

                self._open_confirm_modal(
                    "Allow local shell commands for this TUI session?\n"
                    "This runs commands on your machine and may delete files or reveal secrets.",
                    on_accept=allow,
                    on_decline=deny,
                )
                return
            self._start_local_shell(raw[1:])
            return

        if value.startswith("/"):
            self._handle_slash_command(value)
            if raw not in self.history:
                self.history.append(raw)
            return

        if self.worker_busy:
            self.set_flash("agent busy; wait for the current run to finish", ttl=3.0)
            return

        self.add_user(value)
        if raw not in self.history:
            self.history.append(raw)
        self.worker_busy = True
        self.set_activity("sending")
        self.pending_usage_snapshot = {
            "started_at": time.time(),
            "summary": dict(self.agent.session.summary()),
        }
        self.task_queue.put(("agent", value))

    def _start_local_shell(self, command: str) -> None:
        self.add_user(f"!{command}")
        self.worker_busy = True
        self.set_activity("running")
        self.task_queue.put(("local_shell", command))

    def _load_session_from_arg(self, arg: str) -> bool:
        key = (arg or "").strip()
        if not key:
            return False
        if key in {"new", "reset"}:
            self._new_session()
            return True

        rows = load_saved_sessions(self.saved_session_dir, limit=64)
        exact = None
        fallback = None
        for row in rows:
            path = row["path"]
            stem = os.path.basename(path).rsplit(".", 1)[0]
            label = row["label"]
            if key in {path, stem, label}:
                exact = row
                break
            item = ModalItem(
                value=path,
                label=stem,
                description=label,
                search_text=f"{label} {row['description']} {path}",
            )
            if score_modal_item(item, key) is not None and fallback is None:
                fallback = row

        target = exact or fallback
        if not target:
            self.add_system(f"session not found: {key}")
            return False

        messages = load_conversation_from_session_file(target["path"])
        label = os.path.basename(target["path"]).rsplit(".", 1)[0]
        self._replace_chat_with_messages(messages, label=label)
        return True

    def _handle_slash_command(self, raw: str) -> None:
        parts = raw[1:].split(None, 1)
        name = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if name in ("help", "commands"):
            self._open_help_modal()
            return
        if name == "status":
            self.add_system(self._status_summary())
            return
        if name == "gateway":
            self.add_system(format_gateway_summary(probe_runtime_gateway()))
            return
        if name == "doctor":
            self.add_system(build_doctor_report(OPENVURP_DIR, self.agent.tools.names()).render())
            return
        if name == "setup":
            self.add_system(
                fix_runtime_issues(
                    OPENVURP_DIR,
                    allowed_telegram_users=list(getattr(config, "TELEGRAM_ALLOWED_USERS", []) or []),
                ).render()
            )
            self.agent.rbac = self.agent.rbac.__class__(MEMORY_DIR)
            self.set_flash("runtime setup applied", ttl=4.0)
            return
        if name in ("settings",):
            self._open_settings_modal()
            return
        if name in ("agent", "agents"):
            if arg and name == "agent":
                self.current_agent_id = arg
                self.set_flash(f"agent set to {self.current_agent_id}", ttl=3.0)
                return
            self._open_agent_modal()
            return
        if name in ("session", "sessions"):
            if arg and name == "session":
                self._load_session_from_arg(arg)
                return
            self._open_sessions_modal()
            return
        if name in ("model", "models"):
            if arg:
                self.agent.llm.model = arg
                self._cached_model_items = None
                self._cached_model_items_at = 0.0
                self.add_system(f"model set to {arg}")
                self.set_flash(f"model set to {arg}", ttl=4.0)
            else:
                self._open_model_modal()
            return
        if name == "think":
            if not arg:
                self.add_system(f"thinking {'on' if self.show_thinking else 'off'}")
                return
            if not self._set_thinking_mode(arg):
                self.add_system("usage: /think <on|off>")
            return
        if name == "verbose":
            if not arg:
                self.add_system(f"verbose {self.verbose_level}")
                return
            if not self._set_verbose_level(arg):
                self.add_system("usage: /verbose <off|on|full>")
            return
        if name == "reasoning":
            if not arg:
                self.add_system(f"reasoning {self.reasoning_level}")
                return
            if not self._set_reasoning_level(arg):
                self.add_system("usage: /reasoning <on|off>")
            return
        if name == "usage":
            if not arg:
                self.add_system(f"usage {self.usage_mode}")
                return
            if not self._set_usage_mode(arg):
                self.add_system("usage: /usage <off|tokens|full>")
            return
        if name in ("new", "reset"):
            self._new_session()
            return
        if name in ("trace",):
            self.add_system(self.agent.get_session_trace() or "(empty trace)")
            return
        if name in ("memory",):
            self.add_system(self._memory_summary())
            return
        if name in ("skills",):
            self.add_system(self._skills_summary())
            return
        if name in ("self",):
            self.add_system(self._self_summary())
            return
        if name == "restart":
            self._trigger_restart("restart", arg or "richiesto da /restart")
            return
        if name == "update":
            self._handle_update_command()
            return
        if name == "dashboard":
            self._start_dashboard(announce=True)
            return
        if name in ("exit", "quit", "q"):
            self.running = False
            return
        self.add_system(f"unknown command: /{name}")

    def _trigger_restart(self, mode: str, reason: str = "") -> None:
        """Ferma la TUI e chiede a main() di rilanciare il processo."""
        self.restart_mode = mode
        self.restart_reason = reason
        self.set_flash(
            "updating…" if mode == "update" else "restarting…", ttl=3.0
        )
        self.running = False

    def _handle_update_command(self) -> None:
        from core import updater
        if not updater.is_git_repo():
            self.add_system("update: non è un repository git, niente da aggiornare")
            return
        self.add_system("update: controllo aggiornamenti…")
        info = updater.check_for_updates(fetch=True)
        if info.get("available"):
            self.add_system(
                f"update: {info['summary']} ({info['local']} → {info['remote']}). "
                "Applico e riavvio…"
            )
            self._trigger_restart("update", "auto-update")
        else:
            self.add_system(f"update: {info.get('summary', 'nessun aggiornamento')}")

    def _poll_restart_sentinel(self) -> None:
        """Onora il sentinel memory/.restart (scritto dal tool request_restart
        dell'agente o dal loop di auto-update): rilancia mantenendo il terminale."""
        now = time.time()
        if now - getattr(self, "_last_sentinel_check", 0.0) < 1.0:
            return
        self._last_sentinel_check = now
        try:
            from core import updater
            if updater.restart_pending():
                reason = updater.consume_restart()
                self._trigger_restart("restart", reason or "request_restart")
        except Exception:
            pass

    def _auto_update_loop(self) -> None:
        """Daemon: quando AUTO_UPDATE è attivo, controlla e applica gli update
        sicuri da solo, poi chiede il riavvio via sentinel. Tutto il lavoro
        pesante (git) sta qui, fuori dal thread di rendering."""
        from core import updater
        interval = max(300, int(getattr(config, "AUTO_UPDATE_INTERVAL", 3600)))
        next_check = time.time() + 60  # non subito all'avvio
        while self.running:
            time.sleep(2)
            if time.time() < next_check or self.worker_busy:
                continue
            next_check = time.time() + interval
            try:
                info = updater.check_for_updates(fetch=True)
                if not info.get("available"):
                    continue
                self.event_queue.put(
                    ("system_text", f"auto-update: {info['summary']}, applico…")
                )
                result = updater.apply_update(smoke_test=True)
                if result.get("ok") and result.get("updated"):
                    self.event_queue.put(
                        ("system_text", f"auto-update: {result['summary']}, riavvio…")
                    )
                    updater.request_restart("auto-update")
                elif result.get("rolled_back"):
                    self.event_queue.put(
                        ("system_text", f"auto-update annullato (rollback): {result.get('error', '')}")
                    )
            except Exception as exc:
                self.event_queue.put(("system_text", f"auto-update errore: {exc}"))

    def _start_dashboard(self, announce: bool = False) -> None:
        """Avvia la dashboard web locale con chat collegata all'agente.

        La chat usa lo stesso `agent_lock` del worker, così la TUI e la
        dashboard non toccano l'agente nello stesso momento."""
        if self._dashboard_server is not None:
            if announce:
                port = getattr(config, "DASHBOARD_PORT", 8420)
                self.add_system(f"dashboard già attiva su http://localhost:{port}")
            return
        try:
            from dashboard import DashboardServer, make_chat_fn
            port = int(getattr(config, "DASHBOARD_PORT", 8420))
            host = str(getattr(config, "DASHBOARD_HOST", "127.0.0.1") or "127.0.0.1")
            token = str(getattr(config, "DASHBOARD_TOKEN", "") or "")
            chat_fn = make_chat_fn(self.agent, self.agent_lock, self.bridge)
            server = DashboardServer(self.agent, port=port, chat_fn=chat_fn,
                                     host=host, token=token)
            threading.Thread(target=server.start, daemon=True, name="dashboard").start()
            self._dashboard_server = server
            if announce:
                url = f"http://localhost:{port}/"
                if server.token:
                    url += f"?token={server.token}"
                self.add_system(f"dashboard attiva su {url} — puoi chattare da lì")
        except Exception as exc:
            self.add_system(f"dashboard non avviata: {exc}")

    def _memory_summary(self) -> str:
        lines = ["memory files:"]
        for root, _dirs, files in os.walk(MEMORY_DIR):
            rel_root = os.path.relpath(root, OPENVURP_DIR)
            for name in sorted(files):
                path = os.path.join(rel_root, name)
                lines.append(f"- {path}")
                if len(lines) >= 20:
                    lines.append("...")
                    return "\n".join(lines)
        return "\n".join(lines)

    def _skills_summary(self) -> str:
        skills_dir = os.path.join(OPENVURP_DIR, "skills")
        if not os.path.isdir(skills_dir):
            return "skills directory not found"
        rows = []
        for name in sorted(os.listdir(skills_dir)):
            path = os.path.join(skills_dir, name)
            if os.path.isdir(path):
                rows.append(f"- {name}")
        return "skills:\n" + ("\n".join(rows) if rows else "(no local skills)")

    def _self_summary(self) -> str:
        return (
            f"name: {self.identity_name}\n"
            f"backend: {self.agent.llm.backend}\n"
            f"model: {self.agent.llm.model}\n"
            f"tools: {len(self.agent.tools.names())}\n"
            f"workspace: {OPENVURP_DIR}"
        )

    def _status_summary(self) -> str:
        ctx_pct = self._context_pct()
        session = self.agent.session.summary()
        gateway_summary = format_gateway_summary(probe_runtime_gateway())
        try:
            budget = self.agent.context_mgr.check_budget(self.agent.messages or [])
        except Exception:
            budget = {"total_tokens": 0, "budget_tokens": 0, "ratio": 0.0}
        summary_line = format_tokens_compact(
            int(budget.get("total_tokens", 0) or 0),
            int(budget.get("budget_tokens", 0) or 0),
        )
        return (
            f"agent {self.current_agent_id} | session {self.session_label}\n"
            f"backend {self.agent.llm.backend} | model {self.agent.llm.model}\n"
            f"think {'on' if self.show_thinking else 'off'} | verbose {self.verbose_level} | reasoning {self.reasoning_level} | usage {self.usage_mode}\n"
            f"{summary_line} | llm {session['llm_calls']} | turns {session['turns']} | tools {session['tool_calls']} | ctx {ctx_pct}%\n"
            f"{gateway_summary}"
        )

    def _context_pct(self) -> int:
        try:
            messages = self.agent.messages or []
            budget = self.agent.context_mgr.check_budget(messages)
            return int(float(budget.get("ratio", 0.0)) * 100)
        except Exception:
            return 0

    def _build_header(self, width: int) -> str:
        agent_label = self.current_agent_id
        header = f"openvurp tui - local runtime - agent {agent_label} - session {self.session_label}"
        return header[: max(0, width - 1)]

    def _build_status(self, width: int) -> str:
        now = time.time()
        if self.flash_until and now > self.flash_until and self.activity_status == "idle":
            self.status_text = f"{self.connection_status} | idle"
            self.flash_until = 0.0

        if self.activity_status != "idle":
            frame = SPINNER_FRAMES[int(now * 8) % len(SPINNER_FRAMES)]
            elapsed = ""
            elapsed_seconds = 0
            if self.activity_started_at is not None:
                elapsed_seconds = int(now - self.activity_started_at)
                if elapsed_seconds < 60:
                    elapsed = f" {elapsed_seconds}s"
                else:
                    elapsed = f" {elapsed_seconds // 60}m {elapsed_seconds % 60}s"
            if self.activity_status == "waiting":
                phrase = self.waiting_phrase or choose_waiting_phrase(None)
                text = f"{frame} {phrase}... •{elapsed} | {self.connection_status}"
            else:
                text = f"{frame} {self.activity_status}{elapsed} | {self.connection_status}"
        else:
            text = self.status_text or f"{self.connection_status} | idle"
        return text[: max(0, width - 1)]

    def _build_footer(self, width: int) -> str:
        session = self.agent.session.summary()
        try:
            budget = self.agent.context_mgr.check_budget(self.agent.messages or [])
        except Exception:
            budget = {"total_tokens": 0, "budget_tokens": 0, "ratio": 0.0}
        footer_parts = [
            f"agent {self.current_agent_id}",
            f"session {self.session_label}",
            f"{self.agent.llm.backend}/{self.agent.llm.model}",
            f"think {'on' if self.show_thinking else 'off'}" if self.show_thinking else None,
            f"verbose {self.verbose_level}" if self.verbose_level != "off" else None,
            "reasoning" if self.reasoning_level == "on" else None,
            format_tokens_compact(
                int(budget.get("total_tokens", 0) or 0),
                int(budget.get("budget_tokens", 0) or 0),
            ),
        ]
        footer = " | ".join(part for part in footer_parts if part)
        return footer[: max(0, width - 1)]

    def _entry_lines(self, entry: ChatEntry, width: int) -> list[tuple[str, int]]:
        lines: list[tuple[str, int]] = []
        if entry.kind == "thinking" and not self.show_thinking:
            return lines

        if entry.kind == "user":
            box_width = max(12, width - 2)
            lines.append(("", 0))
            lines.append((f"╭─ you {'─' * max(0, box_width - 7)}"[:width], 8))
            body_lines = wrap_text_block(entry.body, max(12, width - 4))
            for line in body_lines:
                pad = max(0, box_width - 2 - len(line))
                lines.append((f"│ {line}{' ' * pad}"[:width], 8))
            lines.append((f"╰{'─' * max(0, box_width - 1)}"[:width], 8))
            lines.append(("", 0))
            return lines

        if entry.kind == "assistant":
            lines.append(("", 0))
            for line in wrap_text_block(entry.body, width):
                lines.append((line, 9))
            lines.append(("", 0))
            return lines

        if entry.kind in ("system", "thinking", "error", "event"):
            if entry.kind == "error":
                color = 6
            elif entry.kind == "thinking":
                color = 3
            elif entry.kind == "event":
                color = 5
            else:
                color = 2
            lines.append(("", 0))
            for line in wrap_text_block(entry.body, width):
                lines.append((line, color))
            lines.append(("", 0))
            return lines

        if entry.kind == "tool":
            if self.verbose_level == "off":
                return lines
            title = entry.title or "tool"
            emoji, label = tool_display(title)
            state = entry.state or "running"
            state_label = "running" if state == "running" else "done" if state == "done" else "error"
            header = f"╭─ {emoji} {label}{' (running)' if state == 'running' else ''}"
            color = 5
            if state == "done":
                color = 7
            elif state == "error":
                color = 6
            lines.append(("", 0))
            lines.append((header[:width], color))
            body = sanitize_renderable_text(entry.body or "")
            raw_lines = body.splitlines() if body else []
            if raw_lines:
                arg_line = raw_lines[0]
                body_lines = raw_lines[1:]
            else:
                arg_line = ""
                body_lines = []
            if arg_line:
                for line in wrap_text_block(arg_line, max(12, width - 4), prefix="│ "):
                    lines.append((line[:width], 2))
            if self.verbose_level == "full" and body_lines:
                rendered_body = wrap_text_block("\n".join(body_lines), max(12, width - 4), prefix="│ ")
            elif body_lines:
                rendered_body = ["│ …"]
            else:
                rendered_body = []
            if not self.tools_expanded and len(rendered_body) > TOOL_PREVIEW_LINES:
                rendered_body = rendered_body[:TOOL_PREVIEW_LINES] + ["│ …"]
            for line in rendered_body:
                lines.append((line[:width], color))
            lines.append((f"╰─ {state_label}"[:width], color))
            lines.append(("", 0))
            return lines

        for line in wrap_text_block(entry.body, width):
            lines.append((line, 1))
        lines.append(("", 0))
        return lines

    def _chat_lines(self, width: int) -> list[tuple[str, int]]:
        lines: list[tuple[str, int]] = []
        for entry in self.entries:
            lines.extend(self._entry_lines(entry, width))
        if not lines:
            lines.append(("no messages yet", 2))
        return lines

    def _handle_overlay_key(self, key: int) -> None:
        if not self.modal:
            return

        if self.modal.confirm_request:
            if key in (ord("y"), ord("Y")):
                self.modal.confirm_request.response.put(True)
                self.modal = None
                return
            if key in (ord("n"), ord("N")):
                self.modal.confirm_request.response.put(False)
                self.modal = None
                return

        if key in (27, ord("q")):
            if self.modal.search_enabled and self.modal.filter_text:
                self.modal.filter_text = ""
                self.modal.selected = 0
                return
            modal = self.modal
            self.modal = None
            if modal.on_cancel:
                modal.on_cancel()
            elif modal.confirm_request:
                modal.confirm_request.response.put(False)
            return

        if self.modal.kind == "text":
            self.modal = None
            return

        if self.modal.search_enabled and key in (curses.KEY_BACKSPACE, 127, 8):
            if self.modal.filter_text:
                self.modal.filter_text = self.modal.filter_text[:-1]
                self.modal.selected = 0
            return

        if self.modal.search_enabled and 32 <= key <= 126:
            self.modal.filter_text += chr(key)
            self.modal.selected = 0
            return

        items = self._filtered_modal_items()
        self._ensure_modal_selection()

        if key == curses.KEY_UP:
            self.modal.selected = (self.modal.selected - 1) % max(1, len(items))
            return
        if key == curses.KEY_DOWN:
            self.modal.selected = (self.modal.selected + 1) % max(1, len(items))
            return
        if key == curses.KEY_PPAGE:
            self.modal.selected = max(0, self.modal.selected - max(1, self.modal.max_visible - 1))
            return
        if key == curses.KEY_NPAGE:
            self.modal.selected = min(
                max(0, len(items) - 1),
                self.modal.selected + max(1, self.modal.max_visible - 1),
            )
            return
        if key in (10, 13):
            if not items:
                self.modal = None
                return
            item = items[self.modal.selected]
            callback = self.modal.on_select
            self.modal = None
            if callback:
                callback(item.value)
            return

    def _move_history(self, direction: int) -> None:
        if not self.history:
            return
        if self.history_index is None:
            self.history_index = len(self.history) - 1 if direction < 0 else None
        else:
            self.history_index += direction
            if self.history_index < 0:
                self.history_index = 0
            if self.history_index >= len(self.history):
                self.history_index = None
        if self.history_index is None:
            self.input_buffer = ""
        else:
            self.input_buffer = self.history[self.history_index]
        self.cursor_pos = len(self.input_buffer)
        self.slash_selected = 0

    def _insert_text(self, text: str) -> None:
        """Inserisce testo (anche multilinea) al cursore."""
        if not text:
            return
        self.input_buffer = (
            self.input_buffer[: self.cursor_pos] + text + self.input_buffer[self.cursor_pos :]
        )
        self.cursor_pos += len(text)
        self.slash_selected = 0
        self.history_index = None

    def _getch_blocking(self, stdscr, timeout: float = 0.3) -> int:
        """getch con attesa breve (lo stdscr è in nodelay) per leggere sequenze."""
        end = time.time() + timeout
        while time.time() < end:
            c = stdscr.getch()
            if c != -1:
                return c
            time.sleep(0.004)
        return -1

    def _consume_bracketed_paste(self, stdscr) -> Optional[str]:
        """Già letti ESC e '['. Se è un bracketed paste (``200~`` … ``201~``)
        consuma tutto il blocco e ritorna il testo; altrimenti None."""
        head = [self._getch_blocking(stdscr, 0.2) for _ in range(4)]
        if head != [ord("2"), ord("0"), ord("0"), ord("~")]:
            return None
        codes: list[int] = []
        tail: list[int] = []
        state = 0  # 0 testo, 1 visto ESC, 2 visto ESC[
        deadline = time.time() + 8.0
        while time.time() < deadline:
            c = self._getch_blocking(stdscr, 0.3)
            if c == -1:
                continue
            if state == 0:
                if c == 27:
                    state = 1
                else:
                    codes.append(c)
            elif state == 1:
                if c == ord("["):
                    state, tail = 2, []
                else:
                    codes.append(27)
                    if c == 27:
                        state = 1
                    else:
                        codes.append(c)
                        state = 0
            else:  # state == 2
                tail.append(c)
                if tail == [ord("2"), ord("0"), ord("1"), ord("~")]:
                    break
                if len(tail) >= 4:
                    codes.extend([27, ord("[")] + tail)
                    state, tail = 0, []
        return decode_paste_codes(codes)

    def _handle_normal_key(self, key: int) -> None:
        slash_items = self._slash_suggestions()
        if key == curses.KEY_RESIZE:
            # Allinea subito le dimensioni note a curses così il prossimo
            # _render usa la geometria nuova senza un frame di ritardo.
            try:
                curses.update_lines_cols()
            except (AttributeError, curses.error):
                pass
            return
        if key == curses.KEY_PPAGE:
            self._scroll_chat(CHAT_SCROLL_STEP)
            return
        if key == curses.KEY_NPAGE:
            self._scroll_chat(-CHAT_SCROLL_STEP)
            return
        if key == curses.KEY_END:
            self._scroll_chat(-10_000)
            return
        if key == curses.KEY_UP:
            if slash_items:
                self.slash_selected = (self.slash_selected - 1) % max(1, len(slash_items))
                return
            if not self.input_buffer or self.scroll_offset > 0:
                self._scroll_chat(1)
                return
            self._move_history(-1)
            return
        if key == curses.KEY_DOWN:
            if slash_items:
                self.slash_selected = (self.slash_selected + 1) % max(1, len(slash_items))
                return
            if self.scroll_offset > 0 and not self.input_buffer:
                self._scroll_chat(-1)
                return
            self._move_history(1)
            return
        if key == curses.KEY_LEFT:
            self.cursor_pos = max(0, self.cursor_pos - 1)
            return
        if key == curses.KEY_RIGHT:
            self.cursor_pos = min(len(self.input_buffer), self.cursor_pos + 1)
            return
        if key in (ALT_ENTER_KEY, 22):
            self.input_buffer = (
                self.input_buffer[: self.cursor_pos] + "\n" + self.input_buffer[self.cursor_pos :]
            )
            self.cursor_pos += 1
            self.slash_selected = 0
            return
        if key == 9:
            if self._accept_slash_suggestion():
                return
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if self.cursor_pos > 0:
                self.input_buffer = (
                    self.input_buffer[: self.cursor_pos - 1] + self.input_buffer[self.cursor_pos :]
                )
                self.cursor_pos -= 1
                self.slash_selected = 0
            return
        if key == curses.KEY_DC:
            if self.cursor_pos < len(self.input_buffer):
                self.input_buffer = (
                    self.input_buffer[: self.cursor_pos] + self.input_buffer[self.cursor_pos + 1 :]
                )
                self.slash_selected = 0
            return
        if key in (10, 13):
            self.submit_text(self.input_buffer)
            return
        if key == 3:
            now = time.time()
            if self.input_buffer:
                self.input_buffer = ""
                self.cursor_pos = 0
                self.slash_selected = 0
                self.set_flash("cleared input; press ctrl+c again to exit", ttl=2.0)
                self.last_ctrl_c_at = now
                return
            if now - self.last_ctrl_c_at <= 1.0:
                self.running = False
                return
            self.last_ctrl_c_at = now
            self.set_flash("press ctrl+c again to exit", ttl=2.0)
            return
        if key == 4:
            if not self.input_buffer:
                self.running = False
            return
        if key == 7:
            self._open_agent_modal()
            return
        if key == 12:
            self._open_model_modal()
            return
        if key == 15:
            self.tools_expanded = not self.tools_expanded
            self.set_flash(
                f"tools {'expanded' if self.tools_expanded else 'collapsed'}",
                ttl=2.0,
            )
            return
        if key == 16:
            self._open_sessions_modal()
            return
        if key == 20:
            self._set_thinking_mode("off" if self.show_thinking else "on", announce=False)
            return
        if key == 27:
            if self.input_buffer:
                self.input_buffer = ""
                self.cursor_pos = 0
                self.slash_selected = 0
            return

        if 32 <= key <= 126:
            ch = chr(key)
            self.input_buffer = (
                self.input_buffer[: self.cursor_pos] + ch + self.input_buffer[self.cursor_pos :]
            )
            self.cursor_pos += 1
            self.slash_selected = 0

    def _render_overlay(self, stdscr: "curses._CursesWindow", height: int, width: int) -> None:
        if not self.modal:
            return

        items = self._filtered_modal_items()
        self._ensure_modal_selection()

        content_lines = list(self.modal.lines)
        if self.modal.search_enabled:
            content_lines.append(f"search: {self.modal.filter_text}")
        for item in items[: min(len(items), self.modal.max_visible)]:
            label = item.label
            if item.description:
                label = f"{label} - {item.description}"
            content_lines.append(label)
        if self.modal.footer_hint:
            content_lines.append(self.modal.footer_hint)

        box_width = min(
            max(52, max((len(line) for line in content_lines), default=20) + 4),
            width - 4,
        )
        rows_needed = len(self.modal.lines) + (2 if self.modal.search_enabled else 0)
        rows_needed += min(max(1, len(items)), self.modal.max_visible) * (
            2 if box_width >= MODAL_DESCRIPTION_MIN_WIDTH else 1
        )
        rows_needed += 3
        if self.modal.footer_hint:
            rows_needed += 1
        box_height = min(max(9, rows_needed), height - 4)
        top = max(1, (height - box_height) // 2)
        left = max(2, (width - box_width) // 2)
        win = stdscr.derwin(box_height, box_width, top, left)
        win.erase()
        win.bkgd(" ", curses.color_pair(1))
        win.box()
        title = f" {self.modal.title} "
        try:
            win.addnstr(0, max(2, (box_width - len(title)) // 2), title, box_width - 4, curses.color_pair(1) | curses.A_BOLD)
        except curses.error:
            pass

        row = 2
        for line in self.modal.lines:
            try:
                win.addnstr(row, 2, line, box_width - 4, curses.color_pair(3))
            except curses.error:
                pass
            row += 1

        if self.modal.search_enabled:
            prompt = "search: "
            try:
                win.addnstr(row, 2, prompt, box_width - 4, curses.color_pair(5))
                win.addnstr(
                    row,
                    2 + len(prompt),
                    self.modal.filter_text or "",
                    box_width - 4 - len(prompt),
                    curses.color_pair(2),
                )
            except curses.error:
                pass
            row += 1
            try:
                win.hline(row, 1, "-", box_width - 2)
            except curses.error:
                pass
            row += 1

        if not items:
            try:
                win.addnstr(row, 2, "no matches", box_width - 4, curses.color_pair(2) | curses.A_DIM)
            except curses.error:
                pass
            row += 1
        else:
            visible_rows = max(1, box_height - row - 2 - (1 if self.modal.footer_hint else 0))
            per_item = 2 if box_width >= MODAL_DESCRIPTION_MIN_WIDTH else 1
            max_visible = max(1, min(self.modal.max_visible, visible_rows // per_item))
            start_index = 0
            if len(items) > max_visible:
                start_index = max(
                    0,
                    min(
                        self.modal.selected - max_visible // 2,
                        len(items) - max_visible,
                    ),
                )
            end_index = min(len(items), start_index + max_visible)

            for idx in range(start_index, end_index):
                item = items[idx]
                attr = curses.color_pair(3)
                prefix = "  "
                if idx == self.modal.selected:
                    attr = curses.color_pair(4)
                    prefix = "> "
                label = prefix + item.label
                try:
                    win.addnstr(row, 2, label, box_width - 4, attr)
                except curses.error:
                    pass
                row += 1
                if box_width >= MODAL_DESCRIPTION_MIN_WIDTH and item.description and row < box_height - 2:
                    try:
                        win.addnstr(
                            row,
                            4,
                            item.description,
                            box_width - 6,
                            curses.color_pair(2) | curses.A_DIM,
                        )
                    except curses.error:
                        pass
                    row += 1

            if len(items) > max_visible and row < box_height - 1:
                scroll_label = f"{self.modal.selected + 1}/{len(items)}"
                try:
                    win.addnstr(row, box_width - len(scroll_label) - 3, scroll_label, len(scroll_label), curses.color_pair(2))
                except curses.error:
                    pass
                row += 1

        if self.modal.footer_hint and row < box_height - 1:
            try:
                win.addnstr(
                    box_height - 2,
                    2,
                    self.modal.footer_hint,
                    box_width - 4,
                    curses.color_pair(2) | curses.A_DIM,
                )
            except curses.error:
                pass

        win.noutrefresh()

    def _render(self, stdscr: "curses._CursesWindow") -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height < 10 or width < 40:
            stdscr.addnstr(0, 0, "terminal too small for the TUI", max(0, width - 1))
            stdscr.noutrefresh()
            curses.doupdate()
            return

        header = self._build_header(width)
        status = self._build_status(width)
        footer = self._build_footer(width)
        slash_items = [] if self.modal is not None else self._slash_suggestions()
        slash_box_height = min(6, len(slash_items) + 1) if slash_items else 0
        editor_width = max(1, width - 3)
        input_lines, cursor_line, cursor_col = layout_editor_text(
            self.input_buffer,
            self.cursor_pos,
            editor_width,
        )
        input_height = max(1, min(MAX_INPUT_LINES, len(input_lines), height - 6))
        input_start_line = max(0, cursor_line - input_height + 1)
        input_visible = input_lines[input_start_line : input_start_line + input_height]

        stdscr.addnstr(0, 0, header, width - 1, curses.color_pair(1) | curses.A_BOLD)

        chat_top = 1
        bottom_reserved = 3 + slash_box_height + input_height
        chat_height = max(1, height - bottom_reserved)
        lines = self._chat_lines(width - 1)
        max_start = max(0, len(lines) - chat_height)
        start = max(0, max_start - self.scroll_offset)
        end = min(len(lines), start + chat_height)
        visible = lines[start:end]
        for idx, (line, color) in enumerate(visible):
            attr = curses.A_NORMAL
            if color == 2:
                attr = curses.color_pair(2) | curses.A_DIM
            elif color == 3:
                attr = curses.color_pair(3)
            elif color == 4:
                attr = curses.color_pair(4) | curses.A_BOLD
            elif color == 5:
                attr = curses.color_pair(5)
            elif color == 6:
                attr = curses.color_pair(6)
            elif color == 7:
                attr = curses.color_pair(7)
            elif color == 8:
                attr = curses.color_pair(8)
            elif color == 9:
                attr = curses.color_pair(9)
            try:
                stdscr.addnstr(chat_top + idx, 0, line, width - 1, attr)
            except curses.error:
                pass

        divider_y = height - (3 + slash_box_height + input_height)
        status_y = divider_y + 1
        footer_y = divider_y + 2
        suggestions_top = footer_y + 1
        input_top = height - input_height

        stdscr.hline(divider_y, 0, "-", width - 1)
        if start > 0 or end < len(lines):
            view_label = f" view {start + 1}-{end}/{len(lines)} "
            try:
                stdscr.addnstr(
                    divider_y,
                    max(0, width - len(view_label) - 1),
                    view_label,
                    len(view_label),
                    curses.color_pair(2) | curses.A_DIM,
                )
            except curses.error:
                pass
        stdscr.addnstr(status_y, 0, status, width - 1, curses.color_pair(3))
        stdscr.addnstr(footer_y, 0, footer, width - 1, curses.color_pair(2))

        if slash_items:
            try:
                stdscr.addnstr(
                    suggestions_top,
                    0,
                    "slash autocomplete",
                    width - 1,
                    curses.color_pair(5) | curses.A_BOLD,
                )
            except curses.error:
                pass
            max_visible = max(1, slash_box_height - 1)
            start_index = 0
            if len(slash_items) > max_visible:
                start_index = max(
                    0,
                    min(self.slash_selected - max_visible // 2, len(slash_items) - max_visible),
                )
            end_index = min(len(slash_items), start_index + max_visible)
            for idx in range(start_index, end_index):
                item = slash_items[idx]
                row = suggestions_top + 1 + (idx - start_index)
                label = item.label
                if item.description:
                    label = f"{label} - {item.description}"
                attr = curses.color_pair(2)
                if idx == self.slash_selected:
                    attr = curses.color_pair(4) | curses.A_BOLD
                try:
                    stdscr.addnstr(row, 0, label, width - 1, attr)
                except curses.error:
                    pass

        for idx, line in enumerate(input_visible):
            row = input_top + idx
            prefix = "> " if idx == 0 else "  "
            try:
                stdscr.addnstr(row, 0, prefix, width - 1, curses.color_pair(1) | curses.A_BOLD)
                stdscr.addnstr(row, len(prefix), line, width - len(prefix) - 1)
            except curses.error:
                pass

        if self.modal is None:
            visible_cursor_line = max(0, min(input_height - 1, cursor_line - input_start_line))
            cursor_y = input_top + visible_cursor_line
            cursor_x = 2 + cursor_col
            try:
                stdscr.move(min(height - 1, cursor_y), min(width - 1, cursor_x))
            except curses.error:
                pass

        self._render_overlay(stdscr, height, width)

        stdscr.noutrefresh()
        curses.doupdate()

    def run(self, stdscr: "curses._CursesWindow") -> None:
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        curses.use_default_colors()
        curses.start_color()
        curses.init_pair(1, curses.COLOR_YELLOW, -1)
        curses.init_pair(2, curses.COLOR_WHITE, -1)
        curses.init_pair(3, curses.COLOR_CYAN, -1)
        curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_YELLOW)
        curses.init_pair(5, curses.COLOR_YELLOW, -1)
        curses.init_pair(6, curses.COLOR_RED, -1)
        curses.init_pair(7, curses.COLOR_GREEN, -1)
        curses.init_pair(8, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(9, curses.COLOR_WHITE, -1)

        stdscr.nodelay(True)
        stdscr.keypad(True)
        pending_key = -1

        # Bracketed paste: il terminale racchiude il testo incollato tra
        # ESC[200~ ed ESC[201~, così lo inseriamo come testo multilinea invece
        # di interpretare ogni newline come Invio.
        import sys as _sys
        try:
            os.write(_sys.stdout.fileno(), b"\033[?2004h")
        except Exception:
            pass

        while self.running:
            self._process_events()
            self._poll_restart_sentinel()
            self._render(stdscr)
            try:
                if pending_key != -1:
                    key = pending_key
                    pending_key = -1
                else:
                    key = stdscr.getch()
            except KeyboardInterrupt:
                break
            if key == -1:
                time.sleep(0.05)
                continue
            if self.modal is None and key == 27:
                try:
                    next_key = stdscr.getch()
                except KeyboardInterrupt:
                    break
                if next_key in (10, 13):
                    key = ALT_ENTER_KEY
                elif next_key == ord("["):
                    paste = self._consume_bracketed_paste(stdscr)
                    if paste:
                        self._insert_text(paste)
                    continue
                elif next_key != -1:
                    pending_key = next_key
            if self.modal is not None:
                self._handle_overlay_key(key)
            else:
                self._handle_normal_key(key)

        try:
            os.write(_sys.stdout.fileno(), b"\033[?2004l")
        except Exception:
            pass
        try:
            self.agent.save_session()
            self.agent.cleanup()
        except Exception:
            pass
        self.task_queue.put(None)


def main() -> int:
    args = parse_args()
    if not os.isatty(0) or not os.isatty(1):
        print("This TUI needs an interactive terminal.")
        return 1

    # Onboarding guidato: alla prima apertura (o con --setup) niente .env a mano.
    # Gira PRIMA di curses, in terminale normale.
    try:
        from core.setup_wizard import run_wizard
        if not run_wizard(force=getattr(args, "setup", False)):
            return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # il setup non deve mai impedire l'avvio
        print(f"[setup saltato: {exc}]")

    app = ExperimentalTUI(args)
    try:
        curses.wrapper(app.run)
    except KeyboardInterrupt:
        return 130

    # Riavvio "a caldo": curses ha già ripristinato il terminale. Applichiamo
    # l'eventuale update e ri-eseguiamo il processo nello STESSO terminale, così
    # il nuovo codice è caricato senza che tu debba chiudere e riaprire nulla.
    if getattr(app, "restart_mode", None):
        from core import updater
        if app.restart_mode == "update":
            print("↻ update in corso…")
            result = updater.apply_update(smoke_test=True)
            if result.get("ok") and result.get("updated"):
                print(f"  ✓ {result['summary']}")
            elif result.get("rolled_back"):
                print(f"  ⚠ update annullato (rollback): {result.get('error', '')}")
            elif not result.get("ok"):
                print(f"  ⚠ update non applicato: {result.get('error', '')}")
        print(f"↻ riavvio… ({app.restart_reason or app.restart_mode})")
        try:
            updater.restart_in_place()
        except Exception as exc:
            print(f"riavvio fallito: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
