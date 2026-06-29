"""
openvurp Channel — Slack

Usa slack_bolt per Socket Mode o webhook.
"""

from __future__ import annotations

from channels import Channel, ChannelMessage
from core.personality import parse_response_directive, slack_reaction_name


class SlackChannel(Channel):
    """Canale Slack via slack_bolt."""

    def __init__(self, bot_token: str, app_token: str = "", **kwargs):
        super().__init__("slack", kwargs)
        self.bot_token = bot_token
        self.app_token = app_token

        if not bot_token:
            raise ValueError(
                "Token Slack mancante. Imposta SLACK_BOT_TOKEN in config.py o come variabile d'ambiente."
            )

    def start(self):
        try:
            from slack_bolt import App
            from slack_bolt.adapter.socket_mode import SocketModeHandler
        except ImportError:
            raise ImportError(
                "slack_bolt non installato. Installa con: pip install slack_bolt"
            )

        app = App(token=self.bot_token)

        @app.message("")
        def handle_message(message, say):
            text = message.get("text", "")
            if not text:
                return

            user = message.get("user", "")
            msg = ChannelMessage(
                text=text,
                sender=user,
                channel="slack",
                raw=message,
            )
            response = None
            if self._callback:
                response = self._callback(msg)
            directive = parse_response_directive(response)
            if directive.kind == "reaction":
                emoji_name = slack_reaction_name(directive.emoji)
                if emoji_name:
                    try:
                        app.client.reactions_add(
                            channel=message.get("channel"),
                            timestamp=message.get("ts"),
                            name=emoji_name,
                        )
                    except Exception:
                        pass
            elif directive.kind == "text":
                say(directive.text[:4000])

        self._running = True
        if self.app_token:
            handler = SocketModeHandler(app, self.app_token)
            handler.start()
        else:
            app.start(port=3000)

    def stop(self):
        self._running = False

    def send(self, message: str, channel: str = None, **kwargs):
        """Invia messaggio a un canale Slack."""
        if not channel:
            return
        try:
            from slack_sdk import WebClient
            client = WebClient(token=self.bot_token)
            client.chat_postMessage(channel=channel, text=message[:4000])
        except ImportError:
            pass
