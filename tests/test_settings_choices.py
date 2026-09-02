"""Nelle impostazioni si sceglie, non si scrive. E salvare deve bastare.

Due difetti dello stesso genere:

1. «Quali canali accendo» e «quali strumenti concedo» erano campi di testo:
   chiedevano di indovinare il nome esatto (`telegram,discord`) e di
   ricordarselo. L'id Telegram di una persona, poi, andava cercato a mano su
   getUpdates e incollato — mentre il bot lo sa gia' per chiunque gli abbia
   scritto.
2. Salvare scriveva il .env ma i canali restavano quelli dell'avvio: una
   casella che richiede un riavvio per avere effetto e' una casella che mente.
"""

import pytest

import dashboard as D
from core.channels_runtime import Supervisor, build


# ── i valori tornano nel tipo che avevano ────────────────────────────────

def test_a_list_setting_does_not_become_a_string():
    """`CHANNELS_IN` nasce lista: salvata come stringa, chi la scorre otterrebbe
    le singole lettere — «telegram» diventerebbe otto canali inesistenti."""
    assert D._come_prima([], "telegram, discord") == ["telegram", "discord"]
    assert D._come_prima(["x"], "telegram;discord") == ["telegram", "discord"]
    assert D._come_prima([], "") == []


def test_a_list_of_numbers_stays_numbers():
    assert D._come_prima([123], "111, 222") == [111, 222]
    assert D._come_prima([123], "111, non-un-numero") == [111]


def test_other_types_are_preserved():
    assert D._come_prima(False, "true") is True
    assert D._come_prima(False, "no") is False
    assert D._come_prima(12, "20") == 20
    assert D._come_prima(12, "non un numero") == "non un numero"
    assert D._come_prima("", " ciao ") == " ciao "


# ── il supervisore accende e spegne senza riavviare ──────────────────────

class _Canale:
    def __init__(self, **kw):
        self.avviato = False
        self.fermato = False

    def start(self):
        self.avviato = True

    def stop(self):
        self.fermato = True


@pytest.fixture
def supervisore(monkeypatch):
    s = Supervisor()
    s.bind(object(), None)
    fatti = {}

    def finto(nome, conversazione, cfg, ui=None):
        fatti[nome] = _Canale()
        return fatti[nome]

    monkeypatch.setattr("core.channels_runtime.build", finto)
    return s, fatti


def _chiedi(monkeypatch, *nomi):
    import config as cfg
    monkeypatch.setattr(cfg, "CHANNELS_IN", list(nomi), raising=False)


def test_turning_a_channel_on_starts_it_right_away(supervisore, monkeypatch):
    s, fatti = supervisore
    _chiedi(monkeypatch, "telegram")
    esito = s.apply()
    assert esito["started"] == ["telegram"]
    assert fatti["telegram"].avviato


def test_turning_it_off_stops_it(supervisore, monkeypatch):
    s, fatti = supervisore
    _chiedi(monkeypatch, "telegram")
    s.apply()
    _chiedi(monkeypatch)
    esito = s.apply()
    assert esito["stopped"] == ["telegram"] and esito["running"] == []
    assert fatti["telegram"].fermato


def test_saving_twice_does_not_restart_what_is_already_running(supervisore, monkeypatch):
    s, fatti = supervisore
    _chiedi(monkeypatch, "telegram")
    s.apply()
    primo = fatti["telegram"]
    esito = s.apply()
    assert esito["started"] == [] and esito["running"] == ["telegram"]
    assert fatti["telegram"] is primo, "il canale e' stato ricreato inutilmente"


def test_a_broken_channel_is_reported_not_swallowed(monkeypatch):
    s = Supervisor()
    s.bind(object(), None)
    monkeypatch.setattr("core.channels_runtime.build",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("no token")))
    _chiedi(monkeypatch, "telegram")
    esito = s.apply()
    assert esito["running"] == []
    assert esito["errors"] and "no token" in esito["errors"][0]


def test_without_a_conversation_nothing_is_started_and_it_says_so(monkeypatch):
    """La dashboard avviata da sola non ha un agente: non e' un errore, ma
    l'utente deve sapere perche' il canale non si e' acceso."""
    s = Supervisor()
    _chiedi(monkeypatch, "telegram")
    esito = s.apply()
    assert esito["running"] == [] and esito["errors"]


# ── un canale senza autorizzati non parte ────────────────────────────────

def test_a_channel_with_nobody_allowed_refuses_to_start(monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "TELEGRAM_ALLOWED_USERS", [], raising=False)
    monkeypatch.setattr(cfg, "TELEGRAM_TOKEN", "t", raising=False)
    with pytest.raises(ValueError, match="allowed"):
        build("telegram", object(), cfg)


def test_whatsapp_now_builds_like_the_others(monkeypatch):
    """Con Baileys non serve piu' il webhook: stessa regola degli altri —
    senza autorizzati non parte."""
    import config as cfg
    monkeypatch.setattr(cfg, "WHATSAPP_ALLOWED_USERS", [], raising=False)
    with pytest.raises(ValueError, match="allowed"):
        build("whatsapp", object(), cfg)


def test_an_unknown_channel_is_named_in_the_error(monkeypatch):
    import config as cfg
    with pytest.raises(ValueError, match="pippo"):
        build("pippo", object(), cfg)


# ── la pagina propone scelte, non campi da compilare ─────────────────────

def test_the_page_offers_switches_and_tags_instead_of_typing():
    import re
    from tests.test_dashboard_page import _page, _script
    js = _script(_page())
    assert "function setSwitch(" in js and "function setTags(" in js
    assert "function setSelect(" in js
    # Le tre cose che prima si scrivevano a mano.
    assert 'val.CHANNELS_IN=' in js and 'filter(c=>{const s=$("#sw-ch-"+c)' in js
    assert 'val.TELEGRAM_ALLOWED_USERS=spuntati("tg")' in js
    # Gli strumenti sono un profilo, non quaranta caselle: «tutto», «solo
    # consultare», o la scelta fine per chi la vuole davvero.
    assert "SWARM_PRESET" in js
    assert "toolsConsulto.join" in js
    assert 'spuntati("tool")' in js, "la scelta fine deve restare possibile"
    # E chi ha scritto al bot si spunta, non si copia da getUpdates.
    assert "Who has already written" in js and "Nobody has written to the bot yet" in js


def test_after_saving_the_page_says_what_changed_now():
    from tests.test_dashboard_page import _page, _script
    js = _script(_page())
    assert '" On: "' in js and '" Off: "' in js
