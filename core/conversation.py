"""The one place every inbound channel has to go through.

The old Telegram bot was 1,064 lines that talked to openvurp as if the roster,
the rooms, the streaming and the approvals did not exist: it contained zero
references to `chat_store`, `swarm` and `multiplayer`. It was not another door
into the same house, it was a door into the previous version. And that is the
fate of every adapter that reinvents its own idea of a conversation: it is born
behind and stays behind, because everything new has to be built twice.

Channels decide nothing here. They translate an incoming message into a call to
``chat_fn`` — the *same* function the web page uses — and send back whatever
comes out. The only thing that lives here is the command grammar, because a
chat has no sidebar to click: something has to mean "talk to amanda" or
"stop them".

The intended consequence: when the room learns to close itself, or the agents
learn to consult each other, the channels already know. There is nothing to
port.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


HELP = """What you can do:

  tap a name         from then on you write to them, no need to repeat it
  @name message      a single question to that agent
  /agents            who is in the roster
  /all message       ask everyone at once, in the room
  /stop              stop the discussion in progress
  /me                leave this conversation and choose another
  /help              this message"""


@dataclass
class Reply:
    """One reply to send back over the channel."""

    text: str
    author: str = ""
    chat_id: str = ""


@dataclass
class Incoming:
    """A message that arrived from a channel."""

    text: str
    channel: str                 # "telegram", "discord", "slack", "whatsapp"
    peer_id: str                 # who/where, in the channel's own terms
    sender: str = ""
    attachments: list = field(default_factory=list)

    @property
    def actor_id(self) -> str:
        return f"{self.channel}:{self.peer_id}"


class ChannelConversation:
    """Turns a channel's messages into openvurp turns, and back."""

    def __init__(self, chat_fn, store, swarm=None):
        self.chat_fn = chat_fn
        self.store = store
        self.swarm = swarm
        # Who you are talking to right now, per correspondent. A chat has no
        # sidebar: without this you would have to repeat "@amanda" on every
        # line, which becomes unbearable after two messages.
        self._open: dict[str, str] = {}

    # ── where a channel lands in the roster ──────────────────────────────

    # ── the grammar, the same on every channel ───────────────────────────

    def handle(self, msg: Incoming) -> list[Reply]:
        text = (msg.text or "").strip()
        if not text:
            return []

        low = text.lower()
        if low in {"/start", "/inizio"}:
            # On first contact what you need is WHO you can talk to, not which
            # commands exist: the roster first, the instructions after.
            return [Reply(f"{self.roster()}\n\n{HELP}")]
        if low in {"/help", "/aiuto", "help", "aiuto"}:
            return [Reply(HELP)]
        if low in {"/agents", "/agenti", "/who", "/chi", "/roster"}:
            return [Reply(self.roster())]
        if low in {"/stop", "/basta", "/ferma"}:
            return [Reply(self.stop_room())]
        if low in {"/me", "/io", "/openvurp", "/exit", "/esci"}:
            # There is nobody behind openvurp to go back to: it is the place
            # the agents live in, not one of them. Leaving a conversation
            # means choosing another one.
            self._open.pop(msg.actor_id, None)
            return [Reply("Closed. Pick who to write to:\n\n" + self.roster())]

        # "@amanda" on its own is not an empty message: it means "from now on
        # I'm talking to her". It is what happens when you tap a name on the
        # keyboard.
        picked = re.match(r"^@?([\w.\-]+)$", text)
        if picked:
            found = self._find(picked.group(1))
            if found is not None:
                self._open[msg.actor_id] = found["id"]
                return [Reply(
                    f"You're now talking to {found['name']}"
                    f"{' — ' + found['role'] if found.get('role') else ''}.\n"
                    f"Go ahead. /me brings you back to me.")]
            if text.startswith("@"):
                return [Reply(f"There is no agent called '{picked.group(1)}'.\n\n"
                              f"{self.roster()}")]

        direct = re.match(r"^@([\w.\-]+)\s+(.+)$", text, re.S)
        if direct:
            return self.to_agent(direct.group(1), direct.group(2).strip(), msg)

        room = re.match(r"^/(?:all|tutti|room|stanza)\s+(.+)$", text, re.S | re.I)
        if room:
            return self.to_room(room.group(1).strip(), msg)

        # If you picked someone, that is where what you write goes.
        open_with = self._open.get(msg.actor_id, "")
        if open_with:
            chat = self.store.direct_chat_for_agent(open_with)
            if chat is not None:
                return self._run(chat["id"], text, msg)
            self._open.pop(msg.actor_id, None)   # deleted in the meantime

        # Nothing addressed and nobody picked. There is no host to fall back
        # on — openvurp is where the agents are kept, not somebody who answers
        # in their place. So the answer is the question: to whom?
        rows = self.store.agent_roster()
        if len(rows) == 1:
            # With one agent, asking "to whom?" would be pedantry.
            self._open[msg.actor_id] = rows[0]["id"]
            chat = self.store.direct_chat_for_agent(rows[0]["id"])
            if chat is not None:
                return self._run(chat["id"], text, msg)
        return [Reply(self.roster())]

    # ── the actions ──────────────────────────────────────────────────────

    def roster(self) -> str:
        rows = self.store.agent_roster()
        if not rows:
            return ("There are no agents yet. Create one from the web page: "
                    "that is where the roster is built.")
        listing = "\n".join(
            f"  @{a['name']} — {a.get('role') or 'no role yet'}" for a in rows
        )
        return (f"In the roster ({len(rows)}):\n{listing}\n\n"
                f"Tap a name below to talk to them: from then on you write to "
                f"them without repeating the name.")

    def stop_room(self) -> str:
        room = self.store.team_room(create=False)
        if not room:
            return "There is no room open."
        from core.multiplayer import request_stop
        request_stop(room["id"])
        return ("Alright, stopping them. Whoever is speaking finishes their "
                "sentence, then it closes.")

    def _find(self, name: str) -> dict | None:
        wanted = str(name or "").strip().lower()
        return next((a for a in self.store.list_agents(enabled_only=True)
                     if str(a["name"]).strip().lower() == wanted), None)

    def names(self) -> list[str]:
        """For the channel keyboard: you tap, you don't type."""
        return [str(a["name"]) for a in self.store.list_agents(enabled_only=True)]

    def to_agent(self, name: str, text: str, msg: Incoming) -> list[Reply]:
        found = self._find(name)
        if found is None:
            return [Reply(f"There is no agent called '{name}'.\n\n{self.roster()}")]
        chat = self.store.direct_chat_for_agent(found["id"])
        if chat is None:
            return [Reply(f"I can't open the conversation with {name}.")]
        return self._run(chat["id"], text, msg)

    def to_room(self, text: str, msg: Incoming) -> list[Reply]:
        room = self.store.team_room(create=True)
        if room is None:
            return [Reply("I can't open the room.")]
        return self._run(room["id"], text, msg)

    # ── the actual turn, done by whoever does it for the web ─────────────

    def _run(self, chat_id: str, text: str, msg: Incoming) -> list[Reply]:
        try:
            out = self.chat_fn(text, chat_id=chat_id,
                               attachments=list(msg.attachments or []))
        except TypeError:
            # Older chat_fn, without attachments.
            out = self.chat_fn(text, chat_id=chat_id)
        except Exception as exc:
            return [Reply(f"[error: {exc}]", chat_id=chat_id)]
        return self.replies_from(out or {}, chat_id)

    @staticmethod
    def replies_from(out: dict, chat_id: str) -> list[Reply]:
        """A room answers with several voices: all of them go, not just the last."""
        chat_id = str(out.get("chat_id") or chat_id)
        # What the room said about itself — who stayed out, who could not
        # answer, the daily budget — used to reach the page and never the
        # phone: after the budget was spent, /all answered with nothing.
        aside = [
            Reply(f"[{str(t).strip()}]", chat_id=chat_id)
            for t in list(out.get("team_notes") or []) + list(out.get("team_errors") or [])
            if str(t).strip()
        ]
        room = out.get("team_messages") or []
        if room:
            return aside + [
                Reply(str(m.get("content", "")).strip(),
                      author=str(m.get("author_name", "")), chat_id=chat_id)
                for m in room if str(m.get("content", "")).strip()
            ]
        if aside:
            return aside
        text = str(out.get("reply", "") or "").strip()
        if not text or text == "(no reply)":
            # An agent's silence is not sent. A room's is: you asked everyone,
            # and on the phone nothing back looks like a bot that died.
            if "team_messages" in out:
                return [Reply("[nobody had anything to say]", chat_id=chat_id)]
            return []
        return [Reply(text, author=str(out.get("author_name", "")), chat_id=chat_id)]
