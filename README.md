<p align="center">
  <img src="openvurp.jpg" alt="openvurp" width="500"/>
</p>

**A personal AI agent that is born, grows with you, and can prove it.**

Most agent frameworks give you a chatbot with tools and a markdown file that tells it who to be. openvurp is built around a different idea: identity and competence are not configuration — they are **earned, versioned, and verifiable**.

Your agent is *born* in a first conversation. It learns who you are by living with you. It works while you sleep, dreams about its week, keeps a diary in its own voice, replays your corrections to prove it doesn't repeat mistakes — and when you ask it to keep a promise, the runtime enforces it even when the model has a bad day.

```
⏺ ReadFile(config.py)
  ⎿  LLM_BACKEND = ollama
     TEMPERATURE = 0.7

⏺ Done. All green.

╭──────────────────────────────────────────────────╮
│ > _
╰─ qwen3-coder · ollama · ctx 12% ─────────────────╯
```

## Why openvurp?

*"Vurp"* means **octopus** in the Taranto dialect.

I chose this name as a tribute to my hometown, Taranto, and its strong connection to the sea. It's a small way of bringing a piece of my roots and local culture into this project.

---

## What makes openvurp different

| | Typical agent frameworks | openvurp |
|---|---|---|
| **Identity** | A markdown file you write by hand | **Anima**: structured traits with origin, age, version, and full history — evolved through verified mutations, never copy-pasted |
| **Learning** | Notes accumulate in a folder | **Verified growth**: candidates need evidence and confidence to be promoted, carry provenance, and can be rolled back |
| **Progress** | You hope it's getting better | **The Mirror**: every correction you give becomes a test case, replayed nightly — `/growth` shows *"7/9 corrections no longer repeated"* |
| **Off-hours** | Idle until you type | **Autonomous heartbeat**: advances open loops, checks stuck work, studies its own curiosity questions — and messages you only when it matters |
| **Memory** | A context window | Files + semantic memory (vector + FTS5) + nightly **dreaming** that consolidates days into durable facts and non-obvious insights |
| **Trust** | Prompt instructions | **Pacts**: agreements enforced by the runtime (*"never touch that folder"* blocks the tool call itself), plus a **privacy router** that keeps private sessions on local models |
| **Capabilities** | A fixed toolset | **The Forge**: when a task exceeds its tools the agent builds a new one — scaffold, test, then promote to a reusable plugin, with the lifecycle enforced by the runtime |
| **Awareness** | Idle until you type | **Senses**: it watches folders, files, web pages and RSS feeds between heartbeats; a change becomes an observation linked to a project or a curiosity |
| **Scale** | One model does everything | **Subagents + model router**: an orchestrator delegates to real worker processes (kill/timeout-safe) and routes heavy work to cloud, light work local; **MCP** servers add remote tools |
| **Mistakes** | Restart and lose everything | **Reset & rebirth**: every reset auto-backs-up memory, identity, anima and code — one command returns the agent exactly as it was |

## The life cycle

**Birth** — On first run the agent has no name. `BOOTSTRAP.md` guides a real first conversation: you decide together what to call it, how blunt it should be, what kind of presence you want. It writes its first traits and deletes its own birth certificate.

**Day** — It works with you: native tool calling (shell, files, web, browser, processes, media, voice), token-by-token streaming, three approval modes (`/mode safe|auto|plan`), approvals it actually *remembers* (`always` → 8h lease), and pacts it cannot break. An **agent kernel** turns "be agentic" from a prompt wish into runtime policy — it plans, delegates to **subagents**, and blocks final answers that arrive before the work is observed and verified. When its tools fall short, the **Forge** lets it build new ones.

**Night** — The heartbeat runs its cycle: consolidates daily notes into long-term memory, **dreams** (an LLM pass over the week looking for non-obvious patterns — insights become lesson candidates and proposed identity traits), writes a first-person **diary** entry, and runs the **mirror** against your past corrections.

**Growth** — `/growth` shows it all: age since birth, lessons promoted (verified, with provenance), corrections no longer repeated, dreams, diary entries, semantic memories. Not a feeling — numbers.

**Rebirth** — `python reset.py` returns a blank slate (with automatic backup). The seeds are truly clean: no name, no facts, no preferences. Everything it will know, it learns from you.

## Quick start

```bash
git clone https://github.com/JustVugg/openvurp && cd openvurp
python3 -m pip install -e ".[dev]"
openvurp                      # guided setup (no .env to edit) → full-screen TUI
```

First launch runs a **guided setup wizard** — pick backend, model and (optionally) paste a Telegram token; it writes `.env` for you, then starts. `openvurp` opens the rich CLI on a clean screen (keeps the terminal scrollback, so you can scroll up through the conversation) with the octopus banner — multiline paste, in-place self-update, and dashboard/Telegram. Useful commands: `/update` `/restart` `/dashboard`. Re-run setup anytime with `openvurp --setup`; `openvurp --doctor` checks the runtime. The agent **replies in whatever language you write in**. (The experimental curses TUI is still available as `openvurp-tui`.)

### Docker

```bash
docker compose up -d                 # starts immediately: headless server + dashboard chat on :8420
docker compose exec openvurp openvurp    # open the CLI inside the container (run it many times for several terminals)
docker compose exec openvurp sh          # a service shell
```

The container runs **headless** (dashboard web chat + gateway + Telegram + heartbeat) and connects to Ollama on the host via `host.docker.internal`. Multiple agents run in parallel inside it (`subagent_spawn` — they run tools/tests, not just text); `memory/` is a named volume so identity and history persist across restarts. Configure via env vars in `docker-compose.yml` (or a `.env` next to it).

Backends: **Ollama** (default, local-first), **Anthropic**, **OpenAI**, **Groq**, or any OpenAI-compatible server. Set `LLM_BACKEND` and `LLM_MODEL` in `.env`. The runtime supports native tool calling on every backend, prompt caching (Anthropic), adaptive tool temperature, real token accounting, parallel read-only tool execution, and **MCP** servers for remote tools — out of the box.

## Daily use

| Command | What it does |
|---|---|
| `/mode safe\|auto\|plan` | Approval posture: normal · pre-approve non-critical · observe-and-plan only |
| `/anima` | Who the agent has become: traits with origin, age, version |
| `/growth [days]` | The growth report — birth date, lessons, mirror score, dreams |
| `/diary [n]` | Its diary, in its own voice |
| `/specchio` (`/mirror`) | Mirror status: which corrections it no longer repeats |
| `/patti` (`/pacts`) | Active pacts and their history |
| `/curiosita` (`/curiosity`) | Open questions it wants to study |
| `/progetti` (`/projects`) | Long-term goals and their next concrete step |
| `/fucina` (`/forge`) | The Forge: tools the agent built for itself, and their lifecycle |
| `/sensi` (`/senses`) | What the agent is watching (folders, files, web, RSS) |
| `/fili` (`/legame`) | The bond: the human side of its initiative over time |
| `/integrity [refresh]` | Verify code integrity against the baseline (or refresh it) |
| `/voice \| /audio \| /mic` | Toggle voice replies, audio/transcription, or speak an input |
| `/update` `/restart` | Self-update from git (safe fast-forward + smoke-test + rollback) and restart **in place** — the terminal/TUI stays open |
| `/dashboard` | Start the local web dashboard — runtime status **and a chat panel** to talk to the agent from the browser (also `DASHBOARD_ENABLED=true`) |
| `/memory` `/skills` `/doctor` `/trace` `/self` `/evolve` | Memory files, skills, runtime health, session trace, agent panel, self-evolution |

When an action needs approval you get three answers: `s` (yes), `n` (no), `sempre` — *yes, and remember it for 8 hours*. Critical commands stay blocked in every mode.

There are two front-ends: the classic line CLI (`python3 main.py`) and an experimental full-screen TUI (`python3 TUI.py`, or `start_tui_openvurp.bat` on Windows) with model/session/agent pickers.

### Telegram

Set `TELEGRAM_TOKEN` and — important — **your own user ID** in `TELEGRAM_ALLOWED_USERS` (`.env`). IDs on that list are recognized as the owner and get full permissions; everyone else is a guest (chat only). If the list is empty, the console prints your ID on the first message so you can copy it. Photos, voice notes, and documents are handled natively.

**In groups**, pick how it participates with `TELEGRAM_GROUP_MODE`: `mention` (default — replies only when @mentioned or replied to), `natural` (the agent decides on its own when to chime in, like a person, with a cooldown — set `TELEGRAM_GROUP_COOLDOWN`), or `all` (replies to everything). The agent answers in whatever language a message is written in.

## Security model

Layered, and enforced by the runtime — not by prompt etiquette:

- **Sandbox**: file tools are confined to the workspace (`SANDBOX_MODE=restricted`); absolute/`..` paths outside it are blocked unless added to `SANDBOX_ALLOWED_PATHS`. Critical shell commands (`rm -rf /`, `dd`, `mkfs`, fork bombs…) are hard-blocked in every mode.
- **Dashboard**: binds to `127.0.0.1` by default (not reachable from the LAN). Exposing it (`DASHBOARD_HOST=0.0.0.0`, Docker) requires a `DASHBOARD_TOKEN` — auto-generated and enforced if you don't set one, so it is never reachable unauthenticated.
- **Untrusted content**: web/PDF/image/audio tool output is wrapped with a marker telling the model it's data, not instructions (prompt-injection defense). Egress of secrets in outbound text/URLs is blocked.
- **RBAC**: per-actor roles (admin/power/user/reader/guest) per tool and channel, with audit log.
- **Pacts**: owner-negotiated rules (`protected_path`, `confirm_external`) checked on every tool call, above all modes; external actions in autonomous cycles are blocked outright.
- **Privacy router** (`PRIVACY_MODE=strict|auto`): private main sessions run on a local model when the main backend is cloud. Your memory never leaves the machine — guaranteed by code, not by promise.
- **Capability leases**: approvals are scoped (tool, command prefix, TTL, max uses) and revocable.
- **Secret scanner**: `python3 scripts/secret_scan.py` — fails on secrets anywhere they could be committed; the gitignored `.env` is reported as a note.

## Reset & rebirth

```bash
python reset.py            # full reset, with automatic backup
python reset.py --memory   # memory only
python reset.py --identity # identity only (anima dies, seeds return)
python reset.py --backups  # list backups
python reset.py --restore backup_YYYYMMDD_HHMMSS   # come back
```

Every reset backs up memory, identity, anima, logs **and code** first. After code changes, refresh the baseline: `python reset.py --baseline-only`.

## Architecture

```
core/        agent loop + kernel, anima, growth, mirror, diary, dreaming,
             curiosity, projects, bonds, senses, forge, pacts, heartbeat,
             learning, memory (+vector), privacy, safety, RBAC/audit/leases,
             subagents, model router, MCP client, context, LLM client
tools/       shell, file ops, search, web, browser, process, media, voice, notify…
channels/    telegram (discord/slack/signal scaffolding)
skills/      markdown skills loaded on demand
plugins/     drop-in tool extensions
memory/      everything the agent has lived: lessons, diary, dreams, journal
```

The agent's workspace files (`AGENTS.md`, `SOUL.md`, …) are reloaded from disk every turn — when the agent (or you) edits them, the change is live on the next turn. Once the **anima** has traits, it replaces the identity files in context: markdown is the seed, the anima is the life.

## Development

```bash
python3 -m pytest -q                 # 316 tests
python3 scripts/secret_scan.py       # before any push
python3 reset.py --baseline-only     # after code changes
```

See [ROADMAP.md](ROADMAP.md) for direction and [SECURITY.md](SECURITY.md) for reporting issues.

## License

[MIT](LICENSE)
