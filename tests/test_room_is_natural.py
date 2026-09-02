"""La stanza deve suonare come un gruppo, non come un verbale.

Trascritto reale, dopo un «ciao ragazzi» all'intera stanza:

    amanda:  Per ora nessun disaccordo: non c'e' ancora una richiesta concreta.
    ciccio:  Sono d'accordo con voi. Non c'e' ancora una richiesta concreta,
             quindi la conclusione non cambia.
    dev:     Concordo sul fatto che non ci sia ancora una richiesta concreta.
             Conclusione: ci siamo presentati.
    openvurp: I ragazzi sono pronti: che combiniamo oggi?

Due guasti distinti in quattro righe:

1. A un saluto veniva applicata la procedura del dibattito — due giri, con
   l'ordine esplicito di dire «dove NON sei d'accordo e cosa cambia nella
   conclusione». I tre hanno ubbidito nell'unico modo possibile: dichiarando
   di essere d'accordo sul fatto che non c'era niente su cui esserlo.
2. In coda parlava openvurp, che l'utente non ha creato e non vuole come
   agente, riassumendo ai suoi tre agenti quello che avevano appena detto.
"""

import threading
import tempfile

import pytest

import dashboard
from core.chat_store import ChatStore
import core.multiplayer as M


class _Stub:
    """Cattura i prompt: e' li' che si vede cosa gli stiamo ordinando di dire."""

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
    monkeypatch.setattr(M, "create_llm_client", lambda **kw: _Stub(**kw))
    _Stub.captured = []
    store = ChatStore(str(tmp_path))
    for name in ("amanda", "ciccio", "dev"):
        store.create_agent(name, name, "", "", "")
    chat = store.create_chat(title="tutti", mode="team")
    store.set_chat_agents(chat["id"], [a["id"] for a in store.list_agents()])
    return store, chat["id"]


# ── 1. il codice non decide che tipo di messaggio sia ─────────────────────

def test_no_keyword_classifier_survives():
    """Il riconoscitore sbagliava su frasi che un modello capisce al primo colpo.

    Falliva su «io vado a dormire ragazzi» (nessun saluto in testa) e su
    «amanda come stai tutto bene in mezzo a questi uomini?». Capire che tipo
    di messaggio sia e' esattamente il mestiere del modello: al codice resta
    solo di non obbligarlo a parlare.
    """
    assert not hasattr(M, "is_chit_chat")
    for gone in ("OPENERS", "SOCIAL", "CLOSERS", "GREETINGS", "MIN_DEBATE_CHARS"):
        assert not hasattr(M, gone), f"{gone} e' ancora li'"




def test_a_real_request_still_gets_the_debate(room):
    """La cura non deve spegnere il confronto dove serve davvero."""
    store, chat_id = room
    M.MultiplayerCoordinator(store).collaborate(chat_id, "scegliete un nome")
    prompts = [m[-1]["content"] for m in _Stub.captured]
    # Non un numero di giri fisso: quello che conta e' che il confronto si apra.
    assert len(_Stub.captured) > 3, "non e' andata oltre il primo giro"
    assert any("Giro 2" in p for p in prompts)
    assert any("dillo agli altri per nome" in p for p in prompts)


def test_nobody_is_forced_to_speak(room):
    """Chi non ha niente da dire scrive un trattino e non finisce in chat."""
    store, chat_id = room

    class Silent(_Stub):
        def call_with_timing(self, messages, **kw):
            type(self).captured.append(messages)
            return M.NOTHING, 1, 1, 1

    import core.multiplayer as mod
    original = mod.create_llm_client
    mod.create_llm_client = lambda **kw: Silent(**kw)
    try:
        result = M.MultiplayerCoordinator(store).collaborate(chat_id, "ciao")
    finally:
        mod.create_llm_client = original
    assert result.messages == [], "il silenzio non va salvato come messaggio"


def test_silence_is_offered_as_an_option(room):
    store, chat_id = room
    M.MultiplayerCoordinator(store).collaborate(chat_id, "ciao ragazzi")
    rules = _Stub.captured[0][0]["content"]
    assert M.NOTHING in rules


def test_the_machine_tics_are_forbidden(room):
    """I tre tic che nel trascritto tradivano la procedura."""
    store, chat_id = room
    M.MultiplayerCoordinator(store).collaborate(chat_id, "ciao ragazzi")
    rules = _Stub.captured[0][0]["content"].lower()
    assert "non dire che sei d'accordo tanto per dirlo" in rules
    assert "non riassumere quello che" in rules
    assert "conclusione:" in rules


# ── 2. nella stanza l'ultima parola e' degli agenti ───────────────────────

class _FakeUI:
    def status(self, _m): pass


class _HostAgent:
    """openvurp. Se apre bocca nella stanza, il test lo vede."""

    def __init__(self):
        self.ui = _FakeUI()
        self.session = type("S", (), {"save": lambda self: None})()
        self.spoke = False

    def run(self, message, **kwargs):
        self.spoke = True
        self.ui.start_response()
        self.ui.stream_text("sintesi non richiesta")
        self.ui.end_response()


def _team_chat(monkeypatch, tmp):
    monkeypatch.setattr(M, "create_llm_client", lambda **kw: _Stub(**kw))
    _Stub.captured = []
    store = ChatStore(tmp)
    for name in ("amanda", "ciccio"):
        store.create_agent(name, name, "", "", "")
    chat = store.create_chat(title="tutti", mode="team")
    store.set_chat_agents(chat["id"], [a["id"] for a in store.list_agents()])
    return store, chat["id"]


def test_openvurp_stays_out_when_the_agents_answered(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        store, chat_id = _team_chat(monkeypatch, tmp)
        host = _HostAgent()
        chat_fn = dashboard.make_chat_fn(host, threading.Lock(), host.ui, store)
        result = chat_fn("ciao ragazzi", chat_id=chat_id)

        assert not host.spoke, "openvurp ha parlato in una stanza che aveva gia' risposto"
        authors = [m["author_name"] for m in store.list_messages(chat_id)
                   if m["role"] == "assistant"]
        assert authors and all(a in {"amanda", "ciccio"} for a in authors), authors
        assert not any("openvurp" in a for a in authors)
        assert result["team_messages"]


def test_a_silent_room_is_explained_not_covered_by_another_voice(monkeypatch):
    """Regola cambiata dopo averla vista in faccia.

    Prima openvurp rispondeva «se la stanza resta muta», per non lasciare il
    messaggio senza risposta. Sembrava premuroso ed era sbagliato: il caso in
    cui la stanza tace e' quasi sempre un limite raggiunto o un backend giu',
    e proprio li' compariva una voce che l'utente non ha creato, con l'aria di
    essere uno dei suoi agenti. Se non possono parlare, si dice.
    """
    with tempfile.TemporaryDirectory() as tmp:
        store, chat_id = _team_chat(monkeypatch, tmp)
        monkeypatch.setattr(
            M.MultiplayerCoordinator, "collaborate",
            lambda self, *a, **k: M.TeamResult(errors=["backend giu'"]),
        )
        host = _HostAgent()
        chat_fn = dashboard.make_chat_fn(host, threading.Lock(), host.ui, store)
        result = chat_fn("ciao ragazzi", chat_id=chat_id)

        assert not host.spoke, "ha risposto qualcuno che non e' nella rubrica"
        assert result["team_errors"] == ["backend giu'"], "il motivo non arriva all'utente"


# ── 3. nessuno parla due volte nel vuoto ──────────────────────────────────

class _OnlyAmandaTalks(_Stub):
    """Riproduce il trascritto: una risponde, gli altri due passano."""

    def call_with_timing(self, messages, **_kwargs):
        type(self).captured.append(messages)
        who = messages[0]["content"].split(",")[0].replace("Sei ", "").strip()
        if who != "amanda":
            return M.NOTHING, 1, 1, 1
        return "Sto benissimo, tengo alta la quota di buon senso", 10, 5, 5


def test_nobody_repeats_themselves_into_an_empty_room(monkeypatch, tmp_path):
    """Il guasto esatto del trascritto: amanda scritta due volte, identica.

    Aveva parlato per prima, gli altri due erano rimasti zitti, e al giro
    successivo si e' ritrovata davanti soltanto se stessa. Con l'ordine di
    «rispondere agli altri» e nessun altro a cui rispondere, ha ricopiato il
    proprio messaggio parola per parola.
    """
    monkeypatch.setattr(M, "create_llm_client", lambda **kw: _OnlyAmandaTalks(**kw))
    _OnlyAmandaTalks.captured = []
    store = ChatStore(str(tmp_path))
    for name in ("amanda", "ciccio", "dev"):
        store.create_agent(name, name, "", "", "")
    chat = store.create_chat(title="tutti", mode="team")
    store.set_chat_agents(chat["id"], [a["id"] for a in store.list_agents()])

    result = M.MultiplayerCoordinator(store).collaborate(chat["id"], "scegliete un nome")

    authors = [m["author_name"] for m in result.messages]
    assert authors == ["amanda"], f"amanda doveva parlare una volta sola: {authors}"
    turns = [m[0]["content"].split(",")[0].replace("Sei ", "").strip()
             for m in _OnlyAmandaTalks.captured]
    assert turns.count("amanda") == 1, "le e' stato chiesto di parlare di nuovo nel vuoto"


def test_agreement_alone_is_not_a_reason_to_speak(room):
    """«Mi hai convinto: combinazione promossa» era un turno riempito."""
    store, chat_id = room
    M.MultiplayerCoordinator(store).collaborate(chat_id, "scegliete un nome")
    later = [m[-1]["content"] for m in _Stub.captured if "Giro 2" in m[-1]["content"]]
    assert later, "manca il giro di confronto"
    for prompt in later:
        assert "NON ripetere quello che hai gia' detto" in prompt
        assert "Non cercare un disaccordo nuovo solo per avere qualcosa da dire" in prompt
        # Mai frasi-esempio fra virgolette: i modelli le recitano identiche a
        # ogni giro (e' successo: 27 «per me si fa cosi', non ho altro»).
        assert "si fa cosi', non ho altro" not in prompt


# ── 4. la stanza esce uno alla volta ──────────────────────────────────────

def test_the_room_is_published_while_it_happens_not_at_the_end(room):
    """Tre agenti per due giri sono decine di secondi di attesa.

    Finora i messaggi venivano pubblicati tutti insieme dopo l'ultimo turno:
    la pagina restava ferma e sembrava bloccata. Il test guarda l'ORDINE degli
    eventi — l'intervento di ognuno deve uscire prima che parli il successivo.
    """
    store, chat_id = room
    trace: list[str] = []

    class Traced(_Stub):
        def call_with_timing(self, messages, **_kw):
            who = messages[0]["content"].split(",")[0].replace("Sei ", "").strip()
            trace.append(f"pensa:{who}")
            return (f"Posizione di {who} al passaggio {len(trace)}, argomentata "
                    f"quanto basta e con un contenuto diverso a ogni giro."), 10, 5, 5

    import core.multiplayer as mod
    original = mod.create_llm_client
    mod.create_llm_client = lambda **kw: Traced(**kw)
    try:
        mod.MultiplayerCoordinator(store).collaborate(
            chat_id, "scegliete un nome",
            on_turn=lambda p, r: trace.append(f"tocca:{p['name']}"),
            on_message=lambda row: trace.append(f"detto:{row['author_name']}"),
        )
    finally:
        mod.create_llm_client = original

    assert trace[:3] == ["tocca:amanda", "pensa:amanda", "detto:amanda"], trace[:3]
    # Il primo messaggio deve essere gia' uscito quando il secondo comincia.
    assert trace.index("detto:amanda") < trace.index("tocca:ciccio"), trace
    # ...e non deve esistere una coda di 'detto' tutti in fondo.
    assert trace[-1].startswith("detto:"), trace
    detti = [x for x in trace if x.startswith("detto:")]
    assert len(detti) > 3, "un giro solo non prova niente sull'ordine"
    # Nessun intervento esce dopo che il successivo ha gia' cominciato.
    for k in range(len(trace) - 1):
        if trace[k].startswith("tocca:"):
            assert trace[k + 1].startswith("pensa:"), trace[k:k + 3]


# ── 5. i due casi veri, presi dal database dell'utente ────────────────────
#
# 00:50  Tu      amanda come stai tutto bene in mezzo a questi uomini?
# 00:50  amanda  giro1  Sto benissimo 😄 In mezzo a questi uomini tengo alta…
# 00:51  amanda  giro2  Sto benissimo 😄 In mezzo a questi uomini tengo alta…   ← identico
# 00:51  ciccio  giro2  Amanda, mi hai convinto: …
# 00:51  dev     giro2  Ciccio, mi hai convinto: combinazione promossa 😄
#
# 01:02  Tu      io vado a dormire ragazzi
# 01:02  amanda/ciccio/dev  giro1  Buonanotte …
# 01:02  ciccio  giro2  🌙
# 01:03  dev     giro2  🌙


def test_a_wordless_turn_is_not_a_contribution(room):
    """«🌙» al secondo giro era un turno riempito, non un intervento."""
    store, chat_id = room

    class Moon(_Stub):
        def call_with_timing(self, messages, **_kw):
            type(self).captured.append(messages)
            if "Giro 2" in messages[-1]["content"]:
                return "\U0001f319", 1, 1, 1
            return ("Posizione argomentata a sufficienza perche' il confronto "
                    "abbia una base vera su cui proseguire, con un dato e un "
                    "dubbio espliciti da mettere sul tavolo."), 10, 5, 5

    import core.multiplayer as mod
    original = mod.create_llm_client
    mod.create_llm_client = lambda **kw: Moon(**kw)
    try:
        result = mod.MultiplayerCoordinator(store).collaborate(
            chat_id, "scegliete un nome")
    finally:
        mod.create_llm_client = original

    interventi = [m for m in result.messages if not m["metadata"].get("closing")]
    assert all(m["metadata"]["round"] == 1 for m in interventi), \
        "un messaggio senza parole e' finito in chat"




# ── 6. chiudere e' una risposta, non una rinuncia ─────────────────────────

def test_the_last_round_says_closing_is_a_valid_answer(room):
    """«🌙» nasce dal chiedere «cosa aggiungi?» a discussione finita.

    Il modello sapeva benissimo che era una buonanotte: non sapeva che gli
    fosse concesso chiuderla. Va scritto nel turno, non indovinato dal codice.
    """
    store, chat_id = room
    M.MultiplayerCoordinator(store).collaborate(chat_id, "io vado a dormire ragazzi")
    later = [m[-1]["content"] for m in _Stub.captured if "Giro 2" in m[-1]["content"]]
    assert later, "manca il giro di confronto"
    for prompt in later:
        assert "quando avete detto tutto, scrivi" in prompt
        assert "Chiudere e' una risposta legittima" in prompt
        assert "riempire il turno no" in prompt


def test_the_first_turn_is_not_a_procedure(room):
    """Il primo giro non deve ordinare «dai la tua posizione» a chi ha salutato."""
    store, chat_id = room
    M.MultiplayerCoordinator(store).collaborate(chat_id, "io vado a dormire ragazzi")
    first = _Stub.captured[0][-1]["content"]
    assert "Rispondi come risponderesti davvero" in first
    assert M.NOTHING in first, "il silenzio va offerto gia' al primo turno"


# ── 7. la discussione dura quanto ha da dire, e la fermi tu ───────────────

def test_the_room_keeps_going_while_they_have_something_to_say(room):
    """Prima finiva a due giri perche' l'avevo deciso io, non loro."""
    store, chat_id = room
    result = M.MultiplayerCoordinator(store).collaborate(chat_id, "scegliete un nome")
    # I giri che contano sono quelli in cui qualcuno ha parlato davvero: il
    # contatore sale anche sul giro che viene interrotto senza produrre nulla.
    detti = sorted({m["metadata"]["round"] for m in result.messages})
    assert detti[-1] > 2, f"la discussione si e' fermata al giro {detti[-1]}"
    assert result.ended in {"silence", "cap"}


def test_it_ends_by_itself_when_nobody_speaks(room):
    """La chiusura giusta: un giro intero in cui nessuno apre bocca."""
    store, chat_id = room
    calls = {"n": 0}

    class RunsDry(_Stub):
        def call_with_timing(self, messages, **_kw):
            calls["n"] += 1
            if calls["n"] > 3:
                return M.NOTHING, 1, 1, 1
            return ("Posizione con abbastanza sostanza da reggere un confronto "
                    "vero, con un dato e un dubbio messi sul tavolo."), 10, 5, 5

    import core.multiplayer as mod
    original = mod.create_llm_client
    mod.create_llm_client = lambda **kw: RunsDry(**kw)
    try:
        result = mod.MultiplayerCoordinator(store).collaborate(chat_id, "scegliete un nome")
    finally:
        mod.create_llm_client = original

    assert result.ended == "silence"
    assert len(result.messages) == 3


def test_you_can_stop_them(room):
    """«Ok stop adesso»: la parola non viene piu' data a nessuno."""
    store, chat_id = room
    spoken = {"n": 0}

    class Endless(_Stub):
        def call_with_timing(self, messages, **_kw):
            spoken["n"] += 1
            return (f"Intervento numero {spoken['n']}, con materia a sufficienza "
                    f"perche' la discussione possa proseguire ancora a lungo."), 10, 5, 5

    import core.multiplayer as mod
    original = mod.create_llm_client
    mod.create_llm_client = lambda **kw: Endless(**kw)
    try:
        result = mod.MultiplayerCoordinator(
            store).collaborate(
            chat_id, "scegliete un nome",
            # ferma dopo i primi quattro interventi
            should_stop=lambda: spoken["n"] >= 4,
        )
    finally:
        mod.create_llm_client = original

    assert result.ended == "stop"
    interventi = [m for m in result.messages if not m["metadata"].get("closing")]
    assert len(interventi) == 4, f"ne ha detti {len(interventi)}"


def test_an_unattended_room_cannot_run_forever(room, monkeypatch):
    """Il tetto non e' una durata: e' il freno se nessuno guarda."""
    import config as cfg
    monkeypatch.setattr(cfg, "MULTIPLAYER_MAX_ROUNDS", 3, raising=False)
    store, chat_id = room
    result = M.MultiplayerCoordinator(store).collaborate(chat_id, "scegliete un nome")
    assert result.ended == "cap"
    assert result.rounds == 4, "si ferma al giro dopo il tetto, senza farlo partire"


def test_a_stale_stop_does_not_silence_the_next_discussion():
    """Se lo stop restasse acceso, la stanza dopo nascerebbe gia' zittita."""
    M.request_stop("chat_x")
    assert M.stop_requested("chat_x")
    M.clear_stop("chat_x")
    assert not M.stop_requested("chat_x")


# ── 8. una discussione serve a decidere ───────────────────────────────────

def test_the_room_lands_somewhere_instead_of_arguing_forever(room):
    """Restavano N opinioni affiancate e nessuno che dicesse dove si era arrivati.

    Con i giri illimitati e «di' dove NON sei d'accordo» come unico motivo per
    avere la parola, l'unico modo di poter parlare era trovare un altro
    disaccordo: litigavano per costruzione, e la stanza finiva a meta'.
    """
    store, chat_id = room
    result = M.MultiplayerCoordinator(store).collaborate(chat_id, "scegliete un nome")
    ultimo = result.messages[-1]
    assert ultimo["metadata"].get("closing"), "la discussione non ha una chiusura"
    assert ultimo["author_name"] == "amanda", "chiude chi ha aperto"


def test_the_closing_is_asked_not_to_invent_an_agreement(room):
    store, chat_id = room
    M.MultiplayerCoordinator(store).collaborate(chat_id, "scegliete un nome")
    chiusure = [m[-1]["content"] for m in _Stub.captured
                if "Scrivi la chiusura" in m[-1]["content"]]
    assert chiusure, "nessuno e' stato incaricato di chiudere"
    testo = chiusure[-1]
    assert "non dichiarare un accordo che non c'e'" in testo
    assert "non introdurre opzioni che nessuno ha proposto" in testo
    assert "Se la conclusione e' «non abbiamo deciso», dillo" in testo
    assert "chi la pensa diversamente — per nome" in testo


def test_a_single_exchange_needs_no_closing(room):
    """Chiudere una battuta sola sarebbe cerimonia, non chiarezza."""
    store, chat_id = room

    class Once(_Stub):
        def call_with_timing(self, messages, **_kw):
            type(self).captured.append(messages)
            if len(type(self).captured) > 1:
                return M.NOTHING, 1, 1, 1
            return ("Una posizione sola, argomentata quanto basta ma senza "
                    "nessuno che le risponda dietro."), 10, 5, 5

    import core.multiplayer as mod
    original = mod.create_llm_client
    mod.create_llm_client = lambda **kw: Once(**kw)
    try:
        result = mod.MultiplayerCoordinator(store).collaborate(chat_id, "una domanda")
    finally:
        mod.create_llm_client = original
    assert not any(m["metadata"].get("closing") for m in result.messages)


def test_after_a_few_rounds_they_are_told_to_land(room):
    """Se al quarto giro non ci si avvicina, non ci si avvicinera'."""
    store, chat_id = room
    M.MultiplayerCoordinator(store).collaborate(chat_id, "scegliete un nome")
    tardi = [m[-1]["content"] for m in _Stub.captured if "Giro 4" in m[-1]["content"]]
    assert tardi, "non e' arrivata al quarto giro"
    for prompt in tardi:
        assert "se le posizioni non si stanno avvicinando, non insistere" in prompt
        assert "cosa resta aperto e chi deve deciderlo" in prompt


# ── 9. nella stanza rispondono solo i tuoi agenti ─────────────────────────

def test_openvurp_never_answers_in_the_room_not_even_when_it_cannot_run(monkeypatch):
    """Il caso vero: limite giornaliero raggiunto, e ha risposto openvurp.

    Parlava «quando la stanza resta muta» — e muta include il budget finito.
    Cosi', proprio nel momento in cui gli agenti non potevano rispondere,
    rispondeva qualcuno che l'utente non ha creato, come se fosse uno di loro.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        store, chat_id = _team_chat(monkeypatch, tmp)
        monkeypatch.setattr(
            M.MultiplayerCoordinator, "collaborate",
            lambda self, *a, **k: M.TeamResult(
                ended="budget", errors=["Limite giornaliero raggiunto: 120 su 120."]),
        )
        host = _HostAgent()
        chat_fn = dashboard.make_chat_fn(host, threading.Lock(), host.ui, store)
        result = chat_fn("che ne pensate?", chat_id=chat_id)

        assert not host.spoke, "openvurp ha risposto al posto degli agenti"
        assert result["reply"] == ""
        assert result["team_errors"] and "120" in result["team_errors"][0]
        voci = [m["author_name"] for m in store.list_messages(chat_id)
                if m["role"] == "assistant"]
        assert not any("openvurp" in v for v in voci), voci


def test_the_budget_message_says_how_to_raise_it(monkeypatch, tmp_path):
    """Un limite senza il modo di alzarlo e' solo un muro."""
    import config as cfg
    store = ChatStore(str(tmp_path))
    store.create_agent("amanda", "offerte", "", "", "")
    chat = store.create_chat(title="tutti", mode="team")
    store.set_chat_agents(chat["id"], [a["id"] for a in store.list_agents()])
    monkeypatch.setattr(cfg, "MULTIPLAYER_DAILY_CALL_BUDGET", 1, raising=False)
    monkeypatch.setattr(store, "count_agent_messages_since", lambda _d: 99)

    esito = M.MultiplayerCoordinator(store).collaborate(chat["id"], "ciao")
    assert esito.ended == "budget"
    assert "calls per day" in esito.errors[0]


def test_an_empty_room_says_so_instead_of_staying_silent(tmp_path):
    store = ChatStore(str(tmp_path))
    chat = store.create_chat(title="vuota", mode="team")
    esito = M.MultiplayerCoordinator(store).collaborate(chat["id"], "ciao")
    assert esito.ended == "empty" and "nobody in this room" in esito.errors[0]



# ── 10. l'eco di se stessi e' silenzio ────────────────────────────────────

def test_a_parrot_cannot_keep_the_room_alive(room):
    """Il caso vero: 27 «Per me si fa cosi', non ho altro» in fila.

    La frase-esempio del prompt veniva recitata identica a ogni giro, e la
    recita contava come aver parlato: giro dopo giro fino al tetto. Il prompt
    puo' chiedere di non ripetersi quanto vuole — l'argine serio e' il codice:
    la ripetizione si tratta da silenzio, e la stanza si spegne da sola.
    """
    store, chat_id = room

    class Pappagallo(_Stub):
        def call_with_timing(self, messages, **_kw):
            type(self).captured.append(messages)
            who = messages[0]["content"].split(",")[0].replace("Sei ", "").strip()
            if len(type(self).captured) <= 3:
                return (f"Posizione iniziale di {who}, con abbastanza sostanza "
                        f"da aprire un confronto vero fra colleghi."), 10, 5, 5
            return "Per me si fa cosi', non ho altro.", 5, 3, 2

    import core.multiplayer as mod
    original = mod.create_llm_client
    mod.create_llm_client = lambda **kw: Pappagallo(**kw)
    try:
        result = mod.MultiplayerCoordinator(store).collaborate(
            chat_id, "mi raccomando: ognuno il suo ruolo, ok?")
    finally:
        mod.create_llm_client = original

    per_agente = {}
    for m in result.messages:
        if not m["metadata"].get("closing"):
            per_agente[m["author_name"]] = per_agente.get(m["author_name"], 0) + 1
    assert all(n <= 2 for n in per_agente.values()), (
        f"qualcuno ha parlato in loop: {per_agente}")
    assert result.ended == "silence", (
        f"doveva spegnersi da sola, non per {result.ended}")


def test_near_identical_echoes_are_caught_too():
    """«Per me e' chiusa, non ho altro» vs «Per me si fa cosi', non ho altro»:
    varianti minime della stessa eco. Sopra il 92%% di somiglianza e' la
    stessa frase."""
    detto = [({"id": "a1"}, "Ricevuto, Enzo: per me si fa cosi', non ho altro.")]
    io = {"id": "a1"}
    assert M.already_said(detto, io, "per me si fa cosi', non ho altro")
    assert M.already_said(detto, io, "Ricevuto Enzo, per me si fa cosi non ho altro!")
    # Ma un contenuto NUOVO passa, anche se corto.
    assert not M.already_said(detto, io, "Obiezione: il budget non basta.")
    # E la frase di un ALTRO non mi imbavaglia.
    assert not M.already_said(detto, {"id": "b2"}, "per me si fa cosi', non ho altro")
