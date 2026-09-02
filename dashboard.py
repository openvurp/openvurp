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
from urllib.parse import parse_qs, urlparse

from core.runtime_api import (
    collect_gateway_events,
    collect_memory_overview,
    collect_plugin_list,
    collect_runtime_overview,
    collect_saved_sessions,
    collect_session_list,
    collect_subagent_runs,
)
from core.chat_store import ChatStore
from core.multiplayer import MultiplayerCoordinator
from core.providers import provider_catalog


OPENVURP_DIR = os.path.dirname(os.path.abspath(__file__))
ALLOWED_BACKENDS = {
    "", "auto", "codex", "claude_cli", "ollama", "anthropic", "openai", "groq",
    "openai_compatible",
}


def _come_prima(attuale, testo: str):
    """Rimette il valore nel tipo che aveva.

    `CHANNELS_IN` nasce come lista: salvandola come stringa, il codice che la
    scorre otterrebbe le singole lettere — «telegram» diventerebbe otto canali
    inesistenti invece di uno.
    """
    if isinstance(attuale, bool):
        return str(testo).strip().lower() in {"1", "true", "yes", "on", "si"}
    if isinstance(attuale, int) and str(testo).strip().lstrip("-").isdigit():
        return int(str(testo).strip())
    if isinstance(attuale, (list, tuple)):
        pezzi = [p.strip() for p in str(testo).replace(";", ",").split(",") if p.strip()]
        if attuale and all(isinstance(x, int) for x in attuale):
            fuori = []
            for p in pezzi:
                try:
                    fuori.append(int(p))
                except ValueError:
                    pass
            return fuori
        return pezzi
    return testo


def clean_backend(value: object) -> str:
    backend = str(value or "").strip().lower()
    if backend == "claude":
        backend = "claude_cli"
    return backend if backend in ALLOWED_BACKENDS else ""


def filter_stream_snapshot(snapshot: list[dict], requested_chat: str = "",
                           replay: bool = True) -> list[dict]:
    """Filtra il replay SSE.

    La dashboard multi-chat carica la cronologia dal database e apre lo stream
    con replay=false: reinviare lo snapshot del bus duplicava ogni messaggio.
    """
    if not replay:
        return []
    return [
        event for event in snapshot
        if not requested_chat or event.get("chat_id", "") == requested_chat
    ]


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
        # Risposta non-streamata: catturala anche quando non c'e' streaming.
        self.__dict__["response_text"] += str(text)

    def __getattr__(self, name):
        # tutto ciò che non sovrascriviamo va alla UI reale
        return getattr(self.__dict__["_real"], name)


def make_chat_fn(agent, lock, real_ui, chat_store: ChatStore | None = None):
    """Costruisce un chat_fn thread-safe per la dashboard.

    Serializza l'accesso all'agente con `lock` (lo stesso che usa l'host per i
    suoi turni), scambia temporaneamente la UI per catturare la risposta e la
    ripristina.
    """
    if chat_store is None:
        session_root = getattr(getattr(agent, "session_store", None), "root_dir", "")
        if session_root:
            memory_root = os.path.dirname(session_root)
        else:
            import tempfile
            memory_root = tempfile.mkdtemp(prefix="openvurp-dashboard-test-")
        chat_store = ChatStore(memory_root)
    store = chat_store
    multiplayer = MultiplayerCoordinator(store)
    # Lo sciame condivide l'archivio: gli agenti della rubrica sono gli stessi
    # che l'agente principale convoca da CLI.
    swarm = getattr(agent, "swarm", None)
    if swarm is None:
        try:
            from core.swarm import Swarm
            swarm = Swarm(agent, store=store)
        except Exception:
            swarm = None

    def chat_fn(message: str, chat_id: str = "", attachments: list | None = None) -> dict:
        # Gli allegati diventano parte del messaggio: l'agente ha gia' i tool
        # per aprirli (image_analyze, pdf_read, read_file), gli serve il percorso.
        paths = [str(a) for a in (attachments or []) if str(a).strip()][:8]
        if paths:
            listing = "\n".join(f"- {p}" for p in paths)
            # Un vocale registrato dal web arriva GIA' trascritto (lo fa il
            # browser mentre si parla): mandare l'agente a ritrascriverlo con
            # Whisper significa un timeout su questa macchina, non un servizio.
            gia_trascritta = any("-trascritta" in os.path.basename(p) for p in paths)
            nota_audio = (
                "La nota vocale e' GIA' trascritta: il testo del messaggio E' "
                "quello. NON usare audio_transcribe, rispondi al testo."
                if gia_trascritta else
                "audio_transcribe per le note vocali e gli audio"
            )
            message = (
                f"{message}\n\n[ALLEGATI dall'utente — aprili con il tool adatto "
                f"(image_analyze per le immagini, pdf_read per i PDF, "
                f"{nota_audio}, read_file "
                f"per il testo). Se e' una nota vocale non ancora trascritta, "
                f"trascrivila e rispondi a quello che ha detto: non descrivere "
                f"il file]\n{listing}"
            ).strip()
        with lock:
            chat = store.ensure_chat(chat_id)
            chat_id = chat["id"]
            session_key = f"dashboard:chat:{chat_id}"
            # Se la route non ha ancora una history (prima apertura dopo
            # upgrade/riavvio), ricostruiscila dai messaggi durevoli della chat.
            session_store = getattr(agent, "session_store", None)
            if session_store is not None and not session_store.load_messages(session_key):
                history = []
                for item in store.list_messages(chat_id, limit=80):
                    if item.get("author_type") == "user":
                        history.append({"role": "user", "content": item.get("content", "")})
                    elif item.get("author_type") == "assistant":
                        history.append({"role": "assistant", "content": item.get("content", "")})
                if history:
                    session_store.save_messages(session_key, history)
            run_id = store.start_run(chat_id)
            store.add_message(
                chat_id, "user", message, author_type="user",
                author_id="owner", author_name="Tu", run_id=run_id,
            )
            # Chat a due: il messaggio va a QUEL agente, non all'agente
            # principale. Senza questo "apri la chat" su un membro della
            # rubrica aprirebbe un filo che poi risponde con un'altra voce.
            direct_id = str(chat.get("direct_agent_id", "") or "")
            if direct_id and swarm is not None:
                from core.swarm import SwarmError
                member = None
                try:
                    member = swarm.resolve(direct_id)
                    # persist=False: la domanda dell'utente e' gia' stata
                    # scritta sopra con questo run_id.
                    reply = swarm.ask(direct_id, message, sender="owner",
                                      persist=False)
                except SwarmError as exc:
                    reply = f"[{exc}]"
                except Exception as exc:
                    reply = f"[errore parlando con questo agente: {exc}]"
                store.add_message(
                    chat_id, "assistant", reply, author_type="agent",
                    author_id=direct_id,
                    author_name=member.name if member else "agente",
                    run_id=run_id,
                    # Cosa ha fatto va salvato col messaggio: qui persiste la
                    # dashboard, quindi i passaggi vanno presi dallo sciame.
                    metadata={"steps": list(getattr(swarm, "last_steps", []) or [])} or None,
                )
                store.finish_run(run_id)
                return {
                    "chat_id": chat_id,
                    "reply": reply,
                    "author_name": member.name if member else "agente",
                    "author_id": direct_id,
                }

            team = None
            if chat.get("mode") == "team":
                # La stanza esce uno alla volta: con tre agenti e due giri
                # l'attesa e' di decine di secondi, e pubblicare tutto alla fine
                # faceva sembrare la pagina bloccata.
                from core import activity as _act
                room_key = f"dashboard:chat:{chat_id}"

                def _turn(profile, round_no):
                    _act.publish(
                        "room_turn", source="dashboard", chat_id=chat_id,
                        session_key=room_key, author_id=profile["id"],
                        author_name=profile["name"], round=round_no,
                    )

                def _said(row):
                    _act.publish(
                        "room_message", source="dashboard", chat_id=chat_id,
                        session_key=room_key, text=row.get("content", ""),
                        author_id=row.get("author_id", ""),
                        author_name=row.get("author_name", "agente"),
                        round=(row.get("metadata") or {}).get("round", 1),
                    )

                from core import multiplayer as _mp
                _mp.clear_stop(chat_id)   # uno stop vecchio non deve zittire questa
                team = multiplayer.collaborate(
                    chat_id, message, run_id=run_id,
                    on_turn=_turn, on_message=_said,
                    should_stop=lambda: _mp.stop_requested(chat_id),
                )
                _mp.clear_stop(chat_id)
                try:
                    _act.publish("room_turn", source="dashboard", chat_id=chat_id,
                                 session_key=room_key, author_id="", author_name="")
                    _act.publish("room_end", source="dashboard", chat_id=chat_id,
                                 session_key=room_key, reason=team.ended,
                                 rounds=team.rounds)
                except Exception:
                    pass

                # Nella stanza rispondono SOLO gli agenti della rubrica.
                #
                # Prima openvurp parlava quando la stanza restava muta — e
                # «muta» include il budget giornaliero esaurito. Cosi', proprio
                # nel momento in cui i tuoi agenti non potevano rispondere,
                # rispondeva qualcuno che non hai creato, come se fosse uno di
                # loro. Se la stanza non puo' parlare lo si dice, non lo si
                # copre con un'altra voce.
                store.finish_run(
                    run_id,
                    input_tokens=team.input_tokens,
                    output_tokens=team.output_tokens,
                )
                ultimo = team.messages[-1] if team.messages else {}
                return {
                    "chat_id": chat_id,
                    "reply": "",
                    "steps": [],
                    "team_messages": team.messages,
                    "team_errors": team.errors,
                    "author_name": ultimo.get("author_name", ""),
                    "author_id": ultimo.get("author_id", ""),
                }
            capture = CaptureUI(real_ui)
            old_ui = agent.ui
            agent.ui = capture
            existing_session = getattr(agent, "_route_runtime_sessions", {}).get(session_key)
            main_input_before = existing_session.tokens.input_tokens if existing_session else 0
            main_output_before = existing_session.tokens.output_tokens if existing_session else 0
            try:
                agent.run(
                    message, source="dashboard", sender="owner",
                    actor_id="cli_owner", chat_id=chat_id,
                    session_key=session_key,
                    turn_context=team.brief if team else "",
                    llm_backend=chat.get("backend", ""),
                    llm_model=chat.get("model", ""),
                )
                try:
                    agent.session.save()
                except Exception:
                    pass
                reply = capture.response_text.strip() or "(no reply)"
                requested_backend = clean_backend(chat.get("backend", ""))
                route = getattr(agent, "_last_llm_route", {}) or {}
                backend = clean_backend(route.get("backend", requested_backend))
                actual_model = str(route.get("model", chat.get("model", "")) or "")
                author_name = {
                    "codex": "openvurp · Codex",
                    "claude_cli": "openvurp · Claude",
                    "anthropic": "openvurp · Claude API",
                }.get(backend, "openvurp")
                store.add_message(
                    chat_id, "assistant", reply, author_type="assistant",
                    author_id="main", author_name=author_name, run_id=run_id,
                )
                current_session = getattr(agent, "_route_runtime_sessions", {}).get(session_key)
                main_input = 0
                main_output = 0
                if current_session is not None:
                    main_input = max(
                        0, current_session.tokens.input_tokens - main_input_before
                    )
                    main_output = max(
                        0, current_session.tokens.output_tokens - main_output_before
                    )
                store.finish_run(
                    run_id,
                    input_tokens=(team.input_tokens if team else 0) + main_input,
                    output_tokens=(team.output_tokens if team else 0) + main_output,
                )
            except Exception as exc:
                store.finish_run(run_id, error=str(exc))
                raise
            finally:
                agent.ui = old_ui
            return {
                "reply": reply,
                "steps": capture.steps,
                "chat_id": chat_id,
                "team_messages": team.messages if team else [],
                "team_errors": team.errors if team else [],
                "backend": backend,
                "model": actual_model,
                "requested_backend": chat.get("backend", ""),
            }
    chat_fn.chat_store = store
    # Serve alle impostazioni per proporre gli strumenti veri da
    # spuntare, invece di un campo dove scriverne i nomi a memoria.
    chat_fn.agent = agent
    return chat_fn


class DashboardHandler(BaseHTTPRequestHandler):
    workspace_dir = OPENVURP_DIR
    chat_fn = None  # impostato da DashboardServer se l'host fornisce la chat
    chat_store: ChatStore | None = None
    token = ""      # se non vuoto, ogni richiesta deve presentarlo
    _chat_hits: deque = deque()        # rate-limit: timestamp dei messaggi recenti
    _rl_lock = threading.Lock()
    RATE_LIMIT = 30                    # max messaggi chat / minuto
    MAX_BODY_BYTES = 1_000_000
    # Gli allegati viaggiano in base64 dentro il JSON: serve una soglia sua,
    # piu' alta ma comunque chiusa. base64 gonfia di ~4/3.
    MAX_UPLOAD_BYTES = 12_000_000
    UPLOAD_BODY_BYTES = 17_000_000

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
        cls = type(self)
        if not cls.token:
            return True
        import hmac
        return hmac.compare_digest(self._presented_token(), cls.token)

    def _json_response(self, data, status: int = 200):
        body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, max_bytes: int = 0) -> dict | None:
        ctype = (self.headers.get("Content-Type", "") or "").split(";", 1)[0].strip()
        if ctype != "application/json":
            self._json_response({"error": "Content-Type deve essere application/json"}, 415)
            return None
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            self._json_response({"error": "Content-Length non valido"}, 400)
            return None
        if length < 0 or length > (max_bytes or self.MAX_BODY_BYTES):
            self._json_response({"error": "richiesta troppo grande"}, 413)
            return None
        try:
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("object required")
            return data
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            self._json_response({"error": "json non valido"}, 400)
            return None

    def do_GET(self):
        path = urlparse(self.path).path
        cls = type(self)
        workspace_dir = cls.workspace_dir


        # Branding pubblico (non sensibile): logo + favicon senza auth
        if path in {"/favicon.ico", "/favicon.png", "/favicon.svg"}:
            # Lo stesso blob degli avatar, nel colore del marchio: la scheda del
            # browser deve mostrare l'agente, non un logo di un'altra epoca.
            svg = (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><g transform="translate(16 15.5) scale(.86) translate(-16 -16)"><g fill="#e8654a"><rect x="4.48" y="24.2" width="4.38" height="5.8" rx="2.19"/><rect x="10.70" y="24.2" width="4.38" height="6.6" rx="2.19"/><rect x="16.92" y="24.2" width="4.38" height="6.6" rx="2.19"/><rect x="23.14" y="24.2" width="4.38" height="5.8" rx="2.19"/><path d="M0 14.1A14.7 14.1 0 0 1 14.7 0A17.3 14.7 0 0 1 32 14.7A16.6 17.3 0 0 1 15.4 32A15.4 17.9 0 0 1 0 14.1Z"/></g><g fill="#171717"><rect x="8.32" y="10.24" width="3.52" height="9.6" rx="1.76"/><rect x="19.84" y="10.24" width="3.52" height="9.6" rx="1.76"/></g></g></svg>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(svg)))
            self.end_headers()
            return self.wfile.write(svg)
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
        if path == "/api/activity":
            return self._json_response({"activity": self._recent_activity()})
        if path == "/api/events":
            return self._json_response({"events": collect_gateway_events(workspace_dir, limit=40)})
        if path == "/api/subagents":
            return self._json_response({"subagents": collect_subagent_runs(workspace_dir, limit=20)})
        if path == "/api/chat":
            # GET: dice solo se la chat è disponibile su questo host
            return self._json_response({"available": cls.chat_fn is not None})
        if path == "/api/settings":
            return self._json_response(self._settings_payload())
        if path == "/api/models":
            return self._json_response({"models": self._modelli_per_backend()})
        if path == "/api/local-servers":
            # Fuori dal payload delle impostazioni: bussare alle porte costa
            # secondi (misurati: 3,1) e la pagina deve aprirsi subito — la
            # sezione si riempie quando la scansione arriva. Stesso errore
            # gia' pagato con provider_catalog, stessa cura.
            return self._json_response({"servers": self._server_locali()})
        if path == "/api/whatsapp/status":
            # Il QR e lo stato vengono dal ponte vivo: la pagina li mostra
            # dove il canale si accende, non in un terminale da cercare.
            try:
                from core.channels_runtime import channel
                ch = channel("whatsapp")
            except Exception:
                ch = None
            if ch is None:
                return self._json_response({"running": False})
            return self._json_response({
                "running": ch.alive(), "connected": bool(ch.connected),
                "qr": ch.qr, "me": ch.me, "error": ch.errore,
            })
        if path == "/api/file":
            return self._serve_preview()
        if path == "/api/providers":
            return self._json_response({"providers": provider_catalog()})
        store = cls.chat_store
        parts = [part for part in path.split("/") if part]
        if path == "/api/chats" and store:
            return self._json_response({"chats": store.list_chats()})
        if len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "messages" and store:
            chat = store.get_chat(parts[2])
            if not chat:
                return self._json_response({"error": "chat non trovata"}, 404)
            return self._json_response({"chat": chat, "messages": store.list_messages(parts[2])})
        if len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "agents" and store:
            return self._json_response({"agents": store.chat_agents(parts[2])})
        if path == "/api/agents" and store:
            return self._json_response({"agents": store.list_agents()})
        if path == "/api/agents/roster" and store:
            room = store.team_room()
            members = store.chat_agents(room["id"]) if room else []
            return self._json_response({
                "roster": store.agent_roster(),
                "room": dict(room or {}, **store.chat_activity(room["id"]),
                             member_ids=[m["id"] for m in members]) if room else None,
            })
        self.send_error(404)

    def do_POST(self):
        if not self._authed():
            return self.send_error(401, "Unauthorized")
        path = urlparse(self.path).path
        cls = type(self)
        store = cls.chat_store
        parts = [part for part in path.split("/") if part]
        if path == "/api/chats" and store:
            data = self._read_json()
            if data is None:
                return
            return self._json_response(store.create_chat(
                title=str(data.get("title", "Nuova chat")),
                mode=str(data.get("mode", "solo")),
                backend=clean_backend(data.get("backend", "")),
                model=str(data.get("model", "")),
            ), 201)
        if path == "/api/agents" and store:
            data = self._read_json()
            if data is None:
                return
            if not str(data.get("name", "")).strip():
                return self._json_response({"error": "nome agente mancante"}, 400)
            return self._json_response(store.create_agent(
                str(data.get("name", "")), str(data.get("role", "peer")),
                str(data.get("instructions", "")), clean_backend(data.get("backend", "")),
                str(data.get("model", "")),
            ), 201)
        if path == "/api/settings":
            data = self._read_json()
            if data is None:
                return
            values = data.get("values")
            if not isinstance(values, dict) or not values:
                return self._json_response({"error": "nessun valore da salvare"}, 400)
            return self._json_response(self._save_settings(values))
        if len(parts) == 3 and parts[:2] == ["api", "approvals"]:
            data = self._read_json()
            if data is None:
                return
            from core.approvals import answer
            ok = answer(parts[2], str(data.get("choice", "no")))
            return self._json_response({"ok": ok}, 200 if ok else 410)
        if path == "/api/upload":
            return self._handle_upload()
        if len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "stop":
            # «Ok, stop adesso». La discussione si ferma al prossimo intervento:
            # non si tronca un agente a meta' frase, si smette di dargli la parola.
            from core.multiplayer import request_stop
            request_stop(parts[2])
            return self._json_response({"ok": True})
        if len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "read" and store:
            store.mark_read(parts[2])
            return self._json_response({"ok": True})
        if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] == "chat" and store:
            # Apri (o riprendi) la conversazione a due con questo agente.
            chat = store.direct_chat_for_agent(parts[2])
            if chat is None:
                return self._json_response({"error": "agente non trovato"}, 404)
            return self._json_response(chat)
        if len(parts) == 4 and parts[:2] == ["api", "chats"] and parts[3] == "agents" and store:
            data = self._read_json()
            if data is None:
                return
            ids = data.get("agent_ids", [])
            if not isinstance(ids, list):
                return self._json_response({"error": "agent_ids deve essere una lista"}, 400)
            return self._json_response({"agents": store.set_chat_agents(parts[2], ids)})
        if path != "/api/chat":
            return self.send_error(404)
        chat_fn = cls.chat_fn
        if chat_fn is None:
            return self._json_response(
                {"error": "chat non disponibile: avvia la dashboard dall'host con l'agente"},
                status=503,
            )
        # rate-limit: anche col token, evita raffiche/DoS sulla chat
        now = _time.time()
        with cls._rl_lock:
            hits = cls._chat_hits
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= cls.RATE_LIMIT:
                return self._json_response(
                    {"error": "rate limit: troppi messaggi, rallenta."}, status=429)
            hits.append(now)
        data = self._read_json()
        if data is None:
            return
        message = str(data.get("message", "")).strip()
        raw_attachments = data.get("attachments") or []
        attachments = [str(a) for a in raw_attachments if str(a).strip()][:8] \
            if isinstance(raw_attachments, list) else []
        if not message and not attachments:
            return self._json_response({"error": "messaggio vuoto"}, status=400)
        # Niente testo inventato al posto tuo: se mandi solo un file, in chat
        # si vede il file. Le istruzioni per l'agente viaggiano gia' nel blocco
        # ALLEGATI, che la pagina non mostra.
        try:
            chat_id = str(data.get("chat_id", "")).strip()
            try:
                result = chat_fn(message, chat_id=chat_id, attachments=attachments)
            except TypeError as exc:
                # Compatibilita' con host/test che espongono ancora callable(message)
                # o la firma senza allegati.
                text = str(exc)
                if "attachments" in text:
                    result = chat_fn(message, chat_id=chat_id)
                elif "chat_id" in text:
                    result = chat_fn(message)
                else:
                    raise
        except Exception as exc:
            return self._json_response({"error": f"errore agente: {exc}"}, status=500)
        if isinstance(result, dict):
            return self._json_response(result)
        return self._json_response({"reply": result})

    # Estensioni che l'agente sa davvero leggere con i suoi tool. Tutto il
    # resto viene rifiutato: un allegato che nessuno puo' aprire e' solo un file
    # sconosciuto scritto su disco.
    UPLOAD_SUFFIXES = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
        ".pdf", ".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml",
        ".py", ".js", ".ts", ".html", ".css", ".sh", ".toml", ".ini",
        # Il microfono del browser produce webm o ogg secondo il browser:
        # senza questi, registrare dal web finirebbe con "tipo non supportato".
        ".wav", ".mp3", ".m4a", ".ogg", ".webm", ".opus", ".oga",
    }

    # Chiavi che si possono cambiare dal web. Elenco esplicito: un endpoint che
    # scrive un .env con qualunque nome gli arriva e' una porta aperta.
    SETTABLE = (
        "LLM_BACKEND", "LLM_MODEL", "LLM_BASE_URL",
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
        "CODEX_MODEL", "CLAUDE_CLI_MODEL",
        "TELEGRAM_TOKEN", "TELEGRAM_ALLOWED_USERS",
        "VOICE_ENABLED", "DASHBOARD_PORT", "TOOL_ROUTER_MODE",
        "SWARM_TOOLS", "SWARM_MAX_AGENTS", "SWARM_DAILY_CALL_BUDGET",
        "TELEGRAM_CHAT_ID",
        "OPENAI_COMPATIBLE_BASE_URL", "OPENAI_COMPATIBLE_MODEL",
        "OPENAI_COMPATIBLE_API_KEY",
        # Canali in entrata. Le liste di autorizzati stanno qui perche' sono la
        # cosa che va cambiata piu' spesso, ed e' anche l'unica che separa
        # «parlo con i miei agenti dal telefono» da «chiunque comanda il mio PC».
        "CHANNELS_IN",
        "DISCORD_TOKEN", "DISCORD_ALLOWED_USERS",
        "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_ALLOWED_USERS",
        "WHATSAPP_ALLOWED_USERS",
        "MULTIPLAYER_MAX_ROUNDS",
    )
    SECRETS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
               "OPENAI_COMPATIBLE_API_KEY",
               "TELEGRAM_TOKEN", "DISCORD_TOKEN", "SLACK_BOT_TOKEN",
               "SLACK_APP_TOKEN")

    def _recent_activity(self, limit: int = 80) -> list[dict]:
        """Cosa hanno fatto gli agenti, dal registro di controllo.

        La scheda mostrava gli eventi interni del gateway (subagent.spawned e
        simili): diagnostica di un componente che quasi nessuno accende. La
        domanda vera in un portafoglio di agenti che possono eseguire comandi
        e' un'altra — chi ha fatto cosa — e la risposta era gia' su disco,
        nell'audit, senza che nulla la mostrasse.
        """
        import json as _json
        from datetime import datetime

        path = os.path.join(type(self).workspace_dir, "memory", "audit", "audit.jsonl")
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                lines = handle.readlines()[-max(1, min(limit, 500)):]
        except OSError:
            return []

        rows: list[dict] = []
        for raw in reversed(lines):          # i piu' recenti in cima
            try:
                item = _json.loads(raw)
            except ValueError:
                continue
            details = item.get("details")
            if isinstance(details, str):
                try:
                    details = _json.loads(details)
                except ValueError:
                    details = {}
            details = details or {}
            args = details.get("args")
            if isinstance(args, str):
                try:
                    args = _json.loads(args)
                except ValueError:
                    pass
            if isinstance(args, dict):
                summary = ", ".join(f"{k}={str(v)[:70]}" for k, v in args.items())
            else:
                summary = str(args or "")
            source = str(item.get("source", "") or "")
            try:
                when = datetime.fromtimestamp(float(item.get("timestamp", 0)))
                stamp = when.strftime("%d/%m %H:%M")
            except Exception:
                stamp = ""
            rows.append({
                "when": stamp,
                # "agent:dev" -> "dev"; il resto sono i canali (cli, telegram...)
                "who": source.split(":", 1)[1] if source.startswith("agent:") else (source or "openvurp"),
                "by_agent": source.startswith("agent:"),
                "action": str(item.get("action", "")),
                "target": str(item.get("target", "")),
                "args": summary[:220],
                "ok": bool(item.get("success", True)),
                "risk": str(details.get("risk", "") or item.get("risk_level", "")),
                "ms": details.get("duration_ms"),
            })
        return rows

    def _settings_payload(self) -> dict:
        from core.setup_wizard import (
            SUBSCRIPTION_BACKENDS, current_config, detect_ollama_models,
            subscription_login_status,
        )

        env = current_config()
        logins = {}
        for backend, (_binary, _model, how) in SUBSCRIPTION_BACKENDS.items():
            ok, detail = subscription_login_status(backend)
            logins[backend] = {"ok": ok, "detail": detail, "command": how}
        ollama_url = env.get("LLM_BASE_URL", "") or "http://localhost:11434"
        try:
            models = detect_ollama_models(ollama_url)
        except Exception:
            models = []
        return {
            "values": {
                k: env.get(k, "") for k in self.SETTABLE if k not in self.SECRETS
            },
            # I segreti non tornano MAI indietro: si sa solo se ci sono.
            "secrets": {k: bool(env.get(k, "").strip()) for k in self.SECRETS},
            "providers": provider_catalog(),
            "logins": logins,
            "ollama": {"url": ollama_url, "models": models},
            # Le scelte possibili, non un campo dove indovinare cosa scrivere.
            "tools": self._strumenti_disponibili(),
            "telegram_people": self._telegram_conosciuti(),
            "channels_running": self._canali_accesi(),
        }

    @classmethod
    def _strumenti_disponibili(cls) -> list[str]:
        """I nomi veri degli strumenti, per poterli spuntare invece di scriverli."""
        agent = getattr(cls.chat_fn, "agent", None)
        registro = getattr(agent, "tools", None)
        try:
            return sorted(n for n in registro.names() if n)
        except Exception:
            return []

    # Porte note dei server locali che parlano il dialetto OpenAI. Chi ha
    # gia' un'AI locale non deve sapere che porta usa il suo programma: si
    # bussa a tutte, chi risponde a /v1/models esiste.
    PORTE_LOCALI = (
        (1234, "LM Studio"), (8080, "llama.cpp"), (8000, "vLLM"),
        (1337, "Jan"), (5001, "koboldcpp"), (5000, "text-generation-webui"),
        (4891, "GPT4All"),
    )

    @classmethod
    def _server_locali(cls) -> list[dict]:
        """Chi risponde adesso su localhost, con i suoi modelli."""
        import concurrent.futures as futures

        import requests

        def bussa(porta: int, nome: str):
            url = f"http://127.0.0.1:{porta}/v1"
            try:
                r = requests.get(url + "/models", timeout=0.35)
                r.raise_for_status()
                modelli = [str(m.get("id", "")) for m in r.json().get("data", [])]
            except Exception:
                return None
            return {"name": nome, "url": url,
                    "models": [m for m in modelli if m][:12]}

        with futures.ThreadPoolExecutor(max_workers=8) as pool:
            esiti = pool.map(lambda pn: bussa(*pn), cls.PORTE_LOCALI)
        return [e for e in esiti if e]

    @classmethod
    def _modelli_per_backend(cls) -> dict:
        """I modelli fra cui scegliere, backend per backend.

        Il menu del motore chiedeva di SCRIVERE il nome del modello: un nome
        interno, da ricordare lettera per lettera. Dove i modelli si possono
        sapere, si offrono: Ollama e i server locali si interrogano dal vivo,
        per gli abbonamenti c'e' il catalogo, e in ogni caso si aggiungono i
        nomi gia' in uso negli agenti — quelli esistono per definizione.
        """
        import config as cfg

        fuori: dict[str, list] = {
            "codex": ["gpt-5.6-luna", "gpt-5.6-terra"],
            "claude_cli": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"],
            "anthropic": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"],
            "openai": [], "groq": [], "ollama": [], "openai_compatible": [],
        }
        try:
            from core.setup_wizard import current_config, detect_ollama_models
            url = current_config().get("LLM_BASE_URL", "") or "http://localhost:11434"
            fuori["ollama"] = detect_ollama_models(url)[:30]
        except Exception:
            pass
        try:
            import requests
            base = str(getattr(cfg, "OPENAI_COMPATIBLE_BASE_URL", "") or "")
            if base:
                r = requests.get(base.rstrip("/") + "/models", timeout=1.5)
                r.raise_for_status()
                fuori["openai_compatible"] = [
                    str(m.get("id", "")) for m in r.json().get("data", [])][:30]
        except Exception:
            pass
        # I nomi gia' impostati (config e rubrica) esistono per definizione.
        for chiave, dove in (("CODEX_MODEL", "codex"), ("CLAUDE_CLI_MODEL", "claude_cli"),
                             ("LLM_MODEL", ""), ("OPENAI_COMPATIBLE_MODEL", "openai_compatible")):
            valore = str(getattr(cfg, chiave, "") or "").strip()
            if valore and dove and valore not in fuori[dove]:
                fuori[dove].insert(0, valore)
        store = cls.chat_store
        if store is not None:
            try:
                for agente in store.list_agents():
                    b, m = str(agente.get("backend", "")), str(agente.get("model", "")).strip()
                    if m and b in fuori and m not in fuori[b]:
                        fuori[b].append(m)
            except Exception:
                pass
        return fuori

    @staticmethod
    def _canali_accesi() -> list[str]:
        try:
            from core.channels_runtime import SUPERVISOR
            return SUPERVISOR.running()
        except Exception:
            return []

    @staticmethod
    def _telegram_conosciuti() -> list[dict]:
        """Chi ha scritto al bot, per spuntarlo invece di copiare un numero.

        L'id di Telegram non si indovina: finora andava trovato a mano su
        getUpdates e incollato. Il bot pero' lo sa gia', per chiunque gli abbia
        scritto almeno una volta.
        """
        import config as cfg
        token = str(getattr(cfg, "TELEGRAM_TOKEN", "") or "")
        if not token:
            return []
        try:
            import requests
            risposta = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"limit": 100, "timeout": 0}, timeout=6)
            risposta.raise_for_status()
            aggiornamenti = risposta.json().get("result", [])
        except Exception:
            return []
        gente: dict[str, dict] = {}
        for update in aggiornamenti:
            messaggio = (update.get("message") or update.get("edited_message")
                         or update.get("channel_post") or {})
            chi = messaggio.get("from") or {}
            uid = str(chi.get("id", "") or "")
            if not uid:
                continue
            nome = " ".join(x for x in (chi.get("first_name"), chi.get("last_name")) if x)
            gente[uid] = {"id": uid, "name": nome or chi.get("username") or uid,
                          "username": chi.get("username", "")}
        return sorted(gente.values(), key=lambda x: x["name"].lower())

    def _save_settings(self, values: dict) -> dict:
        from core.setup_wizard import write_env

        clean = {}
        for key, raw in values.items():
            if key not in self.SETTABLE:
                continue
            text = str(raw if raw is not None else "").strip()
            # Un segreto lasciato vuoto significa "non toccarlo", non "cancellalo":
            # il web non lo vede mai, quindi non puo' rimandarlo indietro.
            if key in self.SECRETS and not text:
                continue
            clean[key] = text
        if not clean:
            return dict(self._settings_payload(), saved=[])
        write_env(clean)
        # Il processo gira gia': aggiorna anche la config viva, cosi' il motore
        # cambia dal turno successivo senza riavviare.
        try:
            import config as cfg
            for key, text in clean.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, _come_prima(getattr(cfg, key), text))
        except Exception:
            pass

        # Una casella che richiede un riavvio per avere effetto e' una casella
        # che mente: i canali si riallineano subito a quello che hai salvato.
        canali = None
        if any(k in clean for k in (
                "CHANNELS_IN", "TELEGRAM_TOKEN", "TELEGRAM_ALLOWED_USERS",
                "DISCORD_TOKEN", "DISCORD_ALLOWED_USERS", "SLACK_BOT_TOKEN",
                "SLACK_APP_TOKEN", "SLACK_ALLOWED_USERS")):
            try:
                from core.channels_runtime import SUPERVISOR
                canali = SUPERVISOR.apply()
            except Exception as exc:
                canali = {"errors": [str(exc)], "running": [], "started": [], "stopped": []}
        return dict(self._settings_payload(), saved=sorted(clean), channels=canali)

    # ── Anteprima dei file ───────────────────────────────────────────────
    # Un endpoint che serve un file per percorso e' un buco di lettura se non
    # e' chiuso bene: qui il percorso viene RISOLTO (quindi niente ".." e
    # niente collegamenti simbolici che escono) e deve cadere dentro il
    # workspace. Poi ci sono cose che stanno dentro il workspace e non vanno
    # servite comunque — il .env, i database, le chiavi.
    ANTEPRIMA_MAX_TESTO = 2_000_000
    ANTEPRIMA_MAX_BYTES = 40_000_000
    ANTEPRIMA_VIETATI = (".env", ".git", "id_rsa", ".ssh", "vault")
    ANTEPRIMA_SUFFISSI_VIETATI = (".db", ".sqlite", ".sqlite3", ".key", ".pem", ".pfx")
    ANTEPRIMA_TIPI = {
        ".html": "text/html", ".htm": "text/html",
        ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
        ".svg": "image/svg+xml", ".bmp": "image/bmp", ".ico": "image/x-icon",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
        ".mp4": "video/mp4", ".webm": "video/webm",
    }

    @classmethod
    def anteprima_percorso(cls, grezzo: str) -> str:
        """Il percorso da servire, oppure "" se non si puo'."""
        grezzo = str(grezzo or "").strip()
        if not grezzo:
            return ""
        try:
            vero = os.path.realpath(os.path.join(cls.workspace_dir, grezzo))
            radice = os.path.realpath(cls.workspace_dir)
        except Exception:
            return ""
        # commonpath invece di startswith: "/casa/vurp-altro" comincia per
        # "/casa/vurp" ma non ci sta dentro.
        try:
            if os.path.commonpath([vero, radice]) != radice:
                return ""
        except ValueError:
            return ""
        if not os.path.isfile(vero):
            return ""
        pezzi = os.path.relpath(vero, radice).replace("\\", "/").split("/")
        for pezzo in pezzi:
            basso = pezzo.lower()
            if any(basso == v or basso.startswith(v + ".") for v in cls.ANTEPRIMA_VIETATI):
                return ""
        if os.path.splitext(vero)[1].lower() in cls.ANTEPRIMA_SUFFISSI_VIETATI:
            return ""
        return vero

    @staticmethod
    def anteprima_linguaggio(nome: str) -> str:
        return {
            ".py": "python", ".js": "javascript", ".ts": "typescript",
            ".json": "json", ".html": "html", ".css": "css", ".sh": "bash",
            ".md": "markdown", ".yml": "yaml", ".yaml": "yaml", ".sql": "sql",
            ".c": "c", ".h": "c", ".cpp": "cpp", ".rs": "rust", ".go": "go",
            ".java": "java", ".rb": "ruby", ".php": "php", ".toml": "toml",
            ".ini": "ini", ".xml": "xml", ".csv": "csv", ".txt": "",
        }.get(os.path.splitext(nome)[1].lower(), "")

    def _serve_preview(self):
        params = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        vero = self.anteprima_percorso(params.get("path", ""))
        if not vero:
            return self._json_response({"error": "file non disponibile"}, 404)
        nome = os.path.basename(vero)
        suffisso = os.path.splitext(nome)[1].lower()
        dimensione = os.path.getsize(vero)

        if params.get("as") == "pdfmeta":
            # Le pagine di un PDF le rendiamo noi (PyMuPDF): dentro la scheda
            # vanno fogli puliti, non il visore del browser con la sua barra.
            if suffisso != ".pdf":
                return self._json_response({"error": "non e' un PDF"}, 415)
            try:
                import fitz
                with fitz.open(vero) as doc:
                    pagine = doc.page_count
            except ImportError:
                return self._json_response(
                    {"error": "manca PyMuPDF: pip install PyMuPDF"}, 501)
            except Exception as exc:
                return self._json_response({"error": str(exc)}, 500)
            return self._json_response({"name": nome, "size": dimensione,
                                        "pages": min(pagine, 120)})

        if params.get("as") == "pdfpage":
            if suffisso != ".pdf":
                return self._json_response({"error": "non e' un PDF"}, 415)
            try:
                pagina = max(1, int(params.get("page", "1")))
            except ValueError:
                pagina = 1
            try:
                import fitz
                with fitz.open(vero) as doc:
                    if pagina > min(doc.page_count, 120):
                        return self._json_response({"error": "pagina inesistente"}, 404)
                    # Zoom 2: leggibile su schermi densi senza fare file enormi.
                    pix = doc[pagina - 1].get_pixmap(matrix=fitz.Matrix(2, 2))
                    corpo = pix.tobytes("png")
            except ImportError:
                return self._json_response({"error": "manca PyMuPDF"}, 501)
            except Exception as exc:
                return self._json_response({"error": str(exc)}, 500)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(corpo)
            return

        if params.get("as") == "text":
            if dimensione > self.ANTEPRIMA_MAX_TESTO:
                return self._json_response(
                    {"error": f"troppo grande da mostrare ({dimensione // 1000} KB)"}, 413)
            try:
                with open(vero, "r", encoding="utf-8", errors="replace") as handle:
                    testo = handle.read()
            except OSError as exc:
                return self._json_response({"error": str(exc)}, 500)
            return self._json_response({
                "name": nome, "path": vero, "size": dimensione,
                "lang": self.anteprima_linguaggio(nome),
                "lines": testo.count("\n") + 1, "text": testo,
            })

        if dimensione > self.ANTEPRIMA_MAX_BYTES:
            return self._json_response({"error": "file troppo grande"}, 413)
        try:
            with open(vero, "rb") as handle:
                corpo = handle.read()
        except OSError as exc:
            return self._json_response({"error": str(exc)}, 500)
        self.send_response(200)
        self.send_header("Content-Type",
                         self.ANTEPRIMA_TIPI.get(suffisso, "application/octet-stream"))
        self.send_header("Content-Length", str(len(corpo)))
        # Si guarda, non si scarica. E il browser non deve indovinare il tipo:
        # un .txt interpretato come HTML eseguirebbe quello che c'e' dentro.
        self.send_header("Content-Disposition", "inline")
        self.send_header("X-Content-Type-Options", "nosniff")
        # L'HTML in anteprima puo' avere stili e immagini incorporate, mai
        # script ne' rete: e' un'anteprima, non un'app.
        self.send_header("Content-Security-Policy",
                         "sandbox; default-src 'none'; "
                         "style-src 'unsafe-inline'; img-src data:")
        self.end_headers()
        self.wfile.write(corpo)

    def _handle_upload(self):
        import base64
        import re as _re
        from datetime import datetime

        data = self._read_json(self.UPLOAD_BODY_BYTES)
        if data is None:
            return
        raw_name = str(data.get("name", "") or "file")
        # Solo il nome finale, niente percorsi: un allegato non deve poter
        # scegliere dove atterrare.
        safe = _re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(raw_name))[:80] or "file"
        suffix = os.path.splitext(safe)[1].lower()
        if suffix not in self.UPLOAD_SUFFIXES:
            return self._json_response(
                {"error": f"tipo non supportato ({suffix or 'senza estensione'})"}, 415)
        try:
            blob = base64.b64decode(str(data.get("data", "")), validate=True)
        except Exception:
            return self._json_response({"error": "contenuto non valido"}, 400)
        if not blob:
            return self._json_response({"error": "file vuoto"}, 400)
        if len(blob) > self.MAX_UPLOAD_BYTES:
            return self._json_response(
                {"error": f"file troppo grande (max {self.MAX_UPLOAD_BYTES // 1_000_000} MB)"},
                413)

        root = os.path.join(type(self).workspace_dir, "memory", "uploads")
        os.makedirs(root, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = os.path.join(root, f"{stamp}-{safe}")
        n = 1
        while os.path.exists(target):
            target = os.path.join(root, f"{stamp}-{n}-{safe}")
            n += 1
        with open(target, "wb") as handle:
            handle.write(blob)
        return self._json_response({
            "path": target, "name": safe, "size": len(blob),
        }, 201)

    def do_DELETE(self):
        if not self._authed():
            return self.send_error(401, "Unauthorized")
        parts = [part for part in urlparse(self.path).path.split("/") if part]
        store = type(self).chat_store
        if not store or len(parts) != 3 or parts[:2] != ["api", "agents"]:
            return self.send_error(404)
        if not store.delete_agent(parts[2]):
            return self._json_response({"error": "agente non trovato"}, 404)
        return self._json_response({"deleted": parts[2]})

    def do_PATCH(self):
        if not self._authed():
            return self.send_error(401, "Unauthorized")
        path = urlparse(self.path).path
        parts = [part for part in path.split("/") if part]
        store = type(self).chat_store
        if not store or len(parts) != 3 or parts[0] != "api":
            return self.send_error(404)
        data = self._read_json()
        if data is None:
            return
        if parts[1] == "agents":
            agent = store.update_agent(
                parts[2],
                name=str(data["name"]) if "name" in data else None,
                role=str(data["role"]) if "role" in data else None,
                instructions=(
                    str(data["instructions"]) if "instructions" in data else None
                ),
                backend=(
                    clean_backend(data["backend"]) if "backend" in data else None
                ),
                model=str(data["model"]) if "model" in data else None,
                enabled=bool(data["enabled"]) if "enabled" in data else None,
            )
            if not agent:
                return self._json_response({"error": "agente non trovato"}, 404)
            return self._json_response(agent)
        if parts[1] != "chats":
            return self.send_error(404)
        chat = store.update_chat(
            parts[2],
            title=str(data["title"]) if "title" in data else None,
            mode=str(data["mode"]) if "mode" in data else None,
            backend=(clean_backend(data["backend"]) if "backend" in data else None),
            model=str(data["model"]) if "model" in data else None,
            archived=bool(data["archived"]) if "archived" in data else None,
        )
        if not chat:
            return self._json_response({"error": "chat non trovata"}, 404)
        return self._json_response(chat)

    def _serve_stream(self):
        """SSE live filtrato per chat; il replay iniziale è configurabile."""
        try:
            from core import activity
        except Exception:
            return self.send_error(503)
        q, snapshot = activity.subscribe()
        try:
            from urllib.parse import parse_qs
            stream_query = parse_qs(urlparse(self.path).query)
            requested_chat = stream_query.get("chat_id", [""])[0]
            replay = stream_query.get("replay", ["1"])[0].lower() not in {
                "0", "false", "no", "off",
            }
        except Exception:
            requested_chat = ""
            replay = True
        def wanted(evt):
            return not requested_chat or evt.get("chat_id", "") == requested_chat
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            for evt in filter_stream_snapshot(snapshot, requested_chat, replay):
                self.wfile.write(b"data: " + json.dumps(evt).encode("utf-8") + b"\n\n")
            self.wfile.flush()
            while True:
                try:
                    evt = q.get(timeout=15)
                    if not wanted(evt):
                        continue
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

    def _with_boot(self, html: str) -> str:
        return html.replace("<script>", self._boot_script() + "\n<script>", 1)

    def _boot_script(self) -> str:
        """Lo stato iniziale, scritto dentro la pagina."""
        cls = type(self)
        dati = {}
        store = cls.chat_store
        # Se la lettura del database e' ancora in corso, la pagina non la
        # aspetta: meglio la rubrica un attimo dopo che tre secondi di bianco.
        pronto = getattr(cls, "boot_ready", None)
        if pronto is not None and not pronto.is_set():
            store = None
        if store is not None:
            try:
                dati["roster"] = store.agent_roster()
                dati["room"] = store.team_room(create=False)
            except Exception:
                dati = {}
        # I provider NO: sondare i backend (Ollama, gli accessi ai CLI) alla
        # prima chiamata costa 3,2 secondi contro i 78 ms di tutto il resto.
        # Servono alle impostazioni e al badge del motore, non al primo
        # disegno: la pagina li chiede per conto suo, e intanto sono gia'
        # stati scaldati in background.
        testo = json.dumps(dati, ensure_ascii=False, default=str)
        testo = testo.replace("<", "\\u003c").replace(">", "\\u003e")
        return f"<script>window.__BOOT__={testo};</script>"

    def _serve_html(self):
        html = '<!DOCTYPE html>\n<html lang="it">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n<title>openvurp — wallet for agents</title>\n<link rel="icon" type="image/svg+xml" href="/favicon.svg">\n<style>\n:root{\n  --bg:#212121; --side:#171717; --raised:#2f2f2f; --hover:#2a2a2a;\n  --border:rgba(255,255,255,.08); --text:#ececec; --text-dim:#cdcdcd;\n  --muted:#9b9b9b; --faint:#6e6e6e;\n  --accent:#e8654a; --accent-hover:#ff7a5e; --accent-dim:rgba(232,101,74,.14);\n  --ok:#4ade80; --bad:#f87171;\n}\n*{margin:0;padding:0;box-sizing:border-box}\nhtml,body{height:100%}\nbody{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;\n background:var(--bg);color:var(--text);font-size:15px;-webkit-font-smoothing:antialiased;\n overflow:hidden}\n.sec{font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;font-weight:600}\n.modebtn{border:1px solid var(--border);background:var(--raised);color:var(--text-dim);\n border-radius:9px;padding:7px 12px;font-size:13px;cursor:pointer}\n.modebtn:hover{background:var(--hover);color:var(--text)}\n.emptymsg{color:var(--faint);font-size:13px;padding:14px 12px}\n/* ── Rubrica agenti: una lista di conversazioni, non una tabella di config ── */\n.roster{max-width:560px;margin:0 auto}\n.rtop{display:flex;align-items:center;gap:10px;margin-bottom:16px}\n.rfold{border:0;background:none;color:var(--faint);cursor:pointer;padding:4px;\n  border-radius:8px;display:flex;transition:color .12s,transform .18s}\n.rfold svg{width:17px;height:17px}\n.rfold:hover{color:var(--text)}\n/* Chiusa resta solo la fila degli agenti: nomi, ricerca e piede spariscono. */\nbody.folded .left{flex:0 0 76px;padding:18px 8px 14px}\n/* Scoped alla colonna: `body.folded .rmore` nudo nascondeva anche i bottoni\n   dell\'intestazione della chat (parla, menu), che di suo usano la stessa\n   classe — chiudevi la sidebar e sparivano da tutt\'altra parte della pagina. */\nbody.folded .left .rsearch,body.folded .left .rheads,\nbody.folded .left .rtop .sec,body.folded .left .rmeta,body.folded .left .rmore,\nbody.folded .left .rbadge,body.folded .left .emptymsg,\nbody.folded .left .radd{display:none}\n/* Il pulsante per riaprire deve restare visibile e cliccabile: con la colonna\n   a 76px il resto della riga lo spingeva fuori e la barra non si riapriva. */\nbody.folded .rfold{display:flex;margin:0 auto}\n/* Il piede resta, ma ridotto all\'osso: il pallino della connessione e\n   l\'ingranaggio. Nasconderlo tutto significava togliere l\'unico ingresso\n   alle impostazioni proprio quando la colonna e\' chiusa. */\nbody.folded .rfoot{display:flex;flex-direction:column;gap:8px;\n  justify-content:center;padding-top:14px}\nbody.folded .rfoot #rfoot,body.folded .rfoot>span[style]{display:none}\nbody.folded .rgear{margin:0 auto}\nbody.folded .rrow{justify-content:center;padding:8px 0;gap:0;position:relative}\nbody.folded .rstack{width:42px;height:42px}\nbody.folded .rfold{transform:rotate(180deg)}\nbody.folded .rtop{justify-content:center;margin-bottom:10px}\nbody.folded .left .radd{padding:6px}\nbody.folded .runread{position:absolute;top:-2px;right:14px;border:2px solid var(--bg)}\n/* A colonna chiusa restavano solo i polpi: distinti fra loro, ma senza un nome\n   uno non sa chi e\' chi. Sotto ogni avatar il nome in piccolo (basta a\n   riconoscerlo di colpo), e passandoci sopra la targhetta col nome intero e\n   il mestiere — quella che serve quando il nome e\' lungo o si somigliano. */\n.rmini,.rtip{display:none}\nbody.folded .rrow{flex-direction:column;gap:3px;padding:7px 0 9px}\nbody.folded .rmini{display:block;max-width:64px;font-size:10px;line-height:1.2;\n  color:var(--muted);text-align:center;white-space:nowrap;overflow:hidden;\n  text-overflow:ellipsis;letter-spacing:.1px}\nbody.folded .rrow.on .rmini{color:var(--accent);font-weight:650}\nbody.folded .rtip{flex-direction:column;gap:1px;\n  background:var(--raised);border:1px solid var(--border);border-radius:10px;\n  padding:7px 11px;white-space:nowrap;z-index:60;\n  transition:opacity .12s;box-shadow:0 8px 26px rgba(0,0,0,.45)}\nbody.folded .rtip b{font-size:13px;color:var(--text);font-weight:650}\nbody.folded .rtip i{font-size:11.5px;color:var(--muted);font-style:normal}\n\nbody.folded .runread{top:0;right:10px}\n/* La targhetta non puo\' allargare la colonna: aprendo `overflow:visible` per\n   farla uscire comparivano una barra orizzontale e una pagina piu\' larga dello\n   schermo. Esce dal flusso (position:fixed) e la posiziona il codice. */\nbody.folded .left{overflow:hidden}\nbody.folded #rrows{overflow-x:hidden}\nbody.folded .rtip{position:fixed;left:0;top:0;transform:none;\n  opacity:0;pointer-events:none;display:flex}\nbody.folded .rtip.on{opacity:1}\nbody.folded .rrow:hover .rtip{opacity:0}\n\n.rtop .sec{margin:0;flex:1}\n/* Un\'azione, non un pulsante: niente riquadro finche\' non ci passi sopra. */\n.radd{display:flex;align-items:center;gap:6px;border:0;background:none;\n  color:var(--muted);font:inherit;font-size:13px;font-weight:600;cursor:pointer;\n  padding:6px 10px;border-radius:9px;transition:background .12s,color .12s}\n.radd svg{width:14px;height:14px}\n.radd:hover{background:var(--raised);color:var(--text)}\n.rsearch{display:flex;align-items:center;gap:9px;background:var(--raised);\n  border:1px solid transparent;border-radius:11px;padding:8px 12px;margin-bottom:16px;\n  color:var(--faint);font-size:14px;transition:border-color .14s,background .14s}\n.rsearch:focus-within{border-color:var(--border);background:var(--bg);color:var(--muted)}\n.rsearch input{flex:1;background:none;border:0;outline:0;color:var(--text);font:inherit}\n.rheads{display:flex;gap:24px;padding:2px 4px 14px;overflow-x:auto;\n  overscroll-behavior-x:contain;scroll-snap-type:x proximity;\n  scrollbar-width:none;-ms-overflow-style:none;\n  transition:-webkit-mask-image .18s,mask-image .18s}\n.rheads::-webkit-scrollbar{display:none}\n.rhead{scroll-snap-align:center}\n/* Il bordo sfuma SOLO dal lato dove c\'e\' ancora qualcosa: e\' l\'indizio che\n   sostituisce la barra, e non mente quando la lista sta tutta nello schermo. */\n.rheads.fl{-webkit-mask-image:linear-gradient(90deg,transparent,#000 30px);\n  mask-image:linear-gradient(90deg,transparent,#000 30px)}\n.rheads.fr{-webkit-mask-image:linear-gradient(90deg,#000 calc(100% - 30px),transparent);\n  mask-image:linear-gradient(90deg,#000 calc(100% - 30px),transparent)}\n.rheads.fl.fr{-webkit-mask-image:linear-gradient(90deg,transparent,#000 30px,#000 calc(100% - 30px),transparent);\n  mask-image:linear-gradient(90deg,transparent,#000 30px,#000 calc(100% - 30px),transparent)}\n.rhead{text-align:center;cursor:pointer;flex:none}\n.rhead .lbl{margin-top:7px;font-size:12px;color:var(--muted);max-width:76px;\n  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n/* Vive: galleggia, sbatte le palpebre, guarda in giro, ondeggia le braccia.\n   Ogni ritmo e\' sfalsato per avatar (--blink/--float/--look): all\'unisono\n   sembrerebbe un glitch dello schermo, non una rubrica di esseri.\n   Solo transform e opacity: girano sul compositor, restano fluide anche con\n   venti polpi sullo schermo.                                              */\n/* Il blob originale: una macchia di colore e due occhi. In piu\' quattro\n   monconi sotto — bastano a dire "polpo" senza diventare un disegno. */\n.blob{position:relative;flex:none;cursor:pointer;transform-origin:50% 55%;\n  border-radius:46% 54% 52% 48% / 44% 46% 54% 56%;\n  animation-duration:var(--float,4.5s);animation-timing-function:ease-in-out;\n  animation-iteration-count:infinite;animation-name:ovbob}\n.blob .eyes{position:absolute;inset:0;\n  animation:ovlook var(--look,9s) ease-in-out infinite}\n.blob i{position:absolute;top:32%;width:11%;height:30%;background:var(--side);\n  border-radius:99px;transform-origin:center;\n  animation:ovblink var(--blink,6s) infinite}\n.blob i.l{left:26%} .blob i.r{left:62%}\n.blob .legs{position:absolute;left:14%;right:14%;bottom:-7%;height:26%;\n  display:flex;justify-content:space-between;\n  animation:ovwave calc(var(--float,4.5s) * 1.3) ease-in-out infinite}\n.blob .legs b{width:19%;height:100%;background:var(--c);\n  border-radius:0 0 99px 99px;transform-origin:50% 0}\n.blob .legs b:nth-child(2){height:112%} .blob .legs b:nth-child(3){height:104%}\n\n@keyframes ovblink{0%,95.5%,100%{transform:scaleY(1)}97.5%{transform:scaleY(.08)}}\n@keyframes ovlook{0%,42%,100%{transform:translateX(0)}\n  50%,58%{transform:translateX(4%)}68%,80%{transform:translateX(-4%)}}\n@keyframes ovwave{0%,100%{transform:skewX(0deg)}50%{transform:skewX(3deg)}}\n\n/* Cinque indoli diverse: variare solo i TEMPI lasciava tutti a fare la stessa\n   cosa sfasata. Qui ognuno si muove proprio in un altro modo. */\n@keyframes ovbob{0%,100%{transform:translateY(0)}50%{transform:translateY(-2px)}}\n@keyframes ovsway{0%,100%{transform:translateX(-1.8px) rotate(-1.5deg)}\n  50%{transform:translateX(1.8px) rotate(1.5deg)}}\n@keyframes ovbreathe{0%,100%{transform:scale(1)}50%{transform:scale(1.07,.95)}}\n@keyframes ovdrift{0%,100%{transform:translate(0,0)}\n  33%{transform:translate(1.6px,-1.4px)}66%{transform:translate(-1.4px,-.6px)}}\n@keyframes ovtilt{0%,100%{transform:rotate(-4deg)}50%{transform:rotate(4deg)}}\n@keyframes ovhop{0%,62%,100%{transform:translateY(0) scaleY(1)}\n  70%{transform:translateY(1px) scaleY(.9)}82%{transform:translateY(-4px) scaleY(1.06)}}\n.blob.m0{animation-name:ovbob}\n.blob.m1{animation-name:ovsway}\n.blob.m2{animation-name:ovbreathe}\n.blob.m3{animation-name:ovdrift}\n.blob.m4{animation-name:ovtilt}\n.blob.m5{animation-name:ovhop}\n\n/* Queste due devono venire DOPO le indoli: stessa specificita\', vince l\'ultima. */\n.blob.talking{animation:ovbreathe .9s ease-in-out infinite}\n.blob.flip{animation:ovflip .95s cubic-bezier(.3,.75,.3,1)}\n@keyframes ovflip{\n  0%{transform:rotate(0) scale(1)}\n  25%{transform:translateY(-4px) rotate(150deg) scale(1.1)}\n  55%{transform:translateY(-2px) rotate(330deg) scale(1.04)}\n  78%{transform:translateY(0) rotate(372deg) scale(.95)}\n  100%{transform:rotate(360deg) scale(1)}}\n@media (prefers-reduced-motion:reduce){\n  .blob,.blob .eyes,.blob i,.blob .legs,.blob.flip{animation:none}\n}\n\n/* ── scrollbar: sottile, del colore del tema, non quella di sistema ── */\n.left,.thread,.chatlog,.pad{scrollbar-width:thin;\n  scrollbar-color:var(--raised) transparent}\n.left::-webkit-scrollbar,.thread::-webkit-scrollbar,\n.chatlog::-webkit-scrollbar,.pad::-webkit-scrollbar{width:10px}\n.left::-webkit-scrollbar-track,.thread::-webkit-scrollbar-track,\n.chatlog::-webkit-scrollbar-track,.pad::-webkit-scrollbar-track{background:transparent}\n.left::-webkit-scrollbar-thumb,.thread::-webkit-scrollbar-thumb,\n.chatlog::-webkit-scrollbar-thumb,.pad::-webkit-scrollbar-thumb{background:var(--raised);\n  border-radius:99px;border:3px solid transparent;background-clip:content-box}\n.left:hover::-webkit-scrollbar-thumb,.thread:hover::-webkit-scrollbar-thumb{\n  background:#4a4a4a;background-clip:content-box}\n\n/* ── modale ── */\n.mask{position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.55);\n  display:flex;align-items:center;justify-content:center;padding:20px;\n  animation:ovfade .14s ease-out}\n@keyframes ovfade{from{opacity:0}to{opacity:1}}\n.modal{width:min(460px,100%);max-height:90dvh;overflow-y:auto;background:var(--side);\n  border:1px solid var(--border);border-radius:18px;padding:22px;\n  box-shadow:0 24px 70px rgba(0,0,0,.6);animation:ovrise .18s cubic-bezier(.2,.9,.3,1)}\n@keyframes ovrise{from{transform:translateY(12px) scale(.98);opacity:0}to{transform:none;opacity:1}}\n.modal h3{font-size:17px;font-weight:650;margin-bottom:3px}\n.modal .sub{font-size:13px;color:var(--muted);margin-bottom:18px}\n.field{margin-bottom:14px}\n.field label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px;\n  text-transform:uppercase;letter-spacing:.5px;font-weight:650}\n.field input,.field select,.field textarea{width:100%;background:var(--bg);\n  border:1px solid var(--border);color:var(--text);border-radius:10px;\n  padding:10px 12px;font:inherit;font-size:14px;outline:0}\n.field textarea{resize:vertical;min-height:66px;line-height:1.5}\n.field input:focus,.field select:focus,.field textarea:focus{border-color:var(--accent)}\n.field .hint{font-size:11.5px;color:var(--faint);margin-top:5px}\n.mrow{display:flex;gap:10px}\n.mrow .field{flex:1;min-width:0}\n.mfoot{display:flex;gap:9px;justify-content:flex-end;margin-top:20px}\n.mbtn{border:0;background:none;color:var(--muted);\n  border-radius:10px;padding:9px 15px;font:inherit;font-size:14px;cursor:pointer;\n  transition:background .12s,color .12s}\n.mbtn:hover{background:var(--raised);color:var(--text)}\n.mbtn.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:650}\n.mbtn.primary:hover{background:var(--accent-hover)}\n.mpreview{display:flex;align-items:center;gap:12px;background:var(--bg);\n  border:1px solid var(--border);border-radius:12px;padding:11px 13px;margin-bottom:18px}\n.mpreview .nm{font-weight:650;font-size:14.5px}\n.mpreview .rl{font-size:12.5px;color:var(--muted)}\n.rrow{display:flex;align-items:center;gap:13px;padding:11px 12px;border-radius:14px;\n  cursor:pointer;border:1px solid transparent}\n.rrow:hover{background:var(--hover)}\n.rrow.on{background:var(--hover);border-color:var(--border)}\n.rmeta{flex:1;min-width:0}\n.rl1{display:flex;align-items:center;gap:8px}\n.rname{color:var(--text);font-size:15px;font-weight:600;white-space:nowrap;\n  overflow:hidden;text-overflow:ellipsis}\n.rbadge{flex:none;background:none;border:0;color:var(--muted);font:inherit;\n  font-size:11.5px;padding:2px 6px;border-radius:7px;font-weight:600;\n  letter-spacing:.2px;cursor:pointer;max-width:150px;overflow:hidden;\n  text-overflow:ellipsis;white-space:nowrap;\n  transition:background .12s,color .12s}\n.rbadge::after{content:"›";opacity:0;margin-left:4px;font-weight:400;\n  transition:opacity .12s}\n.rrow:hover .rbadge{color:var(--text-dim)}\n.rbadge:hover{background:var(--raised);color:var(--text)}\n.rbadge:hover::after{opacity:.6}\n.rbadge.none{color:var(--faint);font-style:italic;font-weight:500}\n.rtime{flex:none;color:var(--faint);font-size:12.5px}\n.rprev{color:var(--muted);font-size:13.5px;margin-top:3px;white-space:nowrap;\n  overflow:hidden;text-overflow:ellipsis}\n.runread{flex:none;min-width:19px;height:19px;padding:0 6px;border-radius:99px;\n  background:var(--accent);color:#fff;font-size:11px;font-weight:700;\n  display:flex;align-items:center;justify-content:center}\n.rhead{position:relative}\n.rhead .runread{position:absolute;top:-2px;right:-2px;border:2px solid var(--bg)}\n.rrow.unread .rname{color:#fff}\n.rrow.unread .rprev{color:var(--text-dim)}\n.rstack{position:relative;flex:none;width:42px;height:42px}\n.rstack .blob{position:absolute}\n.rstack .blob:nth-child(1){left:0;top:0}\n.rstack .blob:nth-child(2){right:0;top:2px}\n.rstack .blob:nth-child(3){left:9px;bottom:0}\n.rroom{margin-bottom:10px;padding-bottom:14px;position:relative}\n.rroom::after{content:"";position:absolute;left:12px;right:12px;bottom:0;height:1px;\n  background:linear-gradient(90deg,transparent,var(--border) 18%,var(--border) 82%,transparent)}\n/* Il badge del modello E\' il comando: quello che leggi e\' quello che clicchi,\n   niente pannello che si apre sotto e fa saltare la lista. */\n.rmore{flex:none;width:22px;height:22px;border:0;background:none;\n  color:var(--faint);cursor:pointer;opacity:0;transition:opacity .12s,color .12s;\n  display:flex;align-items:center;justify-content:center;font-size:16px;line-height:1}\n.rrow:hover .rmore,.rmore.open{opacity:1}\n.rmore:hover{color:var(--text)}\n@media (hover:none){.rmore{opacity:.45}}\n.pop{position:absolute;z-index:40;min-width:230px;background:var(--raised);\n  border:1px solid var(--border);border-radius:13px;padding:6px;\n  box-shadow:0 14px 40px rgba(0,0,0,.5)}\n.pop .ph{font-size:11px;color:var(--faint);text-transform:uppercase;\n  letter-spacing:.6px;padding:7px 10px 5px;font-weight:650}\n.popitem{display:flex;align-items:center;gap:9px;width:100%;text-align:left;\n  background:none;border:0;color:var(--text-dim);font:inherit;font-size:13.5px;\n  padding:8px 10px;border-radius:9px;cursor:pointer}\n.popitem:hover{background:var(--hover);color:var(--text)}\n.popitem .tick{width:14px;flex:none;color:var(--accent)}\n.popitem.danger{color:var(--bad)}\n.popsep{height:1px;background:var(--border);margin:5px 8px}\n.popfield{padding:4px 6px 6px}\n.popfield input{width:100%;background:var(--bg);border:1px solid var(--border);\n  color:var(--text);border-radius:9px;padding:8px 10px;font:inherit;font-size:13px;outline:0}\n.popfield input:focus{border-color:var(--accent)}\n.popfield select{width:100%;background:var(--bg);border:1px solid var(--border);\n  color:var(--text);border-radius:9px;padding:8px 10px;font:inherit;\n  font-size:13px;cursor:pointer}\n.popfield select:focus{outline:none;border-color:var(--accent)}\n.popfield .hint{font-size:11px;color:var(--faint);padding:6px 3px 0}\n.saved{color:var(--ok)!important}\n.rfoot{display:flex;gap:10px;justify-content:center;padding:20px 0 4px;\n  color:var(--faint);font-size:12px;letter-spacing:.1px}\n\n\n/* ── impaginazione: due colonne su desktop, una schermata alla volta su mobile ── */\n.shell{display:flex;height:100dvh}\n.pane{display:flex;flex-direction:column;min-width:0}\n/* Scorre l\'elenco, non la colonna. Facendo scorrere tutto, cercando un agente\n   in fondo sparivano la ricerca, il «+ Nuovo» e l\'ingranaggio — cioe\' proprio\n   le cose che devono restare a portata mentre scorri. */\n.left{flex:0 0 420px;border-right:1px solid var(--border);overflow:hidden;\n  padding:24px 18px 18px}\n.left .roster{flex:1;display:flex;flex-direction:column;min-height:0}\n.left #rrows{flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;\n  margin-right:-6px;padding-right:6px;\n  scrollbar-width:thin;\n  scrollbar-color:color-mix(in srgb,var(--muted) 30%,transparent) transparent}\n#rrows::-webkit-scrollbar{width:7px}\n#rrows::-webkit-scrollbar-track{background:transparent}\n#rrows::-webkit-scrollbar-thumb{border-radius:99px;\n  background:color-mix(in srgb,var(--muted) 30%,transparent)}\n#rrows::-webkit-scrollbar-thumb:hover{\n  background:color-mix(in srgb,var(--muted) 55%,transparent)}\n/* A colonna chiusa la barra di sistema schiacciava i polpi in 76px: si\n   scorre lo stesso (rotella, trascinamento), ma senza il binario in vista. */\nbody.folded #rrows{scrollbar-width:none;margin-right:0;padding-right:0}\nbody.folded #rrows::-webkit-scrollbar{display:none}\n.left .rtop,.left .rsearch,.left .rheads,.left .rfoot{flex:0 0 auto}\n.left .rfoot{margin-top:10px;padding-top:12px;border-top:1px solid var(--border)}\n.right{flex:1;background:var(--bg)}\n.roster{max-width:100%;margin:0}\n/* Chat spoglia: nessuna linea di separazione, nessuna bolla per l\'agente.\n   Solo quello che scrivi tu ha una forma — il resto e\' testo e basta. */\n.chathead{display:flex;align-items:center;gap:11px;padding:16px 20px 10px;flex:none}\n/* Erano fantasmi al 55% di opacita\': un comando che non si vede e\' un\n   comando che non esiste. Bottoni veri, con un bordo e una casa. */\n.chathead .rmore{opacity:1;color:var(--text-dim);border:1px solid var(--border);\n  background:var(--raised);border-radius:10px;width:36px;height:36px;\n  display:inline-flex;align-items:center;justify-content:center;padding:0}\n.chathead .rmore:hover{color:var(--accent);border-color:var(--accent);\n  background:var(--accent-dim)}\n.chathead .rmore+.rmore{margin-left:8px}\n.chathead .nm{font-weight:650;font-size:15px}\n.chathead .sb{font-size:12.5px;color:var(--faint)}\n.back{display:none;background:none;border:0;color:var(--text-dim);cursor:pointer;\n padding:6px;margin-left:-6px;border-radius:8px}\n.back:hover{color:var(--text)}\n.thread{flex:1;overflow-y:auto;padding:8px 20px 6px;display:flex;\n flex-direction:column;gap:20px}\n.thread .inner{max-width:46rem;width:100%;margin:0 auto;display:flex;\n flex-direction:column;gap:20px}\n.msg{line-height:1.62;font-size:15px}\n.msg.me{align-self:flex-end;max-width:82%;background:var(--raised);\n padding:10px 15px;border-radius:18px;border-bottom-right-radius:7px}\n.msg.them{align-self:stretch;color:var(--text-dim)}\n.msg .who{display:block;font-size:11.5px;color:var(--faint);margin-bottom:5px;\n font-weight:650;letter-spacing:.2px}\n.msg.me .who{display:none}\n.typing{color:var(--faint);font-size:13.5px}\n.approval{align-self:stretch;border:1px solid var(--accent);border-radius:14px;\n  padding:12px 14px;background:var(--raised);animation:ovpeer .25s ease-out both}\n.approval .ahead{font-size:12px;color:var(--accent);font-weight:650;\n  text-transform:uppercase;letter-spacing:.5px;margin-bottom:7px}\n.approval .abody{font-size:13.5px;color:var(--text-dim);white-space:pre-wrap;\n  line-height:1.55;word-break:break-word}\n.approval .afoot{display:flex;gap:8px;justify-content:flex-end;margin-top:11px}\n.approval.done{border-color:var(--border);opacity:.6}\n.approval.done .ahead{color:var(--muted);margin:0}\n.rrow.busy .rprev::after{content:" · sta rispondendo";color:var(--accent)}\n/* Cosa ha fatto l\'agente: una pillola richiudibile sopra la risposta, come\n   nella vecchia dashboard. Qui pero\' resta anche dopo, perche\' viene salvata\n   col messaggio invece di vivere solo durante lo streaming. */\n.activity{margin-bottom:9px;font-size:12.5px;color:var(--faint)}\n.activity summary{cursor:pointer;list-style:none;display:inline-flex;align-items:center;\n  gap:6px;padding:3px 10px;border-radius:99px;border:1px solid var(--border);\n  color:var(--muted);user-select:none}\n.activity summary::-webkit-details-marker{display:none}\n.activity summary:hover{background:var(--hover);color:var(--text-dim)}\n.activity[open] summary{margin-bottom:7px}\n.activity .st{padding:3px 0 3px 12px}\n.activity .st .cmd{display:flex;gap:8px;align-items:baseline}\n.activity .st b{color:var(--accent);font-weight:500;flex-shrink:0;\n  font-family:ui-monospace,monospace}\n.activity .st code{color:var(--muted);font-size:12px;word-break:break-all;\n  font-family:ui-monospace,monospace}\n.activity .st .out{color:var(--faint);font-size:11.5px;padding:2px 0 0 18px;\n  white-space:pre-wrap;word-break:break-word;opacity:.85}\n.typing{display:inline-flex;align-items:center;gap:8px}\n.typing .sp{color:var(--accent);font-size:15px;line-height:1;width:1em;\n  text-align:center;display:inline-block}\n/* Il testo respira invece di lampeggiare: un\'attesa lunga con qualcosa che\n   scorre si legge come "sta lavorando", non come "e\' bloccato". */\n.typing .lbl{background:linear-gradient(90deg,var(--faint) 30%,var(--text-dim) 50%,var(--faint) 70%);\n  background-size:220% 100%;-webkit-background-clip:text;background-clip:text;\n  color:transparent;animation:ovshine 2s linear infinite}\n@keyframes ovshine{to{background-position:-220% 0}}\n.typing .secs{color:var(--faint);font-size:12px;opacity:.75}\n@media (prefers-reduced-motion:reduce){\n  .typing .lbl{animation:none;color:var(--faint);-webkit-text-fill-color:currentColor}\n}\n/* Consulenza fra agenti: due facce e una domanda che passa dall\'una all\'altra. */\n.peer{align-self:stretch;border-left:2px solid var(--border);padding:2px 0 2px 14px;\n  margin:2px 0;animation:ovpeer .32s ease-out both}\n@keyframes ovpeer{from{opacity:0;transform:translateX(-6px)}to{opacity:1;transform:none}}\n.peer .pline{display:flex;align-items:center;gap:8px;margin-bottom:7px}\n.peer .pwho{font-size:11.5px;color:var(--faint);font-weight:650;letter-spacing:.2px}\n.peer .arrow{display:flex;gap:3px;align-items:center}\n.peer .arrow i{width:4px;height:4px;border-radius:50%;background:var(--accent);opacity:.3;\n  transition:opacity .3s}\n.peer.waiting .arrow i{opacity:1;animation:ovdot 1.1s ease-in-out infinite}\n/* La scena: uno si stacca e va verso l\'altro, si parlano, poi tornano al\n   loro posto. I puntini da soli dicevano "sta caricando"; il movimento dice\n   che due tizi si sono avvicinati per dirsi una cosa. */\n.peer .who1,.peer .who2{transition:transform .55s cubic-bezier(.34,1.4,.64,1)}\n.peer.andando .who1{transform:translateX(20px)}\n.peer.andando .who2{transform:translateX(-6px)}\n.peer.parlando .who1{animation:ovchat1 1.5s ease-in-out infinite}\n.peer.parlando .who2{animation:ovchat2 1.5s ease-in-out infinite}\n@keyframes ovchat1{0%,100%{transform:translateX(20px) rotate(0)}\n  25%{transform:translateX(20px) rotate(-7deg) translateY(-2px)}\n  50%{transform:translateX(20px) rotate(0)}}\n@keyframes ovchat2{0%,100%{transform:translateX(-6px) rotate(0)}\n  60%{transform:translateX(-6px) rotate(7deg) translateY(-2px)}\n  85%{transform:translateX(-6px) rotate(0)}}\n.peer.tornando .who1,.peer.tornando .who2{transform:translateX(0)}\n@media (prefers-reduced-motion:reduce){\n  .peer .who1,.peer .who2{transition:none;animation:none!important;transform:none!important}\n}\n.peer .arrow i:nth-child(2){animation-delay:.15s}\n.peer .arrow i:nth-child(3){animation-delay:.3s}\n@keyframes ovdot{0%,100%{opacity:.25;transform:translateX(0)}\n  50%{opacity:1;transform:translateX(2px)}}\n.peer .pq{font-size:13.5px;color:var(--muted);font-style:italic;margin-bottom:6px}\n.peer .pa{font-size:14px;color:var(--text-dim);line-height:1.6}\n@media (prefers-reduced-motion:reduce){.peer,.peer .arrow i{animation:none}}\n.composer{flex-shrink:0;padding:8px 20px 18px}\n.composer .wrap{max-width:46rem;margin:0 auto}\n/* Un rettangolo che contiene tutto: allegati sopra, testo in mezzo, strumenti\n   sotto. Il testo non deve stringersi per far posto ai bottoni. */\n.composer .box{display:flex;flex-direction:column;background:var(--raised);\n  border-radius:18px;padding:10px 10px 8px;box-shadow:0 0 0 1px transparent;\n  transition:box-shadow .14s}\n/* Nessun anello a riposo: il fondo piu\' chiaro basta gia\' a dire "qui si\n   scrive". Il contorno compare solo mentre scrivi. */\n.composer .box:focus-within{box-shadow:0 0 0 1px rgba(255,255,255,.14)}\n.composer textarea{width:100%;background:none;border:none;outline:none;resize:none;\n  color:var(--text);font:inherit;line-height:1.5;max-height:200px;padding:5px 8px 8px}\n.composer textarea::placeholder{color:var(--faint)}\n.ctools{display:flex;align-items:center;gap:2px}\n.ctools .gap{flex:1}\n.ctool{width:30px;height:30px;flex-shrink:0;border:0;border-radius:9px;background:none;\n  color:var(--muted);cursor:pointer;display:grid;place-items:center;\n  transition:background .12s,color .12s}\n.ctool:hover{background:var(--hover);color:var(--text)}\n.ctool svg{width:17px;height:17px}\n.send{width:30px;height:30px;flex-shrink:0;border:none;border-radius:9px;cursor:pointer;\n  background:var(--accent);color:#fff;display:grid;place-items:center;transition:background .1s}\n.send:hover{background:var(--accent-hover)}\n.send:disabled{background:var(--hover);color:var(--faint);cursor:default}\n\n/* Allegati: si vedono PRIMA di inviare, e si tolgono. */\n.chips{display:flex;flex-wrap:wrap;gap:6px;padding:2px 4px 8px}\n.chips:empty{display:none}\n.chip{display:flex;align-items:center;gap:8px;background:var(--bg);\n  border:1px solid var(--border);border-radius:10px;padding:5px 8px 5px 5px;\n  font-size:12.5px;color:var(--text-dim);max-width:220px}\n.chip img{width:28px;height:28px;object-fit:cover;border-radius:6px;flex-shrink:0}\n.chip .ic{width:28px;height:28px;border-radius:6px;background:var(--raised);\n  display:grid;place-items:center;color:var(--muted);font-size:10px;font-weight:700;\n  flex-shrink:0;letter-spacing:.3px}\n.chip .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}\n.chip .x{cursor:pointer;color:var(--faint);font-size:15px;line-height:1;padding:0 2px}\n.chip .x:hover{color:var(--bad)}\n.chip.busy{opacity:.55}\n.composer button:active{transform:translateY(1px)}\n.blank{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;\n gap:12px;color:var(--faint);font-size:14px;text-align:center;padding:30px}\n.note{border-top:1px solid var(--border);margin-top:26px;padding-top:16px;\n color:var(--faint);font-size:12.5px;line-height:1.65}\n.note b{color:var(--muted);font-weight:600}\n.note code{background:var(--raised);padding:1px 5px;border-radius:5px;font-size:11.5px}\n\n@media (max-width:820px){\n  .shell{display:block;height:100dvh}\n  .pane{height:100dvh}\n  .left{flex:none;width:100%;border-right:0;padding:18px 14px 40px}\n  .right{display:none;position:fixed;inset:0;z-index:20;background:var(--bg)}\n  body.chatting .left,body.insettings .left,body.inpanel .left{display:none}\n  body.chatting .right,body.insettings .right,body.inpanel .right{display:flex}\n  .back{display:block}\n  .pop{min-width:min(260px,calc(100vw - 24px))}\n  .msg{max-width:88%}\n}\n/* ── markdown nelle risposte dell\'agente ── */\n.msg.them{line-height:1.7;word-break:break-word}\n.msg.them p{margin:0 0 10px}\n.msg.them p:last-child{margin-bottom:0}\n.msg.them h2,.msg.them h3,.msg.them h4{margin:14px 0 8px;font-weight:600;line-height:1.35}\n.msg.them h2{font-size:19px}.msg.them h3{font-size:16.5px}.msg.them h4{font-size:15px}\n.msg.them ul,.msg.them ol{margin:0 0 10px;padding-left:22px}\n.msg.them li{margin:3px 0}\n.msg.them a{color:var(--accent);text-decoration:none}\n.msg.them a:hover{text-decoration:underline}\n.msg.them code{background:var(--raised);border-radius:5px;padding:1.5px 6px;\n  font-size:.87em;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}\n.msg.them pre{background:#181818;border:1px solid var(--border);border-radius:10px;\n  padding:13px 15px;overflow-x:auto;margin:0 0 10px}\n.msg.them pre code{background:none;padding:0;font-size:13px;line-height:1.55;color:var(--text-dim)}\n.msg.them p:first-child{margin-top:0}\n.msg.me{white-space:pre-wrap}\n.live .cursor{display:inline-block;width:7px;height:15px;background:var(--accent);\n  vertical-align:-2px;margin-left:2px;animation:ovblip 1s steps(2) infinite}\n@keyframes ovblip{0%,50%{opacity:1}51%,100%{opacity:0}}\n.steps{margin:2px 0 8px;font-size:12.5px;color:var(--faint);line-height:1.6}\n.steps b{color:var(--muted);font-weight:600;margin-right:6px}\n\n/* ── aggiunte del prodotto ── */\n#err{display:none;color:var(--bad);font-size:13px;padding:0 20px 6px;flex:none}\n.dot{width:7px;height:7px;border-radius:50%;background:var(--ok);flex-shrink:0}\n.dot.off{background:var(--bad)}\n.rlink{background:none;border:0;color:var(--faint);font:inherit;font-size:12px;\n  cursor:pointer;padding:2px 4px;border-radius:6px;transition:color .12s}\n.rlink:hover{color:var(--text-dim)}\n.rfoot{gap:10px}\n.panelbody{max-height:56vh;overflow-y:auto}\n/* Rilascio file. Prima il drop valeva solo sopra .thread: sbagliare mira di un\n   centimetro faceva aprire il file al browser, che portava via dalla chat. Ora\n   la finestra intera annulla il default e il riquadro accetta ovunque. */\n.dropveil{position:absolute;inset:0;z-index:40;display:none;\n  align-items:center;justify-content:center;padding:18px;\n  background:color-mix(in srgb,var(--bg) 78%,transparent);\n  backdrop-filter:blur(2px)}\n.dropveil.on{display:flex}\n.dropveil .dbox{display:flex;flex-direction:column;align-items:center;gap:8px;\n  width:100%;height:100%;border:2px dashed var(--accent);border-radius:18px;\n  align-items:center;justify-content:center;color:var(--accent);\n  background:color-mix(in srgb,var(--accent) 7%,transparent)}\n.dropveil svg{width:34px;height:34px}\n.dropveil .dt{font-size:15px;font-weight:650;letter-spacing:.2px}\n.dropveil .ds{font-size:12.5px;color:var(--muted)}\n.dropveil.no .dbox{border-color:var(--border);color:var(--muted);\n  background:transparent}\n.pane.right{position:relative}\n/* Dopo un clone la rubrica e\' vuota per scelta, ma la pagina non diceva cosa\n   farne: c\'era solo un polpo. Chi apre openvurp la prima volta non sa che deve\n   creare un agente, ne\' cosa scriverci dentro. */\n.blank .btitle{font-size:19px;font-weight:650;color:var(--text);margin-top:16px}\n.blank .bsub{font-size:14px;color:var(--muted);margin-top:6px;max-width:460px;\n  line-height:1.55}\n.blank .bsteps{margin:22px 0 4px;max-width:520px;text-align:left;\n  display:flex;flex-direction:column;gap:13px}\n.blank .bstep{display:flex;gap:11px;align-items:flex-start;font-size:13.5px;\n  color:var(--text-dim);line-height:1.55}\n.blank .bstep .n{flex:0 0 22px;height:22px;border-radius:50%;background:var(--accent-dim);\n  color:var(--accent);font-size:12px;font-weight:700;display:flex;\n  align-items:center;justify-content:center;margin-top:1px}\n.blank .bstep i{color:var(--faint);font-style:italic}\n.blank .bcta{margin-top:24px;border:1px solid var(--accent);background:var(--accent-dim);\n  color:var(--accent);border-radius:11px;padding:10px 18px;font-size:14px;\n  font-weight:600;cursor:pointer}\n.blank .bcta:hover{background:var(--accent);color:#fff}\n/* L\'anteprima: una scheda accanto alla chat, alla maniera degli artifact di\n   Claude — testata con nome e azioni, interruttore Anteprima/Codice per le\n   pagine, contenuto che riempie. Prima era una colonna nuda con tre bottoni\n   di testo: si vedeva poco e male. */\n.withprev{gap:0}\n.prev{display:none;flex:0 0 55%;min-width:380px;flex-direction:column;\n  margin:10px 14px 12px 0;border:1px solid var(--border);border-radius:16px;\n  background:var(--side);overflow:hidden;\n  box-shadow:0 14px 48px rgba(0,0,0,.4)}\nbody.previewing .prev{display:flex}\nbody.previewing .thread{flex:0 1 45%}\n.phead{display:flex;align-items:center;gap:9px;padding:9px 10px 9px 14px;\n  border-bottom:1px solid var(--border);background:var(--raised);flex:0 0 auto}\n.pico{font-size:16px;line-height:1}\n.ptitle{display:flex;flex-direction:column;min-width:0;gap:1px}\n#prevname{font-size:13px;font-weight:650;white-space:nowrap;overflow:hidden;\n  text-overflow:ellipsis;max-width:220px}\n#prevmeta{font-size:10.5px;color:var(--faint);white-space:nowrap}\n.pswitch{display:flex;background:var(--bg);border:1px solid var(--border);\n  border-radius:99px;padding:2px;margin-left:8px}\n.pswitch button{border:0;background:none;color:var(--muted);font-size:12px;\n  padding:4px 13px;border-radius:99px;cursor:pointer;line-height:1.4}\n.pswitch button.on{background:var(--hover);color:var(--text);font-weight:600}\n.pbtn{width:30px;height:30px;border:0;background:none;color:var(--muted);\n  border-radius:8px;display:inline-flex;align-items:center;justify-content:center;\n  cursor:pointer;padding:0}\n.pbtn svg{width:15px;height:15px}\n.pbtn:hover{background:var(--hover);color:var(--text)}\n.pbody{flex:1;min-height:0;overflow:auto;background:var(--bg);\n  display:flex;flex-direction:column}\n.pbody iframe,.pbody embed{flex:1;width:100%;border:0;background:#fff}\n.pbody .vimg{flex:1;display:flex;align-items:center;justify-content:center;\n  padding:16px}\n.pbody .vimg img{max-width:100%;max-height:100%;border-radius:10px;\n  box-shadow:0 8px 32px rgba(0,0,0,.45)}\n.pbody .sheets{display:flex;flex-direction:column;align-items:center;gap:16px;\n  padding:20px 16px 28px;width:100%}\n.pbody .sheet{width:min(92%,860px);background:#fff;border-radius:6px;\n  box-shadow:0 6px 26px rgba(0,0,0,.5)}\n.pbody .code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;\n  font-size:12.5px;line-height:1.7;display:flex;align-items:flex-start}\n.pbody .ln{flex:0 0 auto;padding:14px 11px 14px 16px;text-align:right;\n  color:var(--faint);user-select:none;border-right:1px solid var(--border);\n  white-space:pre}\n.pbody .src{flex:1;padding:14px 18px;white-space:pre;overflow-x:auto;\n  color:var(--text-dim)}\n.pbody .perr{margin:auto;color:var(--faint);font-size:13.5px;max-width:380px;\n  text-align:center;line-height:1.6;padding:22px}\n/* Sotto una certa larghezza affiancare due colonne le rende illeggibili\n   entrambe: la scheda prende tutto e la chat resta dietro. */\n@media (max-width:900px){\n  .prev{position:absolute;inset:8px;flex:1;min-width:0;margin:0;z-index:30}\n}\n/* Un blocco di codice nel messaggio: si copia e si apre, non si seleziona a mano. */\n.cb{border:1px solid var(--border);border-radius:10px;overflow:hidden;margin:8px 0}\n.cbhead{display:flex;align-items:center;gap:8px;padding:5px 10px;\n  background:var(--raised);border-bottom:1px solid var(--border);\n  font-size:11.5px;color:var(--muted)}\n.cbhead .lang{text-transform:uppercase;letter-spacing:.5px;font-weight:650}\n.cbhead button{border:0;background:none;color:var(--muted);cursor:pointer;\n  font-size:11.5px;padding:2px 5px;border-radius:6px}\n.cbhead button:hover{background:var(--hover);color:var(--text)}\n.cb pre{margin:0;border:0;border-radius:0}\n/* Un percorso di file dentro i passaggi si apre, non si legge e basta. */\n.fileref{color:var(--accent);cursor:pointer;text-decoration:underline;\n  text-decoration-style:dotted;text-underline-offset:2px}\n.ptabs{display:flex;gap:4px;flex-wrap:wrap}\n.ptab{border:1px solid var(--border);background:transparent;color:var(--muted);\n  border-radius:9px;padding:5px 10px;font-size:12.5px;cursor:pointer;white-space:nowrap}\n.ptab:hover{background:var(--hover);color:var(--text)}\n.ptab.on{background:var(--accent-dim);border-color:var(--accent);color:var(--accent)}\n#panelbody .sec2{max-width:820px}\n@media (max-width:640px){.sethead{flex-wrap:wrap}.ptabs{width:100%;order:3}}\n/* Una pagina non si nasconde in un menu a comparsa: l\'ingresso sta in vista. */\n.rfoot{display:flex;align-items:center;gap:8px}\n.rgear{border:0;background:none;color:var(--faint);cursor:pointer;padding:3px;\n  border-radius:7px;display:flex}\n.rgear svg{width:16px;height:16px}\n.rgear:hover{color:var(--text);background:var(--hover)}\nbody.insettings .rgear{color:var(--accent)}\n/* Le impostazioni erano in un modale: si apriva sopra la chat, si leggeva in\n   una finestrella e ogni sezione andava scrollata dentro un riquadro dentro un\n   riquadro. Sono una pagina, non un avviso: qui occupano il posto della chat,\n   con la loro intestazione e il salvataggio sempre in vista. */\n/* Occupare tutto non e\' automatico: la colonna destra deve essere una colonna\n   flessibile e la pagina deve poter rimpicciolire (min-height:0), altrimenti\n   il corpo non scorre e si schiaccia in una striscia con la sua barra. */\n.pane.right{flex:1;display:flex;flex-direction:column;min-width:0;min-height:0}\n.setpage{flex:1;display:none;flex-direction:column;min-height:0;min-width:0}\n.setpage.on{display:flex}\n/* Un riquadro nascosto non deve continuare a occupare la sua fetta. */\n.withprev{flex:1;display:flex;min-height:0;min-width:0}\n.setpage.on{display:flex}\n.sethead{display:flex;align-items:center;gap:12px;padding:14px 20px;\n  border-bottom:1px solid var(--border);flex:0 0 auto}\n.sethead .nm{font-weight:650;font-size:16px}\n.sethead .sb{font-size:12.5px;color:var(--muted)}\n.setnote{font-size:12.5px;color:var(--muted)}\n.setbody{flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;padding:26px 24px 70px}\n.setbody .sec2{max-width:820px;margin:0 auto 34px}\n.setbody .ph{font-size:12px;text-transform:uppercase;letter-spacing:.7px;\n  color:var(--muted);font-weight:650;margin:0 0 4px}\n.setbody .why{font-size:12.5px;color:var(--faint);margin:0 0 14px;line-height:1.5}\n.setbody .field{margin-bottom:12px}\n.setbody label{display:block;font-size:12.5px;color:var(--text-dim);margin-bottom:5px}\n.setbody input,.setbody select{width:100%;background:var(--raised);color:var(--text);\n  border:1px solid var(--border);border-radius:9px;padding:9px 11px;font-size:14px}\n.setbody input:focus,.setbody select:focus{outline:none;border-color:var(--accent)}\n.setbody .hint{font-size:12px;color:var(--faint);margin-top:5px;line-height:1.5}\n.setbody .mrow{display:flex;gap:12px}.setbody .mrow .field{flex:1}\n.setbody .kv{display:flex;justify-content:space-between;gap:12px;padding:7px 0;\n  border-bottom:1px solid var(--border);font-size:13.5px}\n.setbody .kv:last-of-type{border-bottom:0}\n/* Dove si puo\' scegliere non si deve scrivere: un campo di testo per «quali\n   canali accendo» o «quali strumenti concedo» chiede di indovinare il nome\n   esatto e di ricordarselo. */\n.setbody .scelte{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}\n.setbody .sw{display:flex;align-items:center;gap:9px;border:1px solid var(--border);\n  background:var(--raised);border-radius:11px;padding:9px 13px;cursor:pointer;\n  font-size:13.5px;color:var(--text-dim);user-select:none;transition:.12s}\n.setbody .sw:hover{border-color:var(--muted)}\n.setbody .sw input{appearance:none;width:34px;height:19px;border-radius:99px;\n  background:var(--border);position:relative;cursor:pointer;flex:0 0 auto;\n  transition:background .15s}\n.setbody .sw input::after{content:"";position:absolute;top:2px;left:2px;\n  width:15px;height:15px;border-radius:50%;background:var(--muted);\n  transition:transform .15s,background .15s}\n.setbody .sw input:checked{background:var(--accent-dim)}\n.setbody .sw input:checked::after{transform:translateX(15px);background:var(--accent)}\n.setbody .sw:has(input:checked){border-color:var(--accent);color:var(--text)}\n.setbody .sw .sd{display:block;font-size:11.5px;color:var(--faint);margin-top:1px}\n.setbody .tags{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}\n.setbody .tag{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--border);\n  border-radius:99px;padding:5px 11px;font-size:12.5px;color:var(--muted);\n  cursor:pointer;user-select:none;transition:.12s}\n.setbody .tag input{appearance:none;width:0;height:0;margin:0}\n.setbody .tag:has(input:checked){border-color:var(--accent);color:var(--accent);\n  background:var(--accent-dim)}\n.setbody .tag:hover{border-color:var(--muted)}\n.setbody .nessuno{font-size:12.5px;color:var(--faint);padding:2px 0 10px}\n/* Parlare invece di scrivere. Il file va all\'agente come un allegato\n   qualsiasi: e\' lui che lo trascrive, con lo strumento che ha gia\'. */\n#mic.rec{color:var(--bad)}\n#mic.rec svg{animation:micpulse 1.1s ease-in-out infinite}\n@keyframes micpulse{0%,100%{opacity:.45}50%{opacity:1}}\n.rectime{font-size:12px;color:var(--bad);margin-left:6px;align-self:center}\n@media (prefers-reduced-motion:reduce){#mic.rec svg{animation:none}}\n/* Un allegato si vede e si sente, non si legge come percorso. */\n.atts{display:flex;flex-direction:column;align-items:flex-end;gap:7px;margin-top:8px}\n.msg.them .atts{align-items:flex-start}\n/* Il lettore del browser e\' un rettangolo grigio che ignora il tema: qui il\n   play e\' il nostro, col colore del marchio, e la barra si tocca per saltare. */\n.aplay{display:flex;align-items:center;gap:11px;border:1px solid var(--border);\n  background:var(--raised);border-radius:99px;padding:6px 15px 6px 6px;\n  width:300px;max-width:100%}\n.ap-btn{flex:0 0 36px;width:36px;height:36px;border-radius:50%;border:0;\n  background:var(--accent);color:#fff;cursor:pointer;display:flex;\n  align-items:center;justify-content:center;transition:transform .1s}\n.ap-btn:hover{transform:scale(1.06)}\n.ap-btn svg{width:17px;height:17px}\n.aplay .ic-pause{display:none}\n.aplay.on .ic-pause{display:block}\n.aplay.on .ic-play{display:none}\n.ap-track{flex:1;height:5px;border-radius:99px;background:var(--border);\n  cursor:pointer;overflow:hidden}\n.ap-fill{height:100%;width:0;background:var(--accent);border-radius:99px;\n  transition:width .15s linear}\n.ap-time{font-size:11.5px;color:var(--muted);white-space:nowrap;\n  font-variant-numeric:tabular-nums}\n.att-img{max-width:250px;max-height:220px;border-radius:13px;cursor:zoom-in;\n  border:1px solid var(--border)}\n.att-chip{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--border);\n  background:var(--raised);border-radius:10px;padding:7px 12px;font-size:12.5px}\n\n/* La conversazione a voce: l\'agente davanti a te, non una chat con un microfono.\n   La velocita\' viene da tre scelte: ascolta il browser (niente file da caricare\n   ne\' Whisper), parla il browser (niente sintesi remota), e comincia a parlare\n   ALLA PRIMA FRASE mentre il resto sta ancora arrivando. */\n#voicemode{position:fixed;inset:0;z-index:80;display:none;flex-direction:column;\n  align-items:center;justify-content:center;gap:22px;padding:30px;\n  background:radial-gradient(ellipse at 50% 38%,\n    color-mix(in srgb,var(--accent) 9%,var(--bg)) 0%,var(--bg) 62%)}\n#voicemode.on{display:flex}\n#vm-exit{position:absolute;top:18px;right:22px;border:0;background:none;\n  color:var(--muted);font-size:30px;cursor:pointer;line-height:1}\n#vm-exit:hover{color:var(--text)}\n.vm-scene{display:flex;gap:38px;flex-wrap:wrap;justify-content:center;align-items:flex-end}\n.vm-who{display:flex;flex-direction:column;align-items:center;gap:10px;\n  transition:transform .3s,opacity .3s}\n.vm-nome{font-size:14px;color:var(--muted);font-weight:600}\n.vm-who.parla{transform:translateY(-8px) scale(1.1)}\n.vm-who.ospite{opacity:0;transform:translateX(74px) scale(.82)}\n.vm-who.ospite.qui{opacity:1;transform:none}\n.vm-who.ospite.qui.parla{transform:translateY(-8px) scale(1.1)}\n.vm-who.ospite.via{opacity:0;transform:translateX(74px) scale(.82)}\n.vm-who.ospite .vm-nome::after{content:" · dropping by";color:var(--faint);\n  font-weight:400;font-size:11.5px}\n.vm-who.parla .vm-nome{color:var(--accent)}\n#voicemode.talking .vm-who:not(.parla){opacity:.4;transform:scale(.93)}\n.vm-who{position:relative}\n.vm-who.pensa .blob{animation:vmpensa 2.8s ease-in-out infinite}\n.vm-who.pensa::after{content:"···";position:absolute;top:-20px;\n  left:60%;font-size:19px;letter-spacing:2px;color:var(--muted);\n  animation:vmpuntini 1.6s ease-in-out infinite}\n@keyframes vmpensa{0%,100%{transform:rotate(0)}30%{transform:rotate(-5deg) translateY(-4px)}\n  65%{transform:rotate(3deg)}}\n@keyframes vmpuntini{0%,100%{opacity:.25}50%{opacity:.9}}\n#chatavatar .blob.pensa{animation:vmpensa 2.8s ease-in-out infinite}\n.vm-who.parla .blob{animation:vmparla 1s ease-in-out infinite}\n@keyframes vmparla{0%,100%{transform:scale(1)}30%{transform:scale(1.06)}\n  60%{transform:scale(.98)}}\n#vm-status{color:var(--muted);font-size:14px;min-height:20px}\n#vm-live{min-height:26px;max-width:620px;text-align:center;color:var(--text);\n  font-size:17px;line-height:1.5}\n#vm-mic{width:62px;height:62px;border-radius:50%;border:1px solid var(--border);\n  background:var(--raised);color:var(--muted);cursor:pointer;display:flex;\n  align-items:center;justify-content:center;position:relative}\n#vm-mic svg{width:24px;height:24px}\n#voicemode.listening #vm-mic{color:var(--accent);border-color:var(--accent)}\n#voicemode.listening #vm-mic::after{content:"";position:absolute;inset:-7px;\n  border-radius:50%;border:2px solid var(--accent);opacity:.5;\n  animation:vmring 1.6s ease-out infinite}\n@keyframes vmring{0%{transform:scale(.82);opacity:.55}100%{transform:scale(1.28);opacity:0}}\n#voicemode.paused #vm-mic{color:var(--bad);border-color:var(--bad)}\n@media (prefers-reduced-motion:reduce){\n  .vm-who.parla .blob,.vm-who.pensa .blob,.vm-who.pensa::after,\n  #chatavatar .blob.pensa,#voicemode.listening #vm-mic::after{animation:none}}\n.setbody .warn{border:1px solid var(--border);border-left:3px solid var(--accent);\n  border-radius:0 10px 10px 0;padding:10px 12px;font-size:12.5px;color:var(--text-dim);\n  background:var(--raised);line-height:1.55;margin:0 0 14px}\n@media (max-width:640px){.setbody .mrow{flex-direction:column;gap:0}}\n/* La discussione non finisce a giri contati: va avanti finche\' hanno qualcosa\n   da dire. Quindi serve un modo per dire «ok, basta» senza chiudere la pagina. */\n.roombar{display:none;align-items:center;gap:9px;margin:0 auto 8px;\n  max-width:760px;width:100%;padding:8px 12px;border:1px solid var(--border);\n  border-radius:12px;background:var(--raised);font-size:13px;color:var(--muted)}\n.roombar.on{display:flex}\n.roombar .rdot{width:7px;height:7px;border-radius:50%;background:var(--accent);\n  animation:rpulse 1.4s ease-in-out infinite}\n@keyframes rpulse{0%,100%{opacity:.35}50%{opacity:1}}\n.rstop{border:1px solid var(--border);background:transparent;color:var(--text-dim);\n  border-radius:9px;padding:5px 11px;font-size:12.5px;cursor:pointer}\n.rstop:hover{background:var(--hover);color:var(--text);border-color:var(--accent)}\n.rstop:disabled{opacity:.5;cursor:default}\n.roomend{color:var(--faint);font-size:12.5px;text-align:center;padding:10px 0 2px}\n@media (prefers-reduced-motion:reduce){.roombar .rdot{animation:none;opacity:.8}}\n.roomturn{opacity:.75}\n.roomturn .who{display:inline-flex;align-items:center;gap:6px}\n.roomturn .typing{margin-left:8px}\n/* Registro delle azioni: chi, cosa, su cosa. La domanda vera in un\n   portafoglio di agenti che possono eseguire comandi. */\n.act{padding:9px 2px;border-bottom:1px solid var(--border)}\n.act:last-child{border-bottom:0}\n.act .a1{display:flex;align-items:center;gap:8px;font-size:13px}\n.act .who{color:var(--accent);font-weight:650}\n.act .tool{color:var(--text);font-family:ui-monospace,monospace;font-size:12.5px}\n.act .risk{font-size:10.5px;text-transform:uppercase;letter-spacing:.4px;\n  color:var(--muted);border:1px solid var(--border);border-radius:99px;padding:0 6px}\n.act .when{color:var(--faint);font-size:11.5px}\n.act .a2{color:var(--muted);font-size:12px;margin-top:3px;word-break:break-all;\n  font-family:ui-monospace,monospace}\n.act.ko .tool{color:var(--bad)}\n.kv{display:flex;justify-content:space-between;gap:12px;padding:6px 0;font-size:13px}\n.kv .k{color:var(--muted)}.kv .v{color:var(--text);text-align:right;word-break:break-word}\n\n</style>\n</head>\n<body>\n<div class="shell">\n <div class="pane left">\n  <div class="roster">\n   <div class="rtop">\n     <button class="rfold" id="fold" title="Collapse the column"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg></button><div class="sec">Your agents</div>\n    <button class="radd" id="newagent">\n     <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"\n      stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>New</button></div>\n   <div class="rsearch">\n    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"\n     stroke-width="2.4" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>\n    <input id="rq" placeholder="Search an agent…" autocomplete="off"></div>\n   <div class="rheads" id="rheads"></div>\n   <div id="rrows"></div>\n   <div class="rfoot"><span class="dot" id="conn"></span><span id="rfoot"></span>\n  <span style="flex:1"></span>\n  <button class="rgear" id="gear" title="Settings">\n    <!-- Un ingranaggio vero. Prima c\'era il solicello usato per l\'elenco delle\n         azioni: stessa forma in due posti diversi, quindi nessuna delle due\n         diceva piu\' niente. -->\n    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"\n         stroke-linecap="round" stroke-linejoin="round">\n      <circle cx="12" cy="12" r="3.2"/>\n      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9c.2.61.77 1.02 1.41 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>\n    </svg></button></div>\n   </div>\n </div>\n <div class="pane right" id="right">\n<div class="dropveil" id="dropveil"><div class="dbox">\n  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"\n       stroke-linecap="round" stroke-linejoin="round">\n    <path d="M12 16V4M12 4 7 9M12 4l5 5"/><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></svg>\n  <div class="dt">Drop it here</div><div class="ds" id="dropsub"></div></div></div>\n  <div class="blank" id="blank">\n   <div id="blankavatar"></div>\n   <div class="btitle" id="btitle">You have no agents yet</div>\n   <div class="bsub" id="bsub">The roster starts empty on purpose: you make the agents,\n     one at a time, the way you want them.</div>\n   <div class="bsteps" id="bsteps">\n     <div class="bstep"><span class="n">1</span><div><b>Create one</b> with <b>+ New</b>.\n       The job is not a label: it is how the others will know that something is\n       theirs. <i>"amanda — hunts Amazon deals"</i>.</div></div>\n     <div class="bstep"><span class="n">2</span><div><b>Talk to it</b>. It has the\n       same tools openvurp has: it reads files, runs commands, opens pages. You\n       watch what it does while it does it.</div></div>\n     <div class="bstep"><span class="n">3</span><div><b>Make a second one</b> and\n       from then on they know each other: whoever doesn\'t know something asks the\n       right colleague, without you saying so.</div></div>\n     <div class="bstep"><span class="n">4</span><div><b>All together</b> puts them\n       in the same room. They discuss while they have something to say, and you\n       stop them whenever you like.</div></div>\n   </div>\n   <button class="bcta" id="bcta">+ Create your first agent</button>\n  </div>\n  <div class="chathead" id="chathead" style="display:none">\n   <button class="back" id="back" title="Back">\n    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg>\n   </button>\n   <div id="chatavatar"></div>\n   <div style="min-width:0"><div class="nm" id="chatname"></div><div class="sb" id="chatsub"></div></div><span style="flex:1"></span><button class="rmore" id="talk" title="Talk out loud">\n     <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"\n          stroke-width="2" stroke-linecap="round">\n       <path d="M4 10v4M8 7v10M12 4v16M16 7v10M20 10v4"/></svg>\n   </button><button class="rmore" id="chatmenu" title="More">⋯</button><div style="display:none"></div>\n  </div>\n  <p id="err"></p>\n  <div id="voicemode">\n  <button id="vm-exit" title="Close (Esc)">&times;</button>\n  <div class="vm-scene" id="vm-scene"></div>\n  <div id="vm-status"></div>\n  <div id="vm-live"></div>\n  <button id="vm-mic" title="Tap to interrupt or resume">\n    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"\n         stroke-linecap="round" stroke-linejoin="round">\n      <rect x="9" y="2" width="6" height="12" rx="3"/>\n      <path d="M5 11a7 7 0 0 0 14 0M12 18v4"/></svg>\n  </button>\n</div>\n<div class="setpage" id="panelpage" style="display:none">\n  <div class="sethead">\n    <button class="back" id="panelback" title="Back">\n      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"\n           stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg></button>\n    <div style="min-width:0"><div class="nm" id="paneltitle"></div>\n      <div class="sb" id="panelsub"></div></div>\n    <span style="flex:1"></span>\n    <div class="ptabs" id="ptabs"></div>\n  </div>\n  <div class="setbody" id="panelbody"></div>\n</div>\n<div class="setpage" id="setpage" style="display:none">\n  <div class="sethead">\n    <button class="back" id="setback" title="Back">\n      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"\n           stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg></button>\n    <div style="min-width:0"><div class="nm">Settings</div>\n      <div class="sb">Engine, subscriptions, channels. Keys stay on your computer.</div></div>\n    <span style="flex:1"></span>\n    <span class="setnote" id="setnote"></span>\n    <button class="mbtn primary" id="setsave">Save</button>\n  </div>\n  <div class="setbody" id="setbody"></div>\n</div>\n<div class="withprev" id="withprev">\n<div class="thread" id="thread" style="display:none"><div class="inner" id="inner"></div></div>\n<div class="prev" id="prev">\n <div class="phead">\n  <span class="pico" id="pico"></span>\n  <div class="ptitle"><span id="prevname"></span><span id="prevmeta"></span></div>\n  <div class="pswitch" id="pswitch" style="display:none">\n    <button id="pv-render" class="on" type="button">Preview</button>\n    <button id="pv-code" type="button">Code</button>\n  </div>\n  <span style="flex:1"></span>\n  <button class="pbtn" id="prevcopy" title="Copy the contents">\n    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"\n         stroke-linecap="round" stroke-linejoin="round">\n      <rect x="9" y="9" width="12" height="12" rx="2.5"/>\n      <path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg></button>\n  <a class="pbtn" id="prevopen" target="_blank" rel="noopener"\n     title="Open in a browser tab">\n    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"\n         stroke-linecap="round" stroke-linejoin="round">\n      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>\n      <path d="M15 3h6v6M10 14 21 3"/></svg></a>\n  <button class="pbtn" id="prevclose" title="Close (Esc)">\n    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"\n         stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg></button>\n </div>\n <div class="pbody" id="prevbody"></div>\n</div>\n</div>\n  <div class="roombar" id="roombar"><span class="rdot"></span>\n  <span id="roomstate">they are discussing</span><span style="flex:1"></span>\n  <button type="button" class="rstop" id="roomstop">Stop the discussion</button></div>\n<form class="composer" id="composer" style="display:none"><div class="wrap">\n   <div class="box">\n    <div class="chips" id="chips"></div>\n    <textarea id="cin" rows="1" placeholder="Write a message…"></textarea>\n    <div class="ctools">\n     <button type="button" class="ctool" id="attach" title="Attach a file">\n      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg></button>\n     <button type="button" class="ctool" id="mic" title="Speak instead of typing">\n      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"\n           stroke-linecap="round" stroke-linejoin="round">\n        <rect x="9" y="2" width="6" height="12" rx="3"/>\n        <path d="M5 11a7 7 0 0 0 14 0M12 18v4"/></svg></button>\n     <button type="button" class="ctool" id="attachimg" title="Attach an image">\n      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9.5" r="1.6"/><path d="m4 17 5-5 4 4 3-3 4 4"/></svg></button>\n     <span class="gap"></span>\n     <button class="send" id="csend" type="submit" title="Send" disabled>\n      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"\n       stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg></button>\n    </div>\n    <input type="file" id="filein" multiple hidden>\n    <input type="file" id="imgin" accept="image/*" multiple hidden>\n   </div>\n  </div></form>\n </div>\n</div>\n\n<script>\nconst $=s=>document.querySelector(s);\nconst esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;"}[c]));\nfunction setErr(m){const e=$("#err");if(!e)return;e.textContent=m||"";e.style.display=m?"block":"none"}\nasync function jget(u){const r=await fetch(u);if(!r.ok)throw new Error(u+" -> "+r.status);return r.json()}\nasync function jsend(u,method,data){\n  const opt={method,headers:{"Content-Type":"application/json"}};\n  if(data!==null)opt.body=JSON.stringify(data||{});\n  const r=await fetch(u,opt);const d=await r.json().catch(()=>({}));\n  if(!r.ok)throw new Error(d.error||u+" -> "+r.status);return d;\n}\n\n/* ── avatar ─────────────────────────────────────────────────────────────── */\nfunction hashOf(id){\n  let h=2166136261;\n  for(const ch of String(id||"")){h^=ch.charCodeAt(0);h=Math.imul(h,16777619)}\n  h^=h>>>15;h=Math.imul(h,2246822507);h^=h>>>13;h=Math.imul(h,3266489909);\n  return (h^(h>>>16))>>>0;\n}\nfunction pick(id,salt,n){return (hashOf(id+"|"+salt)%n)}\nfunction blob(id,size){\n  const c=blobColor(id);\n  // Indole del movimento + ritmi propri, ognuno da un hash indipendente:\n  // due avatar non si muovono mai uguale.\n  const st=`width:${size}px;height:${size}px;background:${c};--c:${c};`+\n    `--blink:${(4.5+pick(id,"b",70)/10).toFixed(1)}s;`+\n    `--float:${(3.4+pick(id,"f",30)/10).toFixed(1)}s;`+\n    `--look:${(7+pick(id,"l",50)/10).toFixed(1)}s`;\n  return `<div class="blob m${pick(id,"m",6)}" style="${st}">`+\n    `<span class="legs"><b></b><b></b><b></b><b></b></span>`+\n    `<span class="eyes"><i class="l"></i><i class="r"></i></span></div>`;\n}\nconst BLOB_COLORS=["#e8654a","#8b5cf6","#06b6d4","#10b981","#a78bfa","#ec4899","#f59e0b","#3b82f6"];\nfunction blobColor(id){return BLOB_COLORS[pick(id,"c",BLOB_COLORS.length)]}\n\n/* ── stato ──────────────────────────────────────────────────────────────── */\n/* La pagina arrivava vuota e si riempiva dopo: aprendola vedevi per un attimo\n   «non hai ancora nessun agente», poi comparivano. La rubrica il server ce\n   l\'ha gia\' quando serve la pagina — non c\'e\' motivo di andarla a chiedere. */\nconst BOOT=window.__BOOT__||{};\nlet roster=BOOT.roster||[],room=BOOT.room||null,providers=BOOT.providers||[];\nlet current="",currentChat="",messages=[],pending=[],roomEnds={};\nconst IMG=/[.](png|jpe?g|gif|webp|bmp)$/i;\n\nfunction unreadBadge(n,chatId){\n  // Nella conversazione che stai leggendo non ci sono "non letti": li stai\n  // leggendo. Il pallino serve a dire dove NON sei.\n  if(chatId&&chatId===currentChat)return"";\n  return n>0?`<span class="runread">${n>99?"99+":n}</span>`:"";\n}\nfunction engineLabel(a){\n  const m=(a.model||"").trim();\n  if(m)return m.length>26?m.slice(0,25)+"…":m;\n  const p=providers.find(x=>x.id===(a.backend||""));\n  return (a.backend||"").trim()?(p&&p.label||a.backend):"eredita";\n}\nfunction whenLabel(iso){\n  if(!iso)return"";\n  const d=new Date(iso);if(isNaN(d))return"";\n  const now=new Date();\n  if(d.toDateString()===now.toDateString())\n    return d.toLocaleTimeString("it-IT",{hour:"2-digit",minute:"2-digit"});\n  const days=Math.round((now-d)/86400000);\n  if(days<7)return d.toLocaleDateString("it-IT",{weekday:"short"});\n  return d.toLocaleDateString("it-IT",{day:"2-digit",month:"2-digit"});\n}\n\n/* ── striscia orizzontale ───────────────────────────────────────────────── */\nfunction headFades(el){\n  const more=el.scrollWidth-el.clientWidth;\n  el.classList.toggle("fl",more>2&&el.scrollLeft>2);\n  el.classList.toggle("fr",more>2&&el.scrollLeft<more-2);\n}\nfunction setupHeadStrip(){\n  const el=$("#rheads");if(!el||el.dataset.wired)return;\n  el.dataset.wired="1";\n  el.addEventListener("scroll",()=>headFades(el),{passive:true});\n  el.addEventListener("wheel",e=>{\n    if(Math.abs(e.deltaY)<=Math.abs(e.deltaX))return;\n    if(el.scrollWidth<=el.clientWidth)return;\n    el.scrollLeft+=e.deltaY;e.preventDefault();\n  },{passive:false});\n  new ResizeObserver(()=>headFades(el)).observe(el);\n}\n\n/* ── rubrica ────────────────────────────────────────────────────────────── */\nlet rosterSig="";\nasync function loadRoster(force){\n  const d=await jget("/api/agents/roster");\n  roster=d.roster||[];room=d.room||null;\n  // Ricostruire la lista significa ricreare ogni avatar e far ripartire le\n  // animazioni. Se i dati sono gli stessi, non c\'e\' niente da ridisegnare.\n  const sig=JSON.stringify([roster.map(a=>[a.id,a.name,a.model,a.backend,a.unread,a.last_at,a.preview]),\n                            room&&[room.id,room.unread,room.last_at,room.preview,(room.member_ids||[]).length],\n                            currentChat]);\n  if(!force&&sig===rosterSig)return;\n  rosterSig=sig;render();paintBlank();\n}\n/* L\'anteprima di una riga non e\' un messaggio in miniatura: e\' una frase.\n   Renderizzare il markdown qui darebbe grassetti e titoli dentro un rigo alto\n   quindici pixel; lasciarlo grezzo mostra «**Crucial P3 Plus**». Si toglie la\n   punteggiatura di formattazione e resta quello che c\'era scritto. */\nfunction anteprimaTesto(t){\n  let s=String(t||"");\n  s=s.replace(/\\[ALLEGATI dall\'utente[\\s\\S]*/,b=>{\n    if(/\\.(ogg|mp3|wav|m4a|webm|opus|oga)\\b/i.test(b))return "\\u{1F399} messaggio vocale";\n    if(/\\.(png|jpe?g|gif|webp|bmp|svg)\\b/i.test(b))return "\\u{1F4F7} foto";\n    if(/\\.pdf\\b/i.test(b))return "\\u{1F4C4} PDF";\n    return "\\u{1F4CE} allegato";\n  });\n  s=s.replace(/```[\\s\\S]*?```/g,"\\u2039codice\\u203a");   /* un blocco non si riassume */\n  s=s.replace(/`([^`]+)`/g,"$1");\n  s=s.replace(/!\\[([^\\]]*)\\]\\([^)]*\\)/g,(m,alt)=>alt||"\\u2039immagine\\u203a");\n  s=s.replace(/\\[([^\\]]+)\\]\\([^)]*\\)/g,"$1");           /* del link resta il testo */\n  s=s.replace(/^\\s{0,3}#{1,6}\\s+/gm,"");\n  s=s.replace(/^\\s{0,3}>\\s?/gm,"");\n  s=s.replace(/^\\s*(?:[-*+]|\\d+[.)])\\s+/gm,"\\u00b7 ");\n  s=s.replace(/\\*\\*([^*]+)\\*\\*/g,"$1").replace(/__([^_]+)__/g,"$1");\n  s=s.replace(/(^|[\\s(])\\*([^*\\n]+)\\*/g,"$1$2");\n  s=s.replace(/~~([^~]+)~~/g,"$1");\n  s=s.replace(/^\\s*[-*_]{3,}\\s*$/gm,"");\n  return s.replace(/\\s+/g," ").trim();\n}\nfunction roomRow(){\n  if(!room)return"";\n  const ids=(room.member_ids||[]).slice(0,3);\n  const stack=ids.length?ids.map(i=>blob(i,24)).join(""):blob("room",24);\n  const n=room.unread||0;\n  const aperta=room.id===currentChat;\n  return `<div class="rrow rroom${n&&!aperta?" unread":""}${aperta?" on":""}" data-room="1">\n    <div class="rstack">${stack}</div>\n    <span class="rmini">All</span>\n    <span class="rtip"><b>All together</b><i>${(room.member_ids||[]).length} agents in the same room</i></span>\n    <div class="rmeta"><div class="rl1">\n      <span class="rname">All together</span>\n      <span class="rbadge">${(room.member_ids||[]).length} agents</span>\n      <span style="flex:1"></span>${unreadBadge(n,room.id)}\n      <span class="rtime">${esc(whenLabel(room.last_at))}</span></div>\n     <div class="rprev">${esc(anteprimaTesto(room.preview)||"write here and they all answer")}</div></div></div>`;\n}\nfunction render(){\n  const q=($("#rq").value||"").trim().toLowerCase();\n  const rows=roster.filter(a=>!q||(a.name+" "+(a.role||"")+" "+(a.model||"")).toLowerCase().includes(q));\n  const strip=$("#rheads");\n  strip.innerHTML=[...roster].sort((a,b)=>\n    (b.message_count>0)-(a.message_count>0)||String(b.last_at||"").localeCompare(a.last_at||"")\n  ).slice(0,10).map(a=>\n    `<div class="rhead" data-open="${esc(a.id)}">${blob(a.id,60)}${unreadBadge(a.unread||0,a.chat_id)}<div class="lbl">${esc(a.name)}</div></div>`).join("");\n  requestAnimationFrame(()=>headFades(strip));\n\n  $("#rrows").innerHTML=(q?"":roomRow())+(rows.length?rows.map(a=>{\n    const none=!(a.model||"").trim()&&!(a.backend||"").trim(),n=a.unread||0;\n    const aperta=a.chat_id&&a.chat_id===currentChat;\n    return `<div class="rrow${n&&!aperta?" unread":""}${aperta?" on":""}" data-id="${esc(a.id)}">\n      ${blob(a.id,42)}\n      <span class="rmini">${esc(a.name)}</span>\n      <span class="rtip"><b>${esc(a.name)}</b>${a.role?\'<i>\'+esc(a.role)+\'</i>\':""}</span>\n      <div class="rmeta"><div class="rl1">\n        <span class="rname">${esc(a.name)}</span>\n        <button class="rbadge${none?" none":""}" data-engine="${esc(a.id)}" title="Change engine">${esc(engineLabel(a))}</button>\n        <span style="flex:1"></span>${unreadBadge(n,a.chat_id)}\n        <span class="rtime">${esc(whenLabel(a.last_at))}</span>\n        <button class="rmore" data-menu="${esc(a.id)}" title="More">⋯</button></div>\n       <div class="rprev">${esc(anteprimaTesto(a.preview)||a.role||"no messages yet")}</div></div></div>`;\n  }).join(""):\'<div class="emptymsg">No agents yet. Create one with “New”.</div>\');\n  $("#rfoot").textContent=roster.length+" agents · tap to talk, the badge to change engine";\n  wire();\n}\n/* A colonna chiusa la targhetta e\' fuori dal flusso: va messa dove sta la riga,\n   e va tenuta dentro lo schermo se la riga e\' vicina al bordo di sotto. */\nfunction seguiTarghette(){\n  document.querySelectorAll(".rrow").forEach(riga=>{\n    const tip=riga.querySelector(".rtip");\n    if(!tip)return;\n    riga.onmouseenter=()=>{\n      if(!document.body.classList.contains("folded"))return;\n      const r=riga.getBoundingClientRect();\n      tip.style.left=Math.round(r.right+10)+"px";\n      tip.classList.add("on");\n      const t=tip.getBoundingClientRect();\n      const y=Math.min(Math.max(8,r.top+r.height/2-t.height/2),\n                       window.innerHeight-t.height-8);\n      tip.style.top=Math.round(y)+"px";\n    };\n    riga.onmouseleave=()=>tip.classList.remove("on");\n  });\n}\nfunction wire(){\n  setupHeadStrip();\n  seguiTarghette();\n  const rr=$("#rrows").querySelector(\'[data-room="1"]\');\n  if(rr)rr.onclick=()=>openChat("room");\n  document.querySelectorAll(".rhead").forEach(h=>h.onclick=()=>openChat(h.dataset.open));\n  document.querySelectorAll(".rrow[data-id]").forEach(r=>r.onclick=()=>openChat(r.dataset.id));\n  document.querySelectorAll(".rrow .blob").forEach(o=>o.onclick=e=>{\n    // A colonna chiusa l\'avatar E\' la riga: qui il clic deve aprire la chat.\n    // La capriola resta un vezzo solo quando c\'e\' il resto della riga da toccare.\n    if(document.body.classList.contains("folded"))return;\n    e.stopPropagation();o.classList.remove("flip");void o.offsetWidth;o.classList.add("flip");\n    setTimeout(()=>o.classList.remove("flip"),1000)});\n  document.querySelectorAll("[data-engine]").forEach(b=>b.onclick=e=>{e.stopPropagation();enginePop(b.dataset.engine,b)});\n  document.querySelectorAll("[data-menu]").forEach(b=>b.onclick=e=>{e.stopPropagation();rowMenu(b.dataset.menu,b)});\n}\n\n/* ── popover ────────────────────────────────────────────────────────────── */\nfunction closePop(){\n  document.querySelectorAll(".pop").forEach(x=>x.remove());\n  document.querySelectorAll(".rmore.open").forEach(x=>x.classList.remove("open"));\n}\ndocument.addEventListener("click",closePop);\ndocument.addEventListener("keydown",e=>{if(e.key==="Escape"){closePop();closeModal()}});\nfunction popAt(anchor,html){\n  closePop();\n  const pop=document.createElement("div");pop.className="pop";pop.innerHTML=html;\n  pop.onclick=e=>e.stopPropagation();document.body.appendChild(pop);\n  const r=anchor.getBoundingClientRect(),pr=pop.getBoundingClientRect();\n  let top=r.bottom+6;\n  if(top+pr.height>innerHeight-8)top=Math.max(8,r.top-pr.height-6);\n  pop.style.top=top+"px";\n  pop.style.left=Math.max(8,Math.min(r.left,innerWidth-pr.width-10))+"px";\n  return pop;\n}\nfunction providerChoices(sel){\n  return [{id:"",label:"eredita dal globale"}].concat(providers).map(p=>\n    `<button class="popitem" data-p="${esc(p.id)}"><span class="tick">${p.id===(sel||"")?"✓":""}</span><span>${esc(p.label||p.id)}</span></button>`).join("");\n}\n/* I modelli per backend arrivano una volta e restano buoni per la sessione\n   del popup: due minuti, non per sempre — Ollama puo\' scaricare roba nuova. */\nlet MODELLI=null,MODELLI_T=0;\nasync function modelliDisponibili(){\n  const ora=Date.now();\n  if(MODELLI&&ora-MODELLI_T<120000)return MODELLI;\n  try{MODELLI=(await jget("/api/models")).models||{};MODELLI_T=ora}\n  catch(_){MODELLI=MODELLI||{}}\n  return MODELLI;\n}\nfunction enginePop(id,anchor){\n  const a=roster.find(x=>x.id===id);if(!a)return;\n  /* Il modello si SCEGLIE: scrivere un nome interno lettera per lettera resta\n     solo dietro «altro…», per i casi che il menu non conosce. */\n  const pop=popAt(anchor,\'<div class="ph">Engine</div>\'+providerChoices(a.backend)+\n    \'<div class="popsep"></div>\'+\n    \'<div class="popfield" id="popmodels"><div class="hint">carico i modelli\\u2026</div></div>\');\n  pop.querySelectorAll("[data-p]").forEach(b=>b.onclick=async()=>{\n    await jsend("/api/agents/"+id,"PATCH",{backend:b.dataset.p});\n    await loadRoster(true);\n    const riga=document.querySelector(\'[data-engine="\'+CSS.escape(id)+\'"]\');\n    if(riga)enginePop(id,riga);else closePop();\n  });\n  modelliDisponibili().then(tutti=>{\n    const zona=pop.querySelector("#popmodels");if(!zona)return;\n    const lista=(tutti[a.backend||""]||[]).slice();\n    if(a.model&&!lista.includes(a.model))lista.unshift(a.model);\n    zona.innerHTML=\'<select id="popmodel-sel">\'+\n      \'<option value="">predefinito del motore</option>\'+\n      lista.map(m=>\'<option value="\'+esc(m)+\'"\'+(m===(a.model||"")?" selected":"")+\n                   \'>\'+esc(m)+\'</option>\').join("")+\n      \'<option value="__altro__">altro\\u2026</option></select>\'+\n      \'<input id="popmodel" style="display:none" placeholder="nome del modello \\u00b7 Invio">\';\n    const sel=zona.querySelector("#popmodel-sel");\n    const campo=zona.querySelector("#popmodel");\n    sel.onchange=async()=>{\n      if(sel.value==="__altro__"){\n        sel.style.display="none";campo.style.display="";campo.focus();return;\n      }\n      closePop();\n      await jsend("/api/agents/"+id,"PATCH",{model:sel.value});\n      await loadRoster(true);\n    };\n    campo.onkeydown=async e=>{\n      if(e.key!=="Enter")return;\n      e.preventDefault();const v=campo.value.trim();closePop();\n      await jsend("/api/agents/"+id,"PATCH",{model:v});await loadRoster(true);\n    };\n  });\n}\nfunction rowMenu(id,anchor){\n  const a=roster.find(x=>x.id===id);if(!a)return;\n  anchor.classList.add("open");\n  const pop=popAt(anchor,\n    \'<button class="popitem" data-a="open"><span class="tick"></span>Open the chat</button>\'+\n    \'<button class="popitem" data-a="card"><span class="tick"></span>Card and history</button>\'+\'<button class="popitem" data-a="rename"><span class="tick"></span>Rename</button>\'+\n    \'<div class="popsep"></div>\'+\n    \'<button class="popitem danger" data-a="del"><span class="tick"></span>Delete</button>\');\n  pop.querySelector(\'[data-a="open"]\').onclick=()=>{closePop();openChat(id)};\n  pop.querySelector(\'[data-a="card"]\').onclick=()=>{closePop();agentCard(a)};\n  pop.querySelector(\'[data-a="rename"]\').onclick=async()=>{\n    const n=prompt("Nome dell\'agente",a.name);closePop();\n    if(n&&n.trim()){await jsend("/api/agents/"+id,"PATCH",{name:n.trim()});await loadRoster(true)}};\n  pop.querySelector(\'[data-a="del"]\').onclick=async()=>{\n    closePop();\n    if(!confirm("Eliminare "+a.name+"? Sparisce dalla rubrica; la conversazione viene archiviata, non cancellata."))return;\n    await jsend("/api/agents/"+id,"DELETE",null);\n    if(currentChat===a.chat_id){current="";currentChat="";showBlank()}\n    await loadRoster(true);\n  };\n}\nfunction agentCard(a){\n  const kv=(k,v)=>`<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`;\n  modal(\n    \'<h3>\'+esc(a.name)+\'</h3>\'+\n    \'<div class="mpreview">\'+blob(a.id,40)+\'<div><div class="nm">\'+esc(a.name)+\'</div>\'+\n      \'<div class="rl">\'+esc(a.role||"")+\'</div></div></div>\'+\n    \'<div class="panelbody">\'+\n      kv("Motore",engineLabel(a))+\n      kv("Backend",a.backend||"eredita dal globale")+\n      kv("Messaggi scambiati",String(a.message_count||0))+\n      kv("Ultimo scambio",whenLabel(a.last_at)||"mai")+\n      kv("Non letti",String(a.unread||0))+\n      kv("Creato",whenLabel(a.created_at)||"—")+\n      (a.instructions?(\'<div class="kv" style="display:block"><div class="k" style="margin-bottom:6px">Carattere e istruzioni</div>\'+\n        \'<div class="v" style="text-align:left;white-space:pre-wrap">\'+esc(a.instructions)+\'</div></div>\'):"")+\n    \'</div>\'+\n    \'<div class="mfoot"><button class="mbtn" id="c-close">Close</button>\'+\n      \'<button class="mbtn primary" id="c-open">Open the chat</button></div>\');\n  $("#c-close").onclick=closeModal;\n  $("#c-open").onclick=()=>{closeModal();openChat(a.id)};\n}\n\n/* ── conversazione ──────────────────────────────────────────────────────── */\nfunction showBlank(){\n  leaveChat();\n  try{localStorage.removeItem("ov.chat")}catch(_){}\n  showView("blank");\n}\nfunction markRead(id){\n  if(!id)return;\n  jsend("/api/chats/"+id+"/read","POST",{}).catch(()=>{});\n}\n/* Uscire da una chat vale come averla letta: i messaggi arrivati mentre eri\n   dentro li hai visti. Senza questo, cambiando stanza ti ritrovavi il pallino\n   delle non lette proprio su quella da cui stavi uscendo. */\nfunction leaveChat(){\n  if(currentChat)markRead(currentChat);\n}\nasync function openChat(target){\n  let chat=null,who=null;\n  if(target==="room"){chat=room;}\n  else{\n    who=roster.find(a=>a.id===target);\n    if(!who)return;\n    // La rubrica porta gia\' l\'id della chat: chiederlo al server a ogni clic\n    // era un giro di rete (e una scrittura) prima di poter mostrare qualcosa.\n    chat=who.chat_id?{id:who.chat_id}\n        :await jsend("/api/agents/"+target+"/chat","POST",{});\n  }\n  if(!chat)return;\n  /* Aggiornare la pagina non e\' uscire: la chat aperta deve riaprirsi da\n     sola. Si ricorda qui e si dimentica solo quando esci TU (indietro). */\n  try{localStorage.setItem("ov.chat",target)}catch(_){}\n  leaveChat();                       /* quella che lasci l\'hai letta */\n  roomBar(false);   /* paintLive la riaccende se in QUESTA si sta discutendo */\n  current=target;currentChat=chat.id;\n  // Segnare "letto" non deve far aspettare nessuno.\n  markRead(chat.id);\n  showView("chat");\n  growInput();\n  if(target==="room"){\n    $("#chatavatar").innerHTML=`<div class="rstack" style="width:34px;height:34px">${(room.member_ids||[]).slice(0,3).map(i=>blob(i,20)).join("")}</div>`;\n    $("#chatname").textContent="All together";\n    $("#chatsub").textContent=(room.member_ids||[]).length+" agents · they answer in turn";\n  }else{\n    $("#chatavatar").innerHTML=blob(who.id,34);\n    $("#chatname").textContent=who.name;\n    $("#chatsub").textContent=engineLabel(who)+" · "+(who.role||"");\n  }\n  await loadMessages();\n  connectStream();\n  paintLive();   // se qui stava arrivando qualcosa, riprende da dove eri\n  // La rubrica si aggiorna per conto suo: non deve trattenere l\'apertura.\n  loadRoster().catch(()=>{});\n}\nasync function loadMessages(){\n  if(!currentChat)return;\n  const d=await jget("/api/chats/"+encodeURIComponent(currentChat)+"/messages");\n  messages=d.messages||[];\n  paint();\n}\n/* Un percorso di file dentro un comando e\' l\'unica cosa che vuoi guardare, e\n   finora era testo morto: si copiava, si apriva altrove, si tornava indietro. */\nconst RX_PERCORSO=/(?:[A-Za-z]:)?[\\w./\\\\-]*\\.(?:py|js|ts|json|md|txt|html|css|sh|ya?ml|sql|c|h|cpp|rs|go|java|rb|php|toml|ini|xml|csv|pdf|png|jpe?g|gif|webp|svg)\\b/g;\nfunction percorsiApribili(testo){\n  const grezzo=String(testo||"");\n  let fuori="",fine=0,m;\n  RX_PERCORSO.lastIndex=0;\n  while((m=RX_PERCORSO.exec(grezzo))!==null){\n    fuori+=esc(grezzo.slice(fine,m.index));\n    fuori+=\'<span class="fileref" data-file="\'+esc(m[0])+\'">\'+esc(m[0])+\'</span>\';\n    fine=m.index+m[0].length;\n  }\n  return fuori+esc(grezzo.slice(fine));\n}\n/* I percorsi completi dentro un testo: quelli con una cartella e\n   un\'estensione visualizzabile, senza doppioni. */\nfunction trovaPercorsi(testo){\n  const fuori=[],visti=new Set();\n  let m;RX_PERCORSO.lastIndex=0;\n  while((m=RX_PERCORSO.exec(String(testo||"")))!==null){\n    const p=m[0];\n    if(!p.includes("/")&&!p.includes("\\\\"))continue;   /* nome nudo: ambiguo */\n    if(!visti.has(p)){visti.add(p);fuori.push(p)}\n  }\n  return fuori.slice(0,6);\n}\n/* Quali blocchi «azioni» hai aperto TU. Il blocco e\' un <details>, e ogni\n   ridisegno — cioe\' ogni azione nuova — lo ricreava chiuso: lo tenevi aperto\n   per guardare e ti si richiudeva sotto gli occhi. Decidi tu quando chiuderlo. */\nconst apertiSteps=new Set();\n$("#inner").addEventListener("toggle",e=>{\n  const d=e.target.closest("details[data-k]");\n  if(!d)return;\n  if(d.open)apertiSteps.add(d.dataset.k);else apertiSteps.delete(d.dataset.k);\n},true);   /* toggle non risale: serve la cattura */\n\nfunction stepsBlock(steps,chiave){\n  if(!steps||!steps.length)return"";\n  const righe=steps.map(s=>{\n    const segno=s.tool==="shell"?"$"\n      :(s.tool==="ask_peer"?"\\u2192":(s.tool==="ask_everyone"?"\\u21c9":"\\u2699"));\n    const testo=s.tool==="shell"?s.args:(s.tool+(s.args?" "+s.args:""));\n    return `<div class="st"><div class="cmd"><b>${segno}</b><code>${percorsiApribili(testo)}</code></div>`+\n           (s.out?`<div class="out">${esc(s.out)}</div>`:"")+`</div>`;\n  }).join("");\n  return `<details class="activity" data-k="${esc(chiave||"")}"${apertiSteps.has(chiave)?" open":""}><summary>\n    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/></svg>\n    <span>${steps.length} ${steps.length===1?"action":"actions"}</span></summary>${righe}</details>`;\n}\n/* Il blocco [ALLEGATI...] serve all\'agente, non a te: in chat si vedeva tutto\n   il paragrafo di istruzioni con il percorso del file. Qui si separa: il testo\n   resta testo, e i file diventano cose da guardare e ascoltare. */\nconst AUDIO_EST=["mp3","wav","ogg","m4a","webm","opus","oga"];\nfunction spezzaAllegati(testo){\n  const m=String(testo||"").match(/\\n*\\[ALLEGATI dall\'utente[^\\]]*\\]\\n?((?:- .+\\n?)*)/);\n  if(!m)return{testo:String(testo||""),files:[]};\n  const files=(m[1]||"").split("\\n").map(r=>r.replace(/^- /,"").trim()).filter(Boolean);\n  return{testo:String(testo).replace(m[0],"").trim(),files};\n}\nfunction allegatiHTML(files){\n  return \'<div class="atts">\'+files.map(p=>{\n    const nome=p.split(/[\\\\/]/).pop()||p;\n    const est=(nome.split(".").pop()||"").toLowerCase();\n    const src="/api/file?path="+encodeURIComponent(p);\n    if(AUDIO_EST.includes(est))\n      return \'<div class="aplay" data-audio="\'+src+\'">\'+\n        \'<button class="ap-btn" title="Play">\'+\n          \'<svg class="ic-play" viewBox="0 0 24 24" fill="currentColor">\'+\n            \'<path d="M8 5.5v13l11-6.5z"/></svg>\'+\n          \'<svg class="ic-pause" viewBox="0 0 24 24" fill="currentColor">\'+\n            \'<rect x="6.5" y="5.5" width="4" height="13" rx="1.2"/>\'+\n            \'<rect x="13.5" y="5.5" width="4" height="13" rx="1.2"/></svg></button>\'+\n        \'<div class="ap-track"><div class="ap-fill"></div></div>\'+\n        \'<span class="ap-time">\\u00b7\\u00b7\\u00b7</span></div>\';\n    if(IMMAGINI.includes(est))\n      return \'<img class="att-img" src="\'+src+\'" data-file="\'+esc(p)+\'" alt="\'+esc(nome)+\'">\';\n    return \'<span class="att-chip fileref" data-file="\'+esc(p)+\'">\'+esc(nome)+\'</span>\';\n  }).join("")+\'</div>\';\n}\nfunction paint(){\n  const many=current==="room";\n  const out=[];\n  for(let i=0;i<messages.length;i++){\n    const m=messages[i],meta=m.metadata||{},peer=meta.peer;\n    // Una consulenza fra agenti e\' UNO scambio, non due messaggi sciolti:\n    // domanda e risposta si mostrano insieme, con le due facce che si parlano.\n    if(peer&&meta.direction==="ask"){\n      const next=messages[i+1],nm=(next&&next.metadata)||{};\n      if(next&&nm.peer&&nm.direction==="answer"){\n        out.push(`<div class="peer">\n          <div class="pline">\n            <span class="who1">${blob(peer.from,26)}</span>\n            <span class="arrow"><i></i><i></i><i></i></span>\n            <span class="who2">${blob(peer.to,26)}</span>\n            <span class="pwho">${esc(peer.from_name)} asks ${esc(peer.to_name)}</span></div>\n          <div class="pq">${esc(m.content||"")}</div>\n          <div class="pa">${md(next.content||"")}</div>\n        </div>`);\n        i++;continue;\n      }\n    }\n    const me=m.author_type==="user";\n    const who=me?"Tu":(m.author_name||"openvurp");\n    const sp=me?spezzaAllegati(m.content||""):null;\n    /* «Ecco il giornale: /tmp/.../vurpiano.pdf» era testo morto: il file\n       c\'era ma non si apriva da nessuna parte. I percorsi che l\'agente\n       nomina diventano schede sotto il messaggio, come gli allegati tuoi. */\n    const prodotti=me?[]:trovaPercorsi(m.content||"");\n    out.push(`<div class="msg ${me?"me":"them"}">`+\n      (many&&!me?`<span class="who">${esc(who)}</span>`:"")+\n      (me?"":stepsBlock(meta.steps,m.id||currentChat+":"+i))+\n      (me?esc(sp.testo)+(sp.files.length?allegatiHTML(sp.files):"")\n         :md(m.content||"")+(prodotti.length?allegatiHTML(prodotti):""))+`</div>`);\n  }\n  if(roomEnds[currentChat])\n    out.push(\'<div class="roomend">\'+esc(roomEnds[currentChat])+\'</div>\');\n  $("#inner").innerHTML=out.join("");\n  // #inner viene ricostruito da zero: quello che sta accadendo ORA va rimesso,\n  // altrimenti ogni ridisegno cancella animazione e "is typing".\n  paintLive();\n  $("#thread").scrollTop=$("#thread").scrollHeight;\n}\n\n/* ── composer ───────────────────────────────────────────────────────────── */\nconst cin=$("#cin"),csend=$("#csend");\nfunction growInput(){\n  csend.disabled=(!cin.value.trim()&&!pending.length)||!currentChat;\n  // A composer nascosto scrollHeight e\' 0: misurarlo li\' fissa l\'altezza a\n  // zero e il campo resta schiacciato quando poi lo mostri.\n  if(!cin.offsetParent)return;\n  cin.style.height="auto";\n  cin.style.height=Math.min(cin.scrollHeight,200)+"px";\n}\ncin.oninput=growInput;\ncin.onkeydown=e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();$("#composer").requestSubmit()}};\nfunction renderChips(){\n  $("#chips").innerHTML=pending.map((f,i)=>{\n    const thumb=f.preview?`<img src="${f.preview}" alt="">`\n      :`<span class="ic">${esc((f.name.split(".").pop()||"?").slice(0,4).toUpperCase())}</span>`;\n    return `<span class="chip${f.path?"":" busy"}">${thumb}<span class="nm">${esc(f.name)}</span><span class="x" data-rm="${i}" title="Togli">×</span></span>`;\n  }).join("");\n  document.querySelectorAll("[data-rm]").forEach(x=>x.onclick=()=>{\n    const f=pending[+x.dataset.rm];if(f&&f.preview)URL.revokeObjectURL(f.preview);\n    pending.splice(+x.dataset.rm,1);renderChips();growInput()});\n}\nasync function addFile(file){\n  const entry={name:file.name,path:"",preview:IMG.test(file.name)?URL.createObjectURL(file):""};\n  pending.push(entry);renderChips();\n  try{\n    const b64=await new Promise((res,rej)=>{\n      const r=new FileReader();\n      r.onload=()=>res(String(r.result).split(",")[1]||"");\n      r.onerror=()=>rej(new Error("lettura fallita"));\n      r.readAsDataURL(file);\n    });\n    entry.path=(await jsend("/api/upload","POST",{name:file.name,data:b64})).path;\n  }catch(err){\n    setErr(file.name+": "+err.message);\n    const i=pending.indexOf(entry);\n    if(i>=0){if(entry.preview)URL.revokeObjectURL(entry.preview);pending.splice(i,1)}\n  }\n  renderChips();growInput();\n}\nasync function take(list){\n  for(const f of Array.from(list||[]).slice(0,8-pending.length))await addFile(f);\n  $("#filein").value=$("#imgin").value="";\n}\n$("#attach").onclick=()=>$("#filein").click();\n$("#attachimg").onclick=()=>$("#imgin").click();\n/* ── microfono ─────────────────────────────────────────────────────────── */\nlet registratore=null,pezziAudio=[],inizioRec=0,contaRec=null;\n/* La trascrizione la fa il BROWSER mentre registri, non Whisper dopo: su\n   questa macchina perfino importare Whisper muore in timeout, e infatti ogni\n   vocale tornava indietro con «il servizio e\' andato in timeout». Il testo\n   nasce insieme all\'audio; l\'audio resta per riascoltarlo. */\nlet micRec=null,micTesto="";\nasync function accendiMicrofono(){\n  if(!navigator.mediaDevices||!window.MediaRecorder){\n    setErr("this browser cannot record");return;\n  }\n  let flusso;\n  try{flusso=await navigator.mediaDevices.getUserMedia({audio:true})}\n  catch(e){setErr("microphone denied by the browser");return}\n  // Il formato lo sceglie il browser: Chrome fa webm, Firefox ogg.\n  registratore=new MediaRecorder(flusso);\n  pezziAudio=[];micTesto="";\n  const R=window.SpeechRecognition||window.webkitSpeechRecognition;\n  if(R){\n    micRec=new R();micRec.lang="it-IT";\n    micRec.continuous=true;micRec.interimResults=true;\n    micRec.onresult=e=>{\n      let finale="";\n      for(const r of e.results)if(r.isFinal)finale+=r[0].transcript;\n      micTesto=finale.trim();\n    };\n    micRec.onerror=()=>{};\n    try{micRec.start()}catch(_){micRec=null}\n  }\n  registratore.ondataavailable=e=>{if(e.data.size)pezziAudio.push(e.data)};\n  registratore.onstop=()=>{\n    flusso.getTracks().forEach(t=>t.stop());\n    if(micRec){try{micRec.stop()}catch(_){}}\n    const tipo=registratore.mimeType||"audio/webm";\n    const est=tipo.includes("ogg")?"ogg":(tipo.includes("mp4")?"m4a":"webm");\n    const blob=new Blob(pezziAudio,{type:tipo});\n    registratore=null;\n    if(blob.size<1200){setErr("recording too short");micRec=null;return}\n    /* L\'ultima frase del riconoscimento arriva un attimo DOPO lo stop:\n       aspettarla evita di perdere la coda di quello che hai detto. */\n    setTimeout(async()=>{\n      micRec=null;\n      const nome="vocale"+(micTesto?"-trascritta":"")+"."+est;\n      await take([new File([blob],nome,{type:tipo})]);\n      if(micTesto){\n        cin.value=(cin.value?cin.value+" ":"")+micTesto;\n        growInput();cin.focus();\n      }\n    },400);\n  };\n  registratore.start();\n  inizioRec=Date.now();\n  $("#mic").classList.add("rec");\n  $("#mic").title="Stop and send";\n  contaRec=setInterval(()=>{\n    const s=Math.floor((Date.now()-inizioRec)/1000);\n    $("#mic").title="Stop and send \\u00b7 "+s+"s";\n    if(s>=180)spegniMicrofono();      /* tre minuti bastano: oltre e\' un file */\n  },500);\n}\nfunction spegniMicrofono(){\n  if(contaRec){clearInterval(contaRec);contaRec=null}\n  $("#mic").classList.remove("rec");\n  $("#mic").title="Speak instead of typing";\n  if(registratore&&registratore.state!=="inactive")registratore.stop();\n}\n$("#mic").onclick=()=>{registratore?spegniMicrofono():accendiMicrofono()};\n\n$("#filein").onchange=()=>take($("#filein").files);\n$("#imgin").onchange=()=>take($("#imgin").files);\n/* Un drop mancato apre il file nel browser e ti butta fuori dalla chat:\n   la finestra intera annulla il default, sempre. */\nconst veil=$("#dropveil");let dragDepth=0;\nfunction carriesFiles(e){\n  const t=e.dataTransfer;if(!t)return false;\n  return [...(t.types||[])].includes("Files");\n}\nfunction showVeil(on){\n  if(!on){dragDepth=0;veil.classList.remove("on");return}\n  veil.classList.toggle("no",!currentChat);\n  $("#dropsub").textContent=currentChat\n    ? "images, PDFs, documents \\u2014 up to 8 at a time"\n    : "open a chat first";\n  veil.classList.add("on");\n}\nwindow.addEventListener("dragenter",e=>{\n  if(!carriesFiles(e))return;\n  e.preventDefault();dragDepth++;showVeil(true);\n});\nwindow.addEventListener("dragover",e=>{if(carriesFiles(e))e.preventDefault()});\nwindow.addEventListener("dragleave",e=>{\n  if(!carriesFiles(e))return;\n  /* dragleave scatta anche passando su un figlio: senza contatore lampeggia. */\n  if(--dragDepth<=0)showVeil(false);\n});\nwindow.addEventListener("drop",e=>{\n  e.preventDefault();showVeil(false);\n  if(!carriesFiles(e))return;\n  if(!currentChat){setErr("open a chat before attaching");return}\n  take(e.dataTransfer.files);\n});\ncin.addEventListener("paste",e=>{const f=[...(e.clipboardData?.files||[])];if(f.length){e.preventDefault();take(f)}});\n\n$("#composer").onsubmit=async e=>{\n  e.preventDefault();\n  const v=cin.value.trim(),files=pending.filter(f=>f.path).map(f=>f.path);\n  if((!v&&!files.length)||!currentChat)return;\n  if(pending.some(f=>!f.path)){setErr("wait for the attachments to finish uploading");return}\n  cin.value="";pending=[];renderChips();growInput();csend.disabled=true;setErr("");\n  delete roomEnds[currentChat];\n  const blocco=files.length\n    ?"\\n\\n[ALLEGATI dall\'utente]\\n"+files.map(f=>"- "+f).join("\\n"):"";\n  messages.push({author_type:"user",author_name:"Tu",\n                 content:(v+blocco).trim()});paint();\n  const inflight=currentChat;\n  liveOf(inflight).typing=true;paintLive();\n  const av=$("#chatavatar").querySelector(".blob");if(av)av.classList.add("talking");\n  try{\n    const r=await jsend("/api/chat","POST",{message:v,chat_id:currentChat,attachments:files});\n    if(r.team_errors&&r.team_errors.length){\n      /* Se la stanza non ha potuto parlare va detto NELLA stanza, non in una\n         riga di errore che sparisce: e\' li\' che stai guardando. */\n      roomEnds[inflight]=r.team_errors.join(" \\u00b7 ");\n      if(currentChat===inflight)paint();\n    }\n  }catch(err){setErr("error: "+err.message)}\n  finally{\n    delete live[inflight];\n    if(av){av.classList.remove("talking");av.classList.remove("flip");void av.offsetWidth;\n      av.classList.add("flip");setTimeout(()=>av.classList.remove("flip"),1000)}\n    if(currentChat===inflight)await loadMessages();\n    loadRoster().catch(()=>{});growInput();cin.focus();\n  }\n};\n\n/* ── modale nuovo agente ────────────────────────────────────────────────── */\nfunction closeModal(){const m=$("#ovmask");if(m)m.remove()}\nfunction modal(html){\n  closeModal();\n  const mask=document.createElement("div");mask.className="mask";mask.id="ovmask";\n  mask.innerHTML=`<div class="modal">${html}</div>`;\n  mask.onclick=e=>{if(e.target===mask)closeModal()};\n  document.body.appendChild(mask);return mask;\n}\nfunction newAgentModal(){\n  const m=modal(\n    \'<h3>New agent</h3>\'+\n    \'<div class="sub">Vive nella tua rubrica, ha un filo di conversazione suo e può discutere con gli altri.</div>\'+\n    \'<div class="mpreview"><span id="mavatar"></span><div><div class="nm" id="mname">senza nome</div><div class="rl" id="mrole">che ruolo ha?</div></div></div>\'+\n    \'<div class="field"><label>Nome</label><input id="f-name" placeholder="es. revisore" autocomplete="off"><div class="hint">What you call it when you write</div></div>\'+\n    \'<div class="field"><label>Ruolo</label><input id="f-role" placeholder="es. trova i buchi nel ragionamento" autocomplete="off"><div class="hint">The angle it looks from. It helps that it differs from yours.</div></div>\'+\n    \'<div class="field"><label>Carattere e istruzioni</label><textarea id="f-instr" placeholder="Optional — how it is, how it speaks, what it cannot stand."></textarea></div>\'+\n    \'<div class="mrow"><div class="field"><label>Engine</label><select id="f-backend">\'+\n      [{id:"",label:"eredita dal globale"}].concat(providers).map(p=>`<option value="${esc(p.id)}">${esc(p.label||p.id)}</option>`).join("")+\n    \'</select></div><div class="field"><label>Modello</label><input id="f-model" placeholder="predefinito" autocomplete="off"></div></div>\'+\n    \'<div class="mfoot"><button class="mbtn" id="f-cancel">Annulla</button><button class="mbtn primary" id="f-ok">Crea agente</button></div>\');\n  const name=m.querySelector("#f-name"),role=m.querySelector("#f-role"),\n        backend=m.querySelector("#f-backend"),model=m.querySelector("#f-model");\n  const refresh=()=>{\n    m.querySelector("#mavatar").innerHTML=blob(name.value||"nuovo",40);\n    m.querySelector("#mname").textContent=name.value.trim()||"senza nome";\n    m.querySelector("#mrole").textContent=role.value.trim()||"che ruolo ha?";\n  };\n  name.oninput=role.oninput=refresh;refresh();\n  backend.onchange=()=>{const p=providers.find(x=>x.id===backend.value);model.value=p&&p.default_model||""};\n  m.querySelector("#f-cancel").onclick=closeModal;\n  const submit=async()=>{\n    if(!name.value.trim()){name.focus();return}\n    await jsend("/api/agents","POST",{name:name.value.trim(),role:role.value.trim()||"peer",\n      instructions:m.querySelector("#f-instr").value.trim(),\n      backend:backend.value,model:model.value.trim()});\n    closeModal();await loadRoster(true);\n  };\n  m.querySelector("#f-ok").onclick=submit;\n  m.querySelectorAll("input").forEach(i=>i.onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();submit()}});\n  name.focus();\n}\n$("#newagent").onclick=newAgentModal;\n$("#rq").oninput=render;\n// La colonna chiusa lascia solo gli agenti. La scelta resta fra una sessione\n// e l\'altra: e\' una preferenza, non uno stato del momento.\nfunction setFold(on){\n  document.body.classList.toggle("folded",on);\n  try{localStorage.setItem("ov_folded",on?"1":"0")}catch(e){}\n  $("#fold").title=on?"Espandi la colonna":"Riduci la colonna";\n}\n$("#fold").onclick=()=>setFold(!document.body.classList.contains("folded"));\ntry{if(localStorage.getItem("ov_folded")==="1")setFold(true)}catch(e){}\n$("#back").onclick=showBlank;\n/* I tre puntini stanno DOVE serve: sopra la conversazione aperta. Prima i\n   pannelli erano un link in fondo alla rubrica, dove non c\'entravano nulla. */\n$("#chatmenu").onclick=e=>{\n  e.stopPropagation();\n  const a=roster.find(x=>x.id===current);\n  const tabs=[["activity","Cosa hanno fatto"],["runtime","Runtime"],\n              ["memory","Memoria"],["sessions","Sessioni"]];\n  let html="";\n  if(a){\n    html+=\'<button class="popitem" data-a="card"><span class="tick"></span>Card and history</button>\'+\n          \'<button class="popitem" data-a="engine"><span class="tick"></span>Cambia motore</button>\'+\n          \'<button class="popitem" data-a="rename"><span class="tick"></span>Rename</button>\'+\n          \'<div class="popsep"></div>\';\n  }\n  html+=\'<div class="ph">Sistema</div>\'+\n        \'<button class="popitem" data-a="settings"><span class="tick"></span>Settings</button>\'+\n        tabs.map(t=>`<button class="popitem" data-t="${t[0]}"><span class="tick"></span>${t[1]}</button>`).join("");\n  if(a)html+=\'<div class="popsep"></div>\'+\n             \'<button class="popitem danger" data-a="del"><span class="tick"></span>Delete \'+esc(a.name)+\'</button>\';\n  const pop=popAt(e.currentTarget,html);\n  pop.querySelectorAll("[data-t]").forEach(b=>b.onclick=async()=>{closePop();await openPanel(b.dataset.t)});\n  pop.querySelector(\'[data-a="settings"]\').onclick=()=>{closePop();openSettings()};\n  if(!a)return;\n  pop.querySelector(\'[data-a="card"]\').onclick=()=>{closePop();agentCard(a)};\n  pop.querySelector(\'[data-a="engine"]\').onclick=ev=>{closePop();enginePop(a.id,$("#chatmenu"))};\n  pop.querySelector(\'[data-a="rename"]\').onclick=async()=>{\n    const n=prompt("Nome dell\'agente",a.name);closePop();\n    if(n&&n.trim()){await jsend("/api/agents/"+a.id,"PATCH",{name:n.trim()});await loadRoster(true);\n      $("#chatname").textContent=n.trim()}};\n  pop.querySelector(\'[data-a="del"]\').onclick=async()=>{\n    closePop();\n    if(!confirm("Eliminare "+a.name+"? Sparisce dalla rubrica; la conversazione viene archiviata, non cancellata."))return;\n    await jsend("/api/agents/"+a.id,"DELETE",null);\n    current="";currentChat="";showBlank();await loadRoster(true);\n  };\n};\n/* Un solo posto decide cosa si vede. Con ogni vista che si accendeva e\n   spegneva da sola bastava dimenticare una riga per averne due insieme —\n   che e\' poi un modale mascherato. */\nlet vista="blank";\nfunction showView(nome){\n  vista=nome;\n  if(nome!=="chat"){chiudiPrev();chiudiVoce()}\n  const chat=nome==="chat";\n  document.body.classList.toggle("chatting",chat);\n  document.body.classList.toggle("insettings",nome==="settings");\n  document.body.classList.toggle("inpanel",nome==="panel");\n  $("#setpage").style.display=nome==="settings"?"flex":"none";\n  $("#setpage").classList.toggle("on",nome==="settings");\n  $("#panelpage").style.display=nome==="panel"?"flex":"none";\n  $("#panelpage").classList.toggle("on",nome==="panel");\n  $("#blank").style.display=nome==="blank"?"flex":"none";\n  // Il contenitore della chat, non solo il thread: e\' un flex:1 e se resta\n  // acceso si prende lo spazio anche da vuoto, schiacciando la pagina che\n  // dovrebbe occuparlo. Nascondere il figlio non basta.\n  $("#withprev").style.display=chat?"flex":"none";\n  $("#chathead").style.display=chat?"flex":"none";\n  $("#chatmenu").style.display=chat?"flex":"none";\n  $("#thread").style.display=chat?"flex":"none";\n  $("#composer").style.display=chat?"block":"none";\n}\nfunction showSettings(on){ if(on)showView("settings"); else if(vista==="settings")showView("blank"); }\n/* Il QR nasce qualche secondo dopo l\'accensione (e al primo avvio c\'e\' da\n   scaricare Baileys): la pagina lo va a prendere da sola finche\' non sei\n   collegato, invece di chiederti di ricaricare. */\nlet waPoll=null;\nasync function aggiornaWA(){\n  const posto=$("#wa-stato");\n  if(!posto||vista!=="settings"){if(waPoll){clearInterval(waPoll);waPoll=null}return}\n  let d;\n  try{d=await jget("/api/whatsapp/status")}catch(_){return}\n  if(!d.running){posto.textContent="off \\u2014 switch it on and save";return}\n  if(d.connected){\n    posto.innerHTML=\'<span style="color:var(--ok)">\\u2713 collegato\'+\n      (d.me?" as +"+esc(d.me):"")+\'</span>\';\n    clearInterval(waPoll);waPoll=null;return;\n  }\n  if(d.qr){\n    posto.innerHTML=\'Scan with WhatsApp \\u2192 Linked devices:\'+\n      \'<br><img src="\'+d.qr+\'" alt="QR" style="width:216px;border-radius:12px;\'+\n      \'margin-top:8px;background:#fff;padding:8px">\';\n    return;\n  }\n  posto.textContent=d.error||"starting the bridge\\u2026";\n}\nfunction tornaIndietro(){ if(currentChat)openChat(current); else showView("blank"); }\n/* Un interruttore con sotto il perche\', non una parola da scrivere. */\nfunction setSwitch(id,titolo,dettaglio,acceso){\n  return \'<label class="sw"><input type="checkbox" id="sw-\'+id+\'"\'+(acceso?" checked":"")+\'>\'+\n    \'<span>\'+esc(titolo)+(dettaglio?\'<span class="sd">\'+esc(dettaglio)+\'</span>\':"")+\'</span></label>\';\n}\nfunction setTags(prefisso,voci,scelte){\n  if(!voci.length)return \'<div class="nessuno">Nothing to choose here.</div>\';\n  const dentro=new Set((scelte||[]).map(String));\n  return \'<div class="tags">\'+voci.map(v=>{\n    const valore=String(v.id!==undefined?v.id:v),testo=String(v.label!==undefined?v.label:v);\n    return \'<label class="tag"><input type="checkbox" data-\'+prefisso+\'="\'+esc(valore)+\'"\'+\n      (dentro.has(valore)?" checked":"")+\'>\'+esc(testo)+\'</label>\';\n  }).join("")+\'</div>\';\n}\nfunction setSelect(id,label,voci,scelto,aiuto){\n  return \'<div class="field"><label>\'+esc(label)+\'</label><select id="s-\'+id+\'">\'+\n    voci.map(v=>{\n      const valore=String(v.id!==undefined?v.id:v),testo=String(v.label!==undefined?v.label:v);\n      return \'<option value="\'+esc(valore)+\'"\'+(valore===String(scelto||"")?" selected":"")+\n             \'>\'+esc(testo)+\'</option>\';\n    }).join("")+\'</select>\'+(aiuto?\'<div class="hint">\'+aiuto+\'</div>\':"")+\'</div>\';\n}\nfunction elenco(testo){\n  return String(testo||"").replace(/;/g,",").split(",").map(x=>x.trim()).filter(Boolean);\n}\nfunction setField(id,label,valore,extra){\n  return \'<div class="field"><label>\'+esc(label)+\'</label>\'+\n    \'<input id="s-\'+id+\'" \'+(extra||"")+\' value="\'+esc(valore||"")+\'"></div>\';\n}\nfunction setSecret(id,label,impostato,quando){\n  return \'<div class="field"><label>\'+esc(label)+\'</label>\'+\n    \'<input type="password" id="s-\'+id+\'" placeholder="\'+\n    (impostato?"impostato \\u2014 lascia vuoto per non toccarlo":esc(quando||"non impostato"))+\n    \'"></div>\';\n}\n/* Quelli che restano campi: segreti (non si possono elencare) e identificativi\n   che solo tu conosci. Tutto il resto e\' diventato una scelta. */\nconst SET_CAMPI=["OPENAI_API_KEY","ANTHROPIC_API_KEY","GROQ_API_KEY","TELEGRAM_TOKEN",\n  "TELEGRAM_CHAT_ID","SWARM_MAX_AGENTS","SWARM_DAILY_CALL_BUDGET",\n  "MULTIPLAYER_MAX_ROUNDS",\n  "DISCORD_TOKEN","DISCORD_ALLOWED_USERS","SLACK_BOT_TOKEN","SLACK_APP_TOKEN",\n  "SLACK_ALLOWED_USERS","WHATSAPP_ALLOWED_USERS"];\n\nasync function openSettings(){\n  let d;\n  try{d=await jget("/api/settings")}catch(e){setErr("settings: "+e.message);return}\n  const v=d.values||{},sec=d.secrets||{};\n  const opt=(list,cur)=>list.map(p=>\'<option value="\'+esc(p.id)+\'"\'+(p.id===cur?" selected":"")+\n    \'>\'+esc(p.label||p.id)+\'</option>\').join("");\n  const login=(id,label)=>{\n    const l=(d.logins||{})[id]||{};\n    return \'<div class="kv"><span class="k">\'+esc(label)+\'</span><span class="v">\'+\n      (l.ok?\'<span style="color:var(--ok)">\\u2713 collegato</span>\'\n           :\'<span style="color:var(--bad)">non collegato</span> \\u00b7 <code>\'+esc(l.command||"")+\'</code>\')+\n      \'</span></div>\';\n  };\n  const ollama=((d.ollama||{}).models||[]);\n  const strumenti=d.tools||[],gente=d.telegram_people||[];\n  /* "Look-up only": watch and report, never act. Built from the real names\n     available, so it never promises tools that do not exist. */\n  toolsConsulto=strumenti.filter(n=>\n    /^(read|list|find|grep|glob|search|web_|pdf|image|audio|memory|notify|who_is|ask_)/.test(n));\n  const scelti=elenco(v.SWARM_TOOLS);\n  const presetStrumenti=!scelti.length?"tutto"\n    :(scelti.slice().sort().join(",")===toolsConsulto.slice().sort().join(",")\n      ?"consulto":"custom");\n  const accesi=d.channels_running||[],attivi=elenco(v.CHANNELS_IN);\n  let locali=[];\n  /* Il menu partiva mischiando tutto: Ollama piu\' una lista fissa di nomi\n     GPT e Claude, qualunque fosse il backend. Un modello ha senso solo nel\n     SUO motore: la lista arriva da /api/models e segue il backend scelto. */\n  const modelli=v.LLM_MODEL?[v.LLM_MODEL]:[];\n\n  $("#setbody").innerHTML=\n    \'<div class="sec2"><div class="ph">Subscriptions</div>\'+\n      \'<div class="why">Codex and Claude Code use the subscription you already \'+\n        \'pay for, not a metered key.</div>\'+\n      login("codex","Codex \\u00b7 ChatGPT")+login("claude_cli","Claude Code \\u00b7 Claude.ai")+\n      \'<div class="hint">These sign-ins happen in the terminal: they open a consent \'+\n        \'page and wait there. A web page cannot do it for you.</div></div>\'+\n\n    \'<div class="sec2"><div class="ph">Default engine</div>\'+\n      \'<div class="why">Which model answers when a chat does not ask for one of its own.</div>\'+\n      \'<div class="mrow"><div class="field"><label>Backend</label><select id="s-backend">\'+\n        opt([{id:"",label:"not set"}].concat(d.providers||[]),v.LLM_BACKEND||"")+\n      \'</select></div>\'+\n      /* The detected models plus the one already set: you choose, you don\'t\n         retype a name that has to be guessed letter by letter. */\n      setSelect("model","Model",\n        [{id:"",label:"the backend default"}]\n          .concat(modelli.map(m=>({id:m,label:m}))),\n        v.LLM_MODEL,"of the engine chosen beside it")+\n      \'</div>\'+\n      \'<div class="field"><label>Ollama address</label>\'+\n        \'<input id="s-ollama" value="\'+esc(v.LLM_BASE_URL||"")+\'">\'+\n        \'<div class="hint">\'+ollama.length+\' models detected\'+\n        (ollama.length?\': \'+esc(ollama.slice(0,6).join(", ")):"")+\'</div></div></div>\'+\n\n    \'<div class="sec2"><div class="ph">Your local AI</div>\'+\n      \'<div class="why">Anything that exposes an OpenAI-style server works: \'+\n        \'LM Studio, llama.cpp, vLLM, Jan, koboldcpp, GPT4All\\u2026 \'+\n        \'(Ollama has its own row above.) openvurp has just knocked on the \'+\n        \'usual ports:</div>\'+\n      \'<div id="lserv-zone" class="nessuno">knocking on local ports\'+\n        \'\\u2026</div>\'+\n      setField("OPENAI_COMPATIBLE_BASE_URL","Server address",\n               v.OPENAI_COMPATIBLE_BASE_URL,\'placeholder="http://127.0.0.1:1234/v1"\')+\n      setSelect("OPENAI_COMPATIBLE_MODEL","Model",\n        [{id:"",label:"the first one the server offers"}]\n          .concat([...new Set(locali.flatMap(srv=>srv.models)\n            .concat(v.OPENAI_COMPATIBLE_MODEL?[v.OPENAI_COMPATIBLE_MODEL]:[]))]\n          .map(m=>({id:m,label:m}))),\n        v.OPENAI_COMPATIBLE_MODEL)+\n      setSecret("OPENAI_COMPATIBLE_API_KEY","Key (only if the server asks for one)",\n                sec.OPENAI_COMPATIBLE_API_KEY,"almost never needed locally")+\n      \'<div class="hint">To use it: above, Backend \\u2192 \'+\n        \'\\u201cOpenAI-compatible server\\u201d. Or for a single agent, from its \'+\n        \'engine badge in the roster.</div></div>\'+\n\n    \'<div class="sec2"><div class="ph">API keys</div>\'+\n      \'<div class="why">Only needed for metered backends. They stay in your .env: \'+\n        \'they are never read back here, only replaced.</div>\'+\n      ["OPENAI_API_KEY","ANTHROPIC_API_KEY","GROQ_API_KEY"].map(k=>\n        setSecret(k,k.replace("_API_KEY",""),sec[k])).join("")+\'</div>\'+\n\n    \'<div class="sec2"><div class="ph">Notifications</div>\'+\n      \'<div class="why">How an agent reaches you when you are away from the computer: \'+\n        \'the morning brief, \\u201cI\\u2019m done\\u201d, a permission waiting for an answer. \'+\n        \'Not a chat.</div>\'+\n      setSecret("TELEGRAM_TOKEN","Telegram bot token",sec.TELEGRAM_TOKEN,\n                "not set \\u2014 your agents cannot reach you")+\n      \'<div class="hint" style="padding:0 0 8px">Creating the bot takes two minutes: \'+\n        \'message <b>@BotFather</b> on Telegram, <code>/newbot</code>, and paste \'+\n        \'the token it gives you here.</div>\'+\n      setField("TELEGRAM_CHAT_ID","Chat to notify",v.TELEGRAM_CHAT_ID,\n               \'placeholder="your chat id"\')+\n      \'<div class="hint">To find it: send the bot any message and open \'+\n        \'<code>api.telegram.org/bot&lt;token&gt;/getUpdates</code>.</div></div>\'+\n\n    \'<div class="sec2"><div class="ph">Talking to your agents from outside</div>\'+\n      \'<div class="why">Ogni canale passa per la stessa conversazione della pagina web: \'+\n        \'rubrica, stanze e approvazioni valgono anche l\\u00ec. In chat scrivi \'+\n        \'<code>@nome</code>, <code>/agenti</code>, <code>/tutti</code>, <code>/stop</code>.</div>\'+\n      \'<div class="warn">An empty allow-list means <b>nobody</b>, and the channel will not \'+\n        \'start. It is the only thing between \\u201cI talk to my agents from my phone\\u201d \'+\n        \'and \\u201canyone can command my computer\\u201d.</div>\'+\n      \'<div class="scelte">\'+\n        ["telegram","discord","slack","whatsapp"].map(c=>\n          setSwitch("ch-"+c,c==="whatsapp"?"WhatsApp":c.charAt(0).toUpperCase()+c.slice(1),\n            c==="whatsapp"?"unofficial \\u00b7 spare number"\n              :(accesi.includes(c)?"on right now":""),\n            attivi.includes(c))).join("")+\'</div>\'+\n      \'<div class="ph" style="margin-top:6px">Telegram \\u00b7 who may write to it</div>\'+\n      (gente.length\n        ? \'<div class="why">Who has already written to the bot. Tick whom you allow: \'+\n          \'they will be able to make your agents run commands.</div>\'+\n          setTags("tg",gente.map(p=>({id:p.id,label:p.name+(p.username?" @"+p.username:"")})),\n                  elenco(v.TELEGRAM_ALLOWED_USERS))\n        : \'<div class="nessuno">Nobody has written to the bot yet. Send it a message \'+\n          \'and reload this page: they will appear here to tick.</div>\')+\n      \'<div class="ph" style="margin-top:10px">Discord</div>\'+\n      \'<div class="why">To connect it: on <b>discord.com/developers</b> \\u2192 \'+\n        \'New Application \\u2192 Bot tab \\u2192 copy the <b>token</b> and switch on \'+\n        \'<b>Message Content Intent</b>. Invite it to your server from OAuth2 \\u2192 \'+\n        \'URL Generator (tick \\u201cbot\\u201d). Your own ID: Discord Settings \'+\n        \'\\u2192 Advanced \\u2192 Developer Mode, then right-click your name \'+\n        \'\\u2192 Copy User ID.</div>\'+\n      \'<div class="mrow">\'+setSecret("DISCORD_TOKEN","Bot token",sec.DISCORD_TOKEN)+\n        setField("DISCORD_ALLOWED_USERS","Who may write to it (user ID)",\n                 v.DISCORD_ALLOWED_USERS)+\'</div>\'+\n      \'<div class="ph" style="margin-top:10px">Slack</div>\'+\n      \'<div class="why">On <b>api.slack.com/apps</b> \\u2192 Create App. You need \'+\n        \'TWO tokens: the bot one (<b>xoxb-</b>, from OAuth &amp; Permissions, after \'+\n        \'the install) and the app one (<b>xapp-</b>, from Basic Information \'+\n        \'\\u2192 App-Level Tokens with scope <code>connections:write</code>). Switch on \'+\n        \'<b>Socket Mode</b>: that way nothing has to be exposed on the internet.</div>\'+\n      \'<div class="mrow">\'+setSecret("SLACK_BOT_TOKEN","Bot token (xoxb-)",sec.SLACK_BOT_TOKEN)+\n        setSecret("SLACK_APP_TOKEN","App token (xapp-)",sec.SLACK_APP_TOKEN)+\'</div>\'+\n      setField("SLACK_ALLOWED_USERS","Who may write to it (member ID)",\n               v.SLACK_ALLOWED_USERS)+\n      \'<div class="ph" style="margin-top:10px">WhatsApp</div>\'+\n      \'<div class="warn">\\u26a0 This channel uses <b>Baileys</b>, an UNOFFICIAL \'+\n        \'client: Meta detects unofficial clients and <b>can ban the paired \'+\n        \'number</b>, even straight away. Use a <b>spare number</b>, never your \'+\n        \'personal one.</div>\'+\n      \'<div class="why">You pair it by scanning a QR, like WhatsApp Web: switch it \'+\n        \'on, save, and the QR appears below. Node.js must be installed (the first \'+\n        \'start fetches Baileys by itself).</div>\'+\n      setField("WHATSAPP_ALLOWED_USERS","Who may write to it (numbers with country code, e.g. 39333\\u2026)",\n               v.WHATSAPP_ALLOWED_USERS)+\n      \'<div id="wa-stato" class="nessuno"></div></div>\'+\n\n    \'<div class="sec2"><div class="ph">Agents</div>\'+\n      \'<div class="why">What they can do, and how much they can cost you.</div>\'+\n      \'<div class="ph" style="margin-top:2px">What they can do</div>\'+\n      \'<div class="why">One choice: forty checkboxes with internal tool names are \'+\n        \'not a choice, they are an exam. The detail stays under \'+\n        \'\\u201cone by one\\u201d for whoever wants it.</div>\'+\n      setSelect("SWARM_PRESET","",[\n        {id:"tutto",label:"everything openvurp can do"},\n        {id:"consulto",label:"look-up only: read, search, notify you \\u2014 no commands"},\n        {id:"custom",label:"one by one (advanced)"}],presetStrumenti)+\n      \'<div id="s-tools-adv" style="display:\'+(presetStrumenti==="custom"?"block":"none")+\'">\'+\n        setTags("tool",strumenti,elenco(v.SWARM_TOOLS))+\'</div>\'+\n      \'<div class="mrow">\'+\n        setSelect("SWARM_MAX_AGENTS","Maximum number of agents",\n                  ["","3","5","8","12","20"].map(n=>({id:n,label:n||"default"})),\n                  v.SWARM_MAX_AGENTS)+\n        setSelect("SWARM_DAILY_CALL_BUDGET","Calls per day",\n                  ["","50","120","300","1000"].map(n=>({id:n,label:n||"default"})),\n                  v.SWARM_DAILY_CALL_BUDGET)+\'</div>\'+\n      setSelect("MULTIPLAYER_MAX_ROUNDS","Maximum rounds in a discussion",\n                ["","4","8","12","20","40"].map(n=>({id:n,label:n||"default (12)"})),\n                v.MULTIPLAYER_MAX_ROUNDS)+\n      \'<div class="hint">Not a duration: a discussion ends when nobody has anything \'+\n        \'left to say, or when you stop it. This is the brake for when you walk away.</div></div>\';\n\n  showSettings(true);\n  $("#setnote").textContent="";\n  const sp=$("#s-SWARM_PRESET");\n  if(sp)sp.onchange=()=>{\n    $("#s-tools-adv").style.display=sp.value==="custom"?"block":"none"};\n  /* Il modello segue il motore: cambi backend, cambiano le scelte. Il valore\n     gia\' impostato resta sempre in lista — toglierlo lo cancellerebbe. */\n  function riempiModelli(){\n    const sel=$("#s-model"),backend=$("#s-backend").value;\n    const lista=((MODELLI||{})[backend]||[]).slice();\n    const attuale=sel.value||v.LLM_MODEL||"";\n    if(attuale&&!lista.includes(attuale))lista.unshift(attuale);\n    sel.innerHTML=\'<option value="">the backend default</option>\'+\n      lista.map(m=>\'<option value="\'+esc(m)+\'"\'+(m===attuale?" selected":"")+\n                   \'>\'+esc(m)+\'</option>\').join("");\n  }\n  modelliDisponibili().then(riempiModelli);\n  $("#s-backend").onchange=riempiModelli;\n  // La scansione delle porte arriva per conto suo: la pagina non la aspetta.\n  jget("/api/local-servers").then(r=>{\n    locali=r.servers||[];\n    const zona=$("#lserv-zone");if(!zona)return;\n    if(!locali.length){\n      zona.innerHTML=\'Nessun server locale acceso adesso. Avvia il tuo \'+\n        \'programma (in LM Studio: scheda Developer \\u2192 Start Server) e \'+\n        \'riapri questa pagina \\u2014 comparir\\u00e0 qui da scegliere.\';\n      return;\n    }\n    zona.className="tags";\n    zona.innerHTML=locali.map((srv,i)=>\n      \'<label class="tag"><input type="radio" name="lserver" data-lsrv="\'+i+\'"\'+\n      ($("#s-OPENAI_COMPATIBLE_BASE_URL").value===srv.url?" checked":"")+\'>\'+\n      esc(srv.name)+\' \\u00b7 \'+srv.models.length+\n      (srv.models.length===1?" modello":" modelli")+\'</label>\').join("");\n    zona.querySelectorAll("[data-lsrv]").forEach(r2=>r2.onchange=()=>{\n      const srv=locali[+r2.dataset.lsrv];if(!srv)return;\n      $("#s-OPENAI_COMPATIBLE_BASE_URL").value=srv.url;\n      const sel=$("#s-OPENAI_COMPATIBLE_MODEL");\n      sel.innerHTML=\'<option value="">il primo che offre il server</option>\'+\n        srv.models.map(m=>\'<option value="\'+esc(m)+\'">\'+esc(m)+\'</option>\').join("");\n      if(srv.models.length===1)sel.value=srv.models[0];\n    });\n  }).catch(()=>{});\n  aggiornaWA();\n  if(waPoll)clearInterval(waPoll);\n  waPoll=setInterval(aggiornaWA,2500);\n}\n\n/* ── Anteprima ──────────────────────────────────────────────────────────── */\nconst IMMAGINI=["png","jpg","jpeg","gif","webp","svg","bmp","ico"];\nlet prevTesto="",toolsConsulto=[];\n/* Per l\'interruttore Anteprima/Codice di una pagina: le due viste dello\n   stesso file, pronte entrambe. */\nlet prevViste=null;\n\nfunction chiudiPrev(){\n  document.body.classList.remove("previewing");\n  prevTesto="";prevViste=null;\n  $("#prevbody").innerHTML="";\n}\nfunction iconaDi(est){\n  if(est==="pdf")return "\\u{1F4C4}";\n  if(est==="html"||est==="htm")return "\\u{1F310}";\n  if(IMMAGINI.includes(est))return "\\u{1F5BC}\\u{FE0F}";\n  if(AUDIO_EST.includes(est))return "\\u{1F399}\\u{FE0F}";\n  return "\\u{1F4DD}";\n}\nfunction apriPrev(nome,meta,corpo,testo,href,ico){\n  $("#pico").textContent=ico||"\\u{1F4DD}";\n  $("#prevname").textContent=nome;$("#prevname").title=nome;\n  $("#prevmeta").textContent=meta||"";\n  $("#prevbody").innerHTML=corpo;\n  prevTesto=testo||"";\n  $("#prevcopy").style.display=testo?"":"none";\n  const scheda=$("#prevopen");\n  if(href){scheda.style.display="";scheda.href=href}\n  else scheda.style.display="none";\n  $("#pswitch").style.display=prevViste?"flex":"none";\n  document.body.classList.add("previewing");\n}\nfunction codiceHTML(testo){\n  const righe=String(testo).split("\\n");\n  const numeri=righe.map((_,i)=>i+1).join("\\n");\n  return \'<div class="code"><div class="ln">\'+esc(numeri)+\'</div>\'+\n         \'<div class="src">\'+esc(testo)+\'</div></div>\';\n}\nfunction mostraCodice(testo,lang,nome){\n  prevViste=null;\n  const righe=String(testo).split("\\n").length;\n  apriPrev(nome||("codice"+(lang?"."+lang:"")),\n           (lang?lang+" \\u00b7 ":"")+righe+(righe===1?" riga":" righe"),\n           codiceHTML(testo),testo,"",iconaDi(lang||""));\n}\nfunction prevVista(quale){\n  if(!prevViste)return;\n  $("#pv-render").classList.toggle("on",quale==="render");\n  $("#pv-code").classList.toggle("on",quale==="code");\n  $("#prevbody").innerHTML=prevViste[quale];\n}\nasync function apriFile(percorso){\n  const q=encodeURIComponent(percorso);\n  const nome=String(percorso).split(/[\\\\/]/).pop()||percorso;\n  const est=(nome.split(".").pop()||"").toLowerCase();\n  const grezzo="/api/file?path="+q;\n  prevViste=null;\n  if(est==="pdf"){\n    /* Niente visore del browser dentro la scheda: le pagine le rendiamo noi,\n       come fogli appoggiati sul fondo scuro. La barra grigia di Chrome resta\n       fuori — per quella c\'e\' il bottone «apri in scheda». */\n    apriPrev(nome,"PDF \\u00b7 carico\\u2026",\n             \'<div class="perr">rendering the pages\\u2026</div>\',"",grezzo,iconaDi(est));\n    try{\n      const d=await jget("/api/file?as=pdfmeta&path="+q);\n      const fogli=Array.from({length:d.pages},(_,k)=>\n        \'<img class="sheet" loading="lazy" alt="pagina \'+(k+1)+\'" \'+\n        \'src="/api/file?as=pdfpage&page=\'+(k+1)+\'&path=\'+q+\'">\').join("");\n      apriPrev(nome,d.pages+(d.pages===1?" pagina":" pagine")+" \\u00b7 "+\n               Math.max(1,Math.round((d.size||0)/1024))+" KB",\n               \'<div class="sheets">\'+fogli+\'</div>\',"",grezzo,iconaDi(est));\n    }catch(e){\n      apriPrev(nome,"PDF",\'<div class="perr">\'+esc(e.message)+\n               \'<br><br>Puoi comunque aprirlo in una scheda del browser.</div>\',\n               "",grezzo,iconaDi(est));\n    }\n    return;\n  }\n  if(IMMAGINI.includes(est)){\n    apriPrev(nome,est.toUpperCase(),\n             \'<div class="vimg"><img src="\'+grezzo+\'" alt="\'+esc(nome)+\'"></div>\',\n             "",grezzo,iconaDi(est));\n    return;\n  }\n  if(est==="html"||est==="htm"){\n    /* Una pagina ha due facce, come negli artifact: impaginata e sorgente.\n       La sorgente si scarica subito cosi\' l\'interruttore e\' istantaneo e il\n       Copia copia sempre qualcosa. */\n    let sorgente="";\n    try{sorgente=(await jget("/api/file?as=text&path="+q)).text||""}catch(_){}\n    prevViste={render:\'<iframe sandbox="" src="\'+grezzo+\'" title="\'+esc(nome)+\'"></iframe>\',\n               code:codiceHTML(sorgente)};\n    apriPrev(nome,"pagina",prevViste.render,sorgente,grezzo,iconaDi(est));\n    prevVista("render");\n    return;\n  }\n  apriPrev(nome,"loading\\u2026",\'<div class="perr">carico\\u2026</div>\',"","",iconaDi(est));\n  try{\n    const d=await jget("/api/file?as=text&path="+q);\n    apriPrev(d.name||nome,(d.lang?d.lang+" \\u00b7 ":"")+(d.lines||0)+" righe \\u00b7 "+\n             Math.max(1,Math.round((d.size||0)/1024))+" KB",\n             codiceHTML(d.text||""),d.text||"",grezzo,iconaDi(est));\n  }catch(e){\n    apriPrev(nome,"", \'<div class="perr">Non riesco ad aprirlo: \'+esc(e.message)+\n             \'<br><br>Le anteprime valgono solo per i file dentro la cartella di \'+\n             \'openvurp, e mai per .env, database o chiavi.</div>\',"","",\n             "\\u26A0\\u{FE0F}");\n  }\n}\n$("#prevclose").onclick=chiudiPrev;\n$("#pv-render").onclick=()=>prevVista("render");\n$("#pv-code").onclick=()=>prevVista("code");\ndocument.addEventListener("keydown",e=>{\n  if(e.key==="Escape"&&document.body.classList.contains("previewing")&&!vm.on)\n    chiudiPrev();\n});\n$("#prevcopy").onclick=()=>{\n  navigator.clipboard.writeText(prevTesto).then(()=>{\n    const b=$("#prevcopy");b.style.color="var(--ok)";\n    setTimeout(()=>b.style.color="",1100);\n  }).catch(()=>{});\n};\n\n/* Un solo audio alla volta: due vocali insieme sono rumore. */\nlet apAudio=null,apBox=null,apSrc="";\nfunction orario(s){s=Math.max(0,Math.floor(s||0));\n  return Math.floor(s/60)+":"+String(s%60).padStart(2,"0")}\nfunction apFerma(){\n  if(apAudio){try{apAudio.pause()}catch(_){}apAudio=null}\n  if(apBox){apBox.classList.remove("on");apBox=null}\n  apSrc="";\n}\nfunction apClic(box,e){\n  const track=e.target.closest(".ap-track");\n  if(box===apBox&&apAudio){\n    if(track&&isFinite(apAudio.duration)){\n      const r=track.getBoundingClientRect();\n      apAudio.currentTime=(e.clientX-r.left)/r.width*apAudio.duration;\n      return;\n    }\n    if(apAudio.paused){apAudio.play().catch(()=>{});box.classList.add("on")}\n    else{apAudio.pause();box.classList.remove("on")}\n    return;\n  }\n  apFerma();\n  apBox=box;apSrc=box.dataset.audio;\n  apAudio=new Audio(apSrc);\n  box.classList.add("on");\n  apAudio.ontimeupdate=()=>{\n    /* paint() ricostruisce i nodi: il riquadro va ritrovato, non ricordato. */\n    const vivo=[...document.querySelectorAll(".aplay")]\n      .find(b=>b.dataset.audio===apSrc);\n    if(!vivo)return;\n    apBox=vivo;vivo.classList.add("on");\n    const d=isFinite(apAudio.duration)?apAudio.duration:0;\n    vivo.querySelector(".ap-fill").style.width=\n      d?(apAudio.currentTime/d*100)+"%":"0";\n    vivo.querySelector(".ap-time").textContent=\n      orario(apAudio.currentTime)+(d?" / "+orario(d):"");\n  };\n  apAudio.onended=()=>{\n    const vivo=[...document.querySelectorAll(".aplay")]\n      .find(b=>b.dataset.audio===apSrc);\n    if(vivo){vivo.classList.remove("on");\n      vivo.querySelector(".ap-fill").style.width="0"}\n    apAudio=null;apBox=null;apSrc="";\n  };\n  apAudio.onerror=()=>{box.classList.remove("on");\n    box.querySelector(".ap-time").textContent="errore"};\n  apAudio.play().catch(()=>{});\n}\n\n/* Delega sul thread: i messaggi vengono ridisegnati di continuo, agganciare\n   ogni bottone a ogni ridisegno sarebbe una perdita garantita. */\n$("#inner").addEventListener("click",e=>{\n  const lettore=e.target.closest(".aplay");\n  if(lettore){apClic(lettore,e);return}\n  const apri=e.target.closest("[data-code]");\n  if(apri){mostraCodice(CODICI[+apri.dataset.code]||"",apri.dataset.lang||"");return}\n  const copia=e.target.closest("[data-copy]");\n  if(copia){\n    navigator.clipboard.writeText(CODICI[+copia.dataset.copy]||"").then(()=>{\n      copia.textContent="done";setTimeout(()=>copia.textContent="copy",1200);\n    }).catch(()=>{});\n    return;\n  }\n  const file=e.target.closest("[data-file]");\n  if(file){apriFile(file.dataset.file);return}\n});\n\n/* ── Conversazione a voce ──────────────────────────────────────────────────\n   Come si parla tra persone: tu parli, lui risponde, senza pulsanti in mezzo.\n   La velocita\' sta in tre scelte:\n   1. ascolta il BROWSER (riconoscimento integrato: niente upload, niente\n      Whisper — la trascrizione nasce mentre parli);\n   2. parla il BROWSER (sintesi integrata: niente file da generare e scaricare);\n   3. comincia a parlare ALLA PRIMA FRASE, mentre il resto sta ancora\n      arrivando dal modello in streaming.\n   Cosi\' l\'attesa e\' solo il tempo del modello. Limite onesto: il\n   riconoscimento c\'e\' su Chrome ed Edge, non su Firefox. */\nconst vm={on:false,chat:"",room:false,solo:"",rec:null,pending:false,\n          parlando:0,buf:"",muto:false};\n\nfunction voceSupporto(){return !!(window.SpeechRecognition||window.webkitSpeechRecognition)}\nfunction semino(s){let h=2166136261;for(const c of String(s)){h^=c.charCodeAt(0);\n  h=Math.imul(h,16777619)}return h>>>0}\nfunction voceItaliana(){\n  const voci=speechSynthesis.getVoices();\n  return voci.find(v=>/^it/i.test(v.lang)&&/Google|Elsa|Isabella/i.test(v.name))\n      || voci.find(v=>/^it/i.test(v.lang)) || null;\n}\n\n/* Aspettare in silenzio davanti a un pupazzo fermo sembra un blocco. Mentre\n   pensa si mette in posa — ondeggia, guarda in su, puntini sopra la testa —\n   e lo stato racconta, con una frase diversa ogni pochi secondi. */\nconst ATTESE=[\n  "thinking it over\\u2026","choosing the words\\u2026",\n  "tentacles on the keyboard\\u2026","one moment, getting there\\u2026",\n  "joining the dots\\u2026",\n  "meanwhile: think up your next question \\u2014 you win if you surprise it",\n];\nconst ATTESE_TUTTI=[\n  "they are conferring\\u2026","they are consulting each other\\u2026",\n  "there is a stir in the room\\u2026","somebody is raising a hand\\u2026",\n  "meanwhile: guess who will answer first",\n];\nlet vmGiro=null;\nfunction vmAspetta(acceso){\n  if(vmGiro){clearInterval(vmGiro);vmGiro=null}\n  document.querySelectorAll(".vm-who").forEach(w=>w.classList.toggle("pensa",!!acceso));\n  if(!acceso)return;\n  const frasi=vm.room?ATTESE_TUTTI:ATTESE;\n  let i=Math.floor(Math.random()*frasi.length);\n  vmStato(frasi[i%frasi.length]);\n  vmGiro=setInterval(()=>{if(vm.pending)vmStato(frasi[++i%frasi.length]);\n                          else vmAspetta(false)},3400);\n}\nfunction vmStato(testo){$("#vm-status").textContent=testo||""}\n\n/* La consulenza, messa in scena. In chat gia\' si vedeva (i due polpi che si\n   avvicinano): a voce l\'ospite ENTRA nella stanza, risponde con la SUA voce\n   — la senti cambiare — e quando ha detto la sua se ne va. */\nfunction vmPeerArriva(e){\n  if(!vm.on||e.chat_id!==vm.chat)return;\n  vmAspetta(false);\n  let box=document.querySelector(\'.vm-who[data-vm="\'+CSS.escape(String(e.to_id))+\'"]\');\n  if(!box){\n    box=document.createElement("div");\n    box.className="vm-who ospite";box.dataset.vm=e.to_id;\n    box.innerHTML=blob(e.to_id,110)+\'<div class="vm-nome">\'+esc(e.to_name)+\'</div>\';\n    $("#vm-scene").appendChild(box);\n    requestAnimationFrame(()=>box.classList.add("qui"));\n  }\n  vmStato(esc(e.from_name)+" is calling "+esc(e.to_name)+"\\u2026");\n  $("#vm-live").textContent="\\u2192 "+e.to_name+": \\u201c"+(e.question||"")+"\\u201d";\n}\nfunction vmPeerRisponde(e){\n  if(!vm.on||e.chat_id!==vm.chat)return;\n  vmStato(e.to_name+" is answering\\u2026");\n  parla(e.answer||"",e.to_id,()=>vmPeerVia(e.to_id));\n}\nfunction vmPeerVia(chi){\n  const box=document.querySelector(\'.vm-who.ospite[data-vm="\'+CSS.escape(String(chi))+\'"]\');\n  if(!box)return;\n  box.classList.remove("qui");box.classList.add("via");\n  setTimeout(()=>box.remove(),480);\n  $("#vm-live").textContent="";\n}\nfunction vmClass(nome,acceso){$("#voicemode").classList.toggle(nome,!!acceso)}\nfunction vmEvidenzia(chi,acceso){\n  document.querySelectorAll(".vm-who").forEach(w=>{\n    if(w.dataset.vm===String(chi))w.classList.toggle("parla",!!acceso);\n    else if(acceso)w.classList.remove("parla");\n  });\n  vmClass("talking",!!acceso);\n}\n\n/* Le emoji sono per gli occhi. Lette diventano «faccina con occhi a cuore»\n   in mezzo alla frase: la voce le salta, sullo schermo restano. */\nfunction senzaEmoji(t){\n  return String(t||"").replace(\n    /[\\u{1F000}-\\u{1FAFF}\\u{2600}-\\u{27BF}\\u{2B00}-\\u{2BFF}\\u{FE0F}\\u{200D}\\u{1F1E6}-\\u{1F1FF}]/gu,"");\n}\nfunction parla(testo,chi,dopo){\n  /* Il testo arriva in markdown: gli asterischi non si leggono ad alta voce. */\n  const pulito=anteprimaTesto(senzaEmoji(testo));\n  if(!pulito||!vm.on){if(dopo)dopo();return}\n  const u=new SpeechSynthesisUtterance(pulito);\n  u.lang="it-IT";\n  const v=voceItaliana();if(v)u.voice=v;\n  if(chi){\n    /* Ogni agente ha la sua voce: tono e passo derivati dal suo id, stabili\n       nel tempo — la stessa idea dei polpi che si muovono ognuno a modo suo. */\n    const h=semino(chi);\n    u.pitch=0.82+((h>>>3)%37)/100;\n    u.rate=1.0+((h>>>9)%13)/100;\n  }else{u.rate=1.04}\n  vm.parlando++;\n  u.onstart=()=>{vmAspetta(false);if(chi)vmEvidenzia(chi,true);vmStato("")};\n  u.onend=u.onerror=()=>{\n    vm.parlando=Math.max(0,vm.parlando-1);\n    if(chi&&!vm.parlando)vmEvidenzia(chi,false);\n    if(dopo)dopo();\n    if(!vm.parlando&&!vm.pending&&vm.on&&!vm.rec&&!vm.muto)ascolta();\n  };\n  speechSynthesis.speak(u);\n}\n\nfunction voceToken(testo){\n  if(!vm.on)return;\n  vm.buf+=testo;\n  /* Frasi complete via via che arrivano: e\' qui che nasce l\'immediatezza. */\n  let m;\n  while((m=vm.buf.match(/^[\\s\\S]*?[.!?\\u2026]\\s/))){\n    vm.buf=vm.buf.slice(m[0].length);\n    parla(m[0],vm.solo);\n  }\n}\nfunction voceFine(){\n  if(!vm.on)return;\n  vmAspetta(false);\n  if(vm.buf.trim())parla(vm.buf,vm.solo);\n  vm.buf="";vm.pending=false;\n  if(!vm.parlando&&!vm.rec&&!vm.muto)ascolta();\n}\n\nfunction ascolta(){\n  if(!vm.on||vm.muto)return;\n  const R=window.SpeechRecognition||window.webkitSpeechRecognition;\n  const r=new R();vm.rec=r;\n  r.lang="it-IT";r.interimResults=true;\n  vmClass("listening",true);vmStato("listening\\u2026");\n  r.onresult=e=>{\n    let finale="",bozza="";\n    for(const res of e.results)(res.isFinal?finale+=res[0].transcript\n                                           :bozza+=res[0].transcript);\n    $("#vm-live").textContent=finale||bozza;\n    if(finale.trim()){try{r.stop()}catch(_){}vm.rec=null;vmInvia(finale.trim())}\n  };\n  r.onerror=ev=>{\n    vm.rec=null;vmClass("listening",false);\n    if(ev.error==="not-allowed")vmStato("microphone denied by the browser");\n  };\n  r.onend=()=>{\n    /* Il riconoscimento si chiude da solo dopo un silenzio: se il giro non e\'\n       partito, si riapre — la conversazione non deve morire per una pausa. */\n    if(vm.rec===r){vm.rec=null;vmClass("listening",false);\n      if(vm.on&&!vm.pending&&!vm.muto)setTimeout(()=>{\n        if(vm.on&&!vm.rec&&!vm.pending&&!vm.muto)ascolta()},300);}\n  };\n  try{r.start()}catch(_){vm.rec=null}\n}\n\nasync function vmInvia(testo){\n  vm.pending=true;vm.buf="";\n  vmClass("listening",false);\n  vmAspetta(true);\n  $("#vm-live").textContent="\\u201c"+testo+"\\u201d";\n  messages.push({author_type:"user",author_name:"Tu",content:testo});paint();\n  const inflight=currentChat;\n  try{await jsend("/api/chat","POST",{message:testo,chat_id:inflight,attachments:[]})}\n  catch(e){vmStato("error: "+e.message)}\n  finally{\n    /* Come il composer: il turno e\' finito, lo stato dal vivo va via. Senza\n       questo, dopo un giro a voce restavano appesi tool e «sta scrivendo». */\n    delete live[inflight];\n    if(currentChat===inflight){paintLive();loadMessages().catch(()=>{})}\n    loadRoster().catch(()=>{});\n    voceFine();   /* se assistant_end e\' gia\' passato, non fa nulla */\n  }\n}\n\nfunction apriVoce(){\n  if(!currentChat)return;\n  if(!voceSupporto()){\n    setErr("voice conversation works on Chrome and Edge: this browser cannot listen");\n    return;\n  }\n  vm.on=true;vm.chat=currentChat;vm.room=current==="room";\n  vm.solo=vm.room?"":current;vm.muto=false;vm.buf="";vm.pending=false;\n  const scena=$("#vm-scene");\n  scena.innerHTML=vm.room\n    ?(room.member_ids||[]).map(i=>{\n        const a=roster.find(x=>x.id===i)||{};\n        return \'<div class="vm-who" data-vm="\'+esc(i)+\'">\'+blob(i,110)+\n               \'<div class="vm-nome">\'+esc(a.name||"")+\'</div></div>\';\n      }).join("")\n    :\'<div class="vm-who" data-vm="\'+esc(current)+\'">\'+blob(current,170)+\n     \'<div class="vm-nome">\'+esc($("#chatname").textContent)+\'</div></div>\';\n  $("#voicemode").classList.add("on");\n  $("#vm-live").textContent="";\n  try{speechSynthesis.cancel()}catch(_){}\n  try{speechSynthesis.getVoices()}catch(_){}   /* scalda l\'elenco delle voci */\n  ascolta();\n}\nfunction chiudiVoce(){\n  if(!vm.on)return;\n  vmAspetta(false);\n  vm.on=false;vm.pending=false;vm.buf="";\n  if(vm.rec){try{vm.rec.abort()}catch(_){}vm.rec=null}\n  try{speechSynthesis.cancel()}catch(_){}\n  vm.parlando=0;\n  $("#voicemode").classList.remove("on","listening","talking","paused");\n}\n$("#talk").onclick=apriVoce;\n$("#vm-exit").onclick=chiudiVoce;\ndocument.addEventListener("keydown",e=>{if(e.key==="Escape"&&vm.on)chiudiVoce()});\n$("#vm-mic").onclick=()=>{\n  /* Un tocco: se sta parlando lo interrompi e riprendi tu; se ascolta, pausa;\n     se in pausa, riparte. Come alzare una mano. */\n  if(vm.parlando){try{speechSynthesis.cancel()}catch(_){}\n    vm.parlando=0;vmEvidenzia("",false);vm.buf="";ascolta();return}\n  if(vm.rec){vm.muto=true;try{vm.rec.abort()}catch(_){}vm.rec=null;\n    vmClass("listening",false);vmClass("paused",true);vmStato("paused");return}\n  vm.muto=false;vmClass("paused",false);ascolta();\n};\n$("#vm-scene").onclick=()=>{\n  if(vm.parlando){try{speechSynthesis.cancel()}catch(_){}\n    vm.parlando=0;vmEvidenzia("",false);vm.buf="";ascolta()}\n};\n\n$("#gear").onclick=()=>openSettings();\n$("#bcta").onclick=()=>$("#newagent").click();\n/* Con qualche agente gia\' fatto, la guida ai primi passi diventa rumore. */\nfunction paintBlank(){\n  const primi=!roster.length;\n  $("#btitle").textContent=primi?"You have no agents yet"\n                                :"Choose who to talk to";\n  $("#bsub").textContent=primi\n    ? "La rubrica nasce vuota apposta: gli agenti li fai tu, uno per volta, come li vuoi."\n    : "Open a conversation from the column on the left, or put them all in the same room.";\n  $("#bsteps").style.display=primi?"flex":"none";\n  $("#bcta").textContent=primi?"+ Crea il primo agente":"+ New agent";\n}\n$("#setback").onclick=tornaIndietro;\n$("#panelback").onclick=tornaIndietro;\nfunction spuntati(attributo){\n  return [...document.querySelectorAll("[data-"+attributo+"]")]\n    .filter(x=>x.checked).map(x=>x.dataset[attributo]).join(",");\n}\n$("#setsave").onclick=async()=>{\n  const val={LLM_BACKEND:$("#s-backend").value,\n             LLM_MODEL:$("#s-model").value,\n             LLM_BASE_URL:$("#s-ollama").value};\n  SET_CAMPI.forEach(k=>{const el=$("#s-"+k);if(el)val[k]=el.value});\n  // Le scelte spuntate diventano gli elenchi che il server si aspetta.\n  val.CHANNELS_IN=["telegram","discord","slack","whatsapp"]\n    .filter(c=>{const s=$("#sw-ch-"+c);return s&&s.checked}).join(",");\n  val.TELEGRAM_ALLOWED_USERS=spuntati("tg");\n  const sp2=$("#s-SWARM_PRESET");\n  val.SWARM_TOOLS=!sp2||sp2.value==="tutto"?""\n    :(sp2.value==="consulto"?toolsConsulto.join(","):spuntati("tool"));\n  $("#setnote").textContent="saving\\u2026";\n  try{\n    const r=await jsend("/api/settings","POST",{values:val});\n    providers=r.providers||providers;\n    // Non basta dire "salvato": va detto cosa e\' cambiato ADESSO, altrimenti\n    // non sai se il canale che hai acceso sta girando davvero.\n    const c=r.channels||{};\n    let nota="Saved.";\n    if((c.started||[]).length)nota+=" On: "+c.started.join(", ")+".";\n    if((c.stopped||[]).length)nota+=" Off: "+c.stopped.join(", ")+".";\n    if((c.errors||[]).length)nota+=" \\u26a0 "+c.errors.join(" \\u00b7 ");\n    $("#setnote").textContent=nota;\n    if(c.started||c.stopped)setTimeout(()=>openSettings(),400);\n  }catch(e){$("#setnote").textContent="Error: "+e.message}\n};\nconst PANNELLI=[\n  ["activity","Cosa hanno fatto i tuoi agenti",\n   "Ogni comando, ricerca e file toccato, dal registro di controllo."],\n  ["runtime","Runtime","Come sta il programma adesso."],\n  ["memory","Memoria","Cosa si ricorda, e quanto."],\n  ["sessions","Sessioni","I fili di conversazione aperti."]];\n\nasync function openPanel(kind){\n  kind=kind||"activity";\n  const voce=PANNELLI.find(p=>p[0]===kind)||PANNELLI[0];\n  $("#paneltitle").textContent=voce[1];\n  $("#panelsub").textContent=voce[2];\n  $("#ptabs").innerHTML=PANNELLI.map(p=>\n    \'<button class="ptab\'+(p[0]===kind?" on":"")+\'" data-p="\'+p[0]+\'">\'+esc(p[1].split(" ")[0])+\'</button>\').join("");\n  $("#ptabs").querySelectorAll("[data-p]").forEach(b=>b.onclick=()=>openPanel(b.dataset.p));\n  showView("panel");\n  $("#panelbody").innerHTML=\'<div class="sec2"><div class="emptymsg">carico\\u2026</div></div>\';\n\n  let corpo="";\n  try{\n    if(kind==="runtime"){const r=(await jget("/api/status")).runtime||{};\n      corpo=righeChiaveValore(r);\n    }else if(kind==="memory"){\n      corpo=righeChiaveValore((await jget("/api/memory")).stats||{});\n    }else if(kind==="sessions"){const d=await jget("/api/sessions");\n      corpo=(d.route_sessions||[]).map(i=>\n        \'<div class="kv"><span class="k">\'+esc(i.key)+\'</span><span class="v">\'+\n        esc(i.state||"in attesa")+\'</span></div>\').join("")||\n        \'<div class="emptymsg">Nessuna sessione aperta.</div>\';\n    }else{\n      const rows=(await jget("/api/activity")).activity||[];\n      corpo=rows.length?rows.map(r=>\n        \'<div class="act\'+(r.ok?"":" ko")+\'">\'+\n          \'<div class="a1"><span class="who">\'+esc(r.by_agent?r.who:"openvurp")+\'</span>\'+\n            \'<span class="tool">\'+esc(r.target)+\'</span>\'+\n            (r.risk&&r.risk!=="safe"?\'<span class="risk">\'+esc(r.risk)+\'</span>\':"")+\n            \'<span style="flex:1"></span><span class="when">\'+esc(r.when)+\'</span></div>\'+\n          (r.args?\'<div class="a2">\'+esc(r.args)+\'</div>\':"")+\n        \'</div>\').join("")\n        :\'<div class="emptymsg">Nessuna azione registrata. Appena un agente \'+\n         \'esegue un comando o apre una pagina, lo trovi qui.</div>\';\n    }\n  }catch(err){corpo=\'<div class="emptymsg">\'+esc(err.message)+\'</div>\'}\n  $("#panelbody").innerHTML=\'<div class="sec2">\'+corpo+\'</div>\';\n}\n\nfunction righeChiaveValore(oggetto){\n  const righe=Object.entries(oggetto||{});\n  if(!righe.length)return \'<div class="emptymsg">Nothing to show.</div>\';\n  return righe.map(([k,v])=>\n    \'<div class="kv"><span class="k">\'+esc(k)+\'</span><span class="v">\'+\n    esc(String(v))+\'</span></div>\').join("");\n}\n\n\n/* ── markdown ── */\nconst CODICI=[];   /* i blocchi di codice, per poterli riaprire */\nfunction md(t){\n  const blocks=[];\n  let s=String(t).replace(/```(\\w*)\\n?([\\s\\S]*?)```/g,(m,lang,c)=>{\n    const codice=c.replace(/\\n$/,""),id=CODICI.push(codice)-1;\n    blocks.push(\'<div class="cb"><div class="cbhead">\'+\n      \'<span class="lang">\'+esc(lang||"testo")+\'</span>\'+\n      \'<span style="flex:1"></span>\'+\n      \'<button data-copy="\'+id+\'">copy</button>\'+\n      \'<button data-code="\'+id+\'" data-lang="\'+esc(lang||"")+\'">open</button></div>\'+\n      \'<pre><code>\'+esc(codice)+\'</code></pre></div>\');\n    return \'\\u0000\'+(blocks.length-1)+\'\\u0000\';\n  });\n  s=esc(s);\n  s=s.replace(/`([^`\\n]+)`/g,\'<code>$1</code>\');\n  s=s.replace(/^#### (.+)$/gm,\'<h4>$1</h4>\').replace(/^### (.+)$/gm,\'<h4>$1</h4>\')\n     .replace(/^## (.+)$/gm,\'<h3>$1</h3>\').replace(/^# (.+)$/gm,\'<h2>$1</h2>\');\n  s=s.replace(/\\*\\*([^*]+)\\*\\*/g,\'<b>$1</b>\');\n  s=s.replace(/(^|[\\s(])\\*([^*\\n]+)\\*(?=[\\s).,;:!?]|$)/g,\'$1<i>$2</i>\');\n  s=s.replace(/\\[([^\\]]+)\\]\\((https?:[^)\\s]+)\\)/g,\'<a href="$2" target="_blank" rel="noopener">$1</a>\');\n  s=s.replace(/^(?:[-*]|\\d+\\.) .+(?:\\n(?:[-*]|\\d+\\.) .+)*/gm,\n    b=>\'<ul>\'+b.split(\'\\n\').map(l=>\'<li>\'+l.replace(/^(?:[-*]|\\d+\\.) /,\'\')+\'</li>\').join(\'\')+\'</ul>\');\n  s=s.split(/\\n{2,}/).map(p=>{\n    if(/^<(h\\d|ul|pre)/.test(p.trim()))return p;\n    return \'<p>\'+p.replace(/\\n/g,\'<br>\')+\'</p>\';\n  }).join(\'\');\n  s=s.replace(/\\u0000(\\d+)\\u0000/g,(m,i)=>blocks[+i]);\n  return s;\n}\n\n/* ── streaming ───────────────────────────────────────────────────────────\n   Una sola connessione per tutte le conversazioni. Prima era agganciata alla\n   chat aperta: cambiando agente lo stream si staccava e quello che stava\n   arrivando andava perso. Ora gli eventi arrivano sempre e la vista sceglie\n   cosa mostrare; quello che riguarda le altre chat resta da parte e lo ritrovi\n   quando ci torni.                                                          */\nlet stream=null,lastSeq=0;\nconst live={};                    // chat_id → {text, steps, typing}\n/* La rete di sicurezza per l\'evento di chiusura che non arriva mai: uno\n   stream che cade, un processo interrotto — e «sta scrivendo», un tool o un\n   «ci sta pensando» restavano li\' finche\' non aggiornavi la pagina. Cinque\n   minuti SENZA alcun evento non sono un lavoro lento (uno shell lento manda\n   comunque la chiusura): sono un turno morto, e si porta via tutto. */\nsetInterval(()=>{\n  const ora=Date.now();\n  let toccaRidisegnare=false;\n  for(const id of Object.keys(live)){\n    if(ora-(live[id].ts||0)>300000){\n      delete live[id];\n      if(id===currentChat)toccaRidisegnare=true;\n    }\n  }\n  if(toccaRidisegnare){paintLive();roomBar(false)}\n},15000);\nfunction liveOf(id){\n  /* Tutto cio\' che sta accadendo ADESSO in questa chat. Prima le due facce che\n     si parlano e la riga "is typing" esistevano solo come nodi appesi al\n     DOM: paint() ricostruisce #inner da zero, quindi bastava cambiare chat (o\n     un qualsiasi ridisegno) per cancellarle, e tornando indietro non c\'era piu\'\n     niente. Se e\' in corso, deve stare nello stato. */\n  if(!live[id])live[id]={text:"",steps:[],typing:false,peers:[],turn:null,\n                         ts:Date.now()};\n  return live[id];\n}\nfunction connectStream(){\n  if(stream)return;\n  try{\n    stream=new EventSource("/api/stream?replay=0");\n    stream.onmessage=ev=>{try{onEvent(JSON.parse(ev.data))}catch(_){}};\n    stream.onerror=()=>{};\n  }catch(e){}\n}\nfunction onEvent(e){\n  if(e.seq){if(e.seq<=lastSeq)return;lastSeq=e.seq}\n  const id=e.chat_id||"";\n  if(e.kind==="approval"){askApproval(e);return}\n  if(e.kind==="room_turn"){roomTurn(e);return}\n  if(e.kind==="room_message"){roomSaid(e);return}\n  if(e.kind==="room_end"){roomEnd(e);return}\n  if(e.kind==="peer"){showPeer(e);return}\n  if(e.kind==="peer_done"){answerPeer(e);return}\n  if(e.kind==="approval_done"){closeApproval(e.approval_id);return}\n  if(!id)return;\n  const st=liveOf(id);\n  st.ts=Date.now();   /* vivo: la scopa guarda questo */\n  if(e.kind==="step"){\n    st.steps.push({tool:e.step==="shell"?"shell":(e.step||"tool"),\n                   args:String(e.text||""),out:""});\n    st.label=e.step==="shell"?"runs a command":"uses "+(e.step||"a tool");\n  }else if(e.kind==="token"){\n    st.typing=false;st.text+=(e.text||"");stopSpinner();\n    if(vm.on&&id===vm.chat)voceToken(e.text||"");\n  }else if(e.kind==="assistant_end"){\n    delete live[id];\n    if(vm.on&&id===vm.chat)voceFine();\n    if(id===currentChat){\n      // L\'hai appena letta arrivare: non deve tornare "da leggere".\n      markRead(id);\n      const seguiva=apertiSteps.has("live:"+id);\n      apertiSteps.delete("live:"+id);\n      loadMessages().then(()=>{\n        loadRoster(true).catch(()=>{});\n        if(seguiva){\n          /* Stavi guardando le azioni dal vivo: restano aperte anche ora che\n             il turno e\' un messaggio salvato, con la sua chiave nuova. */\n          const ultimo=[...messages].reverse().find(x=>\n            x.author_type!=="user"&&((x.metadata||{}).steps||[]).length);\n          if(ultimo&&ultimo.id){apertiSteps.add(ultimo.id);paint()}\n        }\n        /* Se il turno ha prodotto qualcosa da guardare (un PDF, una pagina,\n           un\'immagine), si apre da solo: «te lo mostro qui» deve mostrare. */\n        const ultimo=[...messages].reverse()\n          .find(x=>x.author_type!=="user");\n        if(ultimo&&!vm.on){\n          const belli=trovaPercorsi(ultimo.content||"").filter(p=>\n            /\\.(pdf|png|jpe?g|webp|gif|html?)$/i.test(p));\n          if(belli.length)apriFile(belli[belli.length-1]);\n        }\n      });\n    }\n    else markBusy();\n    return;\n  }else return;\n  if(id===currentChat)paintLive();else markBusy();\n}\n/* La stanza esce uno alla volta: chi sta scrivendo, poi il suo messaggio.\n   Aspettare in silenzio tre agenti per due giri sembrava un blocco. */\nfunction roomBar(on,label){\n  const bar=$("#roombar");if(!bar)return;\n  bar.classList.toggle("on",!!on);\n  if(on){$("#roomstate").textContent=label||"stanno discutendo";\n    $("#roomstop").disabled=false;$("#roomstop").textContent="Stop the discussion"}\n}\n$("#roomstop").onclick=()=>{\n  const b=$("#roomstop");b.disabled=true;b.textContent="stopping them\\u2026";\n  /* Non si tronca un agente a meta\' frase: smette di essere data la parola,\n     quindi l\'ultimo che sta parlando finisce la sua battuta. */\n  jsend("/api/chats/"+encodeURIComponent(currentChat)+"/stop","POST",{}).catch(()=>{});\n};\nfunction roomEnd(e){\n  const st=live[e.chat_id];if(st)st.turn=null;\n  if(vm.on&&e.chat_id===vm.chat)voceFine();\n  const perche={stop:"discussion stopped by you",\n                silence:"nobody had anything left to add",\n                cap:"round cap reached",\n                budget:"limite giornaliero raggiunto \\u2014 alzalo in Impostazioni, "+\n                       "«Agenti \\u2192 chiamate al giorno»",\n                vuota:"non c\'\\u00e8 nessun agente in questa stanza"}[e.reason]||"";\n  // Anche questa va nello stato: appesa al DOM sparirebbe al primo ridisegno,\n  // ed e\' proprio l\'informazione che dice se hanno finito o li ho tappati io.\n  if(perche)roomEnds[e.chat_id]=perche+\n    (e.rounds?" \\u00b7 "+e.rounds+(e.rounds===1?" giro":" giri"):"");\n  if(e.chat_id===currentChat)paint();\n}\nfunction roomTurn(e){\n  const st=liveOf(e.chat_id);\n  if(vm.on&&e.chat_id===vm.chat&&e.author_name)\n    vmStato(e.author_name+" is thinking\\u2026");\n  st.turn=e.author_id?{author_id:e.author_id,author_name:e.author_name,round:e.round}:null;\n  if(!e.author_id&&!st.text&&!st.steps.length&&!(st.peers||[]).length)delete live[e.chat_id];\n  if(e.chat_id===currentChat)paintLive();\n}\nfunction roomSaid(e){\n  const st=live[e.chat_id];if(st)st.turn=null;\n  /* Nella stanza a voce ognuno parla con la SUA voce, e il suo polpo si fa\n     avanti mentre gli altri si scostano. */\n  if(vm.on&&e.chat_id===vm.chat)parla(e.text||"",e.author_id);\n  if(e.chat_id!==currentChat)return;\n  /* Entra nell\'elenco vero: cosi\' non sparisce al primo repaint. */\n  messages.push({author_type:"agent",author_name:e.author_name,\n                 author_id:e.author_id,content:e.text,metadata:{round:e.round}});\n  paint();\n  markRead(e.chat_id);   /* l\'hai visto arrivare sotto gli occhi */\n}\nfunction showPeer(e){\n  vmPeerArriva(e);\n  /* Va nello stato della SUA chat, anche se stai guardando altrove: tornando\n     indietro devi ritrovare la scena dove l\'avevi lasciata. */\n  const st=liveOf(e.chat_id);\n  st.peers.push({key:e.from_id+"-"+e.to_id+"-"+st.peers.length,\n                 from_id:e.from_id,from_name:e.from_name,\n                 to_id:e.to_id,to_name:e.to_name,\n                 question:e.question||"",answer:null,phase:"waiting"});\n  if(e.chat_id===currentChat)paintLive();\n}\nfunction answerPeer(e){\n  vmPeerRisponde(e);\n  const st=live[e.chat_id];if(!st)return;\n  const p=[...(st.peers||[])].reverse()\n    .find(x=>x.from_id===e.from_id&&x.to_id===e.to_id&&x.answer==null);\n  if(!p)return;\n  p.answer=e.answer||"";\n  p.phase="andando parlando";\n  if(e.chat_id!==currentChat)return;   /* lo stato basta: si ridisegna al rientro */\n  paintLive();\n  // Detto quello che c\'era da dirsi, ognuno torna al suo posto.\n  setTimeout(()=>{\n    p.phase="tornando";\n    const z=$("#livezone"),box=z&&z.querySelector(\'[data-peer="\'+p.key+\'"]\');\n    if(box)box.className="peer tornando";\n  },700);\n}\nfunction markBusy(){\n  // Una conversazione che sta ricevendo qualcosa mentre guardi altrove.\n  document.querySelectorAll(".rrow[data-id]").forEach(r=>{\n    const a=roster.find(x=>x.id===r.dataset.id);\n    r.classList.toggle("busy",!!(a&&a.chat_id&&live[a.chat_id]));\n  });\n}\nconst GLIFI=["\\u2722","\\u2733","\\u2736","\\u273b","\\u273d","\\u273b","\\u2736","\\u2733"];\n// Le parole cambiano da sole: una sola frase ferma, su un\'attesa lunga,\n// sembra un\'immagine bloccata.\nconst PAROLE=["ci sta pensando","sta ragionando","mette insieme i pezzi",\n              "sta valutando","ci gira intorno","prende le misure",\n              "sta ricucendo","fa mente locale"];\nlet spinTimer=null,spinFrom=0;\nfunction tickSpinner(el){\n  const sp=el.querySelector(".sp"),secs=el.querySelector(".secs");\n  if(!sp){stopSpinner();return}\n  if(spinTimer)return;\n  spinFrom=Date.now();\n  let i=0;\n  spinTimer=setInterval(()=>{\n    const s=$("#inner").querySelector(".msg.live .sp");\n    if(!s){stopSpinner();return}\n    s.textContent=GLIFI[i++%GLIFI.length];\n    const t=Math.floor((Date.now()-spinFrom)/1000);\n    const b=$("#inner").querySelector(".msg.live .secs");\n    // I secondi compaiono solo quando l\'attesa comincia a farsi sentire.\n    if(b)b.textContent=t>=3?t+"s":"";\n    const l=$("#inner").querySelector(".msg.live .lbl");\n    const cur=live[currentChat];\n    if(l&&!(cur&&cur.label))l.textContent=PAROLE[Math.floor(t/4)%PAROLE.length];\n  },110);\n}\nfunction stopSpinner(){if(spinTimer){clearInterval(spinTimer);spinTimer=null}}\nfunction peerHTML(p){\n  return `<div class="pline">\n      <span class="who1">${blob(p.from_id,26)}</span>\n      <span class="arrow"><i></i><i></i><i></i></span>\n      <span class="who2">${blob(p.to_id,26)}</span>\n      <span class="pwho">${esc(p.from_name)} asks ${esc(p.to_name)}</span></div>\n    <div class="pq">${esc(p.question||"")}</div>\n    <div class="pa">${p.answer!=null?md(p.answer)\n      :\'<span class="typing"><i class="sp">\\u2733</i>\'+\n       \'<span class="lbl">\'+esc(p.to_name)+\' ci sta pensando</span></span>\'}</div>`;\n}\nfunction paintLive(){\n  const st=live[currentChat];\n  const av=$("#chatavatar").querySelector(".blob");\n  if(av)av.classList.toggle("pensa",\n    !!(st&&(st.typing||st.turn)&&!st.text));\n  let zone=$("#livezone");\n  if(!st){stopSpinner();if(zone)zone.remove();roomBar(false);return}\n  if(!zone){\n    zone=document.createElement("div");zone.id="livezone";\n    $("#inner").appendChild(zone);\n  }\n  /* Le facce che si parlano: ognuna col suo nodo, per non rifare l\'animazione\n     a ogni ridisegno. Chi c\'era gia\' viene solo aggiornato. */\n  (st.peers||[]).forEach(p=>{\n    let box=zone.querySelector(\'[data-peer="\'+p.key+\'"]\');\n    const nuovo=!box;\n    if(nuovo){\n      box=document.createElement("div");box.dataset.peer=p.key;\n      zone.appendChild(box);\n    }\n    box.className="peer "+(p.phase||"waiting");\n    box.innerHTML=peerHTML(p);\n    /* Solo la prima comparsa parte da lontano e si avvicina. Rientrando in\n       chat il riquadro deve gia\' essere al posto giusto, non ricominciare. */\n    if(nuovo&&p.phase==="waiting"){\n      requestAnimationFrame(()=>{p.phase="andando";box.className="peer waiting andando"});\n      setTimeout(()=>{if(p.answer==null){p.phase="andando parlando";\n        box.className="peer andando parlando"}},560);\n    }\n  });\n  zone.querySelectorAll("[data-peer]").forEach(b=>{\n    if(!(st.peers||[]).some(p=>p.key===b.dataset.peer))b.remove();\n  });\n  /* Di chi e\' il turno nella stanza. */\n  let turn=zone.querySelector(".roomturn");\n  if(st.turn){\n    if(!turn){turn=document.createElement("div");turn.className="msg them roomturn";\n      zone.appendChild(turn)}\n    turn.innerHTML=`<span class="who">${blob(st.turn.author_id,20)} ${esc(st.turn.author_name)}</span>\n      <span class="typing"><i class="sp">\\u2733</i><span class="lbl">sta scrivendo</span></span>`;\n    roomBar(true,"stanno discutendo \\u00b7 giro "+(st.turn.round||1));\n  }else{\n    if(turn)turn.remove();\n    roomBar(false);\n  }\n  /* La risposta in arrivo. */\n  let el=zone.querySelector(".msg.live");\n  const parla=st.text||st.typing||st.steps.length;\n  if(!parla){if(el)el.remove();stopSpinner();}\n  else{\n    if(!el){el=document.createElement("div");el.className="msg them live";\n      zone.appendChild(el)}\n    el.innerHTML=(st.steps.length?stepsBlock(st.steps,"live:"+currentChat):"")+\n      (st.text?md(st.text)+\'<span class="cursor"></span>\'\n              :\'<span class="typing"><i class="sp"></i>\'+\n                \'<span class="lbl">\'+esc(st.label||"is typing")+\'</span>\'+\n                \'<span class="secs"></span></span>\');\n    tickSpinner(el);\n  }\n  $("#thread").scrollTop=$("#thread").scrollHeight;\n}\n\n/* ── approvazioni ────────────────────────────────────────────────────────\n   L\'azione parte dal browser, quindi il permesso si chiede qui. Prima la\n   domanda usciva nel terminale e dal web non si vedeva nulla: la richiesta\n   restava appesa finche\' qualcuno non se ne accorgeva per caso.            */\nfunction askApproval(e){\n  if($("#appr-"+e.approval_id))return;\n  const box=document.createElement("div");\n  box.className="approval";box.id="appr-"+e.approval_id;\n  box.innerHTML=`<div class="ahead">${esc(e.actor||"un agente")} chiede il permesso</div>\n    <div class="abody">${esc(e.text||"")}</div>\n    <div class="afoot">\n      <button class="mbtn" data-c="no">No</button>\n      <button class="mbtn" data-c="always">Sempre</button>\n      <button class="mbtn primary" data-c="yes">Consenti</button></div>`;\n  $("#inner").appendChild(box);$("#thread").scrollTop=$("#thread").scrollHeight;\n  box.querySelectorAll("[data-c]").forEach(b=>b.onclick=async()=>{\n    box.querySelectorAll("button").forEach(x=>x.disabled=true);\n    try{await jsend("/api/approvals/"+e.approval_id,"POST",{choice:b.dataset.c})}\n    catch(err){setErr("permesso: "+err.message)}\n    closeApproval(e.approval_id,b.dataset.c);\n  });\n}\nfunction closeApproval(id,choice){\n  const box=$("#appr-"+id);if(!box)return;\n  if(choice){\n    const detto={yes:"consentito",always:"consentito sempre",no:"negato"}[choice]||choice;\n    box.className="approval done";box.innerHTML=`<div class="ahead">${esc(detto)}</div>`;\n    setTimeout(()=>{if(box.parentNode)box.remove()},2500);\n  }else box.remove();\n}\n\n/* ── avvio ──────────────────────────────────────────────────────────────── */\nasync function boot(){\n  $("#blankavatar").innerHTML=blob("openvurp",64);\n  // Prima cosa: disegnare quello che e\' gia\' arrivato con la pagina.\n  render();paintBlank();growInput();\n  let ricordata="";\n  try{ricordata=localStorage.getItem("ov.chat")||""}catch(_){}\n  if(ricordata==="room"&&room){await openChat("room")}\n  else if(ricordata&&roster.some(a=>a.id===ricordata)){await openChat(ricordata)}\n  else{\n    /* L\'agente ricordato non c\'e\' piu\' (cancellato da un\'altra scheda):\n       il ricordo stantio va buttato, non riprovato a ogni avvio. */\n    try{localStorage.removeItem("ov.chat")}catch(_){}\n    showView("blank");\n  }\n  if(!providers.length){try{providers=(await jget("/api/providers")).providers||[]}catch(e){}}\n  // Poi si aggiorna, ma nessuno ha visto una pagina vuota nel frattempo.\n  try{await loadRoster(true)}catch(e){setErr("roster: "+e.message)}\n  const ok=await jget("/api/chat").then(d=>d.available).catch(()=>false);\n  if(!ok){cin.disabled=true;cin.placeholder="chat unavailable — start the dashboard from the host with the agent"}\n  setInterval(()=>{if(!document.querySelector(".pop")&&!$("#ovmask"))loadRoster().catch(()=>{})},15000);\n}\nboot();\n\n</script>\n</body>\n</html>'
        # La rubrica viaggia CON la pagina. Senza, il browser disegna prima il
        # vuoto e poi, a rete finita, gli agenti: aprendo openvurp si legge per
        # un attimo «non hai ancora nessun agente», che e' proprio la frase che
        # non deve comparire a chi ce li ha.
        body = self._with_boot(html).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # La pagina e' generata dal runtime a ogni richiesta: se il browser la
        # tiene in cache, dopo un aggiornamento continui a vedere quella vecchia
        # e sembra che il codice non sia cambiato.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        cls = type(self)
        if cls.token:
            # cookie HttpOnly+SameSite: le richieste successive (e l'SSE) lo portano
            self.send_header("Set-Cookie",
                             f"ovtok={cls.token}; Path=/; HttpOnly; SameSite=Strict")
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
        self.handler_class = type("BoundDashboardHandler", (DashboardHandler,), {})
        self.handler_class.workspace_dir = workspace_dir
        self.handler_class.chat_fn = chat_fn
        self.handler_class.chat_store = (
            getattr(chat_fn, "chat_store", None)
            if chat_fn is not None else ChatStore(os.path.join(workspace_dir, "memory"))
        )
        self.handler_class.token = self.token
        self.handler_class._chat_hits = deque()
        self.handler_class._rl_lock = threading.Lock()
        self._server = None

    def bind(self):
        """Occupa la porta ORA, non dentro il thread.

        Se il bind avviene in background, un "indirizzo gia' in uso" esplode
        dove nessuno lo guarda: l'avvio dice "dashboard attiva", il browser
        parla con l'istanza VECCHIA rimasta sulla porta, e sembra che il codice
        nuovo non sia mai stato caricato.
        """
        if self._server is None:
            self._server = ThreadingHTTPServer((self.host, self.port), self.handler_class)
            self._scalda()
        return self._server

    def _scalda(self):
        """Apre il database PRIMA che arrivi il browser.

        Misurato, non supposto: la prima pagina ci metteva 2,9 secondi, e il
        colpevole non era il database (63 ms per aprirlo, 14 per leggerlo) ma
        `provider_catalog()`, che sonda i backend e alla prima chiamata costa
        3,2 secondi. Quello e' uscito dalla pagina.

        Qui si scaldano entrambi mentre l'utente sta ancora aprendo il browser,
        cosi' quando arriva la richiesta e' tutto pronto. E finche' non lo e',
        la pagina parte comunque: gli agenti arrivano un attimo dopo, come
        prima, invece di far aspettare davanti al bianco.
        """
        store = self.handler_class.chat_store
        if store is None:
            return
        pronto = threading.Event()
        self.handler_class.boot_ready = pronto

        def apri():
            try:
                store.agent_roster()
                store.team_room(create=False)
            except Exception:
                pass   # se fallisce, la pagina se la cava da sola
            finally:
                # Appena la rubrica e' leggibile la pagina puo' portarsela
                # dietro: ~80 ms. Aspettare anche la sonda dei backend
                # significherebbe tenerla fuori per tre secondi.
                pronto.set()
            try:
                provider_catalog()   # 3,2 s alla prima: mai dentro una richiesta
            except Exception:
                pass

        threading.Thread(target=apri, daemon=True, name="dashboard-warmup").start()

    def start(self):
        self.bind().serve_forever()

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
