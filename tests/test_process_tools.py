"""Test per sessioni terminali in background."""

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.process import (
    process_start_handler,
    process_read_handler,
    process_write_handler,
    process_stop_handler,
    process_sessions_handler,
)


def _field(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}=(.*)$", text, re.MULTILINE)
    assert match, f"Campo {name} non trovato in:\n{text}"
    return match.group(1).strip()


def test_background_process_can_start_read_and_stop():
    start = process_start_handler(
        "python3 -u -c \"import sys,time; print('ready'); sys.stdout.flush(); time.sleep(30)\""
    )
    assert start.success, start.error
    session_id = _field(start.output, "session_id")

    try:
        time.sleep(0.4)
        read = process_read_handler(session_id, max_chars=2000)
        assert read.success, read.error
        assert "ready" in read.output

        sessions = process_sessions_handler()
        assert sessions.success, sessions.error
        assert session_id in sessions.output
    finally:
        stop = process_stop_handler(session_id, force=True)
        assert stop.success, stop.error


def test_background_process_accepts_input():
    start = process_start_handler(
        "python3 -u -c \"import sys,time; print('boot'); sys.stdout.flush(); "
        "line = sys.stdin.readline().strip(); print('echo:' + line); sys.stdout.flush(); time.sleep(30)\""
    )
    assert start.success, start.error
    session_id = _field(start.output, "session_id")

    try:
        time.sleep(0.4)
        write = process_write_handler(session_id, "ciao")
        assert write.success, write.error

        time.sleep(0.4)
        read = process_read_handler(session_id, max_chars=3000)
        assert read.success, read.error
        assert "echo:ciao" in read.output
    finally:
        stop = process_stop_handler(session_id, force=True)
        assert stop.success, stop.error


if __name__ == "__main__":
    test_background_process_can_start_read_and_stop()
    test_background_process_accepts_input()
    print("Tutti i test process tools passati!")
