"""
openvurp Channels — Multi-canale

Supporto per Telegram, Discord, Slack, Signal.
Ogni canale è opzionale: import condizionale con messaggio di errore chiaro.
"""

from __future__ import annotations

import os
import time
import threading
from abc import ABC, abstractmethod
from typing import Callable, Optional
from dataclasses import dataclass

from core.personality import parse_response_directive


@dataclass
class ChannelMessage:
    """Messaggio ricevuto da un canale."""
    text: str
    sender: str = ""
    username: str = ""  # @username del mittente (per roster/tag nei gruppi)
    channel: str = ""
    raw: object = None  # Oggetto nativo del canale
    images: list[str] = None  # Lista di immagini in base64
    chat_id: str = ""  # ID chat per rispondere direttamente
    thread_id: str = ""  # Thread/topic id del canale, se presente
    chat_type: str = "private"  # private | group | supergroup | channel
    addressed: bool = True  # il bot è stato interpellato direttamente? (mention/reply)


class Channel(ABC):
    """Interfaccia base per tutti i canali."""

    def __init__(self, name: str, config: dict = None):
        self.name = name
        self.config = config or {}
        self._callback: Optional[Callable] = None
        self._running = False

    @abstractmethod
    def start(self):
        """Avvia polling/webhook."""
        ...

    @abstractmethod
    def stop(self):
        """Ferma il canale."""
        ...

    @abstractmethod
    def send(self, message: str, **kwargs):
        """Invia messaggio."""
        ...

    def on_message(self, callback: Callable[[ChannelMessage], str]):
        """Registra handler per messaggi in arrivo."""
        self._callback = callback

    def _dispatch(self, msg: ChannelMessage):
        """Processa un messaggio in arrivo."""
        if self._callback:
            try:
                directive = parse_response_directive(self._callback(msg))
                if directive.kind == "text":
                    self.send(directive.text)
            except Exception as e:
                self.send(f"[Errore openvurp] {e}")


class ChannelRouter:
    """Gestisce più canali contemporaneamente."""

    def __init__(self):
        self.channels: dict[str, Channel] = {}
        self._callback: Optional[Callable] = None

    def add(self, name: str, channel: Channel):
        """Aggiunge un canale."""
        channel.on_message(self._handle_message)
        self.channels[name] = channel

    def remove(self, name: str):
        """Rimuove un canale."""
        ch = self.channels.pop(name, None)
        if ch:
            ch.stop()

    def on_message(self, callback: Callable[[ChannelMessage], str]):
        """Registra handler globale."""
        self._callback = callback

    def _handle_message(self, msg: ChannelMessage) -> Optional[str]:
        """Dispatch messaggi al callback."""
        if self._callback:
            return self._callback(msg)
        return None

    def broadcast(self, message: str):
        """Invia a tutti i canali."""
        for ch in self.channels.values():
            try:
                ch.send(message)
            except Exception:
                pass

    def start_all(self):
        """Avvia tutti i canali (blocking su ultimo)."""
        threads = []
        channels = list(self.channels.values())

        if not channels:
            return

        # Tutti tranne l'ultimo in thread separati
        for ch in channels[:-1]:
            t = threading.Thread(target=ch.start, daemon=True)
            t.start()
            threads.append(t)

        # Ultimo in foreground (blocking)
        try:
            channels[-1].start()
        except KeyboardInterrupt:
            self.stop_all()

    def stop_all(self):
        """Ferma tutti i canali."""
        for ch in self.channels.values():
            try:
                ch.stop()
            except Exception:
                pass

    def add_from_config(self, channel_name: str, config):
        """Aggiunge un canale dalla configurazione."""
        name = channel_name.strip().lower()

        if name == "discord":
            from channels.discord import DiscordChannel
            token = getattr(config, 'DISCORD_TOKEN', os.environ.get('DISCORD_TOKEN', ''))
            ch = DiscordChannel(token=token)
            self.add(name, ch)

        elif name == "slack":
            from channels.slack import SlackChannel
            token = getattr(config, 'SLACK_BOT_TOKEN', os.environ.get('SLACK_BOT_TOKEN', ''))
            app_token = getattr(config, 'SLACK_APP_TOKEN', os.environ.get('SLACK_APP_TOKEN', ''))
            ch = SlackChannel(bot_token=token, app_token=app_token)
            self.add(name, ch)

        elif name == "signal":
            from channels.signal import SignalChannel
            number = getattr(config, 'SIGNAL_NUMBER', os.environ.get('SIGNAL_NUMBER', ''))
            ch = SignalChannel(number=number)
            self.add(name, ch)

        else:
            raise ValueError(f"Canale sconosciuto: {name}. Disponibili: telegram, discord, slack, signal")
