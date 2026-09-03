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
