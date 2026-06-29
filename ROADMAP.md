# openvurp Roadmap

## 1. Public Hygiene

- done: env-based secrets
- done: `.env.example`
- done: secret scanner
- done: README, SECURITY, CONTRIBUTING
- done: reproducible install and test commands
- CI before first public release

## 2. Safety Core

- done: unified policy engine for every tool call
- done: approval requirements by tool, actor, channel, path, and risk
- done: audit logs with secret redaction
- dry-run support for risky actions
- capability leases for temporary permissions

## 3. Verified Learning Loop

- done: local learning events for feedback and tool failures
- done: review step that creates memory/lesson candidates
- done: guarded promotion of candidates into memory lessons
- structured task journal
- reflection after completed work
- memory, lesson, and skill candidates from full task traces
- secret scan and permission review for generated skills
- tests/dry-runs before promotion
- skill score, provenance, versioning, and rollback

## 4. Memory System

- episodic memory for events
- done: semantic memory for stable facts (SQLite FTS5 + optional embeddings, `remember` tool)
- procedural memory for reusable methods
- done: hybrid retrieval: keyword + vector/FTS5 (summary retrieval still open)
- curator that promotes useful daily notes into long memory

## 5. Skill Runtime

- candidate vs active skills
- skill metadata: triggers, permissions, tests, owner, version
- local/private skill marketplace
- import/export for compatible agent ecosystems

## 6. Sub-Agent Orchestration

- task graph execution
- roles: researcher, coder, reviewer, tester, operator
- isolated workspaces
- controlled merge back into parent context
- budgets for time, tokens, tools, and permissions

## 7. Gateway And Channels

- hardened Telegram pairing
- Discord, Slack, Signal, email
- per-channel session isolation
- group-chat privacy rules
- dashboard approval controls

## 8. Model Router

- task-aware routing
- local model route for private/sensitive tasks
- fallback providers
- cost, latency, and quality tracking
- judge mode for critical outputs
