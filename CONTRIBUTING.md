# Contributing

openvurp is moving toward a public, security-conscious agent runtime. Contributions should preserve that direction.

## Before Opening Changes

Run:

```bash
python3 scripts/secret_scan.py
python3 -m compileall -q core tools channels agent.py main.py config.py
python3 -m pytest -q
```

If `pytest` is not installed, install the dev extra first:

```bash
python3 -m pip install -e ".[dev]"
```

## Engineering Rules

- Keep secrets out of source code.
- Prefer explicit permissions over implicit trust.
- Add tests for safety, routing, parsing, memory, and tool behavior.
- Keep tool output bounded and structured.
- Document new environment variables in `.env.example`.
- Do not add network side effects without an approval path.

## Learning Loop Rules

Future self-improvement features must be verifiable:

- generated skills start as candidates
- risky skills require review before activation
- each promoted skill records provenance
- each skill can be disabled or rolled back
