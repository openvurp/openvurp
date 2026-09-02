"""Test per la chat della dashboard: capture UI, chat_fn e giro HTTP reale."""

import json
import threading
import tempfile
import urllib.request

import dashboard
from core.chat_store import ChatStore


class FakeUI:
    def __init__(self):
        self.calls = []

    def status(self, msg):
        self.calls.append(("status", msg))

    def flash(self, msg):  # metodo NON sovrascritto da CaptureUI → deve delegare
        self.calls.append(("flash", msg))


def test_capture_ui_captures_only_response_text():
    real = FakeUI()
    cap = dashboard.CaptureUI(real)
    cap.start_response()
    cap.stream_text("ciao ")
    cap.stream_text("mondo")
    cap.end_response()
    assert cap.response_text == "ciao mondo"
    # fuori da start/end non cattura
    cap.stream_text("ignorato")
    assert cap.response_text == "ciao mondo"


def test_capture_ui_captures_stream_token_and_openvurp_say():
    # Lo streaming live dell'agente usa stream_token (non stream_text).
    cap = dashboard.CaptureUI(FakeUI())
    cap.start_response()
    cap.stream_token("hi ")
    cap.stream_token("there")
    cap.end_response()
    assert cap.response_text == "hi there"
    # risposta non-streamata via openvurp_say
    cap2 = dashboard.CaptureUI(FakeUI())
    cap2.openvurp_say("direct reply")
    assert cap2.response_text == "direct reply"


def test_capture_ui_delegates_unknown_methods():
    real = FakeUI()
    cap = dashboard.CaptureUI(real)
    cap.flash("ciao")  # metodo non sovrascritto → delega alla UI reale
    assert ("flash", "ciao") in real.calls


def test_capture_ui_records_steps():
    cap = dashboard.CaptureUI(FakeUI())
    cap.show_cmd("pytest -q")
    cap.show_tool("read_file", {})
    cap.status("thinking")
    kinds = [(s["kind"], s["text"]) for s in cap.steps]
    assert ("shell", "pytest -q") in kinds
    assert ("tool", "read_file") in kinds
    assert ("status", "thinking") in kinds


def test_multichat_stream_does_not_replay_persisted_history():
    snapshot = [
        {"seq": 1, "chat_id": "chat_a", "kind": "user"},
        {"seq": 2, "chat_id": "chat_b", "kind": "user"},
    ]
    assert dashboard.filter_stream_snapshot(snapshot, "chat_a", replay=False) == []
    assert dashboard.filter_stream_snapshot(snapshot, "chat_a", replay=True) == [snapshot[0]]


def test_make_chat_fn_runs_agent_and_restores_ui():
    class FakeAgent:
        def __init__(self):
            self.ui = FakeUI()
            self.session = type("S", (), {"save": lambda self: None})()
            self.run_kwargs = {}

        def run(self, message, **kwargs):
            self.run_kwargs = kwargs
            # simula come l'agente emette la risposta sulla UI
            self.ui.start_response()
            self.ui.stream_text(f"reply to {message}")
            self.ui.end_response()

    agent = FakeAgent()
    original_ui = agent.ui
    lock = threading.Lock()
    chat_fn = dashboard.make_chat_fn(agent, lock, original_ui)

    result = chat_fn("ping")
    assert result["reply"] == "reply to ping"
    assert isinstance(result.get("steps"), list)
    # la UI reale è stata ripristinata
    assert agent.ui is original_ui
    # il lock è di nuovo libero
    assert lock.acquire(blocking=False)
    lock.release()


def test_chat_fn_passes_persisted_provider_to_agent():
    class FakeAgent:
        def __init__(self):
            self.ui = FakeUI()
            self.session = type("S", (), {"save": lambda self: None})()
            self.kwargs = {}

        def run(self, _message, **kwargs):
            self.kwargs = kwargs
            self.ui.openvurp_say("via Codex")

    with tempfile.TemporaryDirectory() as tmp:
        store = ChatStore(tmp)
        chat = store.create_chat(backend="codex", model="gpt-5.6-luna")
        agent = FakeAgent()
        chat_fn = dashboard.make_chat_fn(agent, threading.Lock(), agent.ui, store)
        result = chat_fn("prova", chat_id=chat["id"])
    assert result["reply"] == "via Codex"
    assert agent.kwargs["llm_backend"] == "codex"
    assert agent.kwargs["llm_model"] == "gpt-5.6-luna"


def test_chat_fn_empty_reply_has_placeholder():
    class SilentAgent:
        def __init__(self):
            self.ui = FakeUI()
            self.session = type("S", (), {"save": lambda self: None})()

        def run(self, message, **kwargs):
            self.ui.start_response()
            self.ui.end_response()  # nessun testo

    chat_fn = dashboard.make_chat_fn(SilentAgent(), threading.Lock(), FakeUI())
    assert chat_fn("x")["reply"] == "(no reply)"


def test_dashboard_auth_token():
    import urllib.error
    server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    dashboard.DashboardHandler.chat_fn = lambda m: {"reply": "ok", "steps": []}
    dashboard.DashboardHandler.token = "SECRET"
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def status(path, cookie=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        if cookie:
            req.add_header("Cookie", f"ovtok={cookie}")
        try:
            # La pagina porta con se' la rubrica, quindi la prima richiesta puo'
            # dover aprire il database: su /mnt/c sono secondi, non millisecondi.
            # Cinque secondi bastavano finche' la pagina era statica.
            return urllib.request.urlopen(req, timeout=30).status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        assert status("/api/status") == 401              # senza token → bloccato
        assert status("/api/status", cookie="SECRET") == 200   # token giusto
        assert status("/api/status", cookie="NOPE") == 401     # token sbagliato
        assert status("/?token=SECRET") == 200                 # token in query
    finally:
        server.shutdown()
        dashboard.DashboardHandler.token = ""


def test_http_chat_roundtrip():
    server = dashboard.ThreadingHTTPServer(
        ("127.0.0.1", 0), dashboard.DashboardHandler
    )
    dashboard.DashboardHandler.chat_fn = lambda msg: f"echo:{msg}"
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/chat",
            data=json.dumps({"message": "hola"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        assert data["reply"] == "echo:hola"

        # GET /api/chat segnala disponibilità
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/chat", timeout=5) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        assert info["available"] is True
    finally:
        server.shutdown()
        dashboard.DashboardHandler.chat_fn = None


def test_http_multichat_crud_and_team_mode():
    with tempfile.TemporaryDirectory() as tmp:
        dashboard.DashboardHandler.chat_store = ChatStore(tmp)
        dashboard.DashboardHandler.chat_fn = lambda msg, chat_id="": {
            "reply": f"{chat_id}:{msg}", "chat_id": chat_id,
        }
        server = dashboard.ThreadingHTTPServer(
            ("127.0.0.1", 0), dashboard.DashboardHandler
        )
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path, method="GET", body=None):
            data = json.dumps(body).encode("utf-8") if body is not None else None
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", data=data, method=method,
                headers={"Content-Type": "application/json"} if data else {},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            chat = request("/api/chats", "POST", {"mode": "solo"})
            chat_id = chat["id"]
            listed = request("/api/chats")["chats"]
            assert chat_id in {item["id"] for item in listed}

            updated = request(
                f"/api/chats/{chat_id}", "PATCH",
                {"mode": "team", "backend": "auto", "model": ""},
            )
            assert updated["mode"] == "team"
            assert updated["backend"] == "auto"
            # Rubrica vuota all'inizio: la stanza resta senza nessuno finche'
            # non crei tu un agente.
            assert request(f"/api/chats/{chat_id}/agents")["agents"] == []

            request("/api/agents", "POST",
                    {"name": "meteo", "role": "sa tutto del tempo"})
            agents = request("/api/agents")["agents"]
            configured = request(
                f"/api/agents/{agents[0]['id']}", "PATCH",
                {"backend": "codex", "model": "gpt-5.6-luna"},
            )
            assert configured["backend"] == "codex"
            assert configured["model"] == "gpt-5.6-luna"

            result = request(
                "/api/chat", "POST", {"chat_id": chat_id, "message": "ciao"},
            )
            assert result["reply"] == f"{chat_id}:ciao"
        finally:
            server.shutdown()
            dashboard.DashboardHandler.chat_store = None
            dashboard.DashboardHandler.chat_fn = None


def test_direct_chat_talks_to_that_agent_not_the_main_one(tmp_path, monkeypatch):
    """Aprire la chat di un agente dalla rubrica deve dare la voce di QUEL agente.

    Senza questo instradamento il filo si apriva sul nome giusto ma rispondeva
    l'agente principale: una rubrica che mente su chi ti sta parlando.
    """
    import threading

    from core.chat_store import ChatStore
    from core.swarm import Swarm
    import dashboard as D

    class _Stub:
        def __init__(self, **_kwargs):
            self.max_tokens = 0
            self.temperature = 0.0

        def call(self, messages, **_kwargs):
            return "sono " + messages[0]["content"].split("'")[1]

    monkeypatch.setattr("core.llm.create_llm_client", lambda **kw: _Stub(**kw))

    store = ChatStore(str(tmp_path))
    agent_row = store.create_agent("deep work", "analisi lunghe", "", "", "")

    class _UI:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    class _Agent:
        session_store = None
        ui = _UI()

    parent = _Agent()
    parent.swarm = Swarm(parent, store=store)

    chat_fn = D.make_chat_fn(parent, threading.Lock(), _UI(), chat_store=store)
    chat = store.direct_chat_for_agent(agent_row["id"])
    result = chat_fn("che ne pensi?", chat_id=chat["id"])

    assert "deep work" in result["reply"]
    assert result["author_name"] == "deep work"

    messages = store.list_messages(chat["id"])
    # La domanda va scritta una volta sola: la scrive chat_fn, non anche lo sciame.
    assert [m["author_type"] for m in messages] == ["user", "agent"]
    assert messages[1]["author_id"] == agent_row["id"]


# ── Allegati ────────────────────────────────────────────────────────────

def _upload_server(tmp_path):
    """Porta effimera: una porta fissa collide con qualunque cosa giri gia'."""
    import os
    import threading
    import dashboard as D

    os.makedirs(os.path.join(str(tmp_path), "memory"), exist_ok=True)
    srv = D.DashboardServer(port=0, workspace_dir=str(tmp_path), token="t")
    srv.bind()
    threading.Thread(target=srv.start, daemon=True).start()
    return srv._server.server_address[1]


def _post(port, path, body):
    import json as _j
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method="POST",
        data=_j.dumps(body).encode(),
        headers={"Cookie": "ovtok=t", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, _j.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, _j.loads(exc.read().decode())


def test_upload_saves_the_file_and_refuses_what_the_agent_cannot_read(tmp_path):
    import base64
    import os

    port = _upload_server(tmp_path)
    blob = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 200).decode()

    status, ok = _post(port, "/api/upload", {"name": "schermata.png", "data": blob})
    assert status == 201
    assert os.path.isfile(ok["path"])
    assert ok["name"] == "schermata.png"

    # Un allegato che nessun tool sa aprire e' solo un file sconosciuto su disco.
    status, _ = _post(port, "/api/upload", {"name": "payload.exe", "data": blob})
    assert status == 415
    status, _ = _post(port, "/api/upload", {"name": "vuoto.png", "data": ""})
    assert status == 400


def test_upload_cannot_escape_the_uploads_directory(tmp_path):
    """Un allegato non deve poter scegliere dove atterrare."""
    import base64
    import os

    port = _upload_server(tmp_path)
    blob = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 50).decode()
    status, ok = _post(port, "/api/upload",
                       {"name": "../../../../etc/passwd.png", "data": blob})

    assert status == 201
    uploads = os.path.realpath(os.path.join(str(tmp_path), "memory", "uploads"))
    assert os.path.realpath(ok["path"]).startswith(uploads + os.sep)
    assert ".." not in ok["name"] and "/" not in ok["name"]


def test_attachments_reach_the_agent_as_paths_it_can_open(tmp_path, monkeypatch):
    """Il percorso deve arrivare nel messaggio: l'agente ha gia' i tool per aprirlo."""
    import threading

    from core.chat_store import ChatStore
    import dashboard as D

    seen = {}

    class _UI:
        def __getattr__(self, _n):
            return lambda *a, **k: None

    class _Agent:
        session_store = None
        ui = _UI()

        def run(self, message, **kwargs):
            seen["message"] = message

    store = ChatStore(str(tmp_path))
    parent = _Agent()
    parent.swarm = None
    chat_fn = D.make_chat_fn(parent, threading.Lock(), _UI(), chat_store=store)
    chat = store.create_chat()

    chat_fn("che roba è?", chat_id=chat["id"],
            attachments=["/tmp/uploads/schermata.png"])

    assert "/tmp/uploads/schermata.png" in seen.get("message", "")
    assert "ALLEGATI" in seen["message"]
    assert "image_analyze" in seen["message"]
