import tempfile
import json
import os

from core.chat_store import ChatStore


def test_chat_crud_and_message_isolation():
    with tempfile.TemporaryDirectory() as tmp:
        store = ChatStore(tmp)
        first = store.create_chat()
        second = store.create_chat(title="Seconda")
        store.add_message(first["id"], "user", "primo messaggio")
        store.add_message(second["id"], "user", "altro")

        assert store.get_chat(first["id"])["title"] == "primo messaggio"
        assert [m["content"] for m in store.list_messages(first["id"])] == ["primo messaggio"]
        assert [m["content"] for m in store.list_messages(second["id"])] == ["altro"]

        store.update_chat(first["id"], archived=True)
        assert first["id"] not in {chat["id"] for chat in store.list_chats()}


def test_chat_and_agents_persist_independent_providers():
    with tempfile.TemporaryDirectory() as tmp:
        store = ChatStore(tmp)
        chat = store.create_chat(backend="codex", model="gpt-5.6-luna")
        assert chat["backend"] == "codex"
        assert chat["model"] == "gpt-5.6-luna"
        updated = store.update_chat(chat["id"], backend="claude_cli", model="sonnet")
        assert (updated["backend"], updated["model"]) == ("claude_cli", "sonnet")

        agent = store.create_agent("Codificatore", "builder", "Costruisci", "codex", "")
        edited = store.update_agent(agent["id"], backend="ollama", model="qwen")
        assert (edited["backend"], edited["model"]) == ("ollama", "qwen")


def test_team_chat_starts_without_anyone_in_the_room():
    """La rubrica nasce vuota: gli interlocutori li sceglie l'utente.

    Prima openvurp installava tre agenti di default (Ricercatore, Costruttore,
    Revisore). Decidere al posto dell'utente chi gli serve non e' un servizio:
    e' riempirgli la rubrica di gente che non ha chiesto.
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = ChatStore(tmp)
        assert store.list_agents() == []

        chat = store.create_chat(mode="team")
        assert store.chat_agents(chat["id"]) == []

        # Appena ne crei uno, la stanza lo accoglie.
        agent = store.create_agent("meteo", "sa tutto del tempo", "", "", "")
        store.set_chat_agents(chat["id"], [agent["id"]])
        assert [a["name"] for a in store.chat_agents(chat["id"])] == ["meteo"]


def test_legacy_single_dashboard_chat_is_migrated_once():
    with tempfile.TemporaryDirectory() as tmp:
        legacy_dir = os.path.join(tmp, "session_store")
        os.makedirs(legacy_dir)
        with open(os.path.join(legacy_dir, "dashboard_sender_dashboard.json"), "w", encoding="utf-8") as handle:
            json.dump({
                "updated_at": "2026-01-01T00:00:00+00:00",
                "recent_messages": [
                    {"role": "user", "preview": "vecchia domanda"},
                    {"role": "assistant", "preview": "vecchia risposta"},
                ],
            }, handle)
        store = ChatStore(tmp)
        chats = store.list_chats()
        assert [chat["id"] for chat in chats] == ["chat_legacy_dashboard"]
        assert [m["content"] for m in store.list_messages(chats[0]["id"])] == [
            "vecchia domanda", "vecchia risposta",
        ]
        assert len(ChatStore(tmp).list_chats()) == 1


# ── Rubrica agenti (la vista da chat) ───────────────────────────────────

def test_direct_chat_is_created_once_per_agent(tmp_path):
    store = ChatStore(str(tmp_path))
    agent = store.create_agent("revisore", "critica", "", "codex", "gpt-5.6-luna")

    first = store.direct_chat_for_agent(agent["id"])
    second = store.direct_chat_for_agent(agent["id"])
    assert first["id"] == second["id"]
    assert first["direct_agent_id"] == agent["id"]
    # Il motore dell'agente diventa quello della sua chat.
    assert first["backend"] == "codex" and first["model"] == "gpt-5.6-luna"
    # L'agente e' gia' dentro la sua stanza.
    assert [a["id"] for a in store.chat_agents(first["id"])] == [agent["id"]]


def test_direct_chat_for_unknown_agent_is_none(tmp_path):
    assert ChatStore(str(tmp_path)).direct_chat_for_agent("agent_inesistente") is None


def test_roster_carries_last_message_and_engine(tmp_path):
    store = ChatStore(str(tmp_path))
    agent = store.create_agent("everyday driver", "tuttofare", "", "codex", "gpt-5.6-luna")
    chat = store.direct_chat_for_agent(agent["id"])
    store.add_message(chat["id"], "assistant", "morning brief pronto",
                      author_type="agent", author_id=agent["id"],
                      author_name="everyday driver")

    row = next(r for r in store.agent_roster() if r["id"] == agent["id"])
    assert row["preview"] == "morning brief pronto"
    assert row["model"] == "gpt-5.6-luna"
    assert row["message_count"] == 1
    assert row["chat_id"] == chat["id"]


def test_roster_lists_agents_without_a_chat_too(tmp_path):
    """Un agente appena creato deve comparire, non apparire solo dopo il primo messaggio."""
    store = ChatStore(str(tmp_path))
    agent = store.create_agent("nuovo", "appena nato", "", "", "")
    row = next(r for r in store.agent_roster() if r["id"] == agent["id"])
    assert row["preview"] == ""
    assert row["message_count"] == 0


def test_roster_puts_recent_conversations_first(tmp_path):
    store = ChatStore(str(tmp_path))
    old = store.create_agent("vecchio", "r", "", "", "")
    new = store.create_agent("recente", "r", "", "", "")
    store.direct_chat_for_agent(old["id"])
    chat = store.direct_chat_for_agent(new["id"])
    store.add_message(chat["id"], "user", "ciao", author_type="user")

    order = [r["id"] for r in store.agent_roster()]
    assert order.index(new["id"]) < order.index(old["id"])


def test_disabled_agents_leave_the_roster(tmp_path):
    store = ChatStore(str(tmp_path))
    agent = store.create_agent("congedato", "r", "", "", "")
    store.update_agent(agent["id"], enabled=False)
    assert agent["id"] not in {r["id"] for r in store.agent_roster()}
    assert agent["id"] in {r["id"] for r in store.agent_roster(enabled_only=False)}


# ── Non letti e stanza comune ───────────────────────────────────────────

def test_unread_counts_only_what_the_agent_said(tmp_path):
    store = ChatStore(str(tmp_path))
    agent = store.create_agent("deep work", "analisi", "", "", "")
    chat = store.direct_chat_for_agent(agent["id"])

    store.add_message(chat["id"], "user", "una domanda", author_type="user")
    store.add_message(chat["id"], "assistant", "prima", author_type="agent",
                      author_id=agent["id"], author_name="deep work")
    store.add_message(chat["id"], "assistant", "seconda", author_type="agent",
                      author_id=agent["id"], author_name="deep work")

    row = next(r for r in store.agent_roster() if r["id"] == agent["id"])
    # Quello che hai scritto tu non ti torna indietro come "non letto".
    assert row["unread"] == 2

    store.mark_read(chat["id"])
    row = next(r for r in store.agent_roster() if r["id"] == agent["id"])
    assert row["unread"] == 0


def test_new_messages_after_reading_count_again(tmp_path):
    store = ChatStore(str(tmp_path))
    agent = store.create_agent("revisore", "critica", "", "", "")
    chat = store.direct_chat_for_agent(agent["id"])
    store.mark_read(chat["id"])
    store.add_message(chat["id"], "assistant", "ho trovato un caso limite",
                      author_type="agent", author_id=agent["id"], author_name="revisore")
    assert store.chat_activity(chat["id"])["unread"] == 1


def test_team_room_is_unique_and_absorbs_new_agents(tmp_path):
    store = ChatStore(str(tmp_path))
    first = store.team_room()
    assert first["mode"] == "team"
    assert first["title"] == ChatStore.TEAM_ROOM_TITLE
    before = len(store.chat_agents(first["id"]))

    store.create_agent("arrivato dopo", "r", "", "", "")
    again = store.team_room()
    assert again["id"] == first["id"], "la stanza deve restare una sola"
    # "Tutti insieme" deve voler dire tutti, anche chi è arrivato dopo.
    assert len(store.chat_agents(again["id"])) == before + 1


def test_team_room_is_not_created_when_only_asked_for(tmp_path):
    assert ChatStore(str(tmp_path)).team_room(create=False) is None
