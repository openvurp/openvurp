"""Test: la "coscienza della stanza" — percezione che si aggiorna nei gruppi.

Invece di un SI/NO per messaggio, il modello mantiene una NOTA (memoria viva
del gruppo) e sceglie tra tre azioni: RISPONDO / SILENZIO / CHIEDO. Il CHIEDO
è la novità umana: nel dubbio non indovina, chiede conferma.
"""

from core import group_chat as gc


# ── Riflesso: sentire il proprio nome (deterministico, non delegato al modello) ──


def test_name_mentioned_basic():
    assert gc.name_mentioned("Pico mandi un messaggio a Alice?", "Pico") is True
    assert gc.name_mentioned("ehi pico ci sei?", "Pico") is True       # minuscolo
    assert gc.name_mentioned("PICO!!!", "Pico") is True                # maiuscolo
    assert gc.name_mentioned("ciao a tutti", "Pico") is False


def test_name_mentioned_word_boundary():
    # non deve scattare dentro un'altra parola
    assert gc.name_mentioned("ho preso una picozza in montagna", "Pico") is False
    assert gc.name_mentioned("epicofarmaco", "Pico") is False
    # ma con punteggiatura attaccata sì
    assert gc.name_mentioned("allora, Pico?", "Pico") is True
    assert gc.name_mentioned("(Pico) vieni", "Pico") is True


def test_name_mentioned_accents():
    assert gc.name_mentioned("ciao Pìco", "Pico") is True
    assert gc.name_mentioned("ciao Pico", "Pìco") is True


def test_name_mentioned_empty_name():
    assert gc.name_mentioned("qualunque cosa", "") is False
    assert gc.name_mentioned("qualunque cosa", None) is False


# ── La nota persiste nel buffer (memoria del gruppo) ──


def test_buffer_note_roundtrip():
    buf = gc.GroupChatBuffer()
    assert buf.get_note("g1") == ""
    buf.set_note("g1", "Parlo con Mario del concerto, credo sia con me")
    assert "Mario" in buf.get_note("g1")
    # set vuoto non cancella la nota esistente
    buf.set_note("g1", "")
    assert "Mario" in buf.get_note("g1")


# ── Parsing tollerante dell'output del modello piccolo ──


def test_parse_perception_respond():
    p = gc.parse_perception(
        "NOTA: Mario mi sta chiedendo qualcosa, è con me\nAZIONE: RISPONDO"
    )
    assert p.action == "respond"
    assert "Mario" in p.note
    assert p.question == ""


def test_parse_perception_silent():
    p = gc.parse_perception("NOTA: Marco e Lucia parlano tra loro\nAZIONE: SILENZIO")
    assert p.action == "silent"
    assert "Marco" in p.note


def test_parse_perception_ask_with_question():
    p = gc.parse_perception(
        "NOTA: non chiaro se il 'tu' sia io\nAZIONE: CHIEDO\nDOMANDA: Scusate, dite a me?"
    )
    assert p.action == "ask"
    assert p.question == "Scusate, dite a me?"


def test_parse_perception_question_implies_ask():
    # ha scritto una domanda ma azione ambigua → trattato come ask
    p = gc.parse_perception("NOTA: boh\nDOMANDA: dicevate a me?")
    assert p.action == "ask"
    assert p.question == "dicevate a me?"


def test_parse_perception_placeholder_question_ignored():
    p = gc.parse_perception("NOTA: ok\nAZIONE: RISPONDO\nDOMANDA: (vuota)")
    assert p.action == "respond"
    assert p.question == ""


def test_parse_perception_empty_is_silent():
    assert gc.parse_perception("").action == "silent"
    assert gc.parse_perception("   ").action == "silent"


def test_parse_perception_freeform_defaults_silent():
    # modello che ignora il formato e scrive prosa senza intento chiaro → prudente
    assert gc.parse_perception("Non saprei davvero che dire qui.").action == "silent"


# ── perceive(): integra LLM + buffer, prudente sugli errori ──


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def call(self, messages, **kw):
        self.calls.append(messages)
        return self.reply


def test_perceive_respond_keeps_new_note():
    llm = FakeLLM("NOTA: Mario parla con me del viaggio\nAZIONE: RISPONDO")
    p = gc.perceive(llm, "Pico", "(vuota)", [("mario", "ci pensi tu?")], "mario", "ci pensi tu?")
    assert p.action == "respond"
    assert "Mario" in p.note
    assert llm.calls


def test_perceive_trivial_skips_llm():
    class Boom:
        def call(self, *a, **k):
            raise AssertionError("non deve chiamare il modello su testo vuoto")

    p = gc.perceive(Boom(), "Pico", "nota vecchia", [], "mario", "")
    assert p.action == "silent"
    assert p.note == "nota vecchia"  # nota conservata


def test_perceive_error_is_silent_and_keeps_note():
    class Broken:
        def call(self, *a, **k):
            raise RuntimeError("llm down")

    p = gc.perceive(Broken(), "Pico", "nota viva", [("a", "qualcosa di serio?")], "a", "qualcosa di serio?")
    assert p.action == "silent"
    assert p.note == "nota viva"


def test_perceive_no_note_falls_back_to_old():
    # il modello sceglie ma non riscrive la nota → si conserva quella precedente
    llm = FakeLLM("AZIONE: SILENZIO")
    p = gc.perceive(llm, "Pico", "stavo con Lucia", [("b", "ciao")], "b", "ciao a tutti")
    assert p.note == "stavo con Lucia"


# ── Il prompt porta sempre nome + nota ──


def test_perception_prompt_carries_name_and_note():
    msgs = gc.build_perception_messages("Pico", "parlo con Mario", [("mario", "ehi")], "mario", "che dici?")
    system = msgs[0]["content"]
    user = msgs[1]["content"]
    assert "Pico" in system
    assert "parlo con Mario" in user
    assert "che dici?" in user
