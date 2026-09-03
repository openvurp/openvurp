"""Discord inbound — same shape as Telegram, same core.

The channel does only the channel's job. Who answers and how is decided by
``ChannelConversation``, the same path the web page takes.
"""

from __future__ import annotations

import threading

from channels import Channel, ChannelMessage
from channels.telegram import split
from core.conversation import ChannelConversation, Incoming

MESSAGE_LIMIT = 1900        # Discord cuts at 2000


class DiscordChannel(Channel):
    def __init__(self, token: str, conversation: ChannelConversation | None = None,
                 allowed: list | None = None, on_error=None, **kwargs):
        super().__init__("discord", kwargs)
        if not token:
            raise ValueError(
                "Discord token missing: set DISCORD_TOKEN in .env."
            )
        self.token = token
        self.conversation = conversation
        self.allowed = {str(x).strip() for x in (allowed or []) if str(x).strip()}
        self.on_error = on_error
        self._client = None
        self._stop = threading.Event()
        self.stop_reason = ""

    def alive(self) -> bool:
        return not self._stop.is_set()

    def stop(self):
        self._stop.set()
        client = self._client
        if client is not None:
            try:
                import asyncio
                asyncio.run_coroutine_threadsafe(client.close(), client.loop)
            except Exception:
                pass

    def start(self):
        try:
            import discord
        except ImportError:
            raise ImportError(
                "discord.py non installato. Installa con: pip install 'openvurp[discord]'"
            )

        intents = discord.Intents.default()
        intents.message_content = True     # without this, messages arrive empty
        client = discord.Client(intents=intents)
        self._client = client
        self._stop.clear()

        @client.event
        async def on_message(message):     # noqa: D401 - firma imposta da discord.py
            if message.author == client.user:
                return
            text = str(message.content or "").strip()
            if not text:
                return
            author = str(message.author.id)
            if self.allowed and author not in self.allowed:
                return
            # discord.py runs on asyncio: an openvurp turn is blocking and
            # would freeze the whole client. It goes on its own thread.
            import asyncio
            replies = await asyncio.to_thread(self._replies, text, author,
                                              getattr(message.author, "display_name", ""))
            for part in replies:
                # Cut at 1900 characters mid-sentence, with no sign that
                # anything was missing. Telegram already splits: same here.
                for piece in split(part, MESSAGE_LIMIT):
                    await message.channel.send(piece)

        try:
            client.run(self.token, log_handler=None)
        except Exception as exc:
            self.stop_reason = str(exc)
            if self.on_error:
                self.on_error(f"Discord: {exc}")

    def _replies(self, text: str, author: str, name: str) -> list[str]:
        if self.conversation is None:
            return []
        out = []
        for reply in self.conversation.handle(Incoming(
            text=text, channel="discord", peer_id=author, sender=name,
        )):
            out.append(f"**{reply.author}**\n{reply.text}"
                       if reply.author else reply.text)
        return out

    def send(self, message: str, chat_id: str = "", **kwargs):
        """Discord replies in the message's own channel: unsolicited sending
        goes through notifications, not here."""
        return False
