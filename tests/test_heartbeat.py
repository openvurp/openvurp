"""Test per heartbeat e messaggi proattivi."""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.heartbeat import HeartbeatConfig, HeartbeatRunner


def test_heartbeat_filters_only_trivial_acks():
    runner = HeartbeatRunner(HeartbeatConfig(), workspace_dir=".")

    assert runner._is_non_actionable_ack("ok")
    assert runner._is_non_actionable_ack("ricevuto")
    assert not runner._is_non_actionable_ack("Ti ricordo il deploy di stasera.")
    assert not runner._is_non_actionable_ack("Passo io a risentirti domani mattina.")


def _make_runner(tmp, state="stato fermo", idle_skip=True):
    """Runner strumentato: stato controllabile, agente che conta le chiamate.

    active hours 0-24 (sempre attivo), consolidamento notturno disattivato.
    """
    cfg = HeartbeatConfig(idle_skip=idle_skip,
                          active_hours_start=0, active_hours_end=24)
    runner = HeartbeatRunner(cfg, workspace_dir=tmp)
    runner._maybe_consolidate_memory = lambda: None
    runner._state = {"text": state}
    runner._collect_live_state = lambda: runner._state["text"]
    calls = []
    runner.set_agent_callback(lambda prompt: calls.append(prompt) or "HEARTBEAT_OK")
    return runner, calls


def test_idle_beats_skip_the_llm():
    """Il risparmio token: stato identico ⇒ dal secondo battito in poi
    l'LLM non viene chiamato."""
    with tempfile.TemporaryDirectory() as tmp:
        runner, calls = _make_runner(tmp)

        runner._run_heartbeat("interval")   # primo battito: sempre pieno
        runner._run_heartbeat("interval")   # niente di nuovo: skip
        runner._run_heartbeat("interval")   # ancora fermo: skip

        assert len(calls) == 1
        assert runner.get_last_event().reason.startswith("idle")


def test_state_change_wakes_the_agent():
    with tempfile.TemporaryDirectory() as tmp:
        runner, calls = _make_runner(tmp)

        runner._run_heartbeat("interval")
        runner._state["text"] = "Open loops aperti (1): chiudere il deploy"
        runner._run_heartbeat("interval")

        assert len(calls) == 2


def test_queued_event_forces_full_beat():
    with tempfile.TemporaryDirectory() as tmp:
        runner, calls = _make_runner(tmp)

        runner._run_heartbeat("interval")
        runner.add_event("Internet è tornato dopo 12 min")
        runner._run_heartbeat("interval")

        assert len(calls) == 2
        assert "Internet è tornato" in calls[1]


def test_full_beat_due_after_quiet_period():
    """La vita autonoma non muore: anche senza eventi, un battito pieno
    è garantito almeno ogni full_beat_every_seconds."""
    with tempfile.TemporaryDirectory() as tmp:
        runner, calls = _make_runner(tmp)

        runner._run_heartbeat("interval")
        runner._run_heartbeat("interval")           # skip
        runner._last_full_beat_at = time.time() - 5 * 3600
        runner._run_heartbeat("interval")           # dovuto: pieno

        assert len(calls) == 2


def test_manual_trigger_is_never_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        runner, calls = _make_runner(tmp)

        runner._run_heartbeat("interval")
        runner._run_heartbeat("manual")     # trigger esplicito: sempre pieno

        assert len(calls) == 2


def test_idle_skip_can_be_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        runner, calls = _make_runner(tmp, idle_skip=False)

        runner._run_heartbeat("interval")
        runner._run_heartbeat("interval")

        assert len(calls) == 2


def test_config_parses_two_tier_options():
    cfg = HeartbeatConfig.from_dict({"idle_skip": False, "full_beat_every": "2h"})
    assert cfg.idle_skip is False
    assert cfg.full_beat_every_seconds == 7200


if __name__ == "__main__":
    test_heartbeat_filters_only_trivial_acks()
    test_idle_beats_skip_the_llm()
    test_state_change_wakes_the_agent()
    test_queued_event_forces_full_beat()
    test_full_beat_due_after_quiet_period()
    test_manual_trigger_is_never_skipped()
    test_idle_skip_can_be_disabled()
    test_config_parses_two_tier_options()
    print("Tutti i test heartbeat passati!")
