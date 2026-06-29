"""
openvurp Channel — Signal

Usa signal-cli via subprocess per inviare/ricevere messaggi.
"""

from __future__ import annotations

import time
import json
import subprocess
from channels import Channel, ChannelMessage
from core.personality import parse_response_directive, prepare_outbound_response


class SignalChannel(Channel):
    """Canale Signal via signal-cli."""

    def __init__(self, number: str, **kwargs):
        super().__init__("signal", kwargs)
        self.number = number

        if not number:
            raise ValueError(
                "Numero Signal mancante. Imposta SIGNAL_NUMBER in config.py o come variabile d'ambiente."
            )

        # Verifica signal-cli
        try:
            subprocess.run(["signal-cli", "--version"], capture_output=True, timeout=5)
        except FileNotFoundError:
            raise ImportError(
                "signal-cli non installato. Installa da: https://github.com/AsamK/signal-cli"
            )

    def start(self):
        """Avvia ricezione messaggi via signal-cli receive."""
        self._running = True

        while self._running:
            try:
                result = subprocess.run(
                    ["signal-cli", "-a", self.number, "receive", "--json", "-t", "10"],
                    capture_output=True, text=True, timeout=15
                )

                for line in result.stdout.strip().split("\n"):
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        envelope = data.get("envelope", {})
                        data_msg = envelope.get("dataMessage", {})
                        text = data_msg.get("message", "")
                        sender = envelope.get("source", "")

                        if text:
                            msg = ChannelMessage(
                                text=text,
                                sender=sender,
                                channel="signal",
                                raw=data,
                            )
                            response = None
                            if self._callback:
                                response = self._callback(msg)
                            directive = parse_response_directive(response)
                            if directive.kind == "text":
                                self.send(directive.text, recipient=sender)

                    except json.JSONDecodeError:
                        continue

            except subprocess.TimeoutExpired:
                continue
            except Exception:
                time.sleep(5)

    def stop(self):
        self._running = False

    def send(self, message: str, recipient: str = None, group: str = None, **kwargs):
        """Invia messaggio via signal-cli."""
        message = prepare_outbound_response(message, source="signal")
        if (not recipient and not group) or not message:
            return

        cmd = ["signal-cli", "-a", self.number, "send", "-m", message[:4096]]
        if recipient:
            cmd.append(recipient)
        if group:
            cmd.extend(["-g", group])

        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
        except Exception:
            pass
