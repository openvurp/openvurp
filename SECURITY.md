# Security Policy

openvurp is an agent runtime with file, shell, browser, process, network, and notification tools. Treat it like privileged local software.

## Secrets

- Never commit `.env`, API keys, bot tokens, session cookies, or private memory files.
- Use `.env.example` as the only public configuration template.
- Run `python3 scripts/secret_scan.py` before publishing or opening a PR.
- Rotate any credential that was ever committed, pasted into logs, or shared outside your machine.

## Safe Defaults

- `SANDBOX_MODE=restricted` is the default.
- External channels should be paired/allowlisted before use.
- High-risk tool calls should require approval.
- Runtime state under `memory/` and `logs/` is local/private by default.

## Reporting

Until public issue triage is set up, report vulnerabilities privately to the project owner.

Include:

- affected version or commit
- reproduction steps
- impact
- whether credentials, local files, browser state, or external channels are involved
