# openvurp Roadmap

Where openvurp is and where it's going. Everything marked **done** is in the
code today, with tests. The rest is honest direction, not a promise.

**What openvurp is:** the place where you keep your agents. Not an agent
itself. Anything that gave openvurp a character of its own — an identity, a
diary, dreams, a life of its own between your messages — has been removed. That
decision is what most of the sections below now hang from.

## 1. The roster

- done: agents you create — name, job, engine — and none shipped with the product
- done: one conversation per agent, on disk, surviving restarts
- done: every agent carries the roster in its own tools, and asks the right colleague itself
- done: `ask_everyone` — a question to the whole roster, silence from those it doesn't concern
- done: a consulted colleague works **in the conversation you are looking at** — his
  commands, his text as it arrives, the seconds passing — and what he did is saved
  with his answer. Before, it all went into his own conversation, and the asker's
  page sat still for minutes looking stuck
- done: an agent can be renamed or removed from the page; its conversation is archived, not destroyed
- per-agent tool allow-lists (today the toolset is the same for everyone)
- duplicate an agent as a starting point for a new one

## 2. The room

- done: they answer each other, not you in parallel — everyone reads what came before
- done: it ends when a round passes with nobody adding anything, or on Stop
- done: whoever opened it writes the close: what was agreed, what wasn't, who dissents
- done: inventing a consensus is forbidden; "we didn't decide" is a valid ending
- done: repeating yourself counts as silence, not as a turn
- done: bounded by `MULTIPLAYER_MAX_AGENTS`, `MULTIPLAYER_MAX_ROUNDS`, `MULTIPLAYER_DAILY_CALL_BUDGET`,
  all three editable from Settings
- done: **the room says who is missing.** Whoever you call by name is in and
  speaks first; if the cap leaves someone out, or someone cannot answer, a line
  from openvurp says so in the room, at that moment, and the others are told not
  to wait. Nobody may write a line in a colleague's name: the runtime strips it
- done: a room that crashes closes its run and tells the page, instead of
  showing "in discussion" until the five-minute sweeper
- let the owner interject mid-discussion without stopping it

## 3. Safety

- done: unified policy engine on every tool call
- done: approvals by tool, actor, channel, path and risk; leases with TTL, scoped and revocable
- done: sandbox for file and shell tools; destructive commands blocked in every mode
- done: pacts — rules you set once, enforced above every approval mode
- done: privacy router — private sessions stay on a local model
- done: audit log with secret redaction
- done: web/document content marked as data, never as instructions
- dry-run preview for risky actions

## 4. What an agent learns

- done: corrections and tool failures recorded as learning events
- done: review step producing lesson candidates, promoted only under guard
- done: the Mirror — corrections replayed later to check they don't come back
- done: **per agent** — `memory/agents/<id>/`, keyed by id so a rename costs nothing
- done: rollback of a lesson that stops holding
- lessons visible in the page, per agent, with their evidence
- tests before promotion, and provenance on every lesson

## 5. Memory

- done: semantic memory for stable facts (SQLite FTS5 + optional embeddings)
- done: hybrid retrieval — keyword plus vector
- done: fading — memories never recalled quietly archive, reversibly
- done: **per agent** — `memory/agents/<id>/`, and an agent now reads its own
  memories back; before, `remember` wrote into a shared store that only the
  platform ever consulted
- **`memory/uploads` is not program state.** Everything else under `memory/` can
  be rebuilt — conversations happen again, lessons are learned again. The files
  you sent the agents cannot. No automatic backup is planned (a machine that
  runs forever to protect rebuildable things is not worth its keep), but an
  export you press when you want one is

## 6. Always on

- done: headless systemd service (`Restart=always`), survives reboots
- done: sentinel — watches network and model backend, notices outages and recoveries
- done: LLM retry with backoff, so a hiccup doesn't drop the turn
- done: restart in place from `/update`, terminal stays open
- done: heartbeat reduced to mechanical work — fading and the mirror, nothing autobiographical
- reminders (`tools/scheduler.py`) that fail to send are dropped without a trace;
  they should stay, with the error, and be shown
- Windows helper for reboot persistence

## 7. Tools and skills

- done: markdown skills with frontmatter, loaded on demand
- done: candidate skills stay inert until approved
- done: the Forge — an agent drafts and tests a missing tool; adopting it is the owner's call
- done: drop-in plugins
- skill metadata: triggers, permissions, tests, version
- secret scan and permission review on generated skills

## 8. Subagents

- done: real worker subprocesses (kill- and timeout-safe), not just text
- done: model router — heavy work out, light work local
- roles, task graphs, isolated workspaces
- budgets for time, tokens and permissions

## 9. Channels

- done: one core every channel goes through — Telegram, Discord, Slack, WhatsApp
- done: a test forbids any file under `channels/` from touching the roster, the rooms or the swarm
- done: per-channel allow-lists; an empty one refuses to start
- done: shared grammar (`@name`, `/agents`, `/all`, `/stop`, `/help`)
- done: a message from the phone lands in the agent's own conversation
- done: the room's notices and errors reach the phone too (a spent budget used to
  answer with nothing); Telegram shows "typing" while an agent works and reports
  a broken turn; the tap-a-name keyboard exists (it called a method that did not);
  Discord splits long replies instead of cutting them; a dead WhatsApp bridge says so
- **approvals do not reach the channels**: a tool that needs permission, asked
  from the phone, waits on the page and is denied after 180 s with no message
- one conversation per agent **and per person**: today two allowed users writing to the same agent share its thread
- approval controls from the channels, not just from the page

## 10. Engines

- done: Ollama and any OpenAI-compatible local server, discovered by knocking on the usual ports
- done: subscription CLIs (Codex, Claude) with `OPENAI_API_KEY` stripped so they can't fall back to billing
- done: per-agent engine and model
- done: token ceilings per turn, per context and per day
- cost, latency and quality tracking
- judge mode for critical answers

## 11. Release

Continuous integration is deliberately minimal for now: the tests run on push,
nothing else.

- done: the declared Python floor is checked against the sources, not assumed
- full CI: the suite on every supported Python, plus the secret scanner
- **built executables that start the whole thing**, so running openvurp does not
  require a Python toolchain
