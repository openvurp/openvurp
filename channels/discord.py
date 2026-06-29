"""
openvurp Channel — Discord

Usa discord.py per gateway websocket.
"""

from __future__ import annotations

from channels import Channel, ChannelMessage
from core.personality import parse_response_directive


class DiscordChannel(Channel):
    """Canale Discord via discord.py."""

    def __init__(self, token: str, **kwargs):
        super().__init__("discord", kwargs)
        self.token = token
        self._client = None

        if not token:
            raise ValueError(
                "Token Discord mancante. Imposta DISCORD_TOKEN in config.py o come variabile d'ambiente."
            )

    def start(self):
        try:
            import discord
        except ImportError:
            raise ImportError(
                "discord.py non installato. Installa con: pip install discord.py"
            )

        import discord

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        self._client = client

        @client.event
        async def on_ready():
            pass

        @client.event
        async def on_message(message):
            if message.author == client.user:
                return
            if not message.content:
                return

            # Rispondi solo se menzionato o in DM
            is_dm = isinstance(message.channel, discord.DMChannel)
            is_mentioned = client.user in message.mentions

            if not is_dm and not is_mentioned:
                return

            text = message.content
            # Rimuovi menzione dal testo
            if is_mentioned:
                text = text.replace(f'<@{client.user.id}>', '').strip()

            msg = ChannelMessage(
                text=text,
                sender=message.author.display_name,
                channel="discord",
                raw=message,
            )
            response = None
            if self._callback:
                response = self._callback(msg)

            directive = parse_response_directive(response)
            if directive.kind == "reaction":
                try:
                    await message.add_reaction(directive.emoji)
                except Exception:
                    pass
            elif directive.kind == "text":
                # Split per limite 2000 chars Discord
                for i in range(0, len(directive.text), 2000):
                    await message.reply(directive.text[i:i+2000])

        self._running = True
        client.run(self.token)

    def stop(self):
        self._running = False
        if self._client:
            import asyncio
            try:
                asyncio.get_event_loop().create_task(self._client.close())
            except Exception:
                pass

    def send(self, message: str, **kwargs):
        """Invio generico (non supportato senza contesto canale)."""
        pass
