"""
Entry point worker per subagent process-based.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.subagent_runtime import run_subagent_job, write_state_file


def main() -> int:
    parser = argparse.ArgumentParser(description="openvurp subagent worker")
    parser.add_argument("--job", required=True, help="Path del file job JSON")
    parser.add_argument("--state", required=True, help="Path del file stato JSON")
    args = parser.parse_args()

    with open(args.job, "r", encoding="utf-8") as handle:
        job = json.load(handle)

    state = {
        "id": job.get("id", ""),
        "status": "running",
        "pid": os.getpid(),
        "started_at": time.time(),
        "job_path": args.job,
        "state_path": args.state,
        "backend": job.get("backend", ""),
        "model": job.get("model", ""),
        "mode": job.get("mode", ""),
        "parent_session_key": job.get("parent_session_key", ""),
        "child_session_key": job.get("child_session_key", ""),
    }
    write_state_file(args.state, state)

    outcome = run_subagent_job(job)
    state.update(outcome)
    write_state_file(args.state, state)
    return 0 if state.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
