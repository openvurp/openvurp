"""Test di regressione: il bot NON deve mai ignorare mention ripetute nel gruppo.

Scenario reale: in un gruppo, un utente scrive "@pico perché non rispondi?" una
volta → modalità 'natural' → bot interviene (mark_intervention). Pochi secondi
dopo lo stesso utente ribadisce "eh @pico? sei morto?" senza nuove mention
esplicite, oppure arrivano reply di altri utenti che si aspettano una risposta.

Bug del passato: il cooldown 90s scattava dopo il PRIMO intervento, e TUTTI i
messaggi successivi del gruppo per 90s venivano scartati con `return ""` —
anche se il bot era esplicitamente interpellato. Risultato: l'utente vedeva il
bot inerte proprio mentre lo chiamava.

Questi test codificano la regola che la skill `group-chat-behavior` impone:
- mention/addressed battono SEMPRE il cooldown
- mention recenti nel buffer retroagiscono come indirizzate
- il cooldown esiste solo per silenziare interventi spontanei non richiesti
"""

from core import group_chat as gc


# ── Regola 1: mention recente nel buffer "promuove" il messaggio corrente ──


def test_recent_mention_promotes_to_addressed():
    """Se negli ultimi messaggi c'è stata una @menzione, la corrente è implicitamente indirizzata.

    Questa è la regola che il safety net di main.py applica prima del cooldown:
    ripetere "@pico perché non rispondi?" + un nuovo messaggio deve SEMPRE
    passare, anche se il bot ha appena risposto.
    """
    buf = gc.GroupChatBuffer(maxlen=10)
    buf.add("g1", "mario", "ciao a tutti")
    buf.add("g1", "marco", "che si fa?")
    buf.add("g1", "mario", "@pico perché non rispondi?")
    buf.mark_intervention("g1", now=1000)

    recent = buf.recent("g1")

    # Cooldown ancora caldo (50s < 90s) E message successivo SENZA mention:
    # nel nuovo comportamento NON deve filtrare se la finestra recente
    # conteneva una mention.
    bot_username = "pico"
    recent_mentioned = any(
        f"@{bot_username}".lower() in (t or "").lower()
        for _, t in recent[-5:]
    )
    assert recent_mentioned is True, "la mention di mario deve essere vista"


def test_no_mention_no_promotion():
    """Senza mention recenti, il messaggio corrente NON è promosso."""
    buf = gc.GroupChatBuffer(maxlen=10)
    buf.add("g1", "mario", "buongiorno a tutti")
    buf.add("g1", "marco", "che si dice?")
    buf.add("g1", "lucia", "vado al mercato")

    recent = buf.recent("g1")
    bot_username = "pico"
    recent_mentioned = any(
        f"@{bot_username}".lower() in (t or "").lower()
        for _, t in recent[-5:]
    )
    assert recent_mentioned is False


# ── Regola 2: il cooldown NON blocca quando addressed=True ──


def test_cooldown_does_not_block_addressed_call():
    """Dopo un intervento, il cooldown è ancora caldo ma una mention nuova DEVE passare.

    Simula il check che main.py esegue: se la mention è nel messaggio corrente
    (addressed=True) il cooldown non si applica. Qui lo verifichiamo a livello
    di primitive: il buffer continua a tracciare correttamente le mention
    anche durante il cooldown.
    """
    buf = gc.GroupChatBuffer()
    buf.mark_intervention("g1", now=1000)

    # Cooldown caldo: 50s dopo l'ultimo intervento
    assert buf.cooldown_ok("g1", 90, now=1050) is False

    # Ma una mention nuova viene comunque aggiunta al buffer
    buf.add("g1", "mario", "@pico ok adesso?")
    recent = buf.recent("g1")
    assert recent[-1] == ("mario", "@pico ok adesso?")

    # E la logica di "recent_mentioned" la vede
    assert any("@pico" in t.lower() for _, t in recent[-5:])


# ── Regola 3: mention case-insensitive ──


def test_mention_case_insensitive():
    buf = gc.GroupChatBuffer(maxlen=5)
    buf.add("g1", "mario", "@Pico ciao")
    buf.add("g1", "marco", "@PICO?")
    recent = buf.recent("g1")

    bot_username = "pico"
    recent_mentioned = any(
        f"@{bot_username}".lower() in (t or "").lower()
        for _, t in recent[-5:]
    )
    assert recent_mentioned is True


# ── Regola 4: il safety net guarda solo le ultime N mention ──


def test_safety_net_window_respected():
    """Una mention vecchia di 10 messaggi non deve più promuovere il corrente.

    Senza questo bound, una singola mention di giorni fa condizionerebbe
    tutto il gruppo per sempre.
    """
    buf = gc.GroupChatBuffer(maxlen=20)
    buf.add("g1", "mario", "@pico vecchia domanda")  # sarà fuori finestra
    for i in range(15):
        buf.add("g1", f"u{i}", f"chiacchiera numero {i}")

    recent = buf.recent("g1")
    bot_username = "pico"
    # La finestra è -5: le ultime 5 NON contengono mention
    window_has_mention = any(
        f"@{bot_username}".lower() in (t or "").lower()
        for _, t in recent[-5:]
    )
    assert window_has_mention is False, "la mention vecchia non deve pesare"


# ── Regola 5: il prompt del decisore porta SEMPRE il nome dell'agente ──


def test_decision_prompt_carries_identity_name():
    """Il prompt 'Sei  e partecipi a una chat di GRUPPO...' è rotto.

    Il test verifica che la stringa del system prompt contenga un nome
    non vuoto quando viene chiamato dal main con identity_name valorizzato.
    (Vedere la skill group-chat-behavior: identity_name='' è vietato.)
    """
    recent = [("mario", "ciao")]
    msgs = gc.build_decision_messages("Pico", recent, "mario", "che fai stasera?")
    system = msgs[0]["content"]
    assert "Pico" in system, "il system prompt del decisore deve conoscere il nome"
    assert not system.startswith("Sei  "), "niente identity_name vuoto"


# ── Regola 6: worth_considering non blocca mention reali ──


def test_short_mention_is_considered():
    """Il pre-filtro ora è minimo: anche i testi brevi reali passano e arrivano
    al guardiano (prima venivano scartati, ed era un bug). Una mention come
    '@pico?' DEVE passare."""
    assert gc.worth_considering("@pico?") is True
    assert gc.worth_considering("@pico mi aiuti?") is True
    # scarta solo vuoto / singolo carattere
    assert gc.worth_considering("") is False
