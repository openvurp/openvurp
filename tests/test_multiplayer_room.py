"""La stanza deve essere una discussione, non N monologhi affiancati.

Due guasti reali, osservati insieme in una sessione:

1. Gli agenti "parlavano" senza vedersi: il primo giro girava in PARALLELO e
   ognuno riceveva solo la cronologia della stanza, mai i contributi degli
   altri. Uscivano tre proposte scollegate (Nexus / Faro / Orizzonte) che non
   si citavano a vicenda.
2. La sintesi finale annunciava «il nome scelto all'unanimita' e' Lume» — un
   nome che NESSUNO aveva proposto, presentato come decisione del gruppo.
"""

import tempfile

import pytest

from core.chat_store import ChatStore
import core.multiplayer as M


class _Stub:
    """Cattura i prompt: e' li' che si vede se un agente ha letto gli altri."""

    captured: list[list[dict]] = []

    def __init__(self, **_kwargs):
        self.max_tokens = 0
        self.temperature = 0.0

    def call_with_timing(self, messages, **_kwargs):
        type(self).captured.append(messages)
        who = messages[0]["content"].split(",")[0].replace("Sei ", "")
        return (
                f"Come {who} la vedo cosi': la proposta regge, ma il costo "
                f"non e' quantificato. Punto numero {len(type(self).captured)}, "
                f"diverso dai precedenti, per tenere vivo il confronto."
            ), 10, 5, 5


@pytest.fixture
def room(monkeypatch, tmp_path):
    # Il modulo importa create_llm_client in cima: va sostituito LI', non in
    # core.llm, altrimenti la patch non ha effetto e partono chiamate vere.
    monkeypatch.setattr(M, "create_llm_client", lambda **kw: _Stub(**kw))
    _Stub.captured = []
    store = ChatStore(str(tmp_path))
    # La rubrica nasce vuota: gli interlocutori li mette il test, come l'utente.
    for name, role in (("Ricercatore", "researcher"), ("Costruttore", "builder"),
                       ("Revisore", "reviewer")):
        store.create_agent(name, role, "", "", "")
    chat = store.create_chat(title="Tutti insieme", mode="team")
    store.set_chat_agents(chat["id"], [a["id"] for a in store.list_agents()])
    return store, chat["id"]


def _prompt(index: int) -> str:
    return _Stub.captured[index][-1]["content"]


def test_everyone_after_the_first_reads_what_was_already_said(room):
    store, chat_id = room
    M.MultiplayerCoordinator(store).collaborate(chat_id, "scegliete un nome")

    assert len(_Stub.captured) >= 4, "servono piu' giri per avere una discussione"
    # Il primo non ha nessuno da leggere: e' corretto che parta senza contesto.
    assert "detto finora" not in _prompt(0)
    # Tutti gli altri, invece, devono avere davanti chi ha gia' parlato.
    for i in range(1, len(_Stub.captured)):
        if "Scrivi la chiusura" in _prompt(i):
            continue   # la chiusura riceve il tavolo intero, non il "detto finora"
        assert "detto finora" in _prompt(i), f"l'intervento {i + 1} parla al buio"


def test_second_round_asks_them_to_answer_each_other(room):
    store, chat_id = room
    M.MultiplayerCoordinator(store).collaborate(chat_id, "scegliete un nome")
    later = [_prompt(i) for i in range(len(_Stub.captured))]
    assert any("Giro 2" in p for p in later)
    # Il disaccordo va rivolto a qualcuno, non lasciato in aria...
    assert any("dillo agli altri per nome" in p for p in later)
    # ...e va detto anche cosa servirebbe per scioglierlo, altrimenti la
    # discussione non converge mai: e' il difetto per cui litigavano a oltranza.
    assert any("COSA SERVIREBBE per scioglierlo" in p for p in later)


def test_the_brief_forbids_inventing_a_conclusion(room):
    """Il guasto peggiore: annunciare un nome che nessuno aveva proposto."""
    store, chat_id = room
    result = M.MultiplayerCoordinator(store).collaborate(chat_id, "scegliete un nome")

    assert "NON introdurre opzioni che nessuno ha proposto" in result.brief
    assert "NON dichiarare accordo o unanimita'" in result.brief
    assert "Attribuisci ogni posizione a chi l'ha espressa" in result.brief


def test_every_turn_is_saved_with_its_round(room):
    store, chat_id = room
    result = M.MultiplayerCoordinator(store).collaborate(chat_id, "scegliete un nome")
    rounds = sorted({m["metadata"]["round"] for m in result.messages})
    assert rounds[:2] == [1, 2]
    assert len({m["author_name"] for m in result.messages}) >= 2


def test_default_agents_have_distinct_characters():
    """Tre ruoli con lo stesso tono producono tre risposte identiche e educate."""
    store = ChatStore(tempfile.mkdtemp())
    characters = {}
    for agent in store.list_agents():
        lines = [l for l in agent["instructions"].splitlines()
                 if l.startswith("Carattere:")]
        assert lines, f"{agent['name']} non ha un carattere"
        characters[agent["name"]] = lines[0]
    assert len(set(characters.values())) == len(characters), "caratteri duplicati"
