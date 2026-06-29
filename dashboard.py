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
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>openvurp</title>
<link rel="icon" type="image/png" href="/favicon.ico">
<style>
/* Layered glass design system, openvurp accent */
:root{
  --bg:#0e1015; --bg-accent:#13151b; --bg-elevated:#191c24; --bg-hover:#1f2330;
  --card:#161920; --card-fg:#f0f0f2; --popover:#191c24;
  --text:#d4d4d8; --text-strong:#f4f4f5; --muted:#838387; --muted-strong:#75757d;
  --border:#1e2028; --border-strong:#2e3040;
  --accent:#e8654a; --accent-hover:#ff7a5e; --accent-subtle:rgba(232,101,74,.12);
  --ok:#22c55e; --warn:#f59e0b; --danger:#ef4444; --radius:14px;
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{font-family:'Inter',ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  background:var(--bg);color:var(--text);overflow:hidden;-webkit-font-smoothing:antialiased;font-size:14px}
::selection{background:var(--accent-subtle)}
.app{display:flex;height:100vh}
/* Tier-1 glass chrome: never flat/opaque */
.glass1{background:color-mix(in srgb,var(--bg) 82%,transparent);
  backdrop-filter:blur(12px) saturate(1.6);-webkit-backdrop-filter:blur(12px) saturate(1.6)}
.glass2{background:rgba(22,25,32,.75);border:1px solid var(--border);border-radius:var(--radius);
  backdrop-filter:blur(12px) saturate(1.6);-webkit-backdrop-filter:blur(12px) saturate(1.6)}
@supports not (backdrop-filter:blur(1px)){.glass1{background:rgba(14,16,21,.96)}.glass2{background:var(--card)}}

/* sidebar */
.side{width:240px;flex-shrink:0;display:flex;flex-direction:column;padding:16px 12px;
  border-right:1px solid var(--border)}
.brandimg{width:100%;margin-bottom:18px;display:block;mix-blend-mode:screen}
.collapsed .brandimg{aspect-ratio:1;object-fit:cover;object-position:left center;width:40px;margin:0 auto 14px}
.nav{display:flex;flex-direction:column;gap:2px}
.navitem{display:flex;align-items:center;gap:12px;padding:9px 11px;border-radius:10px;color:var(--muted);
  cursor:pointer;font-weight:500;transition:background .1s,color .1s;position:relative}
.navitem:hover{background:var(--bg-hover);color:var(--text)}
.navitem.active{background:var(--accent-subtle);color:var(--text-strong)}
.navitem.active::before{content:"";position:absolute;left:-12px;top:8px;bottom:8px;width:3px;
  border-radius:0 3px 3px 0;background:var(--accent)}
.navitem.active .ic{color:var(--accent)}
.ic{width:17px;height:17px;flex-shrink:0;color:var(--muted-strong)}
.foot{margin-top:auto;display:flex;align-items:center;gap:8px;padding:10px 11px;font-size:12px;color:var(--muted)}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 0 #22c55e88;animation:pulse 2.4s infinite}
.pulse.off{background:var(--danger);animation:none}
@keyframes pulse{0%{box-shadow:0 0 0 0 #22c55e55}70%{box-shadow:0 0 0 7px #22c55e00}100%{box-shadow:0 0 0 0 #22c55e00}}

/* content */
.content{flex:1;display:flex;flex-direction:column;min-width:0}
.topbar{display:flex;align-items:center;gap:10px;padding:15px 22px;border-bottom:1px solid var(--border)}
.topbar h1{font-size:15px;font-weight:600;color:var(--text-strong)}
.topbar .hint{margin-left:auto;font-size:12px;color:var(--muted)}
.panel{flex:1;overflow:hidden}
.view{display:none;height:100%;overflow-y:auto;padding:22px}
.view.active{display:block}
#view-chat.active{display:flex;flex-direction:column;padding:0}
.scroll::-webkit-scrollbar{width:10px}.scroll::-webkit-scrollbar-thumb{background:var(--border-strong);border-radius:99px;border:3px solid var(--bg)}

/* chat */
.chatlog{flex:1;overflow-y:auto;padding:28px 0;display:flex;flex-direction:column;gap:6px}
.row{display:flex;gap:14px;padding:10px clamp(16px,8vw,140px);align-items:flex-start;animation:rise .12s ease both}
.row:hover{background:rgba(255,255,255,.012)}
@keyframes rise{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.av{width:28px;height:28px;border-radius:8px;flex-shrink:0;display:grid;place-items:center;font-size:13px;font-weight:600;margin-top:1px}
.av.bot{background:var(--accent);color:#1a0d08}
.av.me{background:var(--bg-hover);color:var(--muted);font-size:11px}
.msg{flex:1;min-width:0}
.who{font-size:12px;font-weight:600;color:var(--text-strong);margin-bottom:3px}
.body{font-size:14.5px;line-height:1.65;color:var(--text);white-space:pre-wrap;word-break:break-word}
.sys{text-align:center;color:var(--muted);font-size:13px;padding:8px;animation:rise .12s}
.typing{display:flex;gap:5px;padding:5px 0}
.typing i{width:6px;height:6px;border-radius:50%;background:var(--muted);animation:tp 1.1s infinite}
.typing i:nth-child(2){animation-delay:.16s}.typing i:nth-child(3){animation-delay:.32s}
@keyframes tp{0%,80%,100%{opacity:.3;transform:scale(.7)}40%{opacity:1;transform:scale(1)}}
.composer{padding:14px clamp(16px,8vw,140px) 20px}
.composer .box{display:flex;gap:8px;align-items:flex-end;padding:8px 8px 8px 14px;border-radius:14px;transition:border-color .1s}
.composer .box:focus-within{border-color:var(--border-strong)}
.composer textarea{flex:1;background:none;border:none;color:var(--text);font:inherit;font-size:14.5px;
  resize:none;max-height:180px;padding:6px 0;outline:none}
.composer textarea::placeholder{color:var(--muted-strong)}
.send{flex-shrink:0;width:36px;height:36px;border:none;border-radius:10px;cursor:pointer;
  background:var(--accent);color:#1a0d08;display:grid;place-items:center;transition:background .1s,opacity .1s}
.send:hover{background:var(--accent-hover)}
.send:disabled{background:var(--bg-hover);color:var(--muted-strong);cursor:not-allowed}

/* cards */
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.card{padding:18px}
.card h3{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:14px;font-weight:600}
.kv{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-bottom:1px solid var(--border);font-size:13.5px}
.kv:last-child{border:0}.kv .k{color:var(--muted)}.kv .v{color:var(--text-strong);text-align:right;word-break:break-word}
.sec{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin:0 0 12px;font-weight:600}
.list{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.tile{padding:13px 15px}
.tile .t{font-weight:600;color:var(--text-strong);font-size:14px}.tile .m{color:var(--muted);font-size:12.5px;margin-top:5px}
.tag{display:inline-block;padding:2px 9px;border-radius:99px;font-size:11px;margin:0 4px 4px 0;background:var(--bg-hover);color:var(--muted)}
.tag.ok{background:rgba(34,197,94,.14);color:#5ee08a}.tag.warn{background:rgba(245,158,11,.14);color:#fbbf52}.tag.bad{background:rgba(239,68,68,.14);color:#f78a8a}
.empty{color:var(--muted);font-size:13.5px}
#err{display:none;color:#f78a8a;font-size:13px;padding:10px 22px}
/* collapsed sidebar: solo il polpo */
.side{transition:width .16s ease}
.collapsed .side{width:64px;padding-left:8px;padding-right:8px}
.collapsed .navitem{justify-content:center;padding-left:0;padding-right:0}
.collapsed .navitem span,.collapsed #conn-label{display:none}
.collapsed .navitem.active::before{left:-8px}
.collapsebtn{margin-left:auto;background:none;border:none;color:var(--muted-strong);cursor:pointer;
  padding:6px;border-radius:8px;display:grid;place-items:center}
.collapsebtn:hover{background:var(--bg-hover);color:var(--text)}
.collapsebtn svg{transition:transform .16s}
.collapsed .collapsebtn{margin:0 auto}
.collapsed .collapsebtn svg{transform:rotate(180deg)}
/* chat steps (cosa fa) */
.steps{display:flex;flex-direction:column;gap:3px;margin-bottom:8px}
.step{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--muted)}
.step .si{width:13px;height:13px;flex-shrink:0;color:var(--accent);opacity:.85}
.step.done .si{color:var(--ok)}
.step code{color:var(--text);font-size:12px;word-break:break-all}
@media(max-width:760px){.side{width:64px}.navitem span,#conn-label{display:none}.navitem{justify-content:center}}
</style>
</head>
<body>
<div class="app">
  <aside class="side glass1">
    <img src="/openvurp.jpg" class="brandimg" alt="openvurp">
    <nav class="nav" id="nav">
      <div class="navitem active" data-tab="chat"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg><span>Chat</span></div>
      <div class="navitem" data-tab="runtime"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 9h6v6H9z"/></svg><span>Runtime</span></div>
      <div class="navitem" data-tab="memory"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a7 7 0 0 0-7 7c0 3 2 5 2 7v3h10v-3c0-2 2-4 2-7a7 7 0 0 0-7-7z"/></svg><span>Memory</span></div>
      <div class="navitem" data-tab="sessions"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 5h18M3 12h18M3 19h18"/></svg><span>Sessions</span></div>
      <div class="navitem" data-tab="agents"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1"/></svg><span>Agents</span></div>
      <div class="navitem" data-tab="events"><svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2 3 14h7l-1 8 10-12h-7z"/></svg><span>Events</span></div>
    </nav>
    <div class="foot"><span class="pulse" id="conn"></span><span id="conn-label">connected</span><button class="collapsebtn" id="collapse" title="Collapse sidebar"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 18l-6-6 6-6"/></svg></button></div>
  </aside>

  <div class="content">
    <div class="topbar glass1"><h1 id="tab-title">Chat</h1><span class="hint" id="tab-hint">talk to your agent</span></div>
    <p id="err"></p>
    <div class="panel">
      <section id="view-chat" class="view active">
        <div class="chatlog scroll" id="chatlog"></div>
        <form class="composer" id="form"><div class="box glass2">
          <textarea id="input" rows="1" placeholder="Message openvurp…"></textarea>
          <button class="send" id="send" type="submit" title="Send"><svg width="17" height="17" viewBox="0 0 24 24" fill="currentColor"><path d="M3.4 20.4 21 12 3.4 3.6 3 10l12 2-12 2z"/></svg></button>
        </div></form>
      </section>
      <section id="view-runtime" class="view scroll"><div class="grid" id="runtime-grid"></div></section>
      <section id="view-memory" class="view scroll"><div class="grid" id="memory-grid"></div></section>
      <section id="view-sessions" class="view scroll">
        <div class="sec">Route sessions</div><div class="list" id="route-sessions"></div>
        <div class="sec" style="margin-top:22px">Saved sessions</div><div class="list" id="saved-sessions"></div>
      </section>
      <section id="view-agents" class="view scroll"><div class="list" id="subagents"></div></section>
      <section id="view-events" class="view scroll"><div class="list" id="events" style="grid-template-columns:1fr"></div></section>
    </div>
  </div>
</div>
<script>
const $=s=>document.querySelector(s);
const HINTS={chat:"talk to your agent",runtime:"live runtime status",memory:"what the agent remembers",sessions:"conversations",agents:"subagents at work",events:"runtime events"};
function setErr(m){const e=$("#err");if(!m){e.style.display="none";return}e.style.display="block";e.textContent=m}
async function jget(u){const r=await fetch(u);if(!r.ok)throw new Error(u+" -> "+r.status);return r.json()}
const kv=(k,v)=>`<div class="kv"><span class="k">${k}</span><span class="v">${v}</span></div>`;
const card=(t,i)=>`<div class="card glass2"><h3>${t}</h3>${i}</div>`;
const tile=(i)=>`<div class="tile glass2">${i}</div>`;
const tagg=(t,c)=>`<span class="tag ${c||''}">${t}</span>`;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// collapse sidebar (persistente)
const app=document.querySelector(".app");
if(localStorage.getItem("ov_collapsed")==="1")app.classList.add("collapsed");
$("#collapse").onclick=()=>{app.classList.toggle("collapsed");localStorage.setItem("ov_collapsed",app.classList.contains("collapsed")?"1":"0")};
document.querySelectorAll(".navitem").forEach(n=>n.onclick=()=>select(n.dataset.tab));
function select(id){
  document.querySelectorAll(".navitem").forEach(n=>n.classList.toggle("active",n.dataset.tab===id));
  document.querySelectorAll(".view").forEach(v=>v.classList.remove("active"));
  $("#view-"+id).classList.add("active");
  $("#tab-title").textContent=id[0].toUpperCase()+id.slice(1);$("#tab-hint").textContent=HINTS[id]||"";
  if(id==="chat")$("#input").focus();else loadTab(id);
}
function bubble(role,text){
  const log=$("#chatlog");
  const d=document.createElement("div");d.className="sys";d.textContent=text;log.appendChild(d);log.scrollTop=1e9;return d;
}
// --- live stream: tutto ciò che fa l'agente (CLI/Telegram/heartbeat/dashboard) ---
let curBot=null,lastSeq=0;
function srcTag(s){return (s&&s!=="dashboard"&&s!=="cli")?' <span style="color:var(--muted);font-weight:400">· '+esc(s)+'</span>':"";}
function newBotRow(){
  const log=$("#chatlog");const row=document.createElement("div");row.className="row";
  row.innerHTML='<div class="av bot">🐙</div><div class="msg"><div class="who">openvurp</div><div class="steps"></div><div class="body"></div></div>';
  log.appendChild(row);log.scrollTop=log.scrollHeight;
  return {steps:row.querySelector(".steps"),body:row.querySelector(".body"),text:""};
}
function onEvent(e){
  if(e.seq){if(e.seq<=lastSeq)return;lastSeq=e.seq;}
  const log=$("#chatlog");
  if(e.kind==="user"){
    curBot=null;const who=(e.sender&&e.sender!=="user"&&e.sender!=="system")?e.sender:"You";
    const row=document.createElement("div");row.className="row";
    row.innerHTML='<div class="av me">'+esc((who[0]||"y").toLowerCase())+'</div><div class="msg"><div class="who">'+esc(who)+srcTag(e.source)+'</div><div class="body"></div></div>';
    row.querySelector(".body").textContent=e.text||"";log.appendChild(row);log.scrollTop=1e9;
  }else if(e.kind==="step"){
    if(!curBot)curBot=newBotRow();
    const ic=e.step==="shell"?"$":(e.step==="tool"?"⚙":"·");
    const d=document.createElement("div");d.className="step done";
    d.innerHTML='<span class="si">'+ic+'</span><code></code>';d.querySelector("code").textContent=e.text||"";
    curBot.steps.appendChild(d);log.scrollTop=1e9;
  }else if(e.kind==="assistant_start"){
    if(!curBot)curBot=newBotRow();
  }else if(e.kind==="token"){
    if(!curBot)curBot=newBotRow();
    curBot.text+=(e.text||"");curBot.body.textContent=curBot.text;log.scrollTop=1e9;
  }else if(e.kind==="assistant_end"){curBot=null;}
}
function connectStream(){
  try{const es=new EventSource("/api/stream");es.onmessage=ev=>{try{onEvent(JSON.parse(ev.data))}catch(_){}};}catch(e){}
}
async function initChat(){
  const input=$("#input"),send=$("#send"),form=$("#form");
  let ok=false;try{ok=(await jget("/api/chat")).available}catch(e){}
  connectStream();  // mostra l'attività dal vivo comunque
  if(!ok){input.disabled=send.disabled=true;input.placeholder="chat unavailable — run the dashboard from the host with the agent";return;}
  input.oninput=()=>{input.style.height="auto";input.style.height=Math.min(input.scrollHeight,180)+"px"};
  input.onkeydown=e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();form.requestSubmit();}};
  form.onsubmit=async e=>{
    e.preventDefault();const msg=input.value.trim();if(!msg)return;
    input.value="";input.style.height="auto";send.disabled=true;
    try{await fetch("/api/chat",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({message:msg})});}
    catch(err){bubble("sys","error: "+err.message);}
    finally{send.disabled=false;input.focus();}
  };
}
async function loadTab(id){try{setErr("");
  if(id==="runtime"){
    const[s,mem,pl]=await Promise.all([jget("/api/status"),jget("/api/memory"),jget("/api/plugins")]);
    const rt=s.runtime||{},cur=s.current_session||{},st=mem.stats||{};
    let h=card("Runtime",kv("Model",rt.model||"?")+kv("Backend",rt.backend||"?")+kv("Gateway",rt.gateway_enabled?`${rt.gateway_host}:${rt.gateway_port}`:"disabled")+kv("Snapshots",rt.sessions||0));
    h+=card("Current route",(cur&&cur.key)?kv("Key",cur.key)+kv("State",cur.state||"idle")+kv("Turns",cur.turns||0)+kv("Tools",cur.tool_calls||0)+kv("Tokens",cur.tokens_total||0):'<div class="empty">No route session yet.</div>');
    h+=card("Memory",kv("Files",st.files||0)+kv("Lessons",st.lessons||0)+kv("Projects",st.projects||0)+kv("Size",((st.total_size||0)/1024).toFixed(1)+" KB"));
    const rows=pl.plugins||[];h+=card("Plugins",rows.length?rows.map(p=>tagg((p.name||p.id||"")+" "+(p.version||""),p.status==="loaded"?"ok":(p.status==="error"?"warn":""))).join(""):'<div class="empty">No plugins.</div>');
    $("#runtime-grid").innerHTML=h;
  }else if(id==="memory"){const mem=await jget("/api/memory");const st=mem.stats||{};
    $("#memory-grid").innerHTML=card("Memory",kv("Files",st.files||0)+kv("Lessons",st.lessons||0)+kv("Projects",st.projects||0)+kv("Diary",st.diary_entries||0)+kv("Size",((st.total_size||0)/1024).toFixed(1)+" KB"));
  }else if(id==="sessions"){const d=await jget("/api/sessions");
    const fill=(a,r,f)=>{$("#"+a).innerHTML=(r&&r.length)?r.map(f).join(""):'<div class="empty">none</div>'};
    fill("route-sessions",d.route_sessions||[],i=>tile(`<div class="t">${i.key}</div><div class="m">${i.state||"idle"} · llm ${i.llm_calls||0} · tools ${i.tool_calls||0} · tok ${i.tokens_total||0}</div>`));
    fill("saved-sessions",d.saved_sessions||[],i=>tile(`<div class="t">${i.id||"-"}</div><div class="m">${i.turns||0} turns · ${i.tool_calls||0} tools</div>`));
  }else if(id==="agents"){const d=await jget("/api/subagents");const r=d.subagents||[];
    $("#subagents").innerHTML=r.length?r.map(i=>tile(`<div class="t">${i.id} ${tagg(i.status||"?",i.status==="completed"?"ok":(["failed","timed_out"].includes(i.status)?"bad":""))}</div><div class="m">${i.mode||"text"} · ${i.backend||"?"} · ${i.model||"?"}</div>`)).join(""):'<div class="empty">No subagents.</div>';
  }else if(id==="events"){const d=await jget("/api/events");const r=d.events||[];
    $("#events").innerHTML=r.length?r.map(i=>tile(`<div class="t" style="color:var(--accent)">${i.event||"event"}</div><div class="m" style="word-break:break-all">${JSON.stringify(i.payload||{}).slice(0,260)}</div>`)).join(""):'<div class="empty">No events.</div>';
  }}catch(err){setErr("Dashboard error: "+err.message)}}
async function ping(){const c=$("#conn"),l=$("#conn-label");try{await jget("/api/status");c.classList.remove("off");l.textContent="connected"}catch(e){c.classList.add("off");l.textContent="offline"}}
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
            "<body style='font-family:system-ui;background:#0e1015;color:#d4d4d8;"
            "display:grid;place-items:center;height:100vh;margin:0'>"
            "<div style='text-align:center'><h2 style='color:#e8654a'>openvurp</h2>"
            "<p>Unauthorized. Open the dashboard with your token:</p>"
            "<code>http://&lt;host&gt;:PORT/?token=YOUR_TOKEN</code></div></body>"
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
