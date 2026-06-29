"""Test per heartbeat e messaggi proattivi."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.heartbeat import HeartbeatConfig, HeartbeatRunner


def test_heartbeat_filters_only_trivial_acks():
    runner = HeartbeatRunner(HeartbeatConfig(), workspace_dir=".")

    assert runner._is_non_actionable_ack("ok")
    assert runner._is_non_actionable_ack("ricevuto")
    assert not runner._is_non_actionable_ack("Ti ricordo il deploy di stasera.")
    assert not runner._is_non_actionable_ack("Passo io a risentirti domani mattina.")


if __name__ == "__main__":
    test_heartbeat_filters_only_trivial_acks()
    print("Tutti i test heartbeat passati!")
