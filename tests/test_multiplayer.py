import tempfile
from unittest.mock import patch

from core.chat_store import ChatStore
from core.multiplayer import MultiplayerCoordinator


class FakeLLM:
    max_tokens = 0
    temperature = 0

    def call_with_timing(self, messages):
        system = messages[0]["content"]
        name = system.split(",", 1)[0].replace("Sei ", "")
        # Lunghezza realistica: il secondo giro parte solo se il primo ha
        # prodotto abbastanza materia, e tre parole non sono una posizione.
        self.giri = getattr(self, "giri", 0) + 1
        return (
            f"Contributo di {name}: la strada e' quella diretta, ma il costo "
            f"non e' sul tavolo. Al giro {self.giri} porto un argomento "
            f"diverso per non ripetermi."
        ), 2, 100, 20


def test_team_agents_exchange_messages_and_build_brief():
    """Ogni agente parla a ogni giro, e legge chi ha parlato prima.

    Prima era 3 contributi in parallelo + 1 revisione: nessuno vedeva gli
    altri mentre scriveva, e uscivano monologhi affiancati. Ora la stanza e'
    una discussione, quindi i turni sono agenti x giri.
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = ChatStore(tmp)
        for name, role in (("Ricercatore", "researcher"), ("Costruttore", "builder"),
                           ("Revisore", "reviewer")):
            store.create_agent(name, role, "", "", "")
        chat = store.create_chat(mode="team")
        store.set_chat_agents(chat["id"], [a["id"] for a in store.list_agents()])
        store.add_message(chat["id"], "user", "progetta la soluzione")
        with patch("core.multiplayer.create_llm_client", return_value=FakeLLM()):
            result = MultiplayerCoordinator(store).collaborate(
                chat["id"], "progetta la soluzione", run_id="run_test",
            )

        # La discussione dura quanto hanno da dire: non si conta piu' un numero
        # di giri deciso a tavolino, si controlla che il confronto sia avvenuto.
        assert len(result.messages) > 3
        assert sorted({m["metadata"]["round"] for m in result.messages})[:2] == [1, 2]
        assert "DISCUSSIONE DELLA STANZA" in result.brief
        # L'ultimo messaggio e' la chiusura: costa una chiamata come le altre.
        assert result.messages[-1]["metadata"].get("closing")
        assert result.input_tokens == 100 * len(result.messages)
        persisted = store.list_messages(chat["id"])
        assert sum(m["author_type"] == "agent"
                   for m in persisted) == len(result.messages)


def test_team_peer_can_use_automatic_router():
    with tempfile.TemporaryDirectory() as tmp:
        store = ChatStore(tmp)
        profile = store.create_agent(
            "Coder", "builder", "Implementa", backend="auto", model="",
        )
        coordinator = MultiplayerCoordinator(store)
        route = type("Route", (), {
            "backend": "codex", "model": "gpt-5.6-luna",
        })()
        with patch("core.model_router.route_chat_prompt", return_value=route), \
                patch("core.multiplayer.create_llm_client", return_value=FakeLLM()) as factory:
            coordinator._client(profile, "sistema questo bug")
        factory.assert_called_once_with(backend="codex", model="gpt-5.6-luna")
