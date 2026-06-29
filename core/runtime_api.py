"""
openvurp Core — Runtime API

Gateway esterno locale che legge stato durevole dal workspace.
"""

from __future__ import annotations

import json
import os
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import config as cfg

from core.memory import MemoryManager
from core.plugins import PluginManager
from core.session_store import SessionStore


def _tail_jsonl(path: str, limit: int = 50) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()[-max(1, int(limit or 50)):]
    except Exception:
        return []
    items = []
    for line in lines:
        try:
            items.append(json.loads(line))
        except Exception:
            continue
    return items


def _load_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def collect_runtime_overview(workspace_dir: str) -> dict:
    memory_dir = os.path.join(workspace_dir, "memory")
    gateway_log = os.path.join(memory_dir, "runtime", "gateway_events.jsonl")
    session_store = SessionStore(memory_dir)
    return {
        "workspace": workspace_dir,
        "backend": getattr(cfg, "LLM_BACKEND", "?"),
        "model": getattr(cfg, "LLM_MODEL", "?"),
        "gateway_host": getattr(cfg, "GATEWAY_HOST", "127.0.0.1"),
        "gateway_port": int(getattr(cfg, "GATEWAY_PORT", 8421) or 8421),
        "gateway_enabled": bool(getattr(cfg, "GATEWAY_ENABLED", False)),
        "event_log": gateway_log,
        "event_count_estimate": len(_tail_jsonl(gateway_log, limit=200)),
        "sessions": len(session_store.list_snapshots()),
    }


def collect_memory_overview(workspace_dir: str) -> dict:
    memory_dir = os.path.join(workspace_dir, "memory")
    memory = MemoryManager(memory_dir)
    return {
        "stats": memory.stats(),
        "profile": memory.get_profile(),
        "patterns": memory.get_patterns(),
    }


def collect_plugin_list(workspace_dir: str) -> list[dict]:
    manager = PluginManager(os.path.join(workspace_dir, "plugins"))
    return manager.list_plugins()


def collect_saved_sessions(workspace_dir: str, limit: int = 20) -> list[dict]:
    sessions_dir = os.path.join(workspace_dir, "memory", "sessions")
    sessions = []
    if os.path.exists(sessions_dir):
        for name in sorted(os.listdir(sessions_dir), reverse=True):
            if not name.endswith(".json"):
                continue
            data = _load_json(os.path.join(sessions_dir, name))
            if data:
                sessions.append(data)
            if len(sessions) >= max(1, int(limit or 20)):
                break
    return sessions


def collect_session_list(workspace_dir: str) -> list[dict]:
    return SessionStore(os.path.join(workspace_dir, "memory")).list_snapshots()


def collect_session_detail(workspace_dir: str, key: str) -> dict | None:
    return SessionStore(os.path.join(workspace_dir, "memory")).get_snapshot(key)


def collect_subagent_runs(workspace_dir: str, limit: int = 50) -> list[dict]:
    runs_dir = os.path.join(workspace_dir, "memory", "subagents", "runs")
    if not os.path.exists(runs_dir):
        return []
    items = []
    for name in sorted(os.listdir(runs_dir), reverse=True):
        if not name.endswith(".json"):
            continue
        data = _load_json(os.path.join(runs_dir, name))
        if data:
            items.append(data)
        if len(items) >= max(1, int(limit or 50)):
            break
    items.sort(key=lambda item: item.get("started_at", 0), reverse=True)
    return items


def collect_gateway_events(workspace_dir: str, limit: int = 50) -> list[dict]:
    path = os.path.join(workspace_dir, "memory", "runtime", "gateway_events.jsonl")
    return _tail_jsonl(path, limit=limit)


class RuntimeAPIHandler(BaseHTTPRequestHandler):
    workspace_dir = ""

    def log_message(self, format, *args):
        return None

    def _json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, payload: dict, event_name: str = "message") -> None:
        data = json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"event: {event_name}\n".encode("utf-8"))
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _start_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        workspace_dir = RuntimeAPIHandler.workspace_dir

        if path in {"/", "/index.html"}:
            return self._html(_runtime_index_html())
        if path == "/health":
            return self._json({"ok": True, "service": "runtime-gateway"})
        if path == "/api/runtime":
            return self._json(collect_runtime_overview(workspace_dir))
        if path == "/api/memory":
            return self._json(collect_memory_overview(workspace_dir))
        if path == "/api/plugins":
            return self._json({"plugins": collect_plugin_list(workspace_dir)})
        if path == "/api/events":
            limit = int((query.get("limit", ["50"])[0] or "50"))
            return self._json({"events": collect_gateway_events(workspace_dir, limit=limit)})
        if path == "/api/events/stream":
            return self._stream_events(workspace_dir)
        if path == "/api/sessions":
            return self._json({"sessions": collect_session_list(workspace_dir)})
        if path == "/api/saved-sessions":
            limit = int((query.get("limit", ["20"])[0] or "20"))
            return self._json({"sessions": collect_saved_sessions(workspace_dir, limit=limit)})
        if path == "/api/session":
            key = str((query.get("key", [""])[0] or "")).strip()
            if not key:
                return self._json({"error": "missing key"}, status=400)
            data = collect_session_detail(workspace_dir, key)
            if not data:
                return self._json({"error": "not found"}, status=404)
            return self._json(data)
        if path == "/api/subagents":
            limit = int((query.get("limit", ["50"])[0] or "50"))
            return self._json({"subagents": collect_subagent_runs(workspace_dir, limit=limit)})
        return self._json({"error": "not found"}, status=404)

    def _stream_events(self, workspace_dir: str) -> None:
        path = os.path.join(workspace_dir, "memory", "runtime", "gateway_events.jsonl")
        self._start_sse()
        for item in collect_gateway_events(workspace_dir, limit=20):
            self._sse(item, event_name=str(item.get("event", "event")))
        start = time.time()
        position = 0
        try:
            if os.path.exists(path):
                position = os.path.getsize(path)
            while time.time() - start < 30:
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as handle:
                        handle.seek(position)
                        while True:
                            line = handle.readline()
                            if not line:
                                break
                            position = handle.tell()
                            try:
                                item = json.loads(line)
                            except Exception:
                                continue
                            self._sse(item, event_name=str(item.get("event", "event")))
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return None
        return None


def _runtime_index_html() -> str:
    return """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>openvurp Runtime Gateway</title>
<style>
body { font-family: ui-monospace, SFMono-Regular, monospace; background:#0b1220; color:#d9e2f1; padding:24px; }
h1 { margin-bottom: 8px; }
a { color:#86b7ff; }
code { background:#182235; padding:2px 6px; border-radius:6px; }
ul { line-height:1.8; }
</style>
</head>
<body>
<h1>openvurp Runtime Gateway</h1>
<p>API locale per stato runtime, sessioni route-bound, eventi gateway e subagent.</p>
<ul>
<li><a href="/health">/health</a></li>
<li><a href="/api/runtime">/api/runtime</a></li>
<li><a href="/api/memory">/api/memory</a></li>
<li><a href="/api/plugins">/api/plugins</a></li>
<li><a href="/api/events">/api/events</a></li>
<li><a href="/api/events/stream">/api/events/stream</a></li>
<li><a href="/api/sessions">/api/sessions</a></li>
<li><a href="/api/saved-sessions">/api/saved-sessions</a></li>
<li><a href="/api/subagents">/api/subagents</a></li>
</ul>
</body>
</html>"""


class RuntimeAPIServer:
    def __init__(self, workspace_dir: str, host: str = "127.0.0.1", port: int = 8421):
        self.workspace_dir = workspace_dir
        self.host = host
        self.port = port
        RuntimeAPIHandler.workspace_dir = workspace_dir
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> None:
        self._server = ThreadingHTTPServer((self.host, self.port), RuntimeAPIHandler)
        self._server.serve_forever()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
