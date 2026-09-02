"""Telegram inbound — thin by choice.

The previous version was 1,064 lines containing streaming, typing indicators,
group memory, an LLM decider, whitelists, button confirmations and voice
replies: a second idea of a conversation, one that knew nothing about the
roster, the rooms or the approvals. Everything built for the web had to be
built again in there.

What lives here is only the channel's own job: talking to the Telegram API.
Who answers, how, and with how many voices is decided by
``ChannelConversation`` — the same path the web page takes.

No extra dependency: the Telegram API is plain HTTP, and ``requests`` is
already there.
"""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime

import requests

from channels import Channel, ChannelMessage
from core.conversation import ChannelConversation, Incoming

API = "https://api.telegram.org/bot{token}/{method}"
FILE = "https://api.telegram.org/file/bot{token}/{path}"
MESSAGE_LIMIT = 4000        # Telegram cuts at 4096: leave room
DOWNLOAD_LIMIT = 20_000_000  # beyond this the Telegram API will not serve them anyway

# What can arrive besides text, and which extension to save it under. Order
# matters: a photo sent as a document is both things, and the document wins
# because it is not compressed.
ATTACHMENTS = (
    ("document", None),
    ("voice", ".ogg"),
    ("audio", ".mp3"),
    ("video_note", ".mp4"),
    ("video", ".mp4"),
    ("photo", ".jpg"),
)


def split(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """A long message is split where a line ends, not mid-word."""
    text = str(text or "")
    if len(text) <= limit:
        return [text] if text.strip() else []
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:           # a single line longer than the limit
            if current:
                parts.append(current); current = ""
            parts.append(line[:limit]); line = line[limit:]
        if len(current) + len(line) > limit:
            parts.append(current); current = ""
        current += line
    if current.strip():
        parts.append(current)
    return [p for p in parts if p.strip()]


def attachments_dir() -> str:
    """Where files land: the same folder the web uploads use."""
    try:
        from agent import OPENVURP_DIR
        radice = OPENVURP_DIR
    except Exception:
        radice = os.getcwd()
    return os.path.join(str(radice), "memory", "uploads")


class TelegramChannel(Channel):
    """Long polling on getUpdates. It does not know what an agent is, by design."""

    def __init__(self, token: str, conversation: ChannelConversation | None = None,
                 allowed: list | None = None, on_error=None, **kwargs):
        super().__init__("telegram", kwargs)
        if not token:
            raise ValueError(
                "Telegram token missing: set TELEGRAM_TOKEN in .env."
            )
        self.token = token
        self.conversation = conversation
        # Who may talk to it. Empty = nobody: a bot open to anyone would have
        # your terminal in its hands, and that cannot be the default.
        self.allowed = {str(x).strip() for x in (allowed or []) if str(x).strip()}
        self.on_error = on_error
        self._stop = threading.Event()
        self._offset = 0
        self.stop_reason = ""

    # ── API ──────────────────────────────────────────────────────────────

    def _call(self, method: str, **params):
        reply = requests.post(API.format(token=self.token, method=method),
                                 json=params, timeout=65)
        reply.raise_for_status()
        return reply.json()

    # ── incoming files ───────────────────────────────────────────────────

    def _download(self, file_id: str, suffix: str, name: str = "") -> str:
        """Brings a Telegram file to disk. Returns the path, or ""."""
        try:
            info = self._call("getFile", file_id=file_id).get("result", {})
            remote = str(info.get("file_path", "") or "")
            if not remote or int(info.get("file_size", 0) or 0) > DOWNLOAD_LIMIT:
                return ""
            reply = requests.get(FILE.format(token=self.token, path=remote), timeout=60)
            reply.raise_for_status()
        except Exception as exc:
            if self.on_error:
                self.on_error(f"Telegram: cannot download the attachment ({exc})")
            return ""

        base = os.path.join(attachments_dir(), "")
        os.makedirs(base, exist_ok=True)
        clean = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name or remote))[:70]
        if suffix and not clean.lower().endswith(suffix):
            clean = os.path.splitext(clean)[0] + suffix
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(base, f"{stamp}-{clean or 'allegato'}")
        n = 1
        while os.path.exists(path):
            path = os.path.join(base, f"{stamp}-{n}-{clean}")
            n += 1
        with open(path, "wb") as handle:
            handle.write(reply.content)
        return path

    def attachments(self, message: dict) -> tuple[list[str], str]:
        """The message's files, and what to call them when there is no caption.

        The previous channel accepted audio, photos and documents; rewriting it
        thin, only text survived, and a voice note vanished without a word.
        Here the file lands on disk and the path goes to the agent, which
        already has the tools to open it.
        """
        for key, suffix in ATTACHMENTS:
            part = message.get(key)
            if not part:
                continue
            if key == "photo":
                part = max(part, key=lambda p: p.get("file_size", 0))  # the biggest one
            file_id = str(part.get("file_id", "") or "")
            if not file_id:
                continue
            path = self._download(file_id, suffix or "",
                                     str(part.get("file_name", "") or ""))
            if not path:
                return [], ""
            label = {"voice": "a voice note", "audio": "an audio file",
                     "photo": "a photo", "video": "a video",
                     "video_note": "a video message",
                     "document": "a document"}[key]
            return [path], label
        return [], ""

    def send_voice(self, text: str, chat_id: str) -> bool:
        """Answers by voice whoever spoke by voice.

        There is no switch for this: it mirrors the way you addressed it. If
        speech synthesis is missing or off, the text remains — it has already
        been sent, so nothing is lost.
        """
        # Emoji and markdown are for the eyes: the voice skips them.
        clean = re.sub(
            "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF"
            "\uFE0F\u200D\U0001F1E6-\U0001F1FF]", "", str(text or ""))
        clean = re.sub(r"[*_`#]+", "", clean)
        clean = " ".join(clean.split())[:600]
        if not clean:
            return False
        try:
            import config as cfg
            if not bool(getattr(cfg, "VOICE_ENABLED", False)):
                return False
            from voice import speak
            path = speak(clean, play=False)
        except Exception:
            return False
        try:
            from tools.notify import _send_telegram_voice
            return bool(_send_telegram_voice(self.token, str(chat_id), path))
        except Exception:
            return False

    def send(self, message: str, chat_id: str = "", keyboard=None, **kwargs):
        recipient = str(chat_id or kwargs.get("to") or "")
        if not recipient:
            return False
        parts = split(message)
        for index, part in enumerate(parts):
            extra = {}
            # The keyboard goes on the LAST part: attached to the first, the
            # following ones would replace it anyway.
            if keyboard is not None and index == len(parts) - 1:
                extra["reply_markup"] = tastiera
            try:
                self._call("sendMessage", chat_id=recipient, text=part, **extra)
            except Exception as exc:
                if self.on_error:
                    self.on_error(f"Telegram: invio fallito ({exc})")
                return False
        return True

    def keyboard(self):
        """Agent names as buttons: you tap, you don't have to remember.

        Tapping a name sends "@name", which in the shared conversation means
        "from now on I'm talking to them" — so you don't repeat it every line.
        """
        if self.conversation is None:
            return None
        try:
            nomi = self.conversation.nomi()
        except Exception:
            return None
        if not nomi:
            return None
        rows = [[{"text": f"@{n}"} for n in nomi[i:i + 2]]
                 for i in range(0, len(nomi), 2)]
        rows.append([{"text": "/agenti"}, {"text": "/tutti "}, {"text": "/io"}])
        return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}

    def _publish_commands(self):
        """The commands in Telegram's own menu: visible, not memorised."""
        try:
            self._call("setMyCommands", commands=[
                {"command": "agents", "description": "who is in the roster"},
                {"command": "all", "description": "ask everyone at once"},
                {"command": "stop", "description": "stop the discussion"},
                {"command": "me", "description": "go back to openvurp"},
                {"command": "help", "description": "what you can do"},
            ])
        except Exception:
            pass   # a convenience, it must not block startup

    # ── loop ─────────────────────────────────────────────────────────────

    def alive(self) -> bool:
        return not self._stop.is_set()

    def stop(self):
        self._stop.set()

    def start(self):
        self._stop.clear()
        self._publish_commands()
        while not self._stop.is_set():
            try:
                dati = self._call("getUpdates", offset=self._offset, timeout=50)
            except Exception as exc:
                if self.on_error:
                    self.on_error(f"Telegram: {exc}")
                self._stop.wait(5)
                continue
            for update in dati.get("result", []):
                self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                try:
                    self._handle(update)
                except Exception as exc:
                    if self.on_error:
                        self.on_error(f"Telegram: {exc}")

    def _handle(self, update: dict) -> None:
        message = update.get("message") or update.get("edited_message") or {}
        text = str(message.get("text") or message.get("caption") or "").strip()
        sender = message.get("from") or {}
        user_id = str(sender.get("id", ""))
        chat_id = str((message.get("chat") or {}).get("id", ""))

        if self.allowed and user_id not in self.allowed:
            # Silence, not a refusal: a stranger is not even told the bot
            # exists.
            return

        # The file is downloaded only AFTER the check: a stranger does not
        # get to use your bandwidth either.
        files, label = self.attachments(message)
        you_spoke = bool(message.get("voice") or message.get("video_note"))
        if not text and not files:
            return
        if not text:
            text = f"I sent you {label}."

        msg = ChannelMessage(text=text, sender=sender.get("first_name", ""),
                             username=sender.get("username", ""),
                             channel="telegram", raw=update, chat_id=chat_id)
        if self.conversation is None:
            self._dispatch(msg)
            return
        replies = self.conversation.handle(Incoming(
            text=text, channel="telegram", peer_id=user_id or chat_id,
            sender=sender.get("first_name") or sender.get("username") or "",
            attachments=files,
        ))
        # The keyboard refreshes every round: create an agent from the web and
        # the next message already has it among the buttons.
        keyboard = self.keyboard()
        for index, reply in enumerate(replies):
            out_text = reply.text
            if reply.author:
                out_text = f"*{reply.author}*\n{out_text}"
            self.send(out_text, chat_id=chat_id,
                      keyboard=keyboard if index == len(replies) - 1 else None)
            # You spoke: you get spoken back to. The text goes anyway, because
            # a voice note cannot be re-read nor searched.
            if you_spoke and reply.text:
                self.send_voice(reply.text, chat_id)
