"""
openvurp Dashboard — Web UI

Dashboard separata dal runtime agent: legge dallo stato durevole/gateway API.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time as _time
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

from core.runtime_api import (
    collect_gateway_events,
    collect_memory_overview,
    collect_plugin_list,
    collect_runtime_overview,
    collect_saved_sessions,
    collect_session_list,
    collect_subagent_runs,
)


OPENVURP_DIR = os.path.dirname(os.path.abspath(__file__))


class CaptureUI:
    """Avvolge la UI reale dell'host e cattura SOLO il testo della risposta
    dell'assistente. Tutto il resto (status, tool, spinner) è delegato alla UI
    reale, così l'attività resta visibile nell'host (TUI/CLI). Funziona con
    qualunque host perché non assume nulla sull'interfaccia della UI reale."""

    def __init__(self, real_ui):
        self.__dict__["_real"] = real_ui
        self.__dict__["response_text"] = ""
        self.__dict__["_capturing"] = False
        self.__dict__["steps"] = []  # passaggi (cosa fa) per mostrarli in chat

    def _step(self, kind, text):
        t = str(text or "").strip()
        if t:
            self.__dict__["steps"].append({"kind": kind, "text": t[:160]})

    def show_cmd(self, command):
        self._step("shell", command)

    def show_tool(self, name, args=None):
        self._step("tool", name)

    def status(self, text):
        self._step("status", text)

    def start_response(self):
        self.__dict__["_capturing"] = True
        self.__dict__["response_text"] = ""

    def end_response(self):
        self.__dict__["_capturing"] = False

    def stream_text(self, text):
        if self.__dict__["_capturing"]:
            self.__dict__["response_text"] += str(text)

    def stream_token(self, text):
        # Streaming live: l'agente usa stream_token, non stream_text.
        if self.__dict__["_capturing"]:
            self.__dict__["response_text"] += str(text)

    def openvurp_say(self, text):
        # Risposta non-streamata: catturala (come fa il collector di Telegram).
        self.__dict__["response_text"] += str(text)

    def __getattr__(self, name):
        # tutto ciò che non sovrascriviamo va alla UI reale
        return getattr(self.__dict__["_real"], name)


def make_chat_fn(agent, lock, real_ui):
    """Costruisce un chat_fn thread-safe per la dashboard.

    Serializza l'accesso all'agente con `lock` (lo stesso che usa l'host per i
    suoi turni), scambia temporaneamente la UI per catturare la risposta e la
    ripristina. Stesso meccanismo collaudato del canale Telegram.
    """
    def chat_fn(message: str) -> dict:
        with lock:
            capture = CaptureUI(real_ui)
            old_ui = agent.ui
            agent.ui = capture
            try:
                agent.run(message, source="dashboard", sender="dashboard",
                           actor_id="cli_owner")
                try:
                    agent.session.save()
                except Exception:
                    pass
            finally:
                agent.ui = old_ui
            return {
                "reply": capture.response_text.strip() or "(no reply)",
                "steps": capture.steps,
            }
    return chat_fn


class DashboardHandler(BaseHTTPRequestHandler):
    workspace_dir = OPENVURP_DIR
    chat_fn = None  # impostato da DashboardServer se l'host fornisce la chat
    token = ""      # se non vuoto, ogni richiesta deve presentarlo
    _chat_hits: deque = deque()        # rate-limit: timestamp dei messaggi recenti
    _rl_lock = threading.Lock()
    RATE_LIMIT = 30                    # max messaggi chat / minuto

    def log_message(self, format, *args):
        return None

    def _presented_token(self) -> str:
        # da query ?token=, header X-Dashboard-Token, o cookie ovtok
        try:
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            if q.get("token"):
                return q["token"][0]
        except Exception:
            pass
        hv = self.headers.get("X-Dashboard-Token")
        if hv:
            return hv
        cookie = self.headers.get("Cookie", "") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "ovtok":
                return v
        return ""

    def _authed(self) -> bool:
        if not DashboardHandler.token:
            return True
        import hmac
        return hmac.compare_digest(self._presented_token(), DashboardHandler.token)

    def _json_response(self, data, status: int = 200):
        body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        workspace_dir = DashboardHandler.workspace_dir

        # Branding pubblico (non sensibile): logo + favicon senza auth
        if path in {"/favicon.ico", "/favicon.png"}:
            return self._serve_file("dashboard/favicon.png", "image/png")
        if path == "/octopus.png":
            return self._serve_file("dashboard/octopus.png", "image/png")
        if path in {"/openvurp.jpg", "/logo"}:
            return self._serve_logo()

        if not self._authed():
            if path in {"/", "/index.html"}:
                return self._serve_unauthorized()
            return self.send_error(401, "Unauthorized")

        if path in {"/", "/index.html"}:
            return self._serve_html()
        if path == "/api/stream":
            return self._serve_stream()
        if path == "/api/status":
            runtime = collect_runtime_overview(workspace_dir)
            snapshots = collect_session_list(workspace_dir)
            current = snapshots[0] if snapshots else None
            return self._json_response({
                "runtime": runtime,
                "current_session": current,
            })
        if path == "/api/memory":
            return self._json_response(collect_memory_overview(workspace_dir))
        if path == "/api/sessions":
            return self._json_response({
                "route_sessions": collect_session_list(workspace_dir),
                "saved_sessions": collect_saved_sessions(workspace_dir, limit=20),
            })
        if path == "/api/plugins":
            return self._json_response({"plugins": collect_plugin_list(workspace_dir)})
        if path == "/api/events":
            return self._json_response({"events": collect_gateway_events(workspace_dir, limit=40)})
        if path == "/api/subagents":
            return self._json_response({"subagents": collect_subagent_runs(workspace_dir, limit=20)})
        if path == "/api/chat":
            # GET: dice solo se la chat è disponibile su questo host
            return self._json_response({"available": DashboardHandler.chat_fn is not None})
        self.send_error(404)

    def do_POST(self):
        if not self._authed():
            return self.send_error(401, "Unauthorized")
        path = urlparse(self.path).path
        if path != "/api/chat":
            return self.send_error(404)
        chat_fn = DashboardHandler.chat_fn
        if chat_fn is None:
            return self._json_response(
                {"error": "chat non disponibile: avvia la dashboard dall'host con l'agente"},
                status=503,
            )
        # rate-limit: anche col token, evita raffiche/DoS sulla chat
        now = _time.time()
        with DashboardHandler._rl_lock:
            hits = DashboardHandler._chat_hits
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= DashboardHandler.RATE_LIMIT:
                return self._json_response(
                    {"error": "rate limit: troppi messaggi, rallenta."}, status=429)
            hits.append(now)
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return self._json_response({"error": "json non valido"}, status=400)
        message = str(data.get("message", "")).strip()
        if not message:
            return self._json_response({"error": "messaggio vuoto"}, status=400)
        try:
            result = chat_fn(message)
        except Exception as exc:
            return self._json_response({"error": f"errore agente: {exc}"}, status=500)
        if isinstance(result, dict):
            return self._json_response(result)
        return self._json_response({"reply": result})

    def _serve_stream(self):
        """SSE: trasmette in tempo reale l'attività dell'agente (qualsiasi
        canale). Alla connessione invia la storia recente → niente "riparti da zero"."""
        try:
            from core import activity
        except Exception:
            return self.send_error(503)
        q, snapshot = activity.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            for evt in snapshot:
                self.wfile.write(b"data: " + json.dumps(evt).encode("utf-8") + b"\n\n")
            self.wfile.flush()
            while True:
                try:
                    evt = q.get(timeout=15)
                    self.wfile.write(b"data: " + json.dumps(evt).encode("utf-8") + b"\n\n")
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # keepalive
                self.wfile.flush()
        except Exception:
            pass  # client disconnesso
        finally:
            activity.unsubscribe(q)

    def _serve_file(self, relpath, ctype):
        try:
            with open(os.path.join(self.workspace_dir, relpath), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self.send_error(404)

    def _serve_logo(self):
        try:
            with open(os.path.join(self.workspace_dir, "openvurp.jpg"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self.send_error(404)

    def _serve_html(self):
        html = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>openvurp</title>
<link rel="icon" type="image/png" href="/favicon.ico">
<style>
:root{
  --bg:#212121; --side:#171717; --raised:#2f2f2f; --hover:#2a2a2a;
  --border:rgba(255,255,255,.08); --text:#ececec; --text-dim:#cdcdcd;
  --muted:#9b9b9b; --faint:#6e6e6e;
  --accent:#e8654a; --accent-hover:#ff7a5e; --accent-dim:rgba(232,101,74,.14);
  --ok:#4ade80; --bad:#f87171;
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,sans-serif;
  background:var(--bg);color:var(--text);overflow:hidden;font-size:15px;
  -webkit-font-smoothing:antialiased}
::selection{background:var(--accent-dim)}
button{font:inherit}
.app{display:flex;height:100vh}

/* ── Sidebar ── */
.side{width:260px;flex-shrink:0;background:var(--side);display:flex;flex-direction:column;
  padding:12px 10px;transition:width .15s ease;overflow:hidden}
.brand{display:flex;align-items:center;gap:10px;padding:6px 8px 14px}
.brand img{width:30px;height:30px;object-fit:contain;flex-shrink:0}
.brand b{font-size:15px;font-weight:600;white-space:nowrap}
.newchat{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:10px;
  border:1px solid var(--border);background:none;color:var(--text);cursor:pointer;
  font-size:13.5px;white-space:nowrap}
.newchat:hover{background:var(--hover)}
.newchat svg{flex-shrink:0}
.navsec{margin-top:auto;padding-top:10px;border-top:1px solid var(--border);
  display:flex;flex-direction:column;gap:1px}
.navitem{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;
  color:var(--muted);cursor:pointer;font-size:13px;white-space:nowrap}
.navitem:hover{background:var(--hover);color:var(--text-dim)}
.navitem.active{background:var(--hover);color:var(--text)}
.navitem svg{width:15px;height:15px;flex-shrink:0;opacity:.8}
.foot{display:flex;align-items:center;gap:8px;padding:10px 10px 4px;font-size:12px;color:var(--faint)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ok);flex-shrink:0}
.dot.off{background:var(--bad)}
.collapsebtn{margin-left:auto;background:none;border:none;color:var(--faint);cursor:pointer;
  padding:4px;border-radius:6px;display:grid;place-items:center}
.collapsebtn:hover{background:var(--hover);color:var(--text-dim)}
.collapsed .side{width:60px;padding-left:6px;padding-right:6px}
.collapsed .brand b,.collapsed .newchat span,.collapsed .navitem span,.collapsed #conn-label{display:none}
.collapsed .newchat,.collapsed .navitem{justify-content:center}
.collapsed .collapsebtn{margin:0 auto}
.collapsed .collapsebtn svg{transform:rotate(180deg)}

/* ── Main ── */
.main{flex:1;display:flex;flex-direction:column;min-width:0;position:relative}
.topbar{display:flex;align-items:center;gap:8px;padding:12px 20px;font-size:14px;
  color:var(--muted);flex-shrink:0}
.topbar b{color:var(--text);font-weight:600}
#err{display:none;color:var(--bad);font-size:13px;padding:0 20px 8px}
.panel{flex:1;min-height:0;display:flex;flex-direction:column}
.view{display:none;flex:1;min-height:0;overflow-y:auto}
.view.active{display:block}
#view-chat{flex-direction:column;overflow:hidden}
#view-chat.active{display:flex}

/* ── Chat ── */
.chatlog{flex:1;overflow-y:auto;display:flex;flex-direction:column;scroll-behavior:smooth}
.inner{width:100%;max-width:46rem;margin:0 auto;padding:20px 20px 8px;display:flex;
  flex-direction:column;gap:22px}
.hero{margin:auto;text-align:center;padding:40px 20px}
.hero img{width:72px;height:72px;object-fit:contain;margin-bottom:18px}
.hero h1{font-size:26px;font-weight:600;color:var(--text)}
.hero p{color:var(--muted);font-size:14px;margin-top:8px}
.turn{animation:rise .15s ease both}
@keyframes rise{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
.turn.user{display:flex;justify-content:flex-end}
.turn.user .bub{background:var(--raised);border-radius:18px;padding:9px 16px;max-width:75%;
  line-height:1.6;white-space:pre-wrap;word-break:break-word}
.turn.user .meta{font-size:11.5px;color:var(--faint);text-align:right;margin-top:4px}
.turn.bot .body{line-height:1.7;word-break:break-word}
.turn.bot .body p{margin:0 0 10px}
.turn.bot .body p:last-child{margin-bottom:0}
.turn.bot .body h2,.turn.bot .body h3,.turn.bot .body h4{margin:14px 0 8px;font-weight:600;line-height:1.35}
.turn.bot .body h2{font-size:19px}.turn.bot .body h3{font-size:16.5px}.turn.bot .body h4{font-size:15px}
.turn.bot .body ul,.turn.bot .body ol{margin:0 0 10px;padding-left:22px}
.turn.bot .body li{margin:3px 0}
.turn.bot .body a{color:var(--accent);text-decoration:none}
.turn.bot .body a:hover{text-decoration:underline}
.turn.bot .body code{background:var(--raised);border-radius:5px;padding:1.5px 6px;
  font-size:.87em;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.turn.bot .body pre{background:#181818;border:1px solid var(--border);border-radius:10px;
  padding:13px 15px;overflow-x:auto;margin:0 0 10px}
.turn.bot .body pre code{background:none;padding:0;font-size:13px;line-height:1.55;color:var(--text-dim)}
.activity{margin-bottom:8px;font-size:12.5px;color:var(--faint)}
.activity summary{cursor:pointer;list-style:none;display:inline-flex;align-items:center;gap:6px;
  padding:3px 10px;border-radius:99px;border:1px solid var(--border);color:var(--muted);user-select:none}
.activity summary::-webkit-details-marker{display:none}
.activity summary:hover{background:var(--hover)}
.activity[open] summary{margin-bottom:6px}
.activity .st{display:flex;gap:8px;align-items:baseline;padding:2px 0 2px 12px}
.activity .st b{color:var(--accent);font-weight:500;flex-shrink:0;font-family:ui-monospace,monospace}
.activity .st code{color:var(--muted);font-size:12px;word-break:break-all;font-family:ui-monospace,monospace}
.thinking{display:inline-flex;gap:4px;padding:6px 0}
.thinking i{width:7px;height:7px;border-radius:50%;background:var(--muted);animation:tp 1.2s infinite}
.thinking i:nth-child(2){animation-delay:.15s}.thinking i:nth-child(3){animation-delay:.3s}
@keyframes tp{0%,80%,100%{opacity:.25;transform:scale(.75)}40%{opacity:1;transform:scale(1)}}
.srcnote{font-size:11.5px;color:var(--faint);margin-bottom:5px}

/* ── Composer ── */
.composer{flex-shrink:0;padding:8px 20px 18px}
.composer .wrap{max-width:46rem;margin:0 auto}
.composer .box{display:flex;align-items:flex-end;gap:6px;background:var(--raised);
  border-radius:26px;padding:9px 9px 9px 20px;box-shadow:0 0 0 1px var(--border)}
.composer .box:focus-within{box-shadow:0 0 0 1px rgba(255,255,255,.16)}
.composer textarea{flex:1;background:none;border:none;outline:none;resize:none;color:var(--text);
  font:inherit;line-height:1.5;max-height:200px;padding:4px 0}
.composer textarea::placeholder{color:var(--faint)}
.send{width:34px;height:34px;flex-shrink:0;border:none;border-radius:50%;cursor:pointer;
  background:var(--accent);color:#fff;display:grid;place-items:center;transition:background .1s}
.send:hover{background:var(--accent-hover)}
.send:disabled{background:var(--hover);color:var(--faint);cursor:default}
.finehint{text-align:center;font-size:11.5px;color:var(--faint);margin-top:9px}

/* ── Pannelli secondari (minimali) ── */
.pad{max-width:52rem;margin:0 auto;padding:24px 20px}
.pgrid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.pcard{border:1px solid var(--border);border-radius:12px;padding:16px}
.pcard h3{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--faint);
  font-weight:600;margin-bottom:12px}
.kv{display:flex;justify-content:space-between;gap:12px;padding:6px 0;font-size:13.5px}
.kv .k{color:var(--muted)}.kv .v{color:var(--text);text-align:right;word-break:break-word}
.sec{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--faint);
  font-weight:600;margin:0 0 10px}
.plist{display:flex;flex-direction:column;gap:8px}
.tile{border:1px solid var(--border);border-radius:12px;padding:12px 15px}
.tile .t{font-weight:600;font-size:13.5px}
.tile .m{color:var(--muted);font-size:12.5px;margin-top:4px;word-break:break-word}
.tag{display:inline-block;padding:1.5px 8px;border-radius:99px;font-size:11px;margin-left:6px;
  background:var(--hover);color:var(--muted)}
.tag.ok{color:var(--ok)}.tag.bad{color:var(--bad)}
.emptymsg{color:var(--faint);font-size:13.5px}

.chatlog::-webkit-scrollbar,.view::-webkit-scrollbar{width:8px}
.chatlog::-webkit-scrollbar-thumb,.view::-webkit-scrollbar-thumb{background:var(--raised);border-radius:99px}
@media(max-width:760px){
  .side{width:60px;padding-left:6px;padding-right:6px}
  .brand b,.newchat span,.navitem span,#conn-label{display:none}
  .newchat,.navitem{justify-content:center}
  .turn.user .bub{max-width:88%}
}
</style>
</head>
<body>
<div class="app">
  <aside class="side">
    <div class="brand"><img src="/octopus.png" alt=""><b>openvurp</b></div>
    <button class="newchat" id="newchat" title="Nuova chat">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
      <span>Nuova chat</span>
    </button>
    <div class="navsec">
      <div class="navitem active" data-tab="chat"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg><span>Chat</span></div>
      <div class="navitem" data-tab="runtime"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 9h6v6H9z"/></svg><span>Runtime</span></div>
      <div class="navitem" data-tab="memory"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a7 7 0 0 0-7 7c0 3 2 5 2 7v3h10v-3c0-2 2-4 2-7a7 7 0 0 0-7-7z"/></svg><span>Memoria</span></div>
      <div class="navitem" data-tab="sessions"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 5h18M3 12h18M3 19h18"/></svg><span>Sessioni</span></div>
      <div class="navitem" data-tab="agents"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1"/></svg><span>Agenti</span></div>
      <div class="navitem" data-tab="events"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2 3 14h7l-1 8 10-12h-7z"/></svg><span>Eventi</span></div>
    </div>
    <div class="foot"><span class="dot" id="conn"></span><span id="conn-label">connesso</span>
      <button class="collapsebtn" id="collapse" title="Riduci">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 18l-6-6 6-6"/></svg>
      </button>
    </div>
  </aside>

  <div class="main">
    <div class="topbar"><b>openvurp</b><span id="model-label"></span></div>
    <p id="err"></p>
    <div class="panel">
      <section id="view-chat" class="view active">
        <div class="chatlog" id="chatlog">
          <div class="hero" id="hero">
            <img src="/octopus.png" alt="">
            <h1>Come posso aiutarti?</h1>
            <p>Il tuo agente, sempre qui. Scrivi e ci pensa lui.</p>
          </div>
          <div class="inner" id="log"></div>
        </div>
        <form class="composer" id="form"><div class="wrap">
          <div class="box">
            <textarea id="input" rows="1" placeholder="Scrivi a openvurp…"></textarea>
            <button class="send" id="send" type="submit" title="Invia">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>
            </button>
          </div>
          <div class="finehint">openvurp lavora in locale sul tuo computer.</div>
        </div></form>
      </section>
      <section id="view-runtime" class="view"><div class="pad"><div class="pgrid" id="runtime-grid"></div></div></section>
      <section id="view-memory" class="view"><div class="pad"><div class="pgrid" id="memory-grid"></div></div></section>
      <section id="view-sessions" class="view"><div class="pad">
        <div class="sec">Sessioni attive</div><div class="plist" id="route-sessions"></div>
        <div class="sec" style="margin-top:22px">Sessioni salvate</div><div class="plist" id="saved-sessions"></div>
      </div></section>
      <section id="view-agents" class="view"><div class="pad"><div class="plist" id="subagents"></div></div></section>
      <section id="view-events" class="view"><div class="pad"><div class="plist" id="events"></div></div></section>
    </div>
  </div>
</div>
<script>
const $=s=>document.querySelector(s);
function setErr(m){const e=$("#err");if(!m){e.style.display="none";return}e.style.display="block";e.textContent=m}
async function jget(u){const r=await fetch(u);if(!r.ok)throw new Error(u+" -> "+r.status);return r.json()}
const kv=(k,v)=>`<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>`;
const card=(t,i)=>`<div class="pcard"><h3>${t}</h3>${i}</div>`;
const tile=(i)=>`<div class="tile">${i}</div>`;
const tagg=(t,c)=>`<span class="tag ${c||''}">${t}</span>`;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* Markdown minimale e sicuro: prima si escapa TUTTO, poi si trasforma.
   Copre ciò che l'agente usa davvero: code block, inline code, grassetto,
   corsivo, titoli, liste, link. */
function md(t){
  const blocks=[];
  let s=String(t).replace(/```(?:\\w*\\n)?([\\s\\S]*?)```/g,(m,c)=>{
    blocks.push('<pre><code>'+esc(c.replace(/\\n$/,''))+'</code></pre>');
    return '\\u0000'+(blocks.length-1)+'\\u0000';
  });
  s=esc(s);
  s=s.replace(/`([^`\\n]+)`/g,'<code>$1</code>');
  s=s.replace(/^#### (.+)$/gm,'<h4>$1</h4>').replace(/^### (.+)$/gm,'<h4>$1</h4>')
     .replace(/^## (.+)$/gm,'<h3>$1</h3>').replace(/^# (.+)$/gm,'<h2>$1</h2>');
  s=s.replace(/\\*\\*([^*]+)\\*\\*/g,'<b>$1</b>');
  s=s.replace(/(^|[\\s(])\\*([^*\\n]+)\\*(?=[\\s).,;:!?]|$)/g,'$1<i>$2</i>');
  s=s.replace(/\\[([^\\]]+)\\]\\((https?:[^)\\s]+)\\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  s=s.replace(/^(?:[-*]|\\d+\\.) .+(?:\\n(?:[-*]|\\d+\\.) .+)*/gm,
    b=>'<ul>'+b.split('\\n').map(l=>'<li>'+l.replace(/^(?:[-*]|\\d+\\.) /,'')+'</li>').join('')+'</ul>');
  s=s.split(/\\n{2,}/).map(p=>{
    if(/^<(h\\d|ul|pre)/.test(p.trim()))return p;
    return '<p>'+p.replace(/\\n/g,'<br>')+'</p>';
  }).join('');
  s=s.replace(/\\u0000(\\d+)\\u0000/g,(m,i)=>blocks[+i]);
  return s;
}

/* ── layout: sidebar ── */
const app=document.querySelector(".app");
if(localStorage.getItem("ov_collapsed")==="1")app.classList.add("collapsed");
$("#collapse").onclick=()=>{app.classList.toggle("collapsed");
  localStorage.setItem("ov_collapsed",app.classList.contains("collapsed")?"1":"0")};
document.querySelectorAll(".navitem").forEach(n=>n.onclick=()=>select(n.dataset.tab));
function select(id){
  document.querySelectorAll(".navitem").forEach(n=>n.classList.toggle("active",n.dataset.tab===id));
  document.querySelectorAll(".view").forEach(v=>v.classList.remove("active"));
  $("#view-"+id).classList.add("active");
  if(id==="chat")$("#input").focus();else loadTab(id);
}
$("#newchat").onclick=()=>{$("#log").innerHTML="";$("#hero").style.display="";select("chat")};

/* ── chat: rendering dei turni ── */
const log=$("#log"),chatlog=$("#chatlog");
function hideHero(){$("#hero").style.display="none"}
function scrollDown(){chatlog.scrollTop=chatlog.scrollHeight}
function addUser(text,who,source){
  hideHero();
  const d=document.createElement("div");d.className="turn user";
  let meta="";
  if((who&&who!=="user"&&who!=="system")||(source&&source!=="dashboard"&&source!=="cli"))
    meta='<div class="meta">'+esc(who||"")+(source&&source!=="dashboard"&&source!=="cli"?" · "+esc(source):"")+'</div>';
  d.innerHTML='<div><div class="bub"></div>'+meta+'</div>';
  d.querySelector(".bub").textContent=text;
  log.appendChild(d);scrollDown();
}
function newBotTurn(){
  hideHero();
  const d=document.createElement("div");d.className="turn bot";
  d.innerHTML='<details class="activity" style="display:none"><summary>'+
    '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/></svg>'+
    '<span class="alabel">attività</span></summary><div class="asteps"></div></details>'+
    '<div class="thinking"><i></i><i></i><i></i></div><div class="body"></div>';
  log.appendChild(d);scrollDown();
  return {el:d,activity:d.querySelector(".activity"),steps:d.querySelector(".asteps"),
    alabel:d.querySelector(".alabel"),think:d.querySelector(".thinking"),
    body:d.querySelector(".body"),text:"",nsteps:0};
}
let curBot=null,lastSeq=0;
function onEvent(e){
  if(e.seq){if(e.seq<=lastSeq)return;lastSeq=e.seq;}
  if(e.kind==="user"){
    if(curBot){curBot.think.remove();curBot=null;}
    addUser(e.text||"",e.sender||"",e.source||"");
  }else if(e.kind==="step"){
    if(!curBot)curBot=newBotTurn();
    curBot.activity.style.display="";curBot.nsteps++;
    curBot.alabel.textContent="attività · "+curBot.nsteps;
    const d=document.createElement("div");d.className="st";
    d.innerHTML='<b>'+(e.step==="shell"?"$":"⚙")+'</b><code></code>';
    d.querySelector("code").textContent=e.text||"";
    curBot.steps.appendChild(d);scrollDown();
  }else if(e.kind==="assistant_start"){
    if(!curBot)curBot=newBotTurn();
  }else if(e.kind==="token"){
    if(!curBot)curBot=newBotTurn();
    curBot.think.style.display="none";
    curBot.text+=(e.text||"");
    curBot.body.innerHTML=md(curBot.text);scrollDown();
  }else if(e.kind==="assistant_end"){
    if(curBot){curBot.think.remove();}
    curBot=null;
  }
}
function connectStream(){
  try{const es=new EventSource("/api/stream");
    es.onmessage=ev=>{try{onEvent(JSON.parse(ev.data))}catch(_){}};}catch(e){}
}

/* ── composer ── */
async function initChat(){
  const input=$("#input"),send=$("#send"),form=$("#form");
  let ok=false;try{ok=(await jget("/api/chat")).available}catch(e){}
  connectStream();
  if(!ok){input.disabled=send.disabled=true;
    input.placeholder="chat non disponibile — avvia la dashboard dall'host con l'agente";return;}
  input.oninput=()=>{input.style.height="auto";input.style.height=Math.min(input.scrollHeight,200)+"px"};
  input.onkeydown=e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();form.requestSubmit();}};
  form.onsubmit=async e=>{
    e.preventDefault();const msg=input.value.trim();if(!msg)return;
    input.value="";input.style.height="auto";send.disabled=true;
    try{await fetch("/api/chat",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({message:msg})});}
    catch(err){setErr("errore: "+err.message);}
    finally{send.disabled=false;input.focus();}
  };
  input.focus();
}

/* ── pannelli secondari ── */
async function loadTab(id){try{setErr("");
  if(id==="runtime"){
    const[s,mem,pl]=await Promise.all([jget("/api/status"),jget("/api/memory"),jget("/api/plugins")]);
    const rt=s.runtime||{},cur=s.current_session||{},st=mem.stats||{};
    let h=card("Runtime",kv("Modello",rt.model||"?")+kv("Backend",rt.backend||"?")+kv("Gateway",rt.gateway_enabled?`${rt.gateway_host}:${rt.gateway_port}`:"off")+kv("Snapshot",rt.sessions||0));
    h+=card("Sessione corrente",(cur&&cur.key)?kv("Chiave",cur.key)+kv("Stato",cur.state||"idle")+kv("Turni",cur.turns||0)+kv("Tool",cur.tool_calls||0)+kv("Token",cur.tokens_total||0):'<div class="emptymsg">Nessuna sessione ancora.</div>');
    h+=card("Memoria",kv("File",st.files||0)+kv("Lezioni",st.lessons||0)+kv("Progetti",st.projects||0)+kv("Peso",((st.total_size||0)/1024).toFixed(1)+" KB"));
    const rows=pl.plugins||[];h+=card("Plugin",rows.length?rows.map(p=>tagg((p.name||p.id||"")+" "+(p.version||""),p.status==="loaded"?"ok":(p.status==="error"?"bad":""))).join(""):'<div class="emptymsg">Nessun plugin.</div>');
    $("#runtime-grid").innerHTML=h;
  }else if(id==="memory"){const mem=await jget("/api/memory");const st=mem.stats||{};
    $("#memory-grid").innerHTML=card("Memoria",kv("File",st.files||0)+kv("Lezioni",st.lessons||0)+kv("Progetti",st.projects||0)+kv("Diario",st.diary_entries||0)+kv("Peso",((st.total_size||0)/1024).toFixed(1)+" KB"));
  }else if(id==="sessions"){const d=await jget("/api/sessions");
    const fill=(a,r,f)=>{$("#"+a).innerHTML=(r&&r.length)?r.map(f).join(""):'<div class="emptymsg">nessuna</div>'};
    fill("route-sessions",d.route_sessions||[],i=>tile(`<div class="t">${esc(i.key)}</div><div class="m">${esc(i.state||"idle")} · llm ${i.llm_calls||0} · tool ${i.tool_calls||0} · token ${i.tokens_total||0}</div>`));
    fill("saved-sessions",d.saved_sessions||[],i=>tile(`<div class="t">${esc(i.id||"-")}</div><div class="m">${i.turns||0} turni · ${i.tool_calls||0} tool</div>`));
  }else if(id==="agents"){const d=await jget("/api/subagents");const r=d.subagents||[];
    $("#subagents").innerHTML=r.length?r.map(i=>tile(`<div class="t">${esc(i.id)} ${tagg(i.status||"?",i.status==="completed"?"ok":(["failed","timed_out"].includes(i.status)?"bad":""))}</div><div class="m">${esc(i.mode||"text")} · ${esc(i.backend||"?")} · ${esc(i.model||"?")}</div>`)).join(""):'<div class="emptymsg">Nessun subagente.</div>';
  }else if(id==="events"){const d=await jget("/api/events");const r=d.events||[];
    $("#events").innerHTML=r.length?r.map(i=>tile(`<div class="t" style="color:var(--accent)">${esc(i.event||"event")}</div><div class="m">${esc(JSON.stringify(i.payload||{}).slice(0,260))}</div>`)).join(""):'<div class="emptymsg">Nessun evento.</div>';
  }}catch(err){setErr("Errore dashboard: "+err.message)}}

async function ping(){const c=$("#conn"),l=$("#conn-label"),m=$("#model-label");
  try{const s=await jget("/api/status");c.classList.remove("off");l.textContent="connesso";
    const rt=s.runtime||{};if(rt.model)m.textContent=" · "+rt.model;}
  catch(e){c.classList.add("off");l.textContent="offline"}}
initChat();ping();setInterval(ping,10000);
</script>
</body>
</html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if DashboardHandler.token:
            # cookie HttpOnly+SameSite: le richieste successive (e l'SSE) lo portano
            self.send_header("Set-Cookie",
                             f"ovtok={DashboardHandler.token}; Path=/; HttpOnly; SameSite=Strict")
        self.end_headers()
        self.wfile.write(body)

    def _serve_unauthorized(self):
        page = (
            "<!doctype html><meta charset=utf-8><title>openvurp</title>"
            "<body style='font-family:system-ui;background:#212121;color:#ececec;"
            "display:grid;place-items:center;height:100vh;margin:0'>"
            "<div style='text-align:center'>"
            "<img src='/octopus.png' style='width:56px;height:56px;"
            "object-fit:contain;margin-bottom:14px' alt=''>"
            "<h2 style='font-weight:600'>openvurp</h2>"
            "<p style='color:#9b9b9b;margin:8px 0'>Accesso protetto. Apri la dashboard col tuo token:</p>"
            "<code style='background:#2f2f2f;padding:6px 12px;border-radius:8px;font-size:13px'>"
            "http://&lt;host&gt;:PORT/?token=IL_TUO_TOKEN</code></div></body>"
        )
        body = page.encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", ""}


class DashboardServer:
    def __init__(self, agent=None, port: int = 8420, workspace_dir: str = OPENVURP_DIR,
                 chat_fn=None, host: str = "127.0.0.1", token: str = ""):
        self.agent = agent
        self.port = port
        self.host = host or "127.0.0.1"
        self.workspace_dir = workspace_dir
        self.chat_fn = chat_fn
        # Sicurezza: se esposta oltre localhost, esigi SEMPRE un token. Se manca,
        # ne genero uno e lo richiedo (mai esposizione senza autenticazione).
        self.token = token or ""
        if not self.token and self.host not in _LOCAL_HOSTS:
            import secrets
            self.token = secrets.token_urlsafe(18)
            print(f"  [dashboard] esposta su {self.host}: token generato → "
                  f"apri http://<host>:{self.port}/?token={self.token}")
        DashboardHandler.workspace_dir = workspace_dir
        DashboardHandler.chat_fn = chat_fn
        DashboardHandler.token = self.token

    def start(self):
        server = ThreadingHTTPServer((self.host, self.port), DashboardHandler)
        server.serve_forever()
