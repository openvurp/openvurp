"""Test per il giudizio sul cervello (core/escalation.py) e la presenza."""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.escalation import decide_effort
from core.presence import Presence


# ── decide_effort ──

def test_chatter_stays_fast():
    assert decide_effort("ciao!").effort == "fast"
    assert decide_effort("ok").effort == "fast"
    assert decide_effort("grazie mille").effort == "fast"
    assert not decide_effort("va bene").route_deep


def test_judgment_questions_go_deep():
    d = decide_effort("secondo te conviene rifare l'architettura del modulo pagamenti?")
    assert d.effort == "deep" and d.route_deep
    assert decide_effort("è una decisione importante, pensaci bene").route_deep
    assert decide_effort("c'è una vulnerabilità di sicurezza in questo codice?").route_deep
    assert decide_effort("mi conviene investire questi soldi nel mutuo?").route_deep


def test_normal_work_stays_normal():
    d = decide_effort("rinomina la variabile x in counter nel file utils.py")
    assert d.effort == "normal"
    assert not d.route_deep


def test_long_articulated_question_goes_deep():
    text = ("Vorrei capire come strutturare la cosa. " * 20) + " Cosa faresti?"
    assert decide_effort(text).route_deep


# ── Presence ──

def test_presence_touch_and_current():
    with tempfile.TemporaryDirectory() as tmp:
        p = Presence(tmp)
        assert p.current_channel() == ""
        p.touch("cli")
        assert p.current_channel() == "cli"
        time.sleep(0.02)
        p.touch("telegram")
        assert p.current_channel() == "telegram"


def test_presence_ignores_autonomous_sources():
    with tempfile.TemporaryDirectory() as tmp:
        p = Presence(tmp)
        p.touch("heartbeat")
        p.touch("cron")
        p.touch("system")
        assert p.current_channel() == ""


def test_presence_window_expiry():
    with tempfile.TemporaryDirectory() as tmp:
        p = Presence(tmp)
        p.touch("cli")
        assert p.current_channel(window_seconds=3600) == "cli"
        # Finestra già scaduta → assente ovunque
        assert p.current_channel(window_seconds=0) == ""


def test_pick_delivery_channel_rules():
    with tempfile.TemporaryDirectory() as tmp:
        p = Presence(tmp)
        # Assente ovunque → preferisci il canale remoto (ti raggiunge fuori casa)
        assert p.pick_delivery_channel(["cli", "telegram"]) == "telegram"
        # Assente e niente remoto → primo disponibile (TUI)
        assert p.pick_delivery_channel(["cli"]) == "cli"
        # Attivo ora sulla TUI → messaggio sulla TUI
        p.touch("cli")
        assert p.pick_delivery_channel(["cli", "telegram"]) == "cli"
        # Attivo più di recente su Telegram → Telegram
        time.sleep(0.02)
        p.touch("telegram")
        assert p.pick_delivery_channel(["cli", "telegram"]) == "telegram"
