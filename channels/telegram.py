"""
openvurp Channel — Telegram

Usa python-telegram-bot per long polling.
Supporta testo, immagini, audio, documenti.
Streaming: edita il messaggio progressivamente.
Multi-messaggio: accoda messaggi e li processa in ordine.
Rate limiting integrato.
Conferma azioni via bottoni inline (senza conflitto getUpdates).
Menu comandi con setMyCommands.
Feedback live: typing indicator e messaggi di stato.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
import threading
import queue
from channels import Channel, ChannelMessage
from core.personality import parse_response_directive, prepare_outbound_response
from tools.media import IMAGE_TOOL, AUDIO_TRANSCRIBE_TOOL, PDF_TOOL


MEDIA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "memory", "media")
TELEGRAM_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "memory",
    "telegram_state.json",
)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".webm", ".mp4"}


def format_telegram_conflict_message() -> str:
    return (
        "Telegram disabled: another instance is already polling "
        "the same bot. Close the other process or start openvurp with --no-telegram."
    )


def should_respond_in_group(chat_type: str, text: str, bot_username: str,
                            is_reply_to_bot: bool) -> bool:
    """In gruppo il bot risponde SOLO se interpellato (così in chat affollate
    non risponde a tutti). Privata/canale: risponde sempre.

    Interpellato = menzione @bot nel testo, oppure risposta a un suo messaggio.
    """
    if chat_type not in ("group", "supergroup"):
        return True
    if is_reply_to_bot:
        return True
    if bot_username and f"@{bot_username}".lower() in (text or "").lower():
        return True
    return False


def strip_bot_mention(text: str, bot_username: str) -> str:
    """Toglie la menzione @bot dal testo prima di passarlo all'agente."""
    if not text or not bot_username:
        return text or ""
    pattern = re.compile(rf"@{re.escape(bot_username)}\b", re.IGNORECASE)
    return pattern.sub("", text).strip()


# Comandi registrati nel menu del bot
BOT_COMMANDS = [
    ("start", "Avvia openvurp"),
    ("help", "Cosa posso fare"),
    ("status", "Stato di openvurp"),
    ("anima", "Chi sono diventato"),
    ("growth", "Report di crescita"),
    ("diary", "Il mio diario"),
    ("patti", "Patti attivi"),
    ("specchio", "Correzioni non più ripetute"),
    ("progetti", "Progetti a lungo termine"),
    ("fucina", "Tool che mi sono costruito"),
    ("sensi", "Cosa osservo"),
    ("fili", "Legami / follow-up"),
    ("curiosita", "Domande aperte"),
    ("integrity", "Verifica integrità codice"),
    ("memory", "Cosa ricordo di te"),
    ("skills", "Le mie skill"),
    ("doctor", "Diagnosi runtime"),
    ("setup", "Bootstrap runtime"),
    ("restart", "Riavvia openvurp"),
]


class TelegramChannel(Channel):
    """Canale Telegram via long polling con streaming e coda messaggi."""

    def __init__(self, token: str, **kwargs):
        super().__init__("telegram", kwargs)
        self.token = token
        self._offset = 0
        self._running = False
        self._app = None
        self._error_callback = kwargs.get("on_error")

        # Coda messaggi per processing sequenziale
        self._msg_queue: queue.Queue = queue.Queue()
        self._worker_thread = None

        # Conferme inline: callback_id -> {"event": Event, "approved": bool, "display_msg": str}
        self._pending_confirms: dict[str, dict] = {}

        # Status message tracking per feedback live
        self._status_messages: dict[str, int] = {}  # chat_id -> message_id
        self._typing_threads: dict[str, threading.Event] = {}  # chat_id -> stop_event
        self._last_chat_id = self._load_last_chat_id()

        if not token:
            raise ValueError(
                "Token Telegram mancante. Imposta TELEGRAM_TOKEN in .env o nell'ambiente."
            )

        os.makedirs(MEDIA_DIR, exist_ok=True)

    def _load_last_chat_id(self) -> str:
        try:
            import json
            with open(TELEGRAM_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return str(data.get("last_chat_id", "")).strip()
        except Exception:
            return ""

    def _save_last_chat_id(self):
        if not self._last_chat_id:
            return
        try:
            import json
            os.makedirs(os.path.dirname(TELEGRAM_STATE_PATH), exist_ok=True)
            with open(TELEGRAM_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({"last_chat_id": self._last_chat_id}, f, ensure_ascii=False)
        except Exception:
            pass

    def _remember_chat_id(self, chat_id: str):
        if not chat_id:
            return
        chat_id = str(chat_id).strip()
        if not chat_id or chat_id == self._last_chat_id:
            return
        self._last_chat_id = chat_id
        self._save_last_chat_id()

    def _default_chat_id(self) -> str:
        if self._last_chat_id:
            return self._last_chat_id
        try:
            import config as cfg
            allowed = getattr(cfg, "TELEGRAM_ALLOWED_USERS", [])
            if allowed:
                return str(allowed[0])
        except Exception:
            pass
        return ""

    def _message_meta(self, update, context, text: str) -> tuple[str, bool]:
        """Ritorna (chat_type, addressed). addressed = il bot è interpellato
        direttamente (privata sempre; gruppo solo con @menzione o reply)."""
        msg = getattr(update, "message", None)
        if msg is None:
            return "private", True
        chat = getattr(msg, "chat", None)
        chat_type = str(getattr(chat, "type", "private") if chat else "private")
        bot_username = getattr(getattr(context, "bot", None), "username", "") or ""
        reply = getattr(msg, "reply_to_message", None)
        is_reply_to_bot = False
        if reply is not None:
            ru = getattr(reply, "from_user", None)
            if ru is not None:
                is_reply_to_bot = bool(getattr(ru, "is_bot", False)) and (
                    (getattr(ru, "username", "") or "").lower() == bot_username.lower()
                )
        addressed = should_respond_in_group(
            chat_type, text or "", bot_username, is_reply_to_bot
        )
        return chat_type, addressed

    def _should_enqueue(self, chat_type: str, addressed: bool) -> bool:
        """Accoda SEMPRE i messaggi di testo, anche quelli di gruppo non rivolti
        al bot: così l'agente può LEGGERE e MEMORIZZARE tutta la conversazione
        del gruppo. La decisione se rispondere o restare in silenzio la prende
        main.py (che memorizza comunque ogni messaggio)."""
        return True

    def _should_enqueue_media(self, chat_type: str, addressed: bool) -> bool:
        """Come _should_enqueue ma per i media: in gruppo, se non interpellato,
        li processa solo in modalità 'all' (evita vision/whisper su media che
        non riguardano il bot in modalità 'natural')."""
        if addressed or chat_type not in ("group", "supergroup"):
            return True
        try:
            import config as cfg
            return getattr(cfg, "TELEGRAM_GROUP_MODE", "mention") == "all"
        except Exception:
            return False

    def _report_error(self, message: str):
        if callable(self._error_callback):
            try:
                self._error_callback(message)
                return
            except Exception:
                pass
        print(message)

    # ── Pre-analisi media (condivisa tra PTB e fallback requests) ──
    #
    # L'agente riceve la descrizione/trascrizione già pronta, non un ordine
    # di chiamare un tool: così funziona con qualsiasi modello.

    def _describe_photo(self, path: str, caption: str = "") -> str:
        prompt = caption or "Descrivi questa immagine in dettaglio."
        result = IMAGE_TOOL.handler(path=path, prompt=prompt)
        if result.success:
            text = f"[L'utente ha inviato un'immagine. Analisi: {result.output}]"
            if caption:
                text += f"\nDidascalia dell'utente: {caption}"
            text += "\nRispondi in modo utile a ciò che mostra l'immagine."
        else:
            err = result.error or "analisi non riuscita"
            text = (
                f"[L'utente ha inviato un'immagine ma non sono riuscito ad "
                f"analizzarla: {err}. Spiega all'utente il problema "
                f"(es. modello vision non disponibile) in modo chiaro.]"
            )
            if caption:
                text += f"\nDidascalia: {caption}"
        return text

    def _describe_voice(self, path: str) -> str:
        result = AUDIO_TRANSCRIBE_TOOL.handler(path=path)
        if result.success:
            return (
                f"[Vocale dall'utente — {result.output}]\n"
                f"Rispondi al contenuto del vocale come in una chat normale."
            )
        err = result.error or "trascrizione non riuscita"
        return (
            f"[L'utente ha inviato un vocale ma non sono riuscito a "
            f"trascriverlo: {err}. Spiega il problema all'utente "
            f"(es. Whisper non installato: pip install faster-whisper).]"
        )

    def _describe_document(self, path: str, filename: str, caption: str = "") -> str:
        suffix = Path(path).suffix.lower()
        tool = None
        label = "file"
        if suffix == ".pdf":
            tool = lambda: PDF_TOOL.handler(path=path)
            label = "documento PDF"
        elif suffix in IMAGE_EXTENSIONS:
            tool = lambda: IMAGE_TOOL.handler(
                path=path, prompt=caption or "Descrivi questa immagine in dettaglio.")
            label = "immagine"
        elif suffix in AUDIO_EXTENSIONS:
            tool = lambda: AUDIO_TRANSCRIBE_TOOL.handler(path=path)
            label = "audio"

        if tool is not None:
            result = tool()
            if result.success:
                text = (f"[L'utente ha inviato un {label} ({filename}). "
                        f"Contenuto: {result.output}]\nRispondi in modo utile.")
            else:
                text = (f"[L'utente ha inviato un {label} ({filename}) ma non "
                        f"sono riuscito a leggerlo: {result.error or 'errore'}. "
                        f"Spiega il problema all'utente.]")
        else:
            # File generico: l'agente lo legge con read_file (affidabile)
            text = (f"[L'utente ha inviato il file {filename} in {path}. "
                    f"Leggilo con read_file se serve e rispondi.]")
        if caption:
            text += f"\nDidascalia: {caption}"
        return text

    def _register_commands(self):
        """Registra i comandi nel menu del bot via setMyCommands."""
        import requests as req
        base = f"https://api.telegram.org/bot{self.token}"
        commands = [{"command": cmd, "description": desc} for cmd, desc in BOT_COMMANDS]
        try:
            req.post(f"{base}/setMyCommands", json={"commands": commands}, timeout=10)
        except Exception:
            pass

    def send_chat_action(self, chat_id: str, action: str = "typing"):
        """Invia chat action (typing, upload_photo, etc)."""
        import requests as req
        base = f"https://api.telegram.org/bot{self.token}"
        try:
            req.post(f"{base}/sendChatAction", json={
                "chat_id": chat_id, "action": action
            }, timeout=5)
        except Exception:
            pass

    def start_typing_loop(self, chat_id: str):
        """Avvia un loop che manda 'typing' ogni 4s finché non viene fermato."""
        stop_event = threading.Event()
        # Ferma eventuale loop precedente
        old = self._typing_threads.pop(chat_id, None)
        if old:
            old.set()

        self._typing_threads[chat_id] = stop_event

        def _loop():
            while not stop_event.is_set():
                self.send_chat_action(chat_id, "typing")
                stop_event.wait(4)

        t = threading.Thread(target=_loop, daemon=True, name=f"typing-{chat_id}")
        t.start()

    def stop_typing_loop(self, chat_id: str):
        """Ferma il loop typing per questa chat."""
        stop = self._typing_threads.pop(chat_id, None)
        if stop:
            stop.set()

    def send_status(self, chat_id: str, text: str):
        """Invia o aggiorna un messaggio di stato (pensiero/tool in corso)."""
        import requests as req
        base = f"https://api.telegram.org/bot{self.token}"

        existing_id = self._status_messages.get(chat_id)
        if existing_id:
            # Aggiorna il messaggio esistente
            try:
                r = req.post(f"{base}/editMessageText", json={
                    "chat_id": chat_id,
                    "message_id": existing_id,
                    "text": text[:4096],
                }, timeout=5)
                if r.ok:
                    return
                # Se l'edit fallisce (messaggio cancellato/perso), resetta e crea nuovo
                self._status_messages.pop(chat_id, None)
            except Exception:
                self._status_messages.pop(chat_id, None)

        # Invia nuovo messaggio di stato
        try:
            r = req.post(f"{base}/sendMessage", json={
                "chat_id": chat_id,
                "text": text[:4096],
            }, timeout=5)
            msg_id = r.json().get("result", {}).get("message_id")
            if msg_id:
                self._status_messages[chat_id] = msg_id
        except Exception:
            pass

    def clear_status(self, chat_id: str):
        """Cancella il messaggio di stato."""
        import requests as req
        msg_id = self._status_messages.pop(chat_id, None)
        if not msg_id:
            return
        base = f"https://api.telegram.org/bot{self.token}"
        try:
            req.post(f"{base}/deleteMessage", json={
                "chat_id": chat_id, "message_id": msg_id,
            }, timeout=5)
        except Exception:
            pass

    def start(self):
        """Avvia long polling."""
        # Registra comandi nel menu del bot
        self._register_commands()

        # Avvia worker per processare la coda
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._process_queue, daemon=True, name="tg-worker"
        )
        self._worker_thread.start()

        try:
            from telegram import Update
            from telegram.ext import ApplicationBuilder
            self._start_ptb()
        except ImportError:
            self._start_requests()
        except Exception as e:
            if "terminated by other getUpdates request" in str(e):
                self._running = False
                self._report_error(format_telegram_conflict_message())
                return
            raise

    def _start_ptb(self):
        """Avvia con python-telegram-bot."""
        from telegram import Update, BotCommand
        from telegram.error import Conflict
        from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
        import asyncio

        app = ApplicationBuilder().token(self.token).build()
        self._app = app

        async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE):
            err = getattr(context, "error", None)
            if isinstance(err, Conflict):
                self._running = False
                self._report_error(format_telegram_conflict_message())
                try:
                    context.application.stop_running()
                except Exception:
                    pass
                return

            if err:
                self._report_error(f"Telegram errore: {err}")

        async def handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Gestisce tutti i comandi dal menu e li passa come testo all'agent."""
            if not update.message or not update.message.text:
                return
            chat_id = str(update.message.chat_id)
            sender = update.message.from_user.first_name if update.message.from_user else ""
            msg = ChannelMessage(
                text=update.message.text,
                sender=sender,
                channel="telegram",
                raw=update,
                chat_id=chat_id,
                thread_id=str(getattr(update.message, "message_thread_id", "") or ""),
            )
            self._msg_queue.put((msg, chat_id, context))

        async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not update.message or not update.message.text:
                return
            chat_type, addressed = self._message_meta(update, context, update.message.text)
            if not self._should_enqueue(chat_type, addressed):
                return
            chat_id = str(update.message.chat_id)
            sender = update.message.from_user.first_name if update.message.from_user else ""
            bot_username = getattr(getattr(context, "bot", None), "username", "") or ""
            msg = ChannelMessage(
                text=strip_bot_mention(update.message.text, bot_username),
                sender=sender,
                username=(getattr(update.message.from_user, "username", "") or "")
                if update.message.from_user else "",
                channel="telegram",
                raw=update,
                chat_id=chat_id,
                thread_id=str(getattr(update.message, "message_thread_id", "") or ""),
                chat_type=chat_type,
                addressed=addressed,
            )
            self._msg_queue.put((msg, chat_id, context))

        async def handle_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Quando qualcuno viene aggiunto al gruppo lo registriamo nel roster
            (passa dalla pipeline come messaggio ambient: memorizzato, silenzio)."""
            if not update.message or not update.message.new_chat_members:
                return
            chat_id = str(update.message.chat_id)
            chat = getattr(update.message, "chat", None)
            chat_type = str(getattr(chat, "type", "supergroup") if chat else "supergroup")
            for member in update.message.new_chat_members:
                if getattr(member, "is_bot", False):
                    continue
                name = (getattr(member, "first_name", "") or
                        getattr(member, "username", "") or "qualcuno")
                msg = ChannelMessage(
                    text="[si è unito al gruppo]",
                    sender=name,
                    username=getattr(member, "username", "") or "",
                    channel="telegram",
                    raw=update,
                    chat_id=chat_id,
                    thread_id=str(getattr(update.message, "message_thread_id", "") or ""),
                    chat_type=chat_type,
                    addressed=False,
                )
                self._msg_queue.put((msg, chat_id, context))

        async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not update.message or not update.message.photo:
                return
            chat_type, addressed = self._message_meta(update, context, update.message.caption or "")
            if not self._should_enqueue_media(chat_type, addressed):
                return
            try:
                photo = update.message.photo[-1]
                file = await photo.get_file()
                ts = int(time.time())
                path = os.path.join(MEDIA_DIR, f"photo_{ts}.jpg")
                await file.download_to_drive(path)

                caption = update.message.caption or ""

                import asyncio
                loop = asyncio.get_event_loop()
                text = await loop.run_in_executor(
                    None, lambda: self._describe_photo(path, caption)
                )

                chat_id = str(update.message.chat_id)
                msg = ChannelMessage(
                    text=text,
                    sender=update.message.from_user.first_name if update.message.from_user else "",
                    channel="telegram",
                    raw=update,
                    chat_id=chat_id,
                    thread_id=str(getattr(update.message, "message_thread_id", "") or ""),
                    chat_type=chat_type,
                    addressed=addressed,
                )
                self._msg_queue.put((msg, chat_id, context))
            except Exception as e:
                await update.message.reply_text(f"Image receive error: {str(e)[:200]}")

        async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
            voice = None
            if update.message:
                voice = update.message.voice or update.message.audio
            if not voice:
                return
            chat_type, addressed = self._message_meta(update, context, update.message.caption or "")
            if not self._should_enqueue_media(chat_type, addressed):
                return
            try:
                file = await voice.get_file()
                ts = int(time.time())
                ext = "ogg" if update.message.voice else "mp3"
                path = os.path.join(MEDIA_DIR, f"audio_{ts}.{ext}")
                await file.download_to_drive(path)

                import asyncio
                loop = asyncio.get_event_loop()
                text = await loop.run_in_executor(
                    None, lambda: self._describe_voice(path)
                )

                chat_id = str(update.message.chat_id)
                msg = ChannelMessage(
                    text=text,
                    sender=update.message.from_user.first_name if update.message.from_user else "",
                    channel="telegram",
                    raw=update,
                    chat_id=chat_id,
                    thread_id=str(getattr(update.message, "message_thread_id", "") or ""),
                    chat_type=chat_type,
                    addressed=addressed,
                )
                self._msg_queue.put((msg, chat_id, context))
            except Exception as e:
                await update.message.reply_text(f"Audio error: {str(e)[:200]}")

        async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not update.message or not update.message.document:
                return
            chat_type, addressed = self._message_meta(update, context, update.message.caption or "")
            if not self._should_enqueue_media(chat_type, addressed):
                return
            try:
                doc = update.message.document
                file = await doc.get_file()
                filename = doc.file_name or f"doc_{int(time.time())}"
                path = os.path.join(MEDIA_DIR, filename)
                await file.download_to_drive(path)

                caption = update.message.caption or ""

                import asyncio
                loop = asyncio.get_event_loop()
                text = await loop.run_in_executor(
                    None, lambda: self._describe_document(path, filename, caption)
                )

                chat_id = str(update.message.chat_id)
                msg = ChannelMessage(
                    text=text,
                    sender=update.message.from_user.first_name if update.message.from_user else "",
                    channel="telegram",
                    raw=update,
                    chat_id=chat_id,
                    thread_id=str(getattr(update.message, "message_thread_id", "") or ""),
                    chat_type=chat_type,
                    addressed=addressed,
                )
                self._msg_queue.put((msg, chat_id, context))
            except Exception as e:
                await update.message.reply_text(f"Document error: {str(e)[:200]}")

        async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
            """Gestisce risposte ai bottoni inline di conferma."""
            cq = update.callback_query
            if not cq or not cq.data:
                return

            data = cq.data
            if not data.startswith("confirm_yes_") and not data.startswith("confirm_no_"):
                return

            parts = data.split("_", 2)
            if len(parts) < 3:
                return
            callback_id = parts[2]

            pending = self._pending_confirms.get(callback_id)
            if not pending:
                await cq.answer("Request expired.")
                return

            approved = parts[1] == "yes"
            pending["approved"] = approved
            pending["event"].set()

            answer_text = "Done!" if approved else "Blocked."
            await cq.answer(answer_text)

            status = "Approved" if approved else "Blocked"
            original_text = pending.get("display_msg", "")
            try:
                await cq.edit_message_text(f"[{status}] {original_text}")
            except Exception:
                pass

        app.add_handler(CallbackQueryHandler(handle_callback_query))
        # Comandi dal menu del bot — passati all'agent come testo
        for cmd, _ in BOT_COMMANDS:
            app.add_handler(CommandHandler(cmd, handle_command))
        app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_members))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
        app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
        app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
        app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        app.add_error_handler(handle_error)
        self._running = True

        # openvurp avvia il polling in un thread di background: lì i gestori di
        # segnali non si possono installare (set_wakeup_fd richiede il main
        # thread) e serve un event loop dedicato al thread. Lo shutdown lo
        # gestisce il runtime (stop()), non i segnali.
        if threading.current_thread() is threading.main_thread():
            app.run_polling(drop_pending_updates=True)
        else:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())
            app.run_polling(drop_pending_updates=True, stop_signals=None)

    def _process_queue(self):
        """Worker thread che processa messaggi dalla coda."""
        import asyncio

        while self._running:
            try:
                msg, chat_id, context = self._msg_queue.get(timeout=1)
            except queue.Empty:
                continue

            try:
                self._remember_chat_id(chat_id)
                response = None
                if self._callback:
                    response = self._callback(msg)

                directive = parse_response_directive(response)
                if directive.kind == "text" and chat_id:
                    self._send_with_streaming(chat_id, directive.text)
                elif directive.kind == "reaction":
                    self._react_to_message(msg, directive.emoji)

            except Exception as e:
                import traceback
                traceback.print_exc()
                try:
                    self._send_sync(chat_id, f"[Error: {str(e)[:200]}]")
                except Exception:
                    pass

    @staticmethod
    def _split_text(text: str, limit: int = 4000) -> list[str]:
        """Spezza il testo in parti <= limit (cap Telegram è 4096, qui con
        margine) preferendo i confini di riga/parola, così non taglia a metà
        frase. Sostituisce i vecchi `text[:4096]` che troncavano le risposte."""
        text = (text or "").strip()
        if not text:
            return []
        parts: list[str] = []
        rest = text
        while len(rest) > limit:
            window = rest[:limit]
            cut = window.rfind("\n")
            if cut < int(limit * 0.5):
                cut = window.rfind(" ")
            if cut <= 0:
                cut = limit
            parts.append(rest[:cut].rstrip())
            rest = rest[cut:].lstrip()
        if rest:
            parts.append(rest)
        return parts

    def _send_with_streaming(self, chat_id, text: str):
        """Invia la risposta. Spezza i messaggi lunghi in più parti (mai
        troncati) e applica l'effetto streaming SOLO nei DM: nei gruppi Telegram
        rate-limita gli editMessageText, così il messaggio restava bloccato a un
        chunk con ' ...' (i 'messaggi tagliati'). Lì invio diretto e completo."""
        parts = self._split_text(text)
        if not parts:
            return  # silenzio (es. il bot ha deciso di non intervenire in gruppo)

        is_group = str(chat_id).startswith("-")  # chat_id di gruppo è negativo
        # Niente edit-streaming in gruppo o su risposte multi-parte: invio pieno.
        if is_group or len(parts) > 1:
            for part in parts:
                self._send_sync(chat_id, part)
            return
        self._stream_first_part(chat_id, parts[0])

    def _stream_first_part(self, chat_id, text: str):
        """Effetto 'sta scrivendo' su un singolo messaggio (gia <= limite), con
        poche edit e consegna finale SEMPRE garantita: se l'edit finale fallisce
        invia comunque il testo intero, così non resta troncato."""
        import requests as req
        base = f"https://api.telegram.org/bot{self.token}"

        def _send_full():
            req.post(f"{base}/sendMessage",
                     json={"chat_id": chat_id, "text": text}, timeout=10)

        if len(text) <= 200:
            _send_full()
            return

        n = len(text)
        cuts = [int(n * f) for f in (0.3, 0.6, 0.85)]
        try:
            r = req.post(f"{base}/sendMessage", json={
                "chat_id": chat_id, "text": text[:cuts[0]].rstrip() + " …",
            }, timeout=10)
            msg_id = r.json().get("result", {}).get("message_id")
            if not msg_id:
                _send_full()
                return
            for cut in cuts[1:]:
                try:
                    req.post(f"{base}/editMessageText", json={
                        "chat_id": chat_id, "message_id": msg_id,
                        "text": text[:cut].rstrip() + " …",
                    }, timeout=10)
                    time.sleep(0.4)
                except Exception:
                    pass
            rf = req.post(f"{base}/editMessageText", json={
                "chat_id": chat_id, "message_id": msg_id, "text": text,
            }, timeout=10)
            try:
                ok = rf.ok and rf.json().get("ok", False)
            except Exception:
                ok = False
            if not ok:
                _send_full()  # edit rate-limited: garantisci il testo completo
        except Exception:
            _send_full()

    def _send_sync(self, chat_id, text: str, thread_id: str = ""):
        """Invio sincrono. Spezza i messaggi oltre il limite Telegram in più
        messaggi invece di troncarli a 4096."""
        import requests as req
        base = f"https://api.telegram.org/bot{self.token}"
        for part in self._split_text(text):
            payload = {"chat_id": chat_id, "text": part}
            if thread_id:
                payload["message_thread_id"] = thread_id
            req.post(f"{base}/sendMessage", json=payload, timeout=10)

    def _download_file(self, file_id: str, dest_path: str) -> bool:
        """Scarica un file Telegram via Bot API (getFile + download)."""
        import requests as req
        base = f"https://api.telegram.org/bot{self.token}"
        try:
            r = req.get(f"{base}/getFile", params={"file_id": file_id}, timeout=30)
            r.raise_for_status()
            file_path = r.json().get("result", {}).get("file_path", "")
            if not file_path:
                return False
            url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
            data = req.get(url, timeout=120)
            data.raise_for_status()
            with open(dest_path, "wb") as f:
                f.write(data.content)
            return True
        except Exception:
            return False

    def _extract_fallback_text(self, message: dict) -> str:
        """Estrae il testo da un update raw, pre-analizzando i media.

        Ritorna stringa vuota se il messaggio non contiene nulla di utile.
        """
        text = message.get("text", "")
        if text:
            return text

        caption = message.get("caption", "") or ""
        ts = int(time.time())

        photos = message.get("photo") or []
        if photos:
            file_id = photos[-1].get("file_id", "")
            path = os.path.join(MEDIA_DIR, f"photo_{ts}.jpg")
            if file_id and self._download_file(file_id, path):
                return self._describe_photo(path, caption)
            return ("[L'utente ha inviato un'immagine ma il download è fallito. "
                    "Avvisalo dell'errore.]")

        voice = message.get("voice") or message.get("audio")
        if voice:
            ext = "ogg" if message.get("voice") else "mp3"
            path = os.path.join(MEDIA_DIR, f"audio_{ts}.{ext}")
            if voice.get("file_id") and self._download_file(voice["file_id"], path):
                return self._describe_voice(path)
            return ("[L'utente ha inviato un vocale ma il download è fallito. "
                    "Avvisalo dell'errore.]")

        doc = message.get("document")
        if doc:
            filename = doc.get("file_name") or f"doc_{ts}"
            path = os.path.join(MEDIA_DIR, filename)
            if doc.get("file_id") and self._download_file(doc["file_id"], path):
                return self._describe_document(path, filename, caption)
            return (f"[L'utente ha inviato il file {filename} ma il download "
                    f"è fallito. Avvisalo dell'errore.]")

        return ""

    def _start_requests(self):
        """Fallback: long polling con requests (testo + media pre-analizzati)."""
        import requests

        self._report_error(
            "Telegram: python-telegram-bot non installato, uso il polling di "
            "riserva. Funziona, ma per bottoni di conferma e gestione media "
            "più solida: pip install python-telegram-bot"
        )

        base = f"https://api.telegram.org/bot{self.token}"
        self._running = True

        while self._running:
            try:
                r = requests.get(f"{base}/getUpdates", params={
                    "offset": self._offset, "timeout": 30
                }, timeout=35)
                if r.status_code == 409 or "terminated by other getUpdates request" in r.text:
                    self._running = False
                    self._report_error(format_telegram_conflict_message())
                    return
                r.raise_for_status()

                for update in r.json().get("result", []):
                    self._offset = update["update_id"] + 1
                    message = update.get("message", {})
                    text = self._extract_fallback_text(message)
                    if not text:
                        continue

                    sender = message.get("from", {}).get("first_name", "")
                    chat_id = message.get("chat", {}).get("id", "")
                    self._remember_chat_id(str(chat_id))

                    msg = ChannelMessage(
                        text=text,
                        sender=sender,
                        channel="telegram",
                        raw=update,
                        chat_id=str(chat_id),
                        thread_id=str(message.get("message_thread_id", "") or ""),
                    )
                    response = None
                    if self._callback:
                        response = self._callback(msg)

                    directive = parse_response_directive(response)
                    if directive.kind == "text" and chat_id:
                        self._send_with_streaming(chat_id, directive.text)
                    elif directive.kind == "reaction":
                        self._react_to_message(msg, directive.emoji)

            except Exception:
                time.sleep(5)

    def _react_to_message(self, msg: ChannelMessage, emoji: str):
        """Aggiunge una reaction al messaggio originale, se Telegram lo supporta."""
        if not emoji:
            return

        chat_id = msg.chat_id
        message_id = None
        raw = msg.raw

        if hasattr(raw, "message") and raw.message:
            chat_id = chat_id or str(raw.message.chat_id)
            message_id = getattr(raw.message, "message_id", None)
        elif isinstance(raw, dict):
            raw_msg = raw.get("message", {})
            chat_id = chat_id or str(raw_msg.get("chat", {}).get("id", ""))
            message_id = raw_msg.get("message_id")

        if not chat_id or not message_id:
            return

        import requests as req

        base = f"https://api.telegram.org/bot{self.token}"
        try:
            req.post(
                f"{base}/setMessageReaction",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reaction": [{"type": "emoji", "emoji": emoji}],
                    "is_big": False,
                },
                timeout=10,
            )
        except Exception:
            pass

    def request_confirm(self, chat_id: str, msg: str, timeout_seconds: int = 60) -> bool:
        """Invia bottoni Si/No e attende risposta via CallbackQueryHandler.

        Non usa getUpdates — nessun conflitto con run_polling().
        """
        import requests as req
        import uuid

        base = f"https://api.telegram.org/bot{self.token}"
        callback_id = str(uuid.uuid4())[:8]
        display_msg = msg[:200] if len(msg) > 200 else msg

        event = threading.Event()
        self._pending_confirms[callback_id] = {
            "event": event,
            "approved": False,
            "display_msg": display_msg,
        }

        keyboard = {
            "inline_keyboard": [[
                {"text": "Si, esegui", "callback_data": f"confirm_yes_{callback_id}"},
                {"text": "No, blocca", "callback_data": f"confirm_no_{callback_id}"},
            ]]
        }

        try:
            r = req.post(f"{base}/sendMessage", json={
                "chat_id": chat_id,
                "text": f"Confirm action:\n\n{display_msg}",
                "reply_markup": keyboard,
            }, timeout=10)

            if not r.ok:
                self._pending_confirms.pop(callback_id, None)
                return False

            sent_msg_id = r.json().get("result", {}).get("message_id")
        except Exception:
            self._pending_confirms.pop(callback_id, None)
            return False

        got_response = event.wait(timeout=timeout_seconds)

        if got_response:
            approved = self._pending_confirms[callback_id]["approved"]
        else:
            approved = False
            try:
                req.post(f"{base}/editMessageText", json={
                    "chat_id": chat_id,
                    "message_id": sent_msg_id,
                    "text": f"[Timeout — bloccato] {display_msg}",
                }, timeout=5)
            except Exception:
                pass

        self._pending_confirms.pop(callback_id, None)
        return approved

    def stop(self):
        self._running = False
        # Ferma tutti i typing loop
        for stop_event in self._typing_threads.values():
            stop_event.set()
        self._typing_threads.clear()

    def send(self, message: str, chat_id: str = None, **kwargs):
        """Invia messaggio (richiede chat_id)."""
        message = prepare_outbound_response(message, source="telegram")
        chat_id = chat_id or self._default_chat_id()
        thread_id = str(kwargs.get("thread_id", "") or "")
        if not chat_id or not message:
            return
        self._send_sync(chat_id, message, thread_id=thread_id)

    def send_to_last(self, message: str) -> bool:
        """Invia all'ultima chat nota, con fallback all'utente autorizzato."""
        message = prepare_outbound_response(message, source="telegram")
        chat_id = self._default_chat_id()
        if not chat_id or not message:
            return False
        self._send_sync(chat_id, message)
        return True

    def send_voice(self, chat_id: str, audio_path: str):
        """Invia un messaggio vocale su Telegram."""
        import requests as req
        base = f"https://api.telegram.org/bot{self.token}"
        try:
            with open(audio_path, "rb") as f:
                req.post(
                    f"{base}/sendVoice",
                    data={"chat_id": chat_id},
                    files={"voice": f},
                    timeout=30,
                )
        except Exception:
            pass

    def send_photo(self, chat_id: str, image_path: str, caption: str = "") -> bool:
        """Invia una foto su Telegram."""
        from tools.notify import _send_telegram_media
        return _send_telegram_media(self.token, chat_id, image_path, caption=caption, force_document=False)

    def send_file(self, chat_id: str, path: str, caption: str = "", force_document: bool = False) -> bool:
        """Invia un file generico su Telegram."""
        from tools.notify import _send_telegram_media
        return _send_telegram_media(self.token, chat_id, path, caption=caption, force_document=force_document)
