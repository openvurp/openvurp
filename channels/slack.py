"""Slack inbound — Socket Mode, the same core as the others.

Socket Mode rather than webhooks: no public address needed, the bot opens the
connection itself. It needs an app token (``xapp-``) besides the bot token
(``xoxb-``).
"""

from __future__ import annotations

import threading

from channels import Channel
from core.conversation import ChannelConversation, Incoming


class SlackChannel(Channel):
    def __init__(self, bot_token: str, app_token: str = "",
                 conversation: ChannelConversation | None = None,
                 allowed: list | None = None, on_error=None, **kwargs):
        super().__init__("slack", kwargs)
        if not bot_token or not app_token:
            raise ValueError(
                "Slack needs SLACK_BOT_TOKEN (xoxb-) and SLACK_APP_TOKEN (xapp-): "
                "the second one is for Socket Mode, which avoids exposing a public "
                "address."
            )
        self.bot_token = bot_token
        self.app_token = app_token
        self.conversation = conversation
        self.allowed = {str(x).strip() for x in (allowed or []) if str(x).strip()}
        self.on_error = on_error
        self._handler = None
        self._stop = threading.Event()
        self.stop_reason = ""

    def alive(self) -> bool:
        return not self._stop.is_set()

    def stop(self):
        self._stop.set()
        if self._handler is not None:
            try:
                self._handler.close()
            except Exception:
                pass

    def start(self):
        try:
            from slack_bolt import App
            from slack_bolt.adapter.socket_mode import SocketModeHandler
        except ImportError:
            raise ImportError(
                "slack-bolt non installato. Installa con: pip install 'openvurp[slack]'"
            )

        app = App(token=self.bot_token)
        self._stop.clear()

        @app.event("message")
        def _incoming(event, say):
            if event.get("bot_id") or event.get("subtype"):
                return          # the bot's own echoes and system messages
            text = str(event.get("text", "") or "").strip()
            if not text:
                return
            user = str(event.get("user", ""))
            if self.allowed and user not in self.allowed:
                return
            if self.conversation is None:
                return
            for reply in self.conversation.handle(Incoming(
                text=text, channel="slack", peer_id=user, sender=user,
            )):
                say(f"*{reply.author}*\n{reply.text}"
                    if reply.author else reply.text)

        try:
            self._handler = SocketModeHandler(app, self.app_token)
            self._handler.start()
        except Exception as exc:
            self.stop_reason = str(exc)
            if self.on_error:
                self.on_error(f"Slack: {exc}")

    def send(self, message: str, chat_id: str = "", **kwargs):
        return False
