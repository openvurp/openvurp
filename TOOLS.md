# TOOLS.md - Local Tool Notes

This file is a cheat sheet for the openvurp workspace. It does not grant tool access; the runtime tool registry does that.

## Workspace

- Root: the directory containing `config.py` (the runtime injects the real absolute path every turn — trust that, not a hardcoded value)
- Runtime config: `config.py`
- Example env: `.env.example`
- Heartbeat config: `heartbeat.json`
- Environment snapshot: `memory/environment.json`
- Reset baseline: `.reset_baseline/`

## Memory And State

- Daily notes: `memory/YYYY-MM-DD.md`
- Curated memory: `MEMORY.md`
- Audit log: `memory/audit/audit.jsonl`
- Runtime sessions: `memory/sessions/`
- Session snapshots: `memory/session_store/`
- Learning events: `memory/learning/events.jsonl`
- Learning candidates: `memory/learning/candidates.json`
- Lessons: `memory/lessons/`
- Task journal: `memory/task_journal/YYYY-MM-DD.jsonl`
- Reflections: `memory/reflections/YYYY-MM-DD.jsonl`
- Open loops: `memory/open_loops.json`
- Agent state: `memory/agent_state.json`

## Useful Commands

- Secret scan: `python3 scripts/secret_scan.py`
- Compile check: `python3 -m compileall -q core tools tests scripts`
- Test runner when available: `python3 -m pytest -q`
- Fallback tests: run individual `tests/test_*.py` files.

## Audio And Voice

- Desktop default is silent: `VOICE_ENABLED=false`, `VOICE_TOOLS_ENABLED=false`, `MIC_ENABLED=false`.
- File transcription can stay available with `AUDIO_ENABLED=true` and `AUDIO_TRANSCRIBE_ENABLED=true`.
- Disable all audio handling with `AUDIO_ENABLED=false`.
- Enable speech only when wanted: `VOICE_ENABLED=true`, then optionally `VOICE_TOOLS_ENABLED=true` and `MIC_ENABLED=true`.
- Runtime CLI toggles: `/voice on|off`, `/mic on|off`, `/audio on|off`.

## Operating Notes

- Prefer structured tools for file reads, edits, searches, memory, and runtime state.
- Use shell for tests, package managers, git, and real system commands.
- Use the browser tools for pages and visual/browser verification.
- Use `agent_state`, task journal, and open loops for durable work.
- When code changes touch reset-tracked files, update the reset baseline after tests.

## Rule

Never store secrets here.
