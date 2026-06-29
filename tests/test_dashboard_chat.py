"""Test per la chat della dashboard: capture UI, chat_fn e giro HTTP reale."""

import json
import threading
import urllib.request

import dashboard


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


def test_make_chat_fn_runs_agent_and_restores_ui():
    class FakeAgent:
        def __init__(self):
            self.ui = FakeUI()
            self.session = type("S", (), {"save": lambda self: None})()

        def run(self, message, **kwargs):
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
            return urllib.request.urlopen(req, timeout=5).status
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
