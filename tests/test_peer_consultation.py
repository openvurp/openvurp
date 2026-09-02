"""Chiedere aiuto a un collega deve venire da solo, non su richiesta dell'utente.

Verificato sul database reale: `ask_peer` non e' partito **nemmeno una volta**.
Il prompt di sistema lo ordinava perfino in maiuscolo — «NON aspettare che te lo
chiedano» — e non e' bastato.

Il motivo non era la buona volonta' del modello ma dove stava l'informazione:
la rubrica era in una riga del prompt di sistema, mentre lo strumento chiedeva
un generico «nome dell'agente da consultare». Nel momento in cui decide, il
modello guarda gli strumenti che ha in mano: li' non c'era scritto ne' chi
esiste ne' di cosa si occupa, quindi consultare qualcuno richiedeva di
ricordarselo. Ora i nomi sono un elenco chiuso dentro il tool e i mestieri sono
nella descrizione.

Mancava anche il gesto piu' naturale: «chi mi da' una mano con questa cosa?»
rivolto a tutti. C'era solo il faccia a faccia, che obbliga a sapere gia' a chi
rivolgersi.
"""

import tempfile

import pytest

from core.chat_store import ChatStore
from core.swarm import Swarm, SwarmError


@pytest.fixture
def swarm():
    store = ChatStore(tempfile.mkdtemp())
    for name, role in (("amanda", "cerca offerte su amazon"),
                       ("ciccio", "bollette"),
                       ("dev", "programmer")):
        store.create_agent(name, role, "", "", "")
    return Swarm(parent_agent=None, store=store)


def _tool(swarm, member_name, tool_name):
    member = swarm.resolve(member_name)
    for entry in swarm._peer_tools(member):
        if entry["function"]["name"] == tool_name:
            return entry["function"]
    raise AssertionError(f"{tool_name} non offerto a {member_name}")


# ── chi c'e' dev'essere visibile NEL MOMENTO in cui si decide ─────────────

def test_the_tool_itself_lists_who_exists_and_what_they_do(swarm):
    fn = _tool(swarm, "dev", "ask_peer")
    assert "amanda = cerca offerte su amazon" in fn["description"]
    assert "ciccio = bollette" in fn["description"]


def test_the_names_are_a_closed_list_not_free_text(swarm):
    """Con un campo libero il modello deve ricordarsi i nomi; con un elenco no."""
    fn = _tool(swarm, "dev", "ask_peer")
    assert sorted(fn["parameters"]["properties"]["name"]["enum"]) == ["amanda", "ciccio"]


def test_nobody_is_offered_themselves(swarm):
    fn = _tool(swarm, "amanda", "ask_peer")
    assert "amanda" not in fn["parameters"]["properties"]["name"]["enum"]


def test_an_only_child_gets_no_peer_tools():
    store = ChatStore(tempfile.mkdtemp())
    store.create_agent("solo", "unico", "", "", "")
    s = Swarm(parent_agent=None, store=store)
    assert s._peer_tools(s.resolve("solo")) == []


def test_the_tool_says_not_to_ask_the_user_for_permission(swarm):
    """«Vuoi che senta amanda?» rimbalza addosso all'utente una decisione sua."""
    fn = _tool(swarm, "dev", "ask_peer")
    assert "without asking the user first" in fn["description"]


# ── «chi mi da' una mano con questa cosa?» ────────────────────────────────

def test_asking_everyone_exists(swarm):
    fn = _tool(swarm, "dev", "ask_everyone")
    assert "ALL colleagues" in fn["description"]
    assert "amanda = cerca offerte su amazon" in fn["description"]
    assert list(fn["parameters"]["properties"]) == ["question"]


def test_asking_everyone_returns_only_who_had_something_to_say(swarm, monkeypatch):
    """Chi non c'entra tace. Altrimenti torna indietro un coro di scuse."""
    risposte = {"amanda": "Su Amazon lo trovi a 79 euro.", "ciccio": "—"}
    chiamati = []

    def finto_speak(peer, text, **kwargs):
        chiamati.append(peer.name)
        return risposte[peer.name]

    monkeypatch.setattr(swarm, "_speak", finto_speak)
    out = swarm.ask_everyone(swarm.resolve("dev"), "chi mi aiuta a comprare un SSD?")

    assert sorted(chiamati) == ["amanda", "ciccio"], "non li ha sentiti tutti"
    assert "amanda (cerca offerte su amazon): Su Amazon lo trovi a 79 euro." in out
    assert "ciccio" not in out, "chi ha passato la mano non deve comparire"


def test_a_broadcast_tells_them_they_may_stay_silent(swarm, monkeypatch):
    visti = []
    monkeypatch.setattr(swarm, "_speak",
                        lambda peer, text, **kw: visti.append(text) or "—")
    swarm.ask_everyone(swarm.resolve("dev"), "chi se ne intende?")
    assert visti, "non ha sentito nessuno"
    for testo in visti:
        assert "asking the whole roster" in testo
        assert swarm.NOTHING in testo
        assert "do not apologise" in testo


def test_a_direct_question_does_not_invite_silence(swarm, monkeypatch):
    """Se lo chiedi a UNO, quello risponde: il permesso di tacere e' del broadcast."""
    visti = []
    monkeypatch.setattr(swarm, "_speak",
                        lambda peer, text, **kw: visti.append(text) or "eccomi")
    swarm.consult(swarm.resolve("dev"), "amanda", "quanto costa?")
    assert visti == ["dev asks you: quanto costa?"]


def test_everyone_silent_says_so_instead_of_pretending(swarm, monkeypatch):
    monkeypatch.setattr(swarm, "_speak", lambda peer, text, **kw: "—")
    out = swarm.ask_everyone(swarm.resolve("dev"), "qualcuno sa di astrofisica?")
    assert "Nobody answered" in out


def test_you_cannot_ask_yourself(swarm):
    with pytest.raises(SwarmError):
        swarm.consult(swarm.resolve("dev"), "dev", "ci sei?")


# ── la rubrica non e' mai fissa ───────────────────────────────────────────

def test_the_roster_is_read_fresh_every_time(swarm):
    """L'utente crea e cancella agenti quando vuole, anche a lavoro in corso.

    Se gli strumenti venissero costruiti una volta sola, un agente nato dopo
    sarebbe invisibile ai colleghi finche' non si riavvia tutto.
    """
    prima = _tool(swarm, "dev", "ask_peer")["parameters"]["properties"]["name"]["enum"]
    assert "meteo" not in prima

    swarm.store.create_agent("meteo", "previsioni del tempo", "", "", "")

    dopo = _tool(swarm, "dev", "ask_peer")["parameters"]["properties"]["name"]["enum"]
    assert "meteo" in dopo, "un agente nato adesso non e' arrivato ai colleghi"
    assert "meteo = previsioni del tempo" in _tool(swarm, "dev", "ask_peer")["description"]


def test_an_agent_can_ask_who_is_there_right_now(swarm):
    """Serve poterlo CHIEDERE: dentro un turno la lista e' ferma a quando e' iniziato."""
    fn = _tool(swarm, "dev", "who_is_there")
    assert fn["parameters"]["properties"] == {}
    assert "The roster changes" in fn["description"]

    swarm.store.create_agent("meteo", "previsioni del tempo", "", "", "")
    adesso = swarm.roster_text()
    assert "meteo" in adesso, "la risposta non riflette la rubrica di adesso"
    assert "amanda" in adesso and "ciccio" in adesso


def test_a_deleted_agent_disappears_from_the_choices(swarm):
    ids = {a["name"]: a["id"] for a in swarm.store.list_agents()}
    swarm.store.delete_agent(ids["amanda"])
    scelte = _tool(swarm, "dev", "ask_peer")["parameters"]["properties"]["name"]["enum"]
    assert "amanda" not in scelte


def test_naming_someone_who_is_gone_answers_with_who_is_there(swarm):
    """Il fallimento deve insegnare, non solo negare."""
    with pytest.raises(SwarmError) as caught:
        swarm.consult(swarm.resolve("dev"), "fantasma", "ci sei?")
    detto = str(caught.value)
    assert "amanda" in detto and "ciccio" in detto, detto
