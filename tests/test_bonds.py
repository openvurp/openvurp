"""Test per il legame (core/bonds.py): fili, silenzio, ritmo spontaneo."""

import os
import sys
import tempfile
import time
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.bonds import (
    Bonds, BondError, MAX_SPONTANEOUS_PER_DAY, MIN_GAP_SECONDS,
    IGNORED_STREAK_TO_BACK_OFF,
)


def _make(tmp) -> Bonds:
    return Bonds(os.path.join(tmp, "memory"))


# ── Fili ──

def test_add_thread_validation():
    with tempfile.TemporaryDirectory() as tmp:
        bonds = _make(tmp)
        with pytest.raises(BondError):
            bonds.add_thread("corto", "2026-06-13")
        with pytest.raises(BondError):
            bonds.add_thread("colloquio di lavoro importante", "dopodomani")
        t = bonds.add_thread("colloquio di lavoro da ACME", "2026-06-13T18:00",
                             why="ci tiene molto")
        assert t.status == "waiting"
        with pytest.raises(BondError):
            bonds.add_thread("colloquio di lavoro da ACME", "2026-06-13T18:00")


def test_due_threads_mature_at_right_time():
    with tempfile.TemporaryDirectory() as tmp:
        bonds = _make(tmp)
        past = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        future = (datetime.now() + timedelta(days=2)).isoformat(timespec="seconds")
        ready = bonds.add_thread("esame di analisi stamattina", past)
        bonds.add_thread("partita di campionato", future)
        due = bonds.due_threads()
        assert [t.id for t in due] == [ready.id]


def test_thread_lifecycle_asked_then_closed():
    with tempfile.TemporaryDirectory() as tmp:
        bonds = _make(tmp)
        past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        t = bonds.add_thread("visita medica di controllo", past)
        bonds.mark_asked(t.id)
        # Già chiesto: non più tra i maturi
        assert bonds.due_threads() == []
        # Ma ancora aperto finché l'owner non risponde
        assert len(bonds.open_threads()) == 1
        bonds.close_thread(t.id, outcome="tutto bene")
        assert bonds.open_threads() == []
        with pytest.raises(BondError):
            bonds.close_thread(t.id)


# ── Ritmo spontaneo ──

def test_spontaneous_budget_per_day():
    with tempfile.TemporaryDirectory() as tmp:
        bonds = _make(tmp)
        ok, _ = bonds.can_write_spontaneous()
        assert ok
        for _ in range(MAX_SPONTANEOUS_PER_DAY):
            bonds.record_spontaneous()
            # Azzera il gap per testare solo il tetto giornaliero
            bonds._spontaneous["last_sent_at"] = time.time() - MIN_GAP_SECONDS - 1
            bonds._save()
        ok, reason = bonds.can_write_spontaneous()
        assert not ok
        assert "spontanei oggi" in reason


def test_spontaneous_min_gap():
    with tempfile.TemporaryDirectory() as tmp:
        bonds = _make(tmp)
        bonds.record_spontaneous()
        ok, reason = bonds.can_write_spontaneous()
        assert not ok
        assert "recente" in reason


def test_owner_reply_resets_ignored_streak():
    with tempfile.TemporaryDirectory() as tmp:
        bonds = _make(tmp)
        bonds.record_spontaneous()
        bonds.record_owner_reply()  # risposta entro la finestra
        assert bonds._spontaneous["last_replied"] is True
        assert bonds._spontaneous.get("ignored_streak", 0) == 0


def test_ignored_messages_make_agent_shy():
    with tempfile.TemporaryDirectory() as tmp:
        bonds = _make(tmp)
        # 3 spontanei ignorati (l'owner risponde sempre fuori finestra)
        for _ in range(IGNORED_STREAK_TO_BACK_OFF):
            bonds.record_spontaneous()
            bonds._spontaneous["last_sent_at"] = time.time() - (13 * 3600)
            bonds._save()
            bonds.record_owner_reply()  # arriva tardi → ignorato
        assert bonds._spontaneous["ignored_streak"] >= IGNORED_STREAK_TO_BACK_OFF
        # Da timido: max 1 al giorno
        bonds._spontaneous["day"] = datetime.now().date().isoformat()
        bonds._spontaneous["count_today"] = 1
        bonds._spontaneous["last_sent_at"] = time.time() - (25 * 3600)
        bonds._save()
        ok, _ = bonds.can_write_spontaneous()
        assert not ok


# ── Silenzio e rendering ──

def test_silence_uses_presence():
    with tempfile.TemporaryDirectory() as tmp:
        memory_dir = os.path.join(tmp, "memory")
        bonds = Bonds(memory_dir)
        assert bonds.silence_seconds() == 0.0  # mai visto
        from core.presence import Presence
        Presence(memory_dir).touch("cli")
        assert 0 <= bonds.silence_seconds() < 5


def test_heartbeat_state_mentions_due_threads():
    with tempfile.TemporaryDirectory() as tmp:
        bonds = _make(tmp)
        assert bonds.heartbeat_state() == "" or "spontanei" in bonds.heartbeat_state()
        past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        bonds.add_thread("colloquio da ACME stamattina", past, why="ci teneva")
        state = bonds.heartbeat_state()
        assert "Fili maturi" in state
        assert "colloquio da ACME" in state


def test_render_status():
    with tempfile.TemporaryDirectory() as tmp:
        bonds = _make(tmp)
        assert "No threads" in bonds.render_status()
        future = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
        t = bonds.add_thread("esame di guida", future)
        assert "esame di guida" in bonds.render_status()
        bonds.close_thread(t.id, outcome="patente presa!")
        assert "patente presa" in bonds.render_status()


# ── Regressione: heartbeat tick NON deve mangiare il budget spontaneo ──

def test_heartbeat_tick_does_not_consume_budget():
    """L'heartbeat autonomo gira ogni ~30min e chiama record_spontaneous a
    ogni turno. Se ogni tick contasse come messaggio, in 4 ore il budget
    della giornata (MAX_SPONTANEOUS_PER_DAY=2) sarebbe saturo e l'agente
    resterebbe muto fino a mezzanotte. Il tick deve passare delivered=False."""
    with tempfile.TemporaryDirectory() as tmp:
        bonds = _make(tmp)
        # Simula 20 tick di heartbeat di fila
        for _ in range(20):
            bonds.record_spontaneous(delivered=False)
        # Il budget non deve essersi mosso
        assert bonds._spontaneous.get("count_today", 0) == 0
        # E l'agente deve poter ancora scrivere
        ok, _ = bonds.can_write_spontaneous()
        assert ok, "tick di heartbeat non dovrebbe bloccare i messaggi veri"


def test_only_delivered_spontaneous_count():
    """Solo i messaggi realmente recapitati all'owner contano per il budget."""
    with tempfile.TemporaryDirectory() as tmp:
        bonds = _make(tmp)
        # 5 tick silenziosi + 1 messaggio vero
        for _ in range(5):
            bonds.record_spontaneous(delivered=False)
        bonds.record_spontaneous(delivered=True)
        # Solo il delivered consuma budget: i 5 tick silenziosi no.
        assert bonds._spontaneous["count_today"] == 1
        # Il budget giornaliero ha ancora spazio (1 < MAX). Se ora can_write
        # blocca, dev'essere SOLO per il min-gap fra spontanei (appena inviato),
        # non per budget esaurito: così non mascheriamo un bug di conteggio.
        assert bonds._spontaneous["count_today"] < MAX_SPONTANEOUS_PER_DAY
        ok, reason = bonds.can_write_spontaneous()
        assert ok or "troppo recente" in reason, reason
