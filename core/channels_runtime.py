"""Turning inbound channels on and off without restarting openvurp.

While channels were only read at startup, changing `CHANNELS_IN` meant closing
everything and starting again — and a checkbox that needs a restart to take
effect is a checkbox that lies.

Here there is the list of what is running and a single operation: `apply()`,
which compares what runs with what the configuration asks for, and starts or
stops the difference. Calling it twice in a row does nothing the second time.
"""

from __future__ import annotations

import threading


class Supervisor:
    def __init__(self):
        self._lock = threading.RLock()
        self._running: dict[str, object] = {}
        self._conversation = None
        self._ui = None

    # ── what it needs in order to build them ─────────────────────────────

    def bind(self, conversation, ui) -> None:
        with self._lock:
            self._conversation = conversation
            self._ui = ui

    @property
    def ready(self) -> bool:
        return self._conversation is not None

    def running(self) -> list[str]:
        with self._lock:
            return sorted(self._running)

    # ── the only operation ───────────────────────────────────────────────

    def apply(self) -> dict:
        """Line the running channels up with what the configuration asks for."""
        import config as cfg

        wanted = [str(x).strip().lower()
                  for x in (getattr(cfg, "CHANNELS_IN", []) or []) if str(x).strip()]
        with self._lock:
            if not self.ready:
                # No conversation bound: this is the case in tests and when the
                # dashboard runs on its own. Not an error, there is simply
                # nothing to switch on.
                return {"running": sorted(self._running), "started": [], "stopped": [],
                        "errors": ["channels are not available in this process"]
                                  if wanted else []}

            stopped = []
            for name in list(self._running):
                if name not in wanted:
                    self._stop(name)
                    stopped.append(name)

            started, errors = [], []
            for name in wanted:
                if name in self._running:
                    continue
                try:
                    channel = build(name, self._conversation, cfg, self._ui)
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
                    continue
                if channel is None:
                    errors.append(f"{name}: not started")
                    continue
                threading.Thread(target=channel.start, daemon=True,
                                 name=f"channel-{name}").start()
                self._running[name] = channel
                started.append(name)

            return {"running": sorted(self._running), "started": started,
                    "stopped": stopped, "errors": errors}

    def stop_all(self) -> None:
        with self._lock:
            for name in list(self._running):
                self._stop(name)

    def _stop(self, name: str) -> None:
        channel = self._running.pop(name, None)
        if channel is None:
            return
        try:
            channel.stop()
        except Exception:
            pass


SUPERVISOR = Supervisor()
# Kept under the old Italian names too: the dashboard and main.py import them.
SUPERVISORE = SUPERVISOR


def channel(name: str):
    """The running channel with that name, if any (for the status page)."""
    with SUPERVISOR._lock:
        return SUPERVISOR._running.get(str(name))


def build(name: str, conversation, cfg, ui=None):
    """A channel switched on with no allow-list is an open door."""
    def _error(text: str) -> None:
        if ui is not None:
            try:
                ui.error(text)
            except Exception:
                pass

    def _who(key: str) -> list[str]:
        return [str(x).strip() for x in (getattr(cfg, key, []) or []) if str(x).strip()]

    if name == "telegram":
        from channels.telegram import TelegramChannel
        allowed = _who("TELEGRAM_ALLOWED_USERS")
        if not allowed:
            _error("Telegram: no allowed users, channel not started "
                   "(anyone could command your agents).")
            raise ValueError("no allowed users")
        return TelegramChannel(token=getattr(cfg, "TELEGRAM_TOKEN", ""),
                               conversation=conversation, allowed=allowed,
                               on_error=_error)
    if name == "discord":
        from channels.discord import DiscordChannel
        allowed = _who("DISCORD_ALLOWED_USERS")
        if not allowed:
            raise ValueError("no allowed users")
        return DiscordChannel(token=getattr(cfg, "DISCORD_TOKEN", ""),
                              conversation=conversation, allowed=allowed,
                              on_error=_error)
    if name == "slack":
        from channels.slack import SlackChannel
        allowed = _who("SLACK_ALLOWED_USERS")
        if not allowed:
            raise ValueError("no allowed users")
        return SlackChannel(bot_token=getattr(cfg, "SLACK_BOT_TOKEN", ""),
                            app_token=getattr(cfg, "SLACK_APP_TOKEN", ""),
                            conversation=conversation, allowed=allowed,
                            on_error=_error)
    if name == "whatsapp":
        from channels.whatsapp import WhatsAppChannel
        allowed = _who("WHATSAPP_ALLOWED_USERS")
        if not allowed:
            raise ValueError("no allowed numbers")
        try:
            from agent import OPENVURP_DIR
            root = OPENVURP_DIR
        except Exception:
            import os
            root = os.getcwd()
        return WhatsAppChannel(conversation=conversation, allowed=allowed,
                               on_error=_error, workspace_dir=str(root))
    raise ValueError(f"unknown channel '{name}'")


# Old Italian names, still imported elsewhere.
costruisci = build
canale = channel
