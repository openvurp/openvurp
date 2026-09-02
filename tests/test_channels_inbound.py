"""Ogni canale in entrata deve passare per lo stesso cuore.

La regressione da cui nasce tutto: il vecchio bot Telegram contava ZERO
riferimenti a `chat_store`, `swarm` e `multiplayer`. Aveva una sua idea di
conversazione, quindi ogni cosa costruita per il web (rubrica, stanze,
streaming, approvazioni) andava rifatta li' dentro, sempre in ritardo.

Il test che conta e' l'ultimo: nessun canale deve toccare quei moduli da solo.
"""

import pathlib
import re

import pytest

from core.conversation import ChannelConversation, Incoming, Reply


# ── Telegram ─────────────────────────────────────────────────────────────

def test_a_long_answer_is_split_on_line_breaks_not_mid_word():
    from channels.telegram import split, MESSAGE_LIMIT
    testo = "\n".join(f"riga {i} " + "x" * 80 for i in range(200))
    pezzi = split(testo)
    assert len(pezzi) > 1
    assert max(len(p) for p in pezzi) <= MESSAGE_LIMIT
    assert "".join(pezzi) == testo, "spezzando si e' perso del testo"


def test_a_single_endless_line_is_still_cut():
    from channels.telegram import split, MESSAGE_LIMIT
    pezzi = split("y" * 9000)
    assert max(len(p) for p in pezzi) <= MESSAGE_LIMIT
    assert "".join(pezzi) == "y" * 9000


def test_nothing_is_sent_for_an_empty_answer():
    from channels.telegram import split
    assert split("   ") == []
    assert split("") == []


def _telegram(monkeypatch, allowed=None):
    from channels.telegram import TelegramChannel
    inviati = []
    ch = TelegramChannel(token="t", allowed=allowed)
    monkeypatch.setattr(ch, "send",
                        lambda testo, chat_id="", **k: inviati.append((chat_id, testo)))
    return ch, inviati


def _update(testo, user_id="42"):
    return {"update_id": 1, "message": {
        "text": testo, "from": {"id": int(user_id), "first_name": "Mario"},
        "chat": {"id": 777}}}


def test_telegram_hands_the_message_to_the_shared_core(monkeypatch):
    ch, inviati = _telegram(monkeypatch)
    visti = []

    class Finto:
        def handle(self, msg):
            visti.append(msg)
            return [Reply("eccomi", author="amanda")]

    ch.conversation = Finto()
    ch._handle(_update("ciao"))

    assert len(visti) == 1 and visti[0].text == "ciao"
    assert visti[0].channel == "telegram" and visti[0].peer_id == "42"
    assert inviati == [("777", "*amanda*\neccomi")]


def test_telegram_stays_silent_with_strangers(monkeypatch):
    """A uno sconosciuto non si conferma nemmeno che il bot esista."""
    ch, inviati = _telegram(monkeypatch, allowed=["42"])
    chiamato = []
    ch.conversation = type("C", (), {"handle": lambda s, m: chiamato.append(m) or []})()

    ch._handle(_update("ciao", user_id="999"))
    assert not chiamato and not inviati

    ch._handle(_update("ciao", user_id="42"))
    assert chiamato, "l'utente autorizzato e' stato bloccato"


def test_telegram_without_a_token_refuses_to_start():
    from channels.telegram import TelegramChannel
    with pytest.raises(ValueError, match="TELEGRAM_TOKEN"):
        TelegramChannel(token="")


# ── il vincolo che tiene insieme tutto ───────────────────────────────────

def test_no_channel_invents_its_own_conversation():
    """Il difetto originale, reso impossibile da ripetere per distrazione.

    Un adattatore che tocca da solo la rubrica, le stanze o lo sciame si sta
    costruendo una seconda idea di conversazione: da li' nasce il canale che
    resta indietro per sempre.
    """
    radice = pathlib.Path(__file__).resolve().parent.parent / "channels"
    vietati = ("chat_store", "ChatStore", "multiplayer", "core.swarm",
               "MultiplayerCoordinator", "Swarm(")
    for file in radice.glob("*.py"):
        testo = file.read_text(encoding="utf-8")
        for parola in vietati:
            assert parola not in testo, (
                f"{file.name} tocca {parola} da solo: deve passare per "
                f"ChannelConversation")


def test_every_channel_takes_a_conversation_and_a_whitelist():
    radice = pathlib.Path(__file__).resolve().parent.parent / "channels"
    for nome in ("telegram", "discord", "slack", "whatsapp"):
        testo = (radice / f"{nome}.py").read_text(encoding="utf-8")
        assert "conversation" in testo, f"{nome} non riceve il cuore condiviso"
        assert "allowed" in testo, f"{nome} accetta chiunque"


# ── non solo testo: audio, foto, documenti ───────────────────────────────

def test_a_voice_note_reaches_the_agent_as_a_file(monkeypatch, tmp_path):
    """Il difetto vero: riscrivendo il canale sottile avevo tenuto solo il
    testo, e una nota vocale spariva senza dire niente."""
    ch, _ = _telegram(monkeypatch)
    monkeypatch.setattr(ch, "_download",
                        lambda fid, suff, nome="": str(tmp_path / f"vocale{suff}"))
    percorsi, etichetta = ch.attachments({"voice": {"file_id": "abc", "duration": 3}})
    assert percorsi and percorsi[0].endswith(".ogg")
    assert etichetta == "a voice note"


def test_the_biggest_photo_is_taken_not_the_thumbnail(monkeypatch):
    ch, _ = _telegram(monkeypatch)
    presi = []
    monkeypatch.setattr(ch, "_download",
                        lambda fid, suff, nome="": presi.append(fid) or "/tmp/x.jpg")
    ch.attachments({"photo": [{"file_id": "piccola", "file_size": 900},
                           {"file_id": "grande", "file_size": 90000}]})
    assert presi == ["grande"], "ha preso l'anteprima invece della foto"


def test_a_caption_becomes_the_message(monkeypatch, tmp_path):
    ch, inviati = _telegram(monkeypatch)
    visti = []
    ch.conversation = type("C", (), {
        "handle": lambda s, m: visti.append(m) or [],
        "nomi": lambda s: []})()
    monkeypatch.setattr(ch, "_download", lambda *a, **k: str(tmp_path / "foto.jpg"))
    ch._handle({"update_id": 1, "message": {
        "caption": "che ne dici?", "photo": [{"file_id": "x", "file_size": 10}],
        "from": {"id": 42, "first_name": "Mario"}, "chat": {"id": 777}}})
    assert visti[0].text == "che ne dici?"
    assert visti[0].attachments and visti[0].attachments[0].endswith("foto.jpg")


def test_a_file_without_words_still_says_what_arrived(monkeypatch, tmp_path):
    """Un messaggio vuoto non aiuta l'agente: deve sapere cosa gli e' arrivato."""
    ch, _ = _telegram(monkeypatch)
    visti = []
    ch.conversation = type("C", (), {
        "handle": lambda s, m: visti.append(m) or [], "nomi": lambda s: []})()
    monkeypatch.setattr(ch, "_download", lambda *a, **k: str(tmp_path / "v.ogg"))
    ch._handle({"update_id": 1, "message": {
        "voice": {"file_id": "x"}, "from": {"id": 42, "first_name": "Mario"},
        "chat": {"id": 777}}})
    assert "voice note" in visti[0].text


def test_a_stranger_does_not_get_the_bandwidth(monkeypatch):
    """Il file si scarica DOPO il controllo: a uno sconosciuto non si fa
    nemmeno consumare banda."""
    ch, _ = _telegram(monkeypatch, allowed=["42"])
    scaricati = []
    monkeypatch.setattr(ch, "_download", lambda *a, **k: scaricati.append(a) or "/tmp/x")
    ch._handle({"update_id": 1, "message": {
        "voice": {"file_id": "x"}, "from": {"id": 999, "first_name": "Ignoto"},
        "chat": {"id": 777}}})
    assert not scaricati


def test_speaking_gets_a_spoken_answer_but_the_text_stays(monkeypatch):
    """Un vocale non si rilegge e non si cerca: il testo va mandato comunque."""
    import inspect
    from channels.telegram import TelegramChannel
    sorgente = inspect.getsource(TelegramChannel._handle)
    assert "you_spoke" in sorgente
    posto_send = sorgente.index("self.send(out_text")
    posto_voce = sorgente.index("self.send_voice")
    assert posto_send < posto_voce, "la voce non deve sostituire il testo"


def test_no_voice_answer_when_the_voice_is_off(monkeypatch):
    import config as cfg
    from channels.telegram import TelegramChannel
    monkeypatch.setattr(cfg, "VOICE_ENABLED", False, raising=False)
    ch = TelegramChannel(token="t")
    assert ch.send_voice("ciao", "777") is False


# ── WhatsApp via Baileys: il protocollo del ponte, provato senza Node ────

def _wa(allowed=None):
    from channels.whatsapp import WhatsAppChannel
    from core.conversation import Reply
    ch = WhatsAppChannel(allowed=allowed or ["393331234567"])
    visti, inviati = [], []
    ch.conversation = type("C", (), {
        "handle": lambda s, m: visti.append(m) or [Reply("eccomi", author="amanda")]})()
    ch.send = lambda testo, chat_id="", **k: inviati.append((chat_id, testo))
    return ch, visti, inviati


def test_a_whatsapp_message_goes_through_the_shared_core():
    ch, visti, inviati = _wa()
    ch._event({"type": "message", "from": "393331234567@s.whatsapp.net",
                "name": "Enzo", "text": "ciao"})
    assert visti[0].text == "ciao" and visti[0].channel == "whatsapp"
    assert visti[0].peer_id == "393331234567"
    # La risposta porta il nome in *bold* di WhatsApp, non in markdown web.
    assert inviati == [("393331234567@s.whatsapp.net", "*amanda*\neccomi")]


def test_the_device_suffix_in_the_jid_does_not_break_the_allowlist():
    """Baileys manda «39333…:12@s.whatsapp.net»: il :12 e' il dispositivo,
    non fa parte del numero — confrontarlo intero escluderebbe chiunque."""
    ch, visti, _ = _wa()
    ch._event({"type": "message", "from": "393331234567:12@s.whatsapp.net",
                "text": "ci sei?"})
    assert visti, "il suffisso del dispositivo ha bloccato un autorizzato"


def test_a_stranger_gets_silence_on_whatsapp_too():
    ch, visti, inviati = _wa(allowed=["390000000000"])
    ch._event({"type": "message", "from": "393331234567@s.whatsapp.net",
                "text": "ciao"})
    assert not visti and not inviati


def test_the_qr_and_the_connection_state_reach_the_page():
    ch, _, _ = _wa()
    ch._event({"type": "qr", "dataurl": "data:image/png;base64,AAA"})
    assert ch.qr and not ch.connected
    ch._event({"type": "open", "me": "393339876543:2@s.whatsapp.net"})
    assert ch.connected and ch.qr == "" and ch.me == "393339876543"
    ch._event({"type": "close", "code": 428})
    assert not ch.connected


def test_a_revoked_session_says_it_needs_a_new_qr():
    """Il logout non e' un guasto di rete: serve un nuovo QR, e va detto."""
    ch, _, _ = _wa()
    ch._event({"type": "loggedout"})
    assert "new QR" in ch.stop_reason


def test_whatsapp_requires_an_allowlist_like_everyone(monkeypatch):
    import config as cfg
    import pytest as _pt
    from core.channels_runtime import build
    monkeypatch.setattr(cfg, "WHATSAPP_ALLOWED_USERS", [], raising=False)
    with _pt.raises(ValueError, match="allowed"):
        build("whatsapp", object(), cfg)
    monkeypatch.setattr(cfg, "WHATSAPP_ALLOWED_USERS", ["393331234567"], raising=False)
    ch = build("whatsapp", object(), cfg)
    assert ch.name == "whatsapp" and ch.allowed == {"393331234567"}


def test_the_bridge_speaks_only_transport():
    """Il ponte Node non deve decidere niente: e' trasporto, come gli altri
    canali. Un ponte con un cervello e' un secondo cervello che invecchia male."""
    import pathlib
    testo = (pathlib.Path(__file__).resolve().parent.parent
             / "channels" / "wa-bridge" / "bridge.mjs").read_text(encoding="utf-8")
    for vietato in ("chat_store", "swarm", "multiplayer", "roster"):
        assert vietato not in testo
    # E ignora i gruppi: un bot nei gruppi WhatsApp e' un altro progetto.
    assert "@g.us" in testo
