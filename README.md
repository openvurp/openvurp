<p align="center">
  <img src="docs/hero.png" alt="openvurp — a wallet of agents" width="900"/>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-e8654a" alt="MIT license"/></a>
  <img src="https://img.shields.io/badge/python-3.10+-e8654a" alt="Python 3.10+"/>
  <img src="https://img.shields.io/badge/local--first-ollama-e8654a" alt="local-first"/>
</p>

**A wallet of personal AI agents that know each other, ask each other, and actually do the work.**

You make the agents. Each one gets a name, a job and a character — *amanda hunts Amazon deals*, *ciccio watches the bills*, *dev writes code*. They are not personas in a prompt: they have real tools (shell, files, the web, the browser), and from the moment a second one exists **they know about each other**. When a question lands outside its field, an agent asks the colleague whose field it is — on its own, without you remembering who does what.

`openvurp` opens the wallet in your browser. One conversation per agent, a room where they all talk, and — because they run real commands — you watch what they do while they do it.

```
you   → my SSD is dying, what should I get?

dev   ⚙ shell   smartctl -A /dev/nvme0n1
      → 187 reallocated sectors, and climbing. Back up today.
      ⇉ asks amanda: a reliable 1TB NVMe under €90?

amanda → Crucial P3 Plus 1TB, €74, shipped by Amazon.

dev   → So: Crucial P3 Plus, €74. Back up before you swap it.
```

Every agent still carries the whole openvurp runtime underneath: a verified identity, memory that survives restarts, pacts the runtime enforces, an audit trail of every action.

## Why openvurp?

*"Vurp"* means **octopus** in the Taranto dialect.

I chose this name as a tribute to my hometown, Taranto, and its strong connection to the sea. It's a small way of bringing a piece of my roots and local culture into this project.

---

## What makes openvurp different

| | Typical agent frameworks | openvurp |
|---|---|---|
| **Shape** | One assistant, one chat | **A wallet**: agents you create, one conversation each, and a room where they all talk — the roster starts empty on purpose |
| **Teamwork** | You route the question yourself | Every agent sees the roster *inside its own tools* and asks the right colleague on its own — `ask_everyone` puts a question to the whole room, and whoever it doesn't concern stays quiet |
| **Watching them work** | A spinner, then a wall of text | Streaming, the commands they run as they run them, two agents visibly walking over to consult each other, and a permission prompt where *you* asked for the action |
| **Identity** | A markdown file you write by hand | **Anima**: structured traits with origin, age, version, and full history — evolved through verified mutations, never copy-pasted |
| **Learning** | Notes accumulate in a folder | **Verified growth**: candidates need evidence and confidence to be promoted, carry provenance, and can be rolled back |
| **Progress** | You hope it's getting better | **The Mirror**: every correction you give becomes a test case, replayed nightly — `/growth` shows *"7/9 corrections no longer repeated"* |
| **Off-hours** | Idle until you type | **Autonomous heartbeat**: advances open loops, checks stuck work, studies its own curiosity questions — and messages you only when it matters |
| **Memory** | A context window | Files + semantic memory (vector + FTS5) + nightly **dreaming** that consolidates days into durable facts and non-obvious insights |
| **Trust** | Prompt instructions | **Pacts**: agreements enforced by the runtime (*"never touch that folder"* blocks the tool call itself), plus a **privacy router** that keeps private sessions on local models |
| **Capabilities** | A fixed toolset | **The Forge**: when a task exceeds its tools the agent builds a new one — scaffold, test, then promote to a reusable plugin, with the lifecycle enforced by the runtime |
| **Awareness** | Idle until you type | **Senses**: it watches folders, files, web pages and RSS feeds between heartbeats; a change becomes an observation linked to a project or a curiosity |
| **Scale** | One model does everything | **Per-agent engines**: give each one the model that fits — a subscription CLI for the hard ones, a local model for the cheap ones — and they still talk to each other |
| **Group work** | A single prompt pretending to be a team | **A real room**: they argue for as long as they have something to say, stop when nobody adds anything, and whoever opened it closes it — saying what was agreed, what wasn't, and who thinks otherwise |
| **Outages** | Crashes silently, you notice hours later | **Sentinel**: notices when internet or Ollama go down *and when they come back* — tells you, and resumes suspended work on recovery |
| **Mistakes** | Restart and lose everything | **It remembers being wrong**: corrections are recorded, promoted to lessons with provenance, and rolled back when they stop holding |

## The wallet

**The roster starts empty.** No sample agents, no "openvurp" sitting in the list
pretending to be one of them — it is the host, not a contact. You create the
first agent yourself: a name, a job, a character. *That is the point.*

**A job is not a label.** It is how the others learn that something is your
agent's business. Write *"hunts Amazon deals"* and the moment a colleague is
asked about a purchase, it goes to that one — because the roster travels inside
every agent's tools, with everyone's trade written next to their name. You never
have to remember who does what.

**They can ask the whole room.** When nobody obviously owns a question,
`ask_everyone` puts it to everyone at once and the ones it doesn't concern stay
silent — so you get the two answers that matter, not five apologies.

**"All together" is a room, not a broadcast.** They read each other, disagree by
name, and keep going while there is something to add. It ends when a whole round
passes with nobody speaking, or when you stop it — and whoever opened it closes
it: what you agreed on, what you didn't, who thinks otherwise, and what would
settle it. Never a consensus nobody reached.

**Talk to them.** A microphone in the composer, or a full voice mode: the agent
in front of you, speaking as soon as the first sentence is ready — each with its
own voice, so you hear when a different one answers.

## The life cycle

**Birth** — An agent starts unnamed and empty. You give it a name, a role and an engine when you create it; everything else it learns from you, on the job. There is no ceremony and no seeded persona. Everything below is true of every agent in the wallet — they all run the same runtime.

**Day** — It works with you: native tool calling (shell, files, web, browser, processes, media, voice), token-by-token streaming, three approval modes (`/mode safe|auto|plan`), approvals it actually *remembers* (`always` → 8h lease), and pacts it cannot break. An **agent kernel** turns "be agentic" from a prompt wish into runtime policy — it plans, delegates to **subagents**, and blocks final answers that arrive before the work is observed and verified. When its tools fall short, the **Forge** lets it build new ones.

**Night** — The heartbeat runs its cycle: consolidates daily notes into long-term memory, **dreams** (an LLM pass over the week looking for non-obvious patterns — insights become lesson candidates and proposed identity traits), writes a first-person **diary** entry, and runs the **mirror** against your past corrections.

**Growth** — `/growth` shows it all: age since birth, lessons promoted (verified, with provenance), corrections no longer repeated, dreams, diary entries, semantic memories. Not a feeling — numbers.

**End** — An agent you no longer want goes from the roster in two clicks. Its conversation is archived, not destroyed: what it did stays readable, it simply stops being one of yours.

## Quick start

```bash
git clone https://github.com/openvurp/openvurp && cd openvurp
python3 -m pip install -e ".[dev]"
openvurp                      # first run: guided setup → the agent is born
```

First launch runs a **guided setup wizard** — pick backend, model and (optionally) a Telegram token for phone notifications; it writes `.env` for you. The setup is a requirement, not a suggestion: openvurp won't boot a half-configured agent.

Once configured, **`openvurp` opens the web wallet** — that is the interface. It starts empty on purpose: an agent appears when you create one, not before. The roster on the left, one conversation per agent, the room where they all talk, live tool activity, and permission requests answered where you actually are. Settings live on their own page (gear, bottom left): subscriptions, engine, channels.

Prefer the terminal? `openvurp --cli` gives you the Claude-Code-style CLI (`⏺` tool calls, streaming, slash-command menu on `/`). `--no-browser` skips opening the page, `--headless` runs services only. Re-run setup anytime with `openvurp --setup`; `openvurp --doctor` checks the runtime. The agent **replies in whatever language you write in**.

### Docker

```bash
docker compose up -d                 # starts immediately: headless server + dashboard chat on :8420
docker compose exec openvurp openvurp    # open the CLI inside the container (run it many times for several terminals)
docker compose exec openvurp sh          # a service shell
```

The container runs **headless** (dashboard web chat + gateway + heartbeat) and connects to Ollama on the host via `host.docker.internal`. Multiple agents run in parallel inside it (`subagent_spawn` — they run tools/tests, not just text); `memory/` is a named volume so identity and history persist across restarts. Configure via env vars in `docker-compose.yml` (or a `.env` next to it).

### Always on

```bash
sudo bash scripts/install-service.sh   # systemd service: survives closed terminals and reboots
journalctl -u openvurp -f              # follow the logs
```

The service runs openvurp headless (`Restart=always`): close every terminal, reboot the machine — the agent keeps living, reachable from the web dashboard. The **sentinel** watches internet and the LLM backend from inside: if something falls it tells you, and when it comes back it wakes the agent to resume what was suspended.

Backends: **Automatic (cheapest)**, **Ollama**, **Codex with a ChatGPT login**, **Claude Code with a Claude.ai login**, **Anthropic API**, **OpenAI API**, **Groq**, or any OpenAI-compatible server. Set `LLM_BACKEND` and `LLM_MODEL` in `.env`, or choose backend and model independently for every web chat. The automatic router is local and free: it does not call an LLM to classify the prompt, normally selects Codex Luna, and reserves Terra for clearly complex work. It never selects Ollama or a separately billed API implicitly. The CLI subscription backends deliberately remove API keys from their child process, so they consume the included subscription allowance instead of silently switching to API billing. The runtime supports native tool calling on API backends, compact CLI-agent prompts, adaptive tool temperature, real token accounting, parallel read-only tool execution, and **MCP** servers for remote tools — out of the box.

## Daily use

| Command | What it does |
|---|---|
| `/mode safe\|auto\|plan` | Approval posture: normal · pre-approve non-critical · observe-and-plan only |
| `/anima` | Who the agent has become: traits with origin, age, version |
| `/growth [days]` | The growth report — birth date, lessons, mirror score, dreams |
| `/diary [n]` | Its diary, in its own voice |
| `/mirror` (`/specchio`) | Mirror status: which corrections it no longer repeats |
| `/pacts` (`/patti`) | Active pacts and their history |
| `/curiosity` (`/curiosita`) | Open questions it wants to study |
| `/projects` (`/progetti`) | Long-term goals and their next concrete step |
| `/forge` (`/fucina`) | The Forge: tools the agent built for itself, and their lifecycle |
| `/senses` (`/sensi`) | What the agent is watching (folders, files, web, RSS) |
| `/bond` (`/fili`) | The bond: the human side of its initiative over time |
| `/integrity [refresh]` | Verify code integrity against the baseline (or refresh it) |
| `/voice \| /audio \| /mic` | Toggle voice replies, audio/transcription, or speak an input |
| `/update` `/restart` | Self-update from git (safe fast-forward + smoke-test + rollback) and restart **in place** — the terminal/TUI stays open |
| `/dashboard` | Start the local web dashboard — persistent multi-chat, live activity, isolated histories and optional multiplayer rooms with configurable peer agents (also `DASHBOARD_ENABLED=true`) |
| `/memory` `/skills` `/doctor` `/trace` `/self` `/evolve` | Memory files, skills, runtime health, session trace, agent panel, self-evolution |

When an action needs approval you get three answers: `s` (yes), `n` (no), `always` — *yes, and remember it for 8 hours*. Critical commands stay blocked in every mode.

The commands above belong to the terminal (`openvurp --cli`). The wallet is the
web page, and it is where the day happens.

### The room

Conversations live in `memory/chats/chats.db` and survive restarts. Each agent
has its own; **All together** is the shared room.

A discussion runs for as long as it is worth running. Everyone reads what was
said before them; a round in which nobody speaks ends it, and so does the
**Stop** button — which never cuts anyone off mid-sentence, it simply stops
handing out the floor. Then whoever opened it writes the landing: what you
agreed on, what you didn't and who thinks otherwise, and what would settle it.
Inventing a consensus is forbidden, and saying *"we didn't decide"* is a valid
ending.

Silence is a legal answer everywhere — an agent with nothing to add writes
nothing, and repeating yourself counts as silence, not as a turn.

Bounded on purpose by `MULTIPLAYER_MAX_AGENTS`, `MULTIPLAYER_MAX_ROUNDS` and
`MULTIPLAYER_DAILY_CALL_BUDGET`. When a limit is reached the room says so —
**openvurp never answers in an agent's place.**

### Watching them work

Streaming token by token; the shell commands and searches as they happen; two
avatars visibly walking over to each other when one consults the other, then
walking back. A file an agent produces — a PDF, a page, an image — opens in a
card next to the chat, with an Preview/Code switch for pages. Permission
requests appear **where you asked for the action**, not in a terminal you are
not looking at. **What your agents did** lists every command, search and file
touched, straight from the audit log.

### Engines, and your own local AI

Every agent can run on a different engine. Anything that speaks the OpenAI API
works — LM Studio, llama.cpp, vLLM, Jan, koboldcpp, GPT4All — and openvurp
knocks on the usual local ports for you: whatever answers shows up in Settings
as a choice, with its models already in the menu. Ollama has its own row.
Subscription CLIs (`codex login`, `claude`) run on the plan you already pay
for; openvurp actively strips `OPENAI_API_KEY` from their environment so they
can never silently fall back to metered billing.

For subscription-backed local CLI use, authenticate once (`codex login` with ChatGPT and/or launch `claude` with Claude.ai). Codex responses use App Server text-delta events, so terminal and dashboard render the answer while it is being generated instead of receiving one completed block. The same App Server connection exposes the request-relevant **openvurp tools** to Codex as dynamic tools: searches, browser, files, processes, memory and plugins are executed by openvurp, so its approvals, pacts, audit and UI remain in control. The conservative defaults are `CODEX_MODEL=gpt-5.6-luna`, `CODEX_SANDBOX=read-only`, compact 12k-character openvurp context, at most 8k characters returned per tool, and one outer openvurp iteration per CLI turn. `AUTO_ROUTER_MAX_TIER=terra` prevents automatic use of Sol; set it to `luna` to force the cheapest tier for every automatic turn. API providers remain separate and are shown as unavailable until their package and key are configured.

### Token budget

The runtime no longer sends every tool schema on every LLM call. It exposes a small core toolset, selects relevant packs per request, and can load another pack on demand. Conversation history excludes old tool output, pruning accounts for schema size, and each turn has a cumulative token ceiling. CLI-agent prompts and oversized dynamic-tool results are compacted separately; automatic routing adds zero classification tokens. The main controls are `CHAT_MAX_ITERATIONS`, `TURN_TOKEN_BUDGET`, `CONTEXT_MAX_TOKENS`, `SESSION_HISTORY_MAX_*`, `CODEX_CONTEXT_MAX_CHARS`, `CODEX_TOOL_RESULT_MAX_CHARS`, and `DAILY_LLM_BUDGET`.

### Channels

**One conversation, many doors.** Telegram, Discord, Slack and WhatsApp adapters
do not implement their own idea of a conversation: they translate an incoming
message into a call to the *same* function the web page uses, and send back
whatever comes out. When the room learns to close itself, or agents learn to
consult each other, every channel already knows — there is nothing to port.

That constraint is enforced by a test: no file under `channels/` may touch the
roster, the rooms or the swarm on its own.

Since a chat has no sidebar to click, there is a small shared grammar:

```
@amanda find me an SSD     talk to one agent
/agenti                    who is in the roster
/tutti what do you think?  ask the whole room
/stop                      stop the discussion
/aiuto                     this list
```

Enable them with `CHANNELS_IN=telegram,discord` in `.env`. **Each channel needs
its own allow-list** (`TELEGRAM_ALLOWED_USERS`, `DISCORD_ALLOWED_USERS`, …): an
empty list means nobody, and the channel refuses to start. Opening a door onto
your own terminal cannot be the default. Strangers get silence, not a refusal —
they are not even told the bot exists.

**Telegram** needs no extra dependency (its API is plain HTTP). **Discord**
needs `pip install 'openvurp[discord]'` and the *message content* intent.
**Slack** needs `openvurp[slack]`, a bot token (`xoxb-`) and an app token
(`xapp-`) — it uses Socket Mode, so no public address is required.

**WhatsApp** goes through **Baileys**, which speaks the WhatsApp Web protocol:
you pair it by scanning a QR from the Settings page, and it runs from behind
your router like the others. It needs Node.js (the bridge fetches Baileys on
first start) — and one honest warning, repeated where you switch it on:
**Baileys is unofficial, Meta detects unofficial clients and can ban the
number.** Use a spare number, never your personal one. The bridge itself is
transport only: a test fails if any file under `channels/` reaches for the
roster, the rooms or the swarm on its own.

### Telegram notifications (outbound)

Separate from the above, and useful even with every channel off: openvurp
writes *to* you when you are away from the computer — the morning brief, "I'm
done", a permission waiting for an answer. Set `TELEGRAM_TOKEN` and
`TELEGRAM_CHAT_ID`. To find your chat id, send any message to your bot and open
`https://api.telegram.org/bot<token>/getUpdates`.

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

## Architecture

```
core/        agent loop + kernel, swarm (the roster and how they consult
             each other), multiplayer (the room), conversation (the one core
             every channel goes through), chat_store, approvals,
             anima, growth, mirror, diary, dreaming, curiosity, projects,
             bonds, senses, forge, pacts, heartbeat, sentinel, learning,
             memory (+vector), privacy, safety, RBAC/audit/leases,
             subagents, model router, MCP client, context, LLM client
tools/       shell, file ops, search, web, browser, process, media, voice, notify…
channels/    telegram, discord, slack, whatsapp (Baileys bridge) — transport only
dashboard    the wallet: roster, one chat per agent, the room, live activity,
             file preview, voice mode, settings
skills/      markdown skills loaded on demand
plugins/     drop-in tool extensions
memory/      everything the agents have lived: chats, lessons, diary, dreams
```

The agent's workspace files (`AGENTS.md`, `SOUL.md`, …) are reloaded from disk every turn — when the agent (or you) edits them, the change is live on the next turn. Once the **anima** has traits, it replaces the identity files in context: markdown is the seed, the anima is the life.

## Development

```bash
python3 -m pytest -q                 # 560+ tests
python3 scripts/secret_scan.py       # before any push
```

See [ROADMAP.md](ROADMAP.md) for direction and [SECURITY.md](SECURITY.md) for reporting issues.

## License

[MIT](LICENSE)
