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
    assert methods[0] == "sendChatAction" and "sendMessage" in methods
    assert calls[0][1]["action"] == "typing"


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
