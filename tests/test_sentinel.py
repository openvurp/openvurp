"""Test per la sentinella: caduta e ritorno di internet/Ollama/Telegram.

Tutto senza rete e senza thread: clock finto, probe finti, tick() sincrono.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.sentinel import Sentinel, format_downtime


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeProbe:
    """Probe controllabile: si decide dall'esterno se è su o giù."""
    def __init__(self, ok=True):
        self.ok = ok

    def __call__(self):
        return self.ok


class FakeHeartbeat:
    def __init__(self):
        self.events = []
        self.triggers = []

    def add_event(self, text):
        self.events.append(text)

    def trigger_now(self, reason=""):
        self.triggers.append(reason)


def make_sentinel(tmpdir, clock=None):
    return Sentinel(tmpdir, fails_to_down=2, clock=clock or FakeClock())


def test_one_failure_does_not_flip_down():
    """Debounce: un singolo probe fallito non dichiara il servizio giù."""
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock()
        s = make_sentinel(tmp, clock)
        probe = FakeProbe(ok=True)
        s.add_probe("internet", probe)

        s.tick()  # primo avvistamento: su
        assert s._probes["internet"].status == "up"

        probe.ok = False
        transitions = s.tick()  # 1 fallimento: ancora su
        assert transitions == []
        assert s._probes["internet"].status == "up"


def test_down_after_debounce_notifies_and_logs_event():
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock()
        s = make_sentinel(tmp, clock)
        probe = FakeProbe(ok=True)
        hb = FakeHeartbeat()
        notices = []
        s.add_probe("internet", probe)
        s.attach_heartbeat(hb)
        s.set_notifier(lambda text: notices.append(text) or True)

        s.tick()
        probe.ok = False
        s.tick()
        transitions = s.tick()  # secondo fallimento: giù

        assert len(transitions) == 1
        assert transitions[0].status == "down"
        assert s._probes["internet"].status == "down"
        assert any("Internet" in n for n in notices)
        assert any("caduto" in e for e in hb.events)
        # La caduta NON sveglia l'agente (non c'è nulla da riprendere)
        assert hb.triggers == []


def test_recovery_reports_downtime_and_wakes_heartbeat():
    """Il cerchio si chiude: quando internet torna l'owner viene avvisato con
    la durata del blackout e il heartbeat scatta subito."""
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock()
        s = make_sentinel(tmp, clock)
        probe = FakeProbe(ok=True)
        hb = FakeHeartbeat()
        notices = []
        s.add_probe("internet", probe)
        s.attach_heartbeat(hb)
        s.set_notifier(lambda text: notices.append(text) or True)

        s.tick()                     # su
        probe.ok = False
        s.tick(); s.tick()           # giù
        clock.advance(720)           # 12 minuti senza rete
        probe.ok = True
        transitions = s.tick()       # torna

        assert transitions[0].status == "up"
        assert any("tornato" in n and "12 min" in n for n in notices)
        assert any("tornato" in e for e in hb.events)
        assert "internet_back" in hb.triggers


def test_first_sight_up_is_silent():
    """All'avvio, vedere un servizio su non è una notizia."""
    with tempfile.TemporaryDirectory() as tmp:
        s = make_sentinel(tmp)
        notices = []
        s.add_probe("ollama", FakeProbe(ok=True))
        s.set_notifier(lambda text: notices.append(text) or True)

        s.tick()
        assert notices == []
        assert s._probes["ollama"].status == "up"


def test_starting_already_down_notifies():
    """Partire senza rete VA segnalato (unknown → down)."""
    with tempfile.TemporaryDirectory() as tmp:
        s = make_sentinel(tmp)
        notices = []
        s.add_probe("internet", FakeProbe(ok=False))
        s.set_notifier(lambda text: notices.append(text) or True)

        s.tick(); s.tick()
        assert s._probes["internet"].status == "down"
        assert len(notices) == 1


def test_recover_called_with_backoff():
    """Il recupero (es. riattacca Telegram) parte subito quando il servizio
    cade, poi ritenta con backoff — niente tempeste di restart."""
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock()
        s = make_sentinel(tmp, clock)
        probe = FakeProbe(ok=True)
        recoveries = []
        s.add_probe("telegram", probe, recover=lambda: recoveries.append(clock.now) or True,
                    wake_agent=False)

        s.tick()
        probe.ok = False
        s.tick(); s.tick()           # giù → primo recover immediato
        assert len(recoveries) == 1

        clock.advance(5)
        s.tick()                     # dentro il backoff: nessun retry
        assert len(recoveries) == 1

        clock.advance(60)
        s.tick()                     # oltre il backoff: ritenta
        assert len(recoveries) == 2

        probe.ok = True
        s.tick()                     # tornato: contatori azzerati
        assert s._probes["telegram"].recover_attempts == 0


def test_notices_queued_until_delivered():
    """Se la consegna fallisce (es. Telegram giù) l'avviso resta in coda e
    arriva appena il canale torna: nessuna notizia persa."""
    with tempfile.TemporaryDirectory() as tmp:
        s = make_sentinel(tmp)
        probe = FakeProbe(ok=True)
        delivered = []
        can_deliver = {"ok": False}

        def notifier(text):
            if can_deliver["ok"]:
                delivered.append(text)
                return True
            return False

        s.add_probe("internet", probe)
        s.set_notifier(notifier)

        s.tick()
        probe.ok = False
        s.tick(); s.tick()           # giù, ma la consegna fallisce
        assert delivered == []
        assert len(s._pending_notices) == 1

        can_deliver["ok"] = True
        probe.ok = True
        s.tick()                     # torna: consegna arretrati + ritorno
        assert len(delivered) == 2
        assert s._pending_notices == []


def test_status_persisted_to_json():
    with tempfile.TemporaryDirectory() as tmp:
        s = make_sentinel(tmp)
        probe = FakeProbe(ok=False)
        s.add_probe("ollama", probe)

        s.tick(); s.tick()           # transizione → persistenza
        path = os.path.join(tmp, "memory", "sentinel.json")
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["services"]["ollama"]["status"] == "down"
        assert data["recent_transitions"][-1]["name"] == "ollama"


def test_format_downtime():
    assert format_downtime(45) == "45s"
    assert format_downtime(720) == "12 min"
    assert format_downtime(3900) == "1h 05min"


if __name__ == "__main__":
    test_one_failure_does_not_flip_down()
    test_down_after_debounce_notifies_and_logs_event()
    test_recovery_reports_downtime_and_wakes_heartbeat()
    test_first_sight_up_is_silent()
    test_starting_already_down_notifies()
    test_recover_called_with_backoff()
    test_notices_queued_until_delivered()
    test_status_persisted_to_json()
    test_format_downtime()
    print("Tutti i test sentinella passati!")
