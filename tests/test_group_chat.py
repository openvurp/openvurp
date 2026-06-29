"""Test: partecipazione naturale nei gruppi (buffer, cooldown, decisione)."""

from core import group_chat as gc


def test_worth_considering():
    # pre-filtro minimo: scarta solo vuoto / 1 carattere; i corti veri passano
    assert gc.worth_considering("") is False
    assert gc.worth_considering("👍") is False           # singolo carattere
    assert gc.worth_considering("ci sei?") is True       # corto ma reale → passa
    assert gc.worth_considering("ok") is True
    assert gc.worth_considering("qualcuno sa come si fa?") is True


def test_parse_decision_tollerante():
    assert gc.parse_decision("SI") is True
    assert gc.parse_decision("Penso di sì") is True       # modelli piccoli
    assert gc.parse_decision("Certo!") is True
    assert gc.parse_decision("yes") is True
    assert gc.parse_decision("NO") is False
    assert gc.parse_decision("Direi di no") is False
    assert gc.parse_decision("meglio restare in silenzio") is False
    assert gc.parse_decision("") is False


def test_buffer_add_and_recent():
    buf = gc.GroupChatBuffer(maxlen=3)
    for i in range(5):
        buf.add("g1", f"u{i}", f"m{i}")
    recent = buf.recent("g1")
    assert len(recent) == 3                     # rispetta maxlen
    assert recent[-1] == ("u4", "m4")
    assert buf.recent("altro") == []


def test_cooldown_and_mark():
    buf = gc.GroupChatBuffer()
    assert buf.cooldown_ok("g1", 90, now=1000) is True   # mai intervenuto
    buf.mark_intervention("g1", now=1000)
    assert buf.cooldown_ok("g1", 90, now=1050) is False  # troppo presto
    assert buf.cooldown_ok("g1", 90, now=1100) is True   # passato il cooldown


def test_decide_intervention_uses_llm():
    class FakeLLM:
        def __init__(self, reply):
            self.reply = reply
            self.calls = []

        def call(self, messages, **kw):
            self.calls.append(messages)
            return self.reply

    recent = [("ann", "stiamo organizzando la cena")]
    yes = FakeLLM("SI")
    assert gc.decide_intervention(yes, "Seth", recent, "ann", "qualcuno prenota?") is True
    assert yes.calls  # ha chiamato l'LLM

    no = FakeLLM("NO")
    assert gc.decide_intervention(no, "Seth", recent, "ann", "qualcuno prenota?") is False


def test_decide_intervention_prefilter_skips_llm():
    class BoomLLM:
        def call(self, messages, **kw):
            raise AssertionError("non deve essere chiamato su messaggi banali")

    # messaggio banale → niente chiamata LLM, ritorna False
    assert gc.decide_intervention(BoomLLM(), "Seth", [], "ann", "ok") is False


def test_decide_intervention_safe_on_error():
    class BrokenLLM:
        def call(self, messages, **kw):
            raise RuntimeError("llm down")

    assert gc.decide_intervention(BrokenLLM(), "Seth", [], "ann", "domanda lunga e seria?") is False


def test_build_context_prefix():
    assert gc.build_context_prefix([]) == ""
    assert gc.build_context_prefix([("u", "solo questo")]) == ""  # serve >1 msg
    prefix = gc.build_context_prefix([("a", "uno"), ("b", "due")])
    assert "uno" in prefix and "Contesto" in prefix
