"""Un canale in entrata non deve avere una sua idea di conversazione.

Il vecchio bot Telegram erano 1.064 righe che parlavano a openvurp come se
rubrica, stanze, streaming e approvazioni non esistessero: contava ZERO
riferimenti a `chat_store`, `swarm` e `multiplayer`. Non era un'altra porta
sulla stessa casa, era una porta sulla versione precedente — e ogni cosa
costruita per il web andava rifatta una seconda volta li' dentro, sempre in
ritardo.

Qui si verifica il contrario: il canale non decide niente, passa per la STESSA
funzione che usa la pagina web. L'unica cosa sua e' la grammatica dei comandi,
perche' in una chat non c'e' una barra laterale da cliccare.
"""

import tempfile

import pytest

from core.chat_store import ChatStore
from core.conversation import ChannelConversation, Incoming, Reply


class _ChatFn:
    """Sta al posto della chat della dashboard, e registra come viene chiamata."""

    def __init__(self, out=None):
        self.calls = []
        self.out = out or (lambda testo, chat_id: {"chat_id": chat_id,
                                                   "reply": f"risposta a {testo}",
                                                   "author_name": "openvurp"})

    def __call__(self, message, chat_id="", attachments=None):
        self.calls.append({"message": message, "chat_id": chat_id,
                           "attachments": attachments})
        return self.out(message, chat_id)


@pytest.fixture
def canale():
    store = ChatStore(tempfile.mkdtemp())
    for nome, ruolo in (("amanda", "offerte amazon"), ("ciccio", "bollette")):
        store.create_agent(nome, ruolo, "", "", "")
    fn = _ChatFn()
    return ChannelConversation(fn, store), fn, store


def _msg(testo, **kw):
    base = dict(text=testo, channel="telegram", peer_id="42", sender="mario")
    base.update(kw)
    return Incoming(**base)


# ── il canale non fa il lavoro: lo fa fare a chi lo fa per il web ────────

def test_a_plain_message_goes_through_the_same_chat_function(canale):
    conv, fn, _ = canale
    risposte = conv.handle(_msg("che tempo fa?"))
    assert len(fn.calls) == 1, "il canale ha risposto per conto suo"
    assert fn.calls[0]["message"] == "che tempo fa?"
    assert [r.text for r in risposte] == ["risposta a che tempo fa?"]


def test_the_same_person_keeps_the_same_conversation(canale):
    conv, fn, store = canale
    conv.handle(_msg("primo"))
    conv.handle(_msg("secondo"))
    assert fn.calls[0]["chat_id"] == fn.calls[1]["chat_id"], "ogni messaggio apriva una chat nuova"


def test_the_chat_says_where_it_came_from(canale):
    """Dalla pagina web devi vedere che quella chat arriva da Telegram e da chi."""
    conv, _, store = canale
    conv.handle(_msg("ciao"))
    titoli = [c["title"] for c in store.list_chats()]
    assert "Telegram · mario" in titoli, titoli


def test_two_people_do_not_share_a_conversation(canale):
    conv, fn, _ = canale
    conv.handle(_msg("io"))
    conv.handle(_msg("io", peer_id="99", sender="lucia"))
    assert fn.calls[0]["chat_id"] != fn.calls[1]["chat_id"]


# ── la grammatica: in chat non c'e' una barra laterale ───────────────────

def test_you_can_talk_to_one_agent_by_name(canale):
    conv, fn, store = canale
    conv.handle(_msg("@amanda mi trovi un SSD?"))
    assert fn.calls[0]["message"] == "mi trovi un SSD?", "il nome e' finito nel messaggio"
    chat = store.direct_chat_for_agent(
        next(a["id"] for a in store.list_agents() if a["name"] == "amanda"))
    assert fn.calls[0]["chat_id"] == chat["id"], "non e' andato nella chat di amanda"


def test_an_unknown_name_answers_with_who_is_there(canale):
    conv, fn, _ = canale
    risposte = conv.handle(_msg("@fantasma ci sei?"))
    assert not fn.calls, "ha chiamato l'agente comunque"
    testo = risposte[0].text
    assert "fantasma" in testo and "@amanda" in testo and "@ciccio" in testo


def test_the_roster_is_asked_to_the_store_not_hardcoded(canale):
    conv, _, store = canale
    store.create_agent("meteo", "previsioni", "", "", "")
    assert "@meteo" in conv.handle(_msg("/agents"))[0].text


def test_an_empty_roster_says_where_to_make_one():
    store = ChatStore(tempfile.mkdtemp())
    conv = ChannelConversation(_ChatFn(), store)
    assert "web page" in conv.handle(_msg("/agenti"))[0].text


def test_you_can_write_to_the_whole_room(canale):
    conv, fn, store = canale
    conv.handle(_msg("/all what do you think?"))
    stanza = store.team_room(create=False)
    assert stanza is not None, "la stanza non e' stata aperta"
    assert fn.calls[0]["chat_id"] == stanza["id"]
    assert fn.calls[0]["message"] == "what do you think?"


def test_you_can_stop_them_from_the_channel(canale):
    """La discussione va avanti finche' hanno da dire: serve fermarla da qui."""
    conv, _, store = canale
    stanza = store.team_room(create=True)
    import core.multiplayer as M
    M.clear_stop(stanza["id"])
    risposta = conv.handle(_msg("/stop"))[0].text
    assert M.stop_requested(stanza["id"]), "lo stop non e' arrivato alla stanza"
    assert "finishes their sentence" in risposta
    M.clear_stop(stanza["id"])


def test_stopping_with_no_room_says_so(canale):
    conv, _, _ = canale
    assert "no room open" in conv.handle(_msg("/stop"))[0].text


def test_help_lists_what_you_can_type(canale):
    conv, fn, _ = canale
    testo = conv.handle(_msg("/aiuto"))[0].text
    assert not fn.calls, "l'aiuto e' finito all'agente"
    for pezzo in ("@name", "/agents", "/all", "/stop"):
        assert pezzo in testo


# ── una stanza risponde con piu' voci ────────────────────────────────────

def test_every_voice_of_the_room_comes_back_not_just_the_last():
    store = ChatStore(tempfile.mkdtemp())
    fn = _ChatFn(out=lambda testo, chat_id: {
        "chat_id": chat_id, "reply": "",
        "team_messages": [
            {"content": "io dico di si", "author_name": "amanda"},
            {"content": "", "author_name": "muto"},
            {"content": "io no, e spiego", "author_name": "ciccio"},
        ],
    })
    conv = ChannelConversation(fn, store)
    risposte = conv.handle(_msg("/all decide"))
    assert [(r.author, r.text) for r in risposte] == [
        ("amanda", "io dico di si"), ("ciccio", "io no, e spiego")]


def test_silence_is_not_sent_as_a_message(canale):
    conv, _, _ = canale
    conv.chat_fn = _ChatFn(out=lambda t, c: {"chat_id": c, "reply": "(no reply)"})
    assert conv.handle(_msg("ciao")) == []


def test_a_broken_turn_answers_instead_of_going_silent(canale):
    conv, _, _ = canale

    def esplode(_t, _c):
        raise RuntimeError("backend giu'")

    conv.chat_fn = _ChatFn(out=esplode)
    risposte = conv.handle(_msg("ciao"))
    assert risposte and "backend giu'" in risposte[0].text


def test_attachments_are_carried_through(canale):
    conv, fn, _ = canale
    conv.handle(_msg("guarda questa", attachments=["/tmp/foto.jpg"]))
    assert fn.calls[0]["attachments"] == ["/tmp/foto.jpg"]


# ── nel bot devi VEDERE con chi puoi parlare ─────────────────────────────

def test_the_first_message_shows_who_you_can_talk_to(canale):
    """«Non lo so» era la risposta giusta: /start elencava i comandi, non la
    rubrica. La prima cosa da sapere e' CON CHI puoi parlare."""
    conv, fn, _ = canale
    testo = conv.handle(_msg("/start"))[0].text
    assert not fn.calls
    assert "@amanda" in testo and "@ciccio" in testo
    assert "offerte amazon" in testo, "the trade is what lets you choose"
    assert "Tap a name" in testo


def test_touching_a_name_keeps_the_conversation_with_that_agent(canale):
    """Ripetere «@amanda» a ogni riga diventa insopportabile dopo due messaggi."""
    conv, fn, store = canale
    risposta = conv.handle(_msg("@amanda"))[0].text
    assert not fn.calls, "un nome da solo non e' una domanda"
    assert "You're now talking to amanda" in risposta

    conv.handle(_msg("quanto costa?"))
    chat = store.direct_chat_for_agent(
        next(a["id"] for a in store.list_agents() if a["name"] == "amanda"))
    assert fn.calls[0]["chat_id"] == chat["id"], "e' andato all'agente sbagliato"
    assert fn.calls[0]["message"] == "quanto costa?"


def test_you_can_go_back_to_openvurp(canale):
    conv, fn, store = canale
    conv.handle(_msg("@amanda"))
    conv.handle(_msg("/me"))
    conv.handle(_msg("una cosa mia"))
    titoli = {c["id"]: c["title"] for c in store.list_chats()}
    assert titoli[fn.calls[-1]["chat_id"]] == "Telegram · mario"


def test_choosing_someone_who_is_gone_falls_back_instead_of_swallowing(canale):
    """Se cancelli l'agente dal web mentre il telefono lo tiene aperto, il
    messaggio successivo non deve sparire nel nulla."""
    conv, fn, store = canale
    conv.handle(_msg("@amanda"))
    store.delete_agent(next(a["id"] for a in store.list_agents() if a["name"] == "amanda"))
    conv.handle(_msg("ci sei?"))
    assert fn.calls, "il messaggio e' andato perso"


def test_the_selection_is_per_person_not_global(canale):
    conv, fn, store = canale
    conv.handle(_msg("@amanda"))
    conv.handle(_msg("qualcosa", peer_id="99", sender="lucia"))
    titoli = {c["id"]: c["title"] for c in store.list_chats()}
    assert titoli[fn.calls[-1]["chat_id"]] == "Telegram · lucia", \
        "la scelta di uno ha dirottato i messaggi di un altro"


def test_an_unknown_name_still_answers_with_the_roster(canale):
    conv, _, _ = canale
    assert "@amanda" in conv.handle(_msg("@fantasma"))[0].text
