"""WhatsApp inbound — through Baileys, with eyes open.

Meta's official API only works over webhooks: it needs a public address, which
you do not have at home. Baileys speaks the WhatsApp Web protocol — you pair it
by scanning a QR, like from the browser — and it runs from behind your router.

The price, stated plainly: it is UNOFFICIAL. Meta detects unofficial clients
and can ban the paired number, sometimes on the first try. That is why this
channel should be used with a SPARE NUMBER, never your personal one — the same
warning sits on the settings page, where you switch it on.

Like every other channel: transport only. The Node bridge (wa-bridge/) talks to
WhatsApp and passes JSON lines; who answers and how is decided by
``ChannelConversation``, the same path the web page takes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading

from core.conversation import ChannelConversation, Incoming

BRIDGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wa-bridge")


def _digits_only(text: str) -> str:
    return re.sub(r"\D", "", str(text or ""))


class WhatsAppChannel:
    name = "whatsapp"

    def __init__(self, conversation: ChannelConversation | None = None,
                 allowed: list | None = None, on_error=None,
                 workspace_dir: str = ""):
        self.conversation = conversation
        # Numbers with country code, compared digits-only:
        # empty = nobody, as with every other channel.
        self.allowed = {_digits_only(x) for x in (allowed or []) if _digits_only(x)}
        self.on_error = on_error
        self.workspace_dir = workspace_dir or os.getcwd()
        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._scrivi_lock = threading.Lock()
        self.stop_reason = ""
        # State for the page: the QR to scan, and who we are once inside.
        self.qr = ""
        self.connected = False
        self.me = ""
        self.error = ""

    # ── loop ─────────────────────────────────────────────────────────────

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        self._stop.set()
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def _warn(self, text: str) -> None:
        self.error = text
        if self.on_error:
            try:
                self.on_error(f"WhatsApp: {text}")
            except Exception:
                pass

    def start(self):
        node = shutil.which("node")
        if node is None:
            self._warn("Node.js is required (the bridge uses Baileys). Install Node and retry.")
            return
        if not os.path.isdir(os.path.join(BRIDGE_DIR, "node_modules")):
            npm = shutil.which("npm")
            if npm is None:
                self._warn("npm is required for the first start (it fetches Baileys).")
                return
            self._warn("first start: fetching Baileys, about a minute…")
            outcome = subprocess.run([npm, "install", "--no-audit", "--no-fund"],
                                   cwd=BRIDGE_DIR, capture_output=True, text=True,
                                   timeout=600)
            if outcome.returncode != 0:
                self._warn(f"npm install failed: {outcome.stderr[-300:]}")
                return
            self.error = ""

        auth = os.path.join(self.workspace_dir, "memory", "whatsapp", "auth")
        os.makedirs(auth, exist_ok=True)
        self._stop.clear()
        self.proc = subprocess.Popen(
            [node, os.path.join(BRIDGE_DIR, "bridge.mjs"), auth],
            cwd=BRIDGE_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        for riga in self.proc.stdout:
            if self._stop.is_set():
                break
            try:
                event = json.loads(riga)
            except Exception:
                continue
            try:
                self._event(event)
            except Exception as exc:
                self._warn(str(exc))
        if not self._stop.is_set():
            # The bridge died on its own: without this line nothing said so,
            # and every message from the phone from here on was lost quietly.
            self._warn("bridge exited: WhatsApp is off until openvurp restarts")

    # ── the bridge protocol ──────────────────────────────────────────────

    def _event(self, e: dict) -> None:
        kind = str(e.get("type", ""))
        if kind == "qr":
            self.qr = str(e.get("dataurl", ""))
            self.connected = False
        elif kind == "open":
            self.connected = True
            self.qr = ""
            self.me = _digits_only(str(e.get("me", "")).split("@")[0].split(":")[0])
            self.error = ""
        elif kind == "close":
            self.connected = False
        elif kind == "loggedout":
            # The session was revoked (from the phone, or by Meta): a new QR
            # is needed, and it must be said — this is not a network glitch.
            self.connected = False
            self.stop_reason = "session expired: a new QR is needed"
            self._warn(self.stop_reason)
        elif kind == "fatal":
            self._warn(str(e.get("error", ""))[:200])
        elif kind == "message":
            self._message(e)

    def _message(self, e: dict) -> None:
        jid = str(e.get("from", ""))
        number = _digits_only(jid.split("@")[0].split(":")[0])
        if self.allowed and number not in self.allowed:
            return   # silence: a stranger is not even told you exist
        text = str(e.get("text", "")).strip()
        if not text or self.conversation is None:
            return
        for reply in self.conversation.handle(Incoming(
            text=text, channel="whatsapp", peer_id=number,
            sender=str(e.get("name", "")),
        )):
            out = reply.text
            if reply.author:
                out = f"*{reply.author}*\n{out}"   # WhatsApp *bold*
            self.send(out, chat_id=jid)

    def send(self, message: str, chat_id: str = "", **_kwargs) -> bool:
        if not self.alive() or not chat_id:
            return False
        jid = str(chat_id)
        if "@" not in jid:
            jid = _digits_only(jid) + "@s.whatsapp.net"
        try:
            with self._scrivi_lock:
                self.proc.stdin.write(json.dumps(
                    {"type": "send", "to": jid, "text": str(message or "")}) + "\n")
                self.proc.stdin.flush()
            return True
        except Exception as exc:
            self._warn(f"send failed ({exc})")
            return False
