"""On the phone, silence looks like a bot that died.

Found by reading, all at once: the room's own notices and errors never
reached the channels; the Telegram keyboard called a method that did not
exist and swallowed the error; Discord cut long replies mid-sentence; the
WhatsApp bridge could die without a word.
"""

import inspect

from core.conversation import ChannelConversation, Reply


def _replies(out):
    return [(r.author, r.text) for r in ChannelConversation.replies_from(out, "c")]


def test_the_rooms_notices_and_errors_reach_the_phone():
    out = {"chat_id": "c", "team_messages": [
        {"author_name": "amanda", "content": "my point"}],
        "team_notes": ["Fuori da questa discussione: dev"],
        "team_errors": ["ciccio: timeout"]}
    assert _replies(out) == [
        ("", "[Fuori da questa discussione: dev]"),
        ("", "[ciccio: timeout]"),
        ("amanda", "my point"),
    ]


def test_a_spent_budget_is_said_not_swallowed():
    out = {"chat_id": "c", "team_messages": [], "reply": "",
           "team_errors": ["Daily limit reached: 120 contributions out of 120."]}
    assert _replies(out) == [("", "[Daily limit reached: 120 contributions out of 120.]")]


def test_a_silent_room_says_so_but_a_silent_agent_stays_silent():
    assert _replies({"chat_id": "c", "team_messages": [], "reply": ""}) == \
        [("", "[nobody had anything to say]")]
    assert _replies({"chat_id": "c", "reply": "(no reply)"}) == []


class _Conv:
    def names(self):
        return ["amanda", "ciccio", "dev"]

    def handle(self, incoming):
        return [Reply("ok", author="amanda")]


def _telegram(calls):
    from channels.telegram import TelegramChannel

    ch = TelegramChannel("t", conversation=_Conv())
    ch._call = lambda method, **params: calls.append((method, params)) or {}
    return ch


def test_the_telegram_keyboard_actually_exists():
    calls = []
    ch = _telegram(calls)
    kb = ch.keyboard()
    assert kb and kb["keyboard"][0] == [{"text": "@amanda"}, {"text": "@ciccio"}]

    ch.send("hello", chat_id="1", keyboard=kb)
    assert calls[-1][1]["reply_markup"] == kb, "the keyboard is not sent with the message"


def test_telegram_shows_a_sign_of_life_while_the_agent_works():
    calls = []
    ch = _telegram(calls)
    ch._handle({"message": {"text": "ciao", "from": {"id": "7", "first_name": "Enzo"},
                            "chat": {"id": "7"}}})
    methods = [m for m, _ in calls]
    assert "sendChatAction" in methods and "sendMessage" in methods
    assert [p for m, p in calls if m == "sendChatAction"][0]["action"] == "typing"


def test_telegram_reports_a_broken_turn_instead_of_dying_quietly():
    calls = []
    ch = _telegram(calls)

    class _Broken(_Conv):
        def handle(self, incoming):
            raise RuntimeError("store locked")

    ch.conversation = _Broken()
    ch._handle({"message": {"text": "ciao", "from": {"id": "7"}, "chat": {"id": "7"}}})
    sent = [p["text"] for m, p in calls if m == "sendMessage"]
    assert sent and "store locked" in sent[0]


def test_discord_splits_long_replies_instead_of_cutting_them():
    import channels.discord as discord_channel

    source = inspect.getsource(discord_channel)
    assert "split(part, MESSAGE_LIMIT)" in source
    assert "part[:MESSAGE_LIMIT]" not in source


def test_a_dead_whatsapp_bridge_says_so():
    from channels.whatsapp import WhatsAppChannel

    assert "bridge exited" in inspect.getsource(WhatsAppChannel.start)


def test_the_page_reads_the_whatsapp_error_that_exists():
    import dashboard

    source = inspect.getsource(dashboard)
    assert "ch.errore" not in source and "ch.error" in source


def test_the_swarm_budget_message_points_to_the_page_not_the_env_file():
    import core.swarm as swarm

    assert ".env" not in inspect.getsource(swarm.Swarm._charge)


# ── what the agent is doing, told to the phone while it happens ──────────

def test_the_phone_is_told_what_the_agent_is_doing(tmp_path, monkeypatch):
    """On the page you watch the commands run; on the phone it was dead air."""
    from core import activity
    from core.chat_store import ChatStore
    from core.conversation import ChannelConversation, Incoming

    monkeypatch.setattr("core.chat_store.DEFAULT_AGENTS", (), raising=False)
    store = ChatStore(str(tmp_path))
    amanda = store.create_agent("amanda", "offerte", "", "", "")
    chat = store.direct_chat_for_agent(amanda["id"])

    def chat_fn(text, chat_id="", attachments=None):
        activity.publish("step", chat_id=chat_id, step="shell", text="ls -la")
        activity.publish("peer", chat_id=chat_id, from_name="amanda",
                         to_name="meteo", question="domani piove?")
        activity.publish("step", chat_id="somebody-else", step="shell", text="rm x")
        activity.publish("token", chat_id=chat_id, text="Ecco")
        return {"chat_id": chat_id, "reply": "fatto", "author_name": "amanda"}

    seen = []
    conv = ChannelConversation(chat_fn, store)
    replies = conv.handle(Incoming(text="@amanda cerca un ssd", channel="telegram",
                                   peer_id="7"), on_progress=seen.append)

    assert [r.text for r in replies] == ["fatto"]
    assert seen[0] == "✓ amanda has it", seen
    last = seen[-1].splitlines()
    assert "$ ls -la" in last
    assert "→ amanda asks meteo: domani piove?" in last
    assert "✍ writing the answer…" in last
    assert not any("rm x" in line for line in last), "another chat's command leaked in"


def test_telegram_keeps_one_status_message_and_removes_it_at_the_end():
    calls = []

    class _Live(_Conv):
        def handle(self, incoming, on_progress=None):
            on_progress("✓ amanda has it")
            on_progress("✓ amanda has it\n$ ls")
            return [Reply("ok", author="amanda")]

    from channels.telegram import TelegramChannel

    ch = TelegramChannel("t", conversation=_Live())

    def fake(method, **params):
        calls.append((method, params))
        return {"result": {"message_id": 5}} if method == "sendMessage" else {}

    ch._call = fake
    ch._handle({"message": {"text": "cerca un ssd", "from": {"id": "7"},
                            "chat": {"id": "7"}}})
    flow = [(m, p.get("text", p.get("message_id"))) for m, p in calls
            if m != "sendChatAction"]
    assert flow == [
        ("sendMessage", "✓ amanda has it"),
        ("editMessageText", "✓ amanda has it\n$ ls"),
        ("deleteMessage", 5),
        ("sendMessage", "*amanda*\nok"),
    ], flow


# ── permissions, answered from the phone ─────────────────────────────────

def test_a_permission_question_reaches_the_phone_and_the_answer_comes_back(tmp_path, monkeypatch):
    """A tool that stops to ask used to wait on the page while you were on
    Telegram, and be denied after 180 s with no message either way."""
    import threading

    from core import approvals
    from core.chat_store import ChatStore
    from core.conversation import ChannelConversation, Incoming

    monkeypatch.setattr("core.chat_store.DEFAULT_AGENTS", (), raising=False)
    store = ChatStore(str(tmp_path))
    amanda = store.create_agent("amanda", "offerte", "", "", "")

    outcome = {}

    def chat_fn(text, chat_id="", attachments=None):
        # The tool asks, and waits for whoever answers first.
        outcome["choice"] = approvals.request("rm -rf build/", chat_id, actor="amanda")
        return {"chat_id": chat_id, "reply": "fatto"}

    asked = []

    def on_approval(evt):
        asked.append(evt)
        if evt["kind"] == "approval":
            # The phone answers a moment later, from another thread — as a
            # button press would.
            threading.Timer(0.1, ChannelConversation.answer_approval,
                            args=(evt["approval_id"], "yes")).start()

    conv = ChannelConversation(chat_fn, store)
    replies = conv.handle(Incoming(text="@amanda pulisci", channel="telegram", peer_id="7"),
                          on_approval=on_approval)

    assert [r.text for r in replies] == ["fatto"]
    assert outcome["choice"] == "yes"
    assert [e["kind"] for e in asked] == ["approval", "approval_done"]
    assert asked[0]["text"] == "rm -rf build/" and asked[0]["actor"] == "amanda"
    assert asked[1]["choice"] == "yes"


def test_telegram_asks_with_buttons_and_a_press_answers():
    calls = []

    class _Asking(_Conv):
        answered = []

        def handle(self, incoming, on_progress=None, on_approval=None):
            on_approval({"kind": "approval", "approval_id": "abc",
                         "text": "rm -rf build/", "actor": "amanda"})
            return [Reply("ok", author="amanda")]

        def answer_approval(self, approval_id, choice):
            type(self).answered.append((approval_id, choice))
            return True

    from channels.telegram import TelegramChannel

    ch = TelegramChannel("t", conversation=_Asking())

    def fake(method, **params):
        calls.append((method, params))
        return {"result": {"message_id": 9}} if method == "sendMessage" else {}

    ch._call = fake
    ch._handle({"message": {"text": "pulisci", "from": {"id": "7"}, "chat": {"id": "7"}}})
    question = [p for m, p in calls if m == "sendMessage" and "reply_markup" in p][0]
    assert "amanda chiede il permesso" in question["text"] and "rm -rf build/" in question["text"]
    buttons = question["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["appr:abc:yes", "appr:abc:always", "appr:abc:no"]

    # The press arrives as its own update, while the turn may still be running.
    ch._handle({"callback_query": {"id": "q1", "from": {"id": "7"},
                                   "data": "appr:abc:always",
                                   "message": {"message_id": 9, "chat": {"id": "7"}}}})
    assert _Asking.answered == [("abc", "always")]
    edited = [p for m, p in calls if m == "editMessageText" and p.get("message_id") == 9]
    assert edited and "consentito, sempre" in edited[-1]["text"]


def test_a_stranger_cannot_press_the_buttons():
    class _Asking(_Conv):
        answered = []

        def answer_approval(self, approval_id, choice):
            type(self).answered.append((approval_id, choice))
            return True

    from channels.telegram import TelegramChannel

    ch = TelegramChannel("t", conversation=_Asking(), allowed=["7"])
    ch._call = lambda method, **params: {}
    ch._handle({"callback_query": {"id": "q1", "from": {"id": "666"},
                                   "data": "appr:abc:yes",
                                   "message": {"message_id": 9, "chat": {"id": "666"}}}})
    assert _Asking.answered == []


def test_telegram_polling_does_not_wait_for_a_slow_turn():
    """Handled in line, a turn of minutes stopped the polling: the button
    press that grants a permission could never arrive before it timed out."""
    import threading
    import time

    from channels.telegram import TelegramChannel

    ch = TelegramChannel("t", conversation=_Conv())
    polls = {"n": 0}

    def fake(method, **params):
        if method == "getUpdates":
            polls["n"] += 1
            if polls["n"] == 1:
                return {"result": [{"update_id": 1, "message": {"text": "a"}},
                                   {"update_id": 2, "message": {"text": "b"}}]}
            ch.stop()
        return {}

    ch._call = fake
    seen = []

    def slow(update):
        seen.append(threading.current_thread().name)
        time.sleep(0.3)

    ch._handle = slow
    t0 = time.time()
    ch.start()
    assert len(seen) == 2
    assert time.time() - t0 < 0.5, "the two updates were handled one after the other"
    assert "MainThread" not in seen
