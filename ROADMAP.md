# openvurp Roadmap

Where openvurp is and where it's going. Everything marked **done** is in the
code today, with tests. The rest is honest direction, not promises — a public
roadmap with open items is an invitation, not a weakness.

## 1. Public Hygiene

- done: env-based secrets
- done: `.env.example`
- done: secret scanner (`scripts/secret_scan.py`)
- done: README, SECURITY
- done: reproducible install and test commands
- done: code integrity baseline + verify (`/integrity`)

## 2. Safety Core

- done: unified policy engine for every tool call
- done: approval requirements by tool, actor, channel, path, and risk
- done: audit logs with secret redaction
- done: capability leases for temporary permissions (scoped, TTL, revocable)
- done: sandbox for file/shell tools; critical commands hard-blocked in every mode
- done: pacts — owner-negotiated rules enforced above all approval modes
- done: privacy router — private sessions stay on a local model
- dry-run preview for risky actions

## 3. Verified Learning Loop

- done: local learning events for feedback and tool failures
- done: review step that creates memory/lesson candidates
- done: guarded promotion of candidates into memory lessons
- done: structured task journal
- done: reflection after completed work
- done: the Mirror — corrections replayed nightly, scored in `/growth`
- memory, lesson, and skill candidates from full task traces
- secret scan and permission review for generated skills
- tests/dry-runs before promotion
- skill score, provenance, versioning, and rollback

## 4. Memory System

- done: semantic memory for stable facts (SQLite FTS5 + optional embeddings, `remember` tool)
- done: hybrid retrieval — keyword + vector/FTS5
- done: nightly curator — consolidates useful daily notes into long-term memory
- done: nightly dreaming — non-obvious insights over the week
- done: fading — memories never recalled quietly archive (reversible)
- episodic memory for events
- procedural memory for reusable methods (early scaffolding in `core/method.py`)
- summary-level retrieval

## 5. Resilience & Always-On

- done: headless systemd service (`Restart=always`), survives reboots
- done: sentinel — watches internet, LLM backend and Telegram; notices outages
  **and recoveries**, warns the owner, auto-reattaches Telegram, resumes suspended work
- done: LLM connection retry with backoff (Ollama hiccups don't drop the turn)
- done: restart-in-place from `/update` and `/restart`, terminal stays open
- Windows Task Scheduler helper for full-reboot persistence

## 6. Skill Runtime

- done: candidate vs active skills (candidates stay inert until approved)
- done: markdown skills with frontmatter, loaded on demand
- skill metadata: triggers, permissions, tests, owner, version
- import/export for compatible agent ecosystems

## 7. Sub-Agent Orchestration

- done: real worker subprocesses (kill/timeout-safe), not just text
- done: model router — heavy work to cloud, light work local
- task graph execution
- roles: researcher, coder, reviewer, tester, operator
- isolated workspaces with controlled merge back into parent context
- budgets for time, tokens, tools, and permissions

## 8. Gateway And Channels

- done: Telegram — owner/guest roles, media (photos/voice/docs), inline confirms
- done: per-channel session isolation
- done: group-chat privacy rules + chat-id whitelist (private memory never leaks to groups)
- done: web dashboard — persistent multi-chat + live activity stream + runtime panels, token-gated
- done: multiplayer rooms — persistent peer profiles, attributed messages, peer review and bounded coordinator synthesis
- done: token economy — dynamic tool packs, schema-aware budgets, compact route history and per-turn ceilings
- hardened Telegram pairing
- Discord, Slack, Signal, email (scaffolding present)
- dashboard approval controls

## 9. Model Router

- done: local model route for private/sensitive tasks
- done: fallback backend on retryable failures
- task-aware routing
- cost, latency, and quality tracking
- judge mode for critical outputs

## 10. Release

The repository is private today, so continuous integration is deliberately
minimal: tests on push, nothing else. It is not worth building a pipeline for
an audience of one.

- when it goes public: full CI — the suite across every supported Python, the
  secret scanner, and **built executables that start the whole thing**, so
  running openvurp does not require a Python toolchain
- done: declared Python floor is checked against the sources, not assumed
