"""Sciame: creazione, conversazione a uno/a tutti, discussione fra pari."""

import pytest

from core.swarm import Swarm, SwarmError


class _StubClient:
    """Client LLM finto: risponde citando l'ultimo messaggio ricevuto."""

    calls: list[list[dict]] = []

    def __init__(self, *_args, **_kwargs):
        self.max_tokens = 0
        self.temperature = 0.0

    def call(self, messages, **_kwargs):
        type(self).calls.append(messages)
        who = messages[0]["content"].split("'")[1]
        return f"{who} dice: {messages[-1]['content'][-40:]}"


@pytest.fixture
def swarm(tmp_path, monkeypatch):
    monkeypatch.setattr("core.llm.create_llm_client",
                        lambda **kwargs: _StubClient(**kwargs))
    _StubClient.calls = []
    # Roster vuoto: i DEFAULT_AGENTS della dashboard renderebbero ambigui i
    # test su "quanti membri ci sono".
    monkeypatch.setattr("core.chat_store.DEFAULT_AGENTS", (), raising=False)
    return Swarm(parent_agent=None, memory_dir=str(tmp_path))


def test_spawn_persists_across_instances(swarm, tmp_path):
    swarm.spawn("revisore", "trova i buchi nel ragionamento")
    revived = Swarm(parent_agent=None, memory_dir=str(tmp_path))
    assert "revisore" in revived.members
    assert revived.members["revisore"].role == "trova i buchi nel ragionamento"


def test_spawn_rejects_duplicates_and_enforces_limit(swarm, monkeypatch):
    swarm.spawn("revisore", "critica")
    with pytest.raises(SwarmError, match="esiste"):
        swarm.spawn("revisore", "altro")

    import config as cfg
    monkeypatch.setattr(cfg, "SWARM_MAX_AGENTS", 1, raising=False)
    with pytest.raises(SwarmError, match="pieno"):
        swarm.spawn("secondo", "altro ruolo")


def test_resolve_tolerates_extra_words(swarm):
    swarm.spawn("revisore", "critica")
    assert swarm.resolve("il-revisore").name == "revisore"
    with pytest.raises(SwarmError, match="Nessuno specialista"):
        swarm.resolve("archeologo")


def test_ask_one_and_broadcast_to_all(swarm):
    swarm.spawn("revisore", "critica")
    swarm.spawn("sicurezza", "guarda i rischi")

    single = swarm.ask("revisore", "questa idea regge?")
    assert single.startswith("revisore dice:")

    everyone = swarm.broadcast("questa idea regge?")
    assert set(everyone) == {"revisore", "sicurezza"}
    # Pareri indipendenti: nessuno dei due vede la risposta dell'altro.
    for messages in _StubClient.calls:
        assert "dice:" not in messages[-1]["content"]


def test_discuss_feeds_previous_turns_from_round_two(swarm):
    swarm.spawn("revisore", "critica")
    swarm.spawn("sicurezza", "rischi")

    transcript = swarm.discuss("vale la pena rilasciare oggi?", rounds=2)
    assert [e["round"] for e in transcript] == [1, 1, 2, 2]

    first_round_prompt = _StubClient.calls[0]
    assert all("Contesto della discussione" not in m["content"]
               for m in first_round_prompt)
    third_speaker_prompt = _StubClient.calls[2]
    assert any("Contesto della discussione" in m["content"]
               for m in third_speaker_prompt)

    rendered = Swarm.render_discussion(transcript)
    assert "Giro 1" in rendered and "Giro 2" in rendered


def test_discuss_needs_two_participants(swarm):
    swarm.spawn("revisore", "critica")
    with pytest.raises(SwarmError, match="almeno due"):
        swarm.discuss("qualcosa")


def test_daily_budget_stops_the_swarm(swarm, monkeypatch):
    import config as cfg
    swarm.spawn("revisore", "critica")
    monkeypatch.setattr(cfg, "SWARM_DAILY_CALL_BUDGET", 1, raising=False)
    swarm.ask("revisore", "prima domanda")
    with pytest.raises(SwarmError, match="Budget giornaliero"):
        swarm.ask("revisore", "seconda domanda")


def test_transcript_records_both_directions(swarm):
    swarm.spawn("revisore", "critica")
    swarm.ask("revisore", "domanda tracciata", sender="utente")
    entries = swarm.transcript(10)
    kinds = [e["kind"] for e in entries]
    assert "prompt" in kinds and "reply" in kinds
    reply = next(e for e in entries if e["kind"] == "reply")
    assert reply["from"] == "revisore"


def test_the_roster_is_shared_with_the_dashboard(swarm):
    """Un agente creato da CLI deve comparire nella rubrica web, e viceversa."""
    swarm.spawn("revisore", "critica")
    names = {row["name"] for row in swarm.store.agent_roster()}
    assert "revisore" in names

    swarm.store.create_agent("dalla-web", "creato dalla dashboard", "", "", "")
    assert "dalla-web" in swarm.members


def test_dismiss_disables_but_keeps_the_conversation(swarm):
    """Un agente congedato sparisce dal roster, la sua storia no."""
    member = swarm.spawn("revisore", "critica")
    swarm.ask("revisore", "un parere")
    assert swarm.dismiss("revisore") == "revisore"
    assert swarm.list_members() == []

    chat = swarm.store.direct_chat_for_agent(member.id)
    assert chat is not None
    assert swarm.store.list_messages(chat["id"])


# ── Strumenti ───────────────────────────────────────────────────────────

class _Tools:
    ALL = ["read_file", "grep", "find_files", "web_search", "web_fetch",
           "remember", "notify", "evolve_self", "shell", "request_restart",
           "doctor_fix", "read_self"]

    def names(self):
        return list(self.ALL)

    def to_openai_schema(self, names=None):
        return [{"type": "function", "function": {"name": n, "parameters": {}}}
                for n in sorted(names or [])]


class _Parent:
    def __init__(self):
        self.tools = _Tools()
        self.calls = []

    def _execute_tool(self, name, args, source):
        self.calls.append((name, args, source))
        return "risultato del tool"


def _tool_swarm(tmp_path, monkeypatch, client):
    monkeypatch.setattr("core.llm.create_llm_client", lambda **kw: client)
    monkeypatch.setattr("core.chat_store.DEFAULT_AGENTS", (), raising=False)
    parent = _Parent()
    swarm = Swarm(parent, memory_dir=str(tmp_path))
    swarm.spawn("meteo", "sa tutto del tempo")
    return swarm, parent


def test_agents_get_the_same_operational_tools_as_openvurp(tmp_path, monkeypatch):
    """Parita' operativa: quello che fai con openvurp lo fai anche qui."""
    swarm, _parent = _tool_swarm(tmp_path, monkeypatch, _StubClient())
    offered = swarm.tool_names()

    assert {"shell", "read_file", "grep", "find_files", "web_search",
            "web_fetch", "remember", "notify"}.issubset(offered)


def test_agents_still_cannot_rewrite_openvurp_itself(tmp_path, monkeypatch):
    """Potere operativo si', auto-modifica no.

    Un agente lo crei in trenta secondi da una modale: puo' agire sul mondo
    come openvurp, ma non riscrivere il runtime in cui vive.
    """
    swarm, _parent = _tool_swarm(tmp_path, monkeypatch, _StubClient())
    offered = swarm.tool_names()

    forbidden = {"evolve_self", "read_self",
                 "request_restart", "doctor_fix"}
    assert not (offered & forbidden), sorted(offered & forbidden)
    # E nemmeno convocare altri agenti: sarebbe una matrioska.
    assert not {n for n in offered if n.startswith(("swarm_", "subagent_"))}


def test_self_edit_can_be_unlocked_deliberately(tmp_path, monkeypatch):
    import config as cfg

    monkeypatch.setattr(cfg, "SWARM_TOOLS_ALLOW_SELF_EDIT", True, raising=False)
    swarm, _parent = _tool_swarm(tmp_path, monkeypatch, _StubClient())
    assert "evolve_self" in swarm.tool_names()


def test_an_explicit_list_narrows_the_tools(tmp_path, monkeypatch):
    import config as cfg

    monkeypatch.setattr(cfg, "SWARM_TOOLS", "read_file,web_search", raising=False)
    swarm, _parent = _tool_swarm(tmp_path, monkeypatch, _StubClient())
    assert swarm.tool_names() == {"read_file", "web_search"}


def test_an_agent_actually_calls_a_tool_and_the_audit_knows_who(tmp_path, monkeypatch):
    from core.llm import LLMResponse, ToolCall

    class _WithTools:
        backend = "openai"
        supports_function_calling = True
        supports_tool_transport = True

        def __init__(self, **_kw):
            self.max_tokens = 0
            self.temperature = 0.0
            self.turn = 0

        def call_with_tools(self, messages, schema):
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(text="", tool_calls=[
                    ToolCall(id="1", name="web_search", args={"query": "meteo"})])
            return LLMResponse(text="Domani sereno.")

        def call(self, messages, **_kw):
            return "(senza tool)"

    swarm, parent = _tool_swarm(tmp_path, monkeypatch, _WithTools())
    reply = swarm.ask("meteo", "che tempo fa?")

    assert reply == "Domani sereno."
    assert parent.calls, "l'agente non ha usato nessun tool"
    name, _args, source = parent.calls[0]
    assert name == "web_search"
    # L'audit deve poter dire QUALE agente ha agito, non solo "un agente".
    assert source == "agent:meteo"


def test_the_agent_activity_is_published_into_its_own_chat(tmp_path, monkeypatch):
    """Devi vedere cosa fa l'agente, e nella conversazione giusta.

    openvurp pubblica ogni azione leggendo il route attivo: senza spostarlo,
    le azioni di un agente della rubrica finirebbero nella chat sbagliata.
    """
    from core.llm import LLMResponse, ToolCall

    published = []

    class _Recording(_Parent):
        def __init__(self):
            super().__init__()
            self._active_route = None
            self._active_channel = "cli"

        def _execute_tool(self, name, args, source):
            published.append(getattr(self._active_route, "chat_id", ""))
            return super()._execute_tool(name, args, source)

    class _Client:
        backend = "openai"
        supports_function_calling = True
        supports_tool_transport = True

        def __init__(self, **_kw):
            self.max_tokens = 0
            self.temperature = 0.0
            self.turn = 0

        def call_with_tools(self, messages, schema):
            self.turn += 1
            if self.turn == 1:
                return LLMResponse(text="", tool_calls=[
                    ToolCall(id="1", name="shell", args={"command": "ls"})])
            return LLMResponse(text="fatto")

        def call(self, messages, **_kw):
            return "(senza tool)"

    monkeypatch.setattr("core.llm.create_llm_client", lambda **kw: _Client(**kw))
    monkeypatch.setattr("core.chat_store.DEFAULT_AGENTS", (), raising=False)
    parent = _Recording()
    swarm = Swarm(parent, memory_dir=str(tmp_path))
    member = swarm.spawn("ciccio", "bollette")
    chat = swarm.store.direct_chat_for_agent(member.id)

    swarm.ask("ciccio", "guarda la cartella")

    assert published == [chat["id"]], "l'attivita' non finisce nella chat dell'agente"
    # E il route torna com'era: non deve restare puntato altrove.
    assert parent._active_route is None


def test_without_a_parent_agent_it_still_answers_without_tools(tmp_path, monkeypatch):
    """La dashboard puo' costruire lo sciame prima dell'agente: non deve esplodere."""
    monkeypatch.setattr("core.llm.create_llm_client", lambda **kw: _StubClient())
    monkeypatch.setattr("core.chat_store.DEFAULT_AGENTS", (), raising=False)
    swarm = Swarm(None, memory_dir=str(tmp_path))
    swarm.spawn("meteo", "tempo")
    assert swarm.tool_names() == set()
    assert swarm.ask("meteo", "ciao")


# ── Conversazione fra agenti ────────────────────────────────────────────

def _peer_setup(tmp_path, monkeypatch, client):
    monkeypatch.setattr("core.llm.create_llm_client", lambda **kw: client)
    monkeypatch.setattr("core.chat_store.DEFAULT_AGENTS", (), raising=False)
    swarm = Swarm(_Parent(), memory_dir=str(tmp_path))
    swarm.spawn("ciccio", "bollette")
    swarm.spawn("meteo", "previsioni del tempo")
    return swarm


class _PeerClient:
    """Il primo interpellato gira la domanda; il secondo risponde."""

    backend = "openai"
    supports_function_calling = True
    supports_tool_transport = True
    seen: dict = {}

    def __init__(self, **_kw):
        self.max_tokens = 0
        self.temperature = 0.0

    def call_with_tools(self, messages, schema):
        from core.llm import LLMResponse, ToolCall

        who = messages[0]["content"].split("'")[1]
        type(self).seen.setdefault(who, [f["function"]["name"] for f in schema])
        if who == "meteo":
            return LLMResponse(text="Domani sereno.")
        if not any(m.get("role") == "tool_result" for m in messages):
            return LLMResponse(text="", tool_calls=[ToolCall(
                id="1", name="ask_peer",
                args={"name": "meteo", "question": "domani piove?"})])
        return LLMResponse(text="Ho chiesto a meteo: sereno.")

    def call(self, messages, **_kw):
        return "(senza tool)"


def test_an_agent_can_hand_the_question_to_the_right_peer(tmp_path, monkeypatch):
    _PeerClient.seen = {}
    swarm = _peer_setup(tmp_path, monkeypatch, _PeerClient())
    reply = swarm.ask("ciccio", "devo uscire domani?")

    assert "meteo" in reply
    assert "ask_peer" in _PeerClient.seen["ciccio"]


def test_the_consulted_peer_cannot_bounce_it_back(tmp_path, monkeypatch):
    """Due agenti che si rimpallano la palla sono un ciclo, non collaborazione."""
    _PeerClient.seen = {}
    swarm = _peer_setup(tmp_path, monkeypatch, _PeerClient())
    swarm.ask("ciccio", "devo uscire domani?")

    assert "ask_peer" not in _PeerClient.seen["meteo"]


def test_the_exchange_stays_visible_in_the_conversation(tmp_path, monkeypatch):
    """Devi poter vedere CHI ha chiesto a CHI, non solo la risposta finale."""
    _PeerClient.seen = {}
    swarm = _peer_setup(tmp_path, monkeypatch, _PeerClient())
    asker = swarm.resolve("ciccio")
    swarm.ask("ciccio", "devo uscire domani?")

    chat = swarm.store.direct_chat_for_agent(asker.id)
    exchange = [m for m in swarm.store.list_messages(chat["id"])
                if (m.get("metadata") or {}).get("peer")]
    assert len(exchange) == 2
    ask, answer = exchange
    assert ask["metadata"]["direction"] == "ask"
    assert answer["metadata"]["direction"] == "answer"
    assert ask["metadata"]["peer"]["from_name"] == "ciccio"
    assert ask["metadata"]["peer"]["to_name"] == "meteo"


def test_an_agent_alone_gets_no_peer_tool(tmp_path, monkeypatch):
    _PeerClient.seen = {}
    monkeypatch.setattr("core.llm.create_llm_client", lambda **kw: _PeerClient())
    monkeypatch.setattr("core.chat_store.DEFAULT_AGENTS", (), raising=False)
    swarm = Swarm(_Parent(), memory_dir=str(tmp_path))
    swarm.spawn("solo", "unico")
    swarm.ask("solo", "ciao")
    assert "ask_peer" not in _PeerClient.seen["solo"]


# ── Chiacchiere spontanee ───────────────────────────────────────────────

class _Chatty:
    """Risponde con qualcosa e registra cosa gli e' stato chiesto."""

    prompts: list[str] = []

    def __init__(self, **_kw):
        self.max_tokens = 0
        self.temperature = 0.0

    def call(self, messages, **_kw):
        type(self).prompts.append(messages[-1]["content"])
        return "una battuta"

    def call_with_tools(self, messages, schema):  # non deve mai servire
        raise AssertionError("le chiacchiere non devono usare strumenti")


def _chatty_swarm(tmp_path, monkeypatch, names=("ciccio", "meteo")):
    monkeypatch.setattr("core.llm.create_llm_client", lambda **kw: _Chatty(**kw))
    monkeypatch.setattr("core.chat_store.DEFAULT_AGENTS", (), raising=False)
    _Chatty.prompts = []
    swarm = Swarm(_Parent(), memory_dir=str(tmp_path))
    for name in names:
        swarm.spawn(name, "un ruolo")
    return swarm


def test_two_agents_say_something_without_being_asked(tmp_path, monkeypatch):
    swarm = _chatty_swarm(tmp_path, monkeypatch)
    exchange = swarm.small_talk()

    assert len(exchange) == 2
    assert exchange[0]["author_name"] != exchange[1]["author_name"]
    # Il secondo risponde davvero al primo, non parla per conto suo.
    assert "ha detto" in _Chatty.prompts[1]


def test_nobody_suggests_them_a_topic(tmp_path, monkeypatch):
    """Argomento e motivo sono loro. Una lista di spunti sarebbe un copione."""
    swarm = _chatty_swarm(tmp_path, monkeypatch)
    swarm.small_talk()

    apertura = _Chatty.prompts[0]
    assert "Se ti va di dire qualcosa" in apertura
    assert "su quello che vuoi" in apertura
    # Nessuna traccia di argomenti imposti da noi.
    for copione in ("Di' una cosa", "Chiedi a", "Racconta in due righe",
                    "Fai un'osservazione"):
        assert copione not in apertura


def test_an_agent_with_nothing_to_say_stays_quiet(tmp_path, monkeypatch):
    """Il silenzio deve essere una risposta possibile, non un turno da riempire."""
    class _Silent(_Chatty):
        def call(self, messages, **_kw):
            type(self).prompts.append(messages[-1]["content"])
            return "—"

    monkeypatch.setattr("core.llm.create_llm_client", lambda **kw: _Silent(**kw))
    monkeypatch.setattr("core.chat_store.DEFAULT_AGENTS", (), raising=False)
    _Silent.prompts = []
    swarm = Swarm(_Parent(), memory_dir=str(tmp_path))
    swarm.spawn("ciccio", "bollette")
    swarm.spawn("meteo", "previsioni")

    assert swarm.small_talk() == []
    room = swarm.store.team_room()
    assert swarm.store.list_messages(room["id"]) == []


def test_the_opener_can_be_left_hanging(tmp_path, monkeypatch):
    """Se l'altro non raccoglie, resta quello che ha detto il primo."""
    replies = iter(["Ho pensato a una cosa strana.", "—"])

    class _Half(_Chatty):
        def call(self, messages, **_kw):
            type(self).prompts.append(messages[-1]["content"])
            return next(replies)

    monkeypatch.setattr("core.llm.create_llm_client", lambda **kw: _Half(**kw))
    monkeypatch.setattr("core.chat_store.DEFAULT_AGENTS", (), raising=False)
    _Half.prompts = []
    swarm = Swarm(_Parent(), memory_dir=str(tmp_path))
    swarm.spawn("ciccio", "bollette")
    swarm.spawn("meteo", "previsioni")

    exchange = swarm.small_talk()
    assert len(exchange) == 1
    assert exchange[0]["content"] == "Ho pensato a una cosa strana."


def test_small_talk_lands_in_the_shared_room_and_is_marked(tmp_path, monkeypatch):
    """Deve finire dove lo trovi se ti va, e si deve capire che non l'hai chiesto tu."""
    swarm = _chatty_swarm(tmp_path, monkeypatch)
    swarm.small_talk()

    room = swarm.store.team_room()
    messages = swarm.store.list_messages(room["id"])
    assert messages
    assert all((m.get("metadata") or {}).get("idle") for m in messages)


def test_small_talk_needs_at_least_two_agents(tmp_path, monkeypatch):
    swarm = _chatty_swarm(tmp_path, monkeypatch, names=("solo",))
    assert swarm.small_talk() == []
    assert _Chatty.prompts == []


def test_small_talk_respects_the_daily_budget(tmp_path, monkeypatch):
    """E' rumore di fondo: non deve poter bruciare la giornata."""
    import config as cfg

    swarm = _chatty_swarm(tmp_path, monkeypatch)
    monkeypatch.setattr(cfg, "SWARM_DAILY_CALL_BUDGET", 1, raising=False)
    swarm.small_talk()
    # Esaurito il budget la chiacchiera si ferma da sola, senza esplodere.
    assert swarm.small_talk() == []


def test_it_can_be_switched_off(tmp_path, monkeypatch):
    import config as cfg

    swarm = _chatty_swarm(tmp_path, monkeypatch)
    monkeypatch.setattr(cfg, "SWARM_IDLE_CHAT", False, raising=False)
    assert swarm.start_small_talk() is None


# ── Streaming ───────────────────────────────────────────────────────────

def test_the_reply_arrives_in_pieces_not_in_one_block(tmp_path, monkeypatch):
    """Senza questo l'agente aspetta tutto e poi consegna: da fuori sembra bloccato."""
    import threading
    import time as _t

    from core import activity

    class _Streaming:
        backend = "codex"
        supports_function_calling = False
        supports_tool_transport = False

        def __init__(self, **_kw):
            self.max_tokens = 0
            self.temperature = 0.0

        def call_streamed(self, messages, on_text=None, **_kw):
            text = ""
            for piece in ("Domani ", "sereno, ", "22 gradi."):
                text += piece
                if on_text:
                    on_text(piece)
            return text

        def call(self, messages, **_kw):
            return "tutto insieme"

    monkeypatch.setattr("core.llm.create_llm_client", lambda **kw: _Streaming(**kw))
    monkeypatch.setattr("core.chat_store.DEFAULT_AGENTS", (), raising=False)
    swarm = Swarm(None, memory_dir=str(tmp_path))
    member = swarm.spawn("meteo", "previsioni")
    chat = swarm.store.direct_chat_for_agent(member.id)

    events = []
    queue, _snapshot = activity.subscribe()

    def collect():
        while True:
            try:
                events.append(queue.get(timeout=2))
            except Exception:
                return

    worker = threading.Thread(target=collect, daemon=True)
    worker.start()
    swarm.ask("meteo", "che tempo fa?")
    _t.sleep(0.4)

    tokens = [e for e in events if e.get("kind") == "token"]
    assert [e["text"] for e in tokens] == ["Domani ", "sereno, ", "22 gradi."]
    # E devono finire nella conversazione di QUEL agente.
    assert all(e.get("chat_id") == chat["id"] for e in tokens)
    assert any(e.get("kind") == "assistant_end" for e in events)


def test_what_the_agent_did_is_saved_with_the_message(tmp_path, monkeypatch):
    """I passaggi devono sopravvivere al ricaricamento.

    Prima vivevano solo durante lo streaming: appena la risposta veniva riletta
    dal database sparivano, e non c'era piu' modo di sapere se l'agente avesse
    davvero eseguito qualcosa o se se lo fosse inventato.
    """
    from core.llm import LLMResponse, ToolCall

    turns = {"n": 0}

    class _Worker:
        backend = "openai"
        supports_function_calling = True
        supports_tool_transport = True

        def __init__(self, **_kw):
            self.max_tokens = 0
            self.temperature = 0.0

        def call_with_tools(self, messages, schema):
            turns["n"] += 1
            if turns["n"] == 1:
                return LLMResponse(text="", tool_calls=[ToolCall(
                    id="1", name="shell", args={"command": "wmic diskdrive get model"})])
            return LLMResponse(text="E' un Patriot P300.")

        def call(self, messages, **_kw):
            return "x"

    monkeypatch.setattr("core.llm.create_llm_client", lambda **kw: _Worker(**kw))
    monkeypatch.setattr("core.chat_store.DEFAULT_AGENTS", (), raising=False)
    swarm = Swarm(_Parent(), memory_dir=str(tmp_path))
    member = swarm.spawn("dev", "sviluppo")

    swarm.ask("dev", "che ssd ho?")

    chat = swarm.store.direct_chat_for_agent(member.id)
    reply = swarm.store.list_messages(chat["id"])[-1]
    steps = (reply.get("metadata") or {}).get("steps") or []

    assert steps, "nessun passaggio salvato: dalla chat non si capisce cosa ha fatto"
    assert steps[0]["tool"] == "shell"
    assert "wmic diskdrive" in steps[0]["args"]
    # E anche cosa ha risposto il comando, non solo che e' stato lanciato.
    assert steps[0]["out"]


def test_a_consultation_does_not_erase_the_asker_steps(tmp_path, monkeypatch):
    """Chi persiste il messaggio legge `last_steps`: una consulenza annidata
    non deve sovrascriverli con quelli — di solito vuoti — del collega.

    Era esattamente questo a far sparire tutte le azioni dalla chat: `dev`
    eseguiva un comando, consultava `amanda`, e nel farlo cancellava la
    memoria di quello che aveva appena fatto.
    """
    from core.llm import LLMResponse, ToolCall

    turns = {"n": 0}

    class _Both:
        backend = "openai"
        supports_function_calling = True
        supports_tool_transport = True

        def __init__(self, **_kw):
            self.max_tokens = 0
            self.temperature = 0.0

        def call_with_tools(self, messages, schema):
            who = messages[0]["content"].split("'")[1]
            if who == "amanda":
                return LLMResponse(text="Samsung 990 Pro.")
            turns["n"] += 1
            if turns["n"] == 1:
                return LLMResponse(text="", tool_calls=[ToolCall(
                    id="1", name="shell", args={"command": "lsblk"})])
            if turns["n"] == 2:
                return LLMResponse(text="", tool_calls=[ToolCall(
                    id="2", name="ask_peer",
                    args={"name": "amanda", "question": "un sostituto?"})])
            return LLMResponse(text="Patriot P300, e amanda dice Samsung.")

        def call(self, messages, **_kw):
            return "x"

    monkeypatch.setattr("core.llm.create_llm_client", lambda **kw: _Both(**kw))
    monkeypatch.setattr("core.chat_store.DEFAULT_AGENTS", (), raising=False)
    swarm = Swarm(_Parent(), memory_dir=str(tmp_path))
    swarm.spawn("dev", "sviluppo")
    swarm.spawn("amanda", "acquisti")

    swarm.ask("dev", "che ssd ho? e un sostituto?", persist=False)

    tools_used = [s["tool"] for s in swarm.last_steps]
    assert tools_used == ["shell", "ask_peer"], tools_used
    assert swarm.last_steps[0]["out"], "manca l'output del comando"
