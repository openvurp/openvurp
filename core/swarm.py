"""The swarm: the specialists you and the host agent can talk to.

One roster of agents for the whole of openvurp. There used to be three
disconnected ones — subagents (throwaway processes), the dashboard's
multiplayer rooms, and a swarm on JSON files — and "my agents" was not a
question with a single answer. Here the source of truth is ``ChatStore``: the
same agents you see in the dashboard roster are the ones the host summons and
questions from the CLI.

``SubagentManager`` stays separate on purpose: it delegates a task to a
process, it is not a correspondent.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone

from core.chat_store import ChatStore


def _slug(name: str) -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in "-_" else "-" for ch in str(name or "").strip().lower()
    )
    return "-".join(part for part in cleaned.split("-") if part)[:40]


# Gli agenti della rubrica hanno gli STESSI strumenti di openvurp: shell, file,
# processi, web, memoria, notifiche. Ogni chiamata passa comunque dal suo
# esecutore, quindi valgono le stesse soglie: l'innocuo parte, il rischioso
# chiede conferma, il distruttivo viene bloccato.
#
# Restano fuori solo i tool con cui un agente riscriverebbe openvurp stesso —
# il proprio codice, la propria identita', i propri plugin. Non e' una
# limitazione di potere operativo: e' che una personalita' creata in trenta
# secondi non deve poter rifare il runtime in cui vive. Si toglie con
# SWARM_TOOLS_ALLOW_SELF_EDIT=true.
SELF_EDIT_TOOLS = (
    "evolve_self", "read_self", "request_restart",
    "doctor_fix", "scaffold_plugin", "reload_plugins", "forge",
)


@dataclass
class _Route:
    """Route minima: serve solo a dire al bus in quale chat sta succedendo."""

    chat_id: str

    @property
    def session_key(self) -> str:
        return f"dashboard:chat:{self.chat_id}"


class SwarmError(RuntimeError):
    """Errore leggibile dello sciame, da mostrare al modello o all'utente."""


@dataclass
class SwarmMember:
    """Vista di un agente del roster, con i campi che servono a farlo parlare."""

    id: str
    name: str
    role: str
    instructions: str = ""
    backend: str = ""
    model: str = ""
    created_at: str = ""

    @staticmethod
    def from_row(row: dict) -> "SwarmMember":
        return SwarmMember(
            id=str(row.get("id", "")),
            name=str(row.get("name", "")),
            role=str(row.get("role", "")),
            instructions=str(row.get("instructions", "") or ""),
            backend=str(row.get("backend", "") or ""),
            model=str(row.get("model", "") or ""),
            created_at=str(row.get("created_at", "") or ""),
        )

    def describe(self) -> str:
        engine = " / ".join(x for x in (self.backend, self.model) if x) or "motore dell'agente"
        return f"{self.name} — {self.role} [{engine}]"


class Swarm:
    """Registro degli specialisti + i modi in cui si parlano."""

    ROOM_TITLE = "Sciame — discussioni"

    def __init__(self, parent_agent=None, store: ChatStore | None = None,
                 memory_dir: str | None = None):
        self.agent = parent_agent
        if store is None:
            if not memory_dir:
                import os
                root = getattr(parent_agent, "openvurp_dir", None) or os.getcwd()
                memory_dir = os.path.join(str(root), "memory")
            store = ChatStore(str(memory_dir))
        self.store = store
        self._lock = threading.RLock()
        # Passaggi dell'ultimo intervento. Chi persiste il messaggio per conto
        # suo (la dashboard) li prende da qui: altrimenti li raccogliamo e li
        # buttiamo via, e dalla chat non si capisce piu' cosa e' stato fatto.
        self.last_steps: list[dict] = []

    # ── Roster ───────────────────────────────────────────────────────────

    def list_members(self) -> list[SwarmMember]:
        return [SwarmMember.from_row(row)
                for row in self.store.list_agents(enabled_only=True)]

    @property
    def members(self) -> dict[str, SwarmMember]:
        """Indicizzati per nome-slug: e' cosi' che li chiama chi scrive."""
        return {_slug(m.name): m for m in self.list_members()}

    def resolve(self, name: str) -> SwarmMember:
        wanted = _slug(name)
        if not wanted:
            raise SwarmError("Serve il nome di uno specialista.")
        members = self.list_members()
        for member in members:
            if member.id == str(name) or _slug(member.name) == wanted:
                return member
        # Match tollerante: il modello scrive "il revisore" dove il membro si
        # chiama "revisore". Meglio capirlo che fallire per una parola in piu'.
        loose = [m for m in members if wanted in _slug(m.name) or _slug(m.name) in wanted]
        if len(loose) == 1:
            return loose[0]
        known = ", ".join(sorted(_slug(m.name) for m in members)) or "(nessuno)"
        raise SwarmError(f"Nessuno specialista chiamato '{name}'. Ci sono: {known}.")

    def spawn(self, name: str, role: str, instructions: str = "",
              backend: str = "", model: str = "") -> SwarmMember:
        import config as cfg

        if not str(role or "").strip():
            raise SwarmError("Serve un ruolo: cosa deve saper fare questo specialista?")
        clean = str(name or role).strip()[:80]
        if not clean:
            raise SwarmError("Serve un nome.")
        with self._lock:
            existing = {_slug(m.name) for m in self.list_members()}
            if _slug(clean) in existing:
                raise SwarmError(
                    f"'{clean}' esiste già. Parlagli con swarm_ask o congedalo "
                    f"con swarm_dismiss."
                )
            limit = max(1, int(getattr(cfg, "SWARM_MAX_AGENTS", 6)))
            if len(existing) >= limit:
                raise SwarmError(
                    f"Sciame pieno ({len(existing)}/{limit}). Congeda qualcuno "
                    f"con swarm_dismiss prima di crearne altri."
                )
            row = self.store.create_agent(
                clean, " ".join(str(role).split())[:80],
                str(instructions or "").strip()[:4000],
                str(backend or "").strip(), str(model or "").strip(),
            )
        member = SwarmMember.from_row(row)
        # La chat a due nasce subito: l'agente compare nella rubrica con un
        # filo suo, non come una riga vuota da attivare.
        self.store.direct_chat_for_agent(member.id)
        return member

    def dismiss(self, name: str) -> str:
        member = self.resolve(name)
        # Disattivato, non cancellato: la conversazione avuta con lui resta
        # leggibile, e un agente che sparisce con la sua storia e' una perdita.
        self.store.update_agent(member.id, enabled=False)
        return _slug(member.name)

    def roster_text(self) -> str:
        members = self.list_members()
        if not members:
            return "Sciame vuoto: nessuno specialista convocato."
        lines = [f"Sciame ({len(members)}):"]
        for member in members:
            lines.append(f"  - {member.describe()}")
        return "\n".join(lines)

    def transcript(self, limit: int = 20) -> list[dict]:
        """Ultimi scambi, uniti da tutte le conversazioni dello sciame."""
        entries: list[dict] = []
        for member in self.list_members():
            chat = self.store.direct_chat_for_agent(member.id)
            if not chat:
                continue
            for message in self.store.list_messages(chat["id"], limit=limit):
                entries.append({
                    "at": message.get("created_at", ""),
                    "from": message.get("author_name") or "?",
                    "to": member.name if message.get("author_type") == "user" else "tu",
                    "text": message.get("content", ""),
                    "kind": "reply" if message.get("author_type") == "agent" else "prompt",
                })
        room = self._room(create=False)
        if room:
            for message in self.store.list_messages(room["id"], limit=limit):
                entries.append({
                    "at": message.get("created_at", ""),
                    "from": message.get("author_name") or "?",
                    "to": "sciame",
                    "text": message.get("content", ""),
                    "kind": "discussione",
                })
        entries.sort(key=lambda item: item.get("at", ""))
        return entries[-max(1, int(limit)):]

    # ── Budget ───────────────────────────────────────────────────────────

    def _charge(self, calls: int = 1) -> None:
        """Un dubbio non deve poter bruciare il credito di una giornata."""
        import config as cfg

        limit = max(0, int(getattr(cfg, "SWARM_DAILY_CALL_BUDGET", 200)))
        if not limit:
            return
        used = self.store.count_agent_messages_since(
            datetime.now(timezone.utc).date().isoformat()
        )
        if used + calls > limit:
            raise SwarmError(
                f"Budget giornaliero dello sciame esaurito ({used}/{limit}). "
                f"Alza SWARM_DAILY_CALL_BUDGET nel .env."
            )

    # ── Conversazione ────────────────────────────────────────────────────

    # ── Strumenti ────────────────────────────────────────────────────────

    def tool_names(self) -> set[str]:
        """What an agent in the roster can do: EVERYTHING openvurp does,
        except rewrite itself.

        (This note used to say the opposite of what the code does — "shell is
        left out..." — and the misinformation reached the chat: dev told gram
        the environment was read-only and the PDF could not be produced. Shell
        and file writing ARE there; what stays out is self-modification
        (evolve_self, doctor_fix...) and the swarm tools. Every call still goes
        through the main agent's executor, with its approvals and audit.)
        """
        import config as cfg

        available = set(getattr(getattr(self.agent, "tools", None), "names", lambda: [])())
        if not available:
            return set()
        raw = str(getattr(cfg, "SWARM_TOOLS", "") or "").strip()
        if raw:
            return {n.strip() for n in raw.split(",") if n.strip()} & available
        wanted = set(available)
        if not bool(getattr(cfg, "SWARM_TOOLS_ALLOW_SELF_EDIT", False)):
            wanted -= set(SELF_EDIT_TOOLS)
        # The swarm's own tools: an agent that summons more agents and makes
        # them argue is a matryoshka nobody asked for.
        wanted -= {n for n in wanted if n.startswith(("swarm_", "subagent_"))}
        return wanted

    def _peer_tools(self, member: SwarmMember) -> list[dict]:
        """The tools for reaching colleagues, with the roster INSIDE them.

        The roster used to live in a single line of the system prompt, while
        the tool asked for a generic "name of the agent to consult". At the
        moment it decides, the model looks at the tools in its hands: if
        neither who exists nor what they do is written there, consulting
        someone requires remembering it. It never happened — `ask_peer` never
        fired once. Here the names are a closed list and the trades are in the
        description: the choice is in front of its eyes when it is needed.
        """
        others = [p for p in self.list_members() if p.id != member.id]
        if not others:
            return []
        listing = "; ".join(f"{p.name} = {p.role}" for p in others)
        return [
            {
                "type": "function",
                "function": {
                    "name": "who_is_there",
                    "description": (
                        "Who is in the roster right now, and what each of them "
                        "does. The roster changes: the user creates and deletes "
                        "agents whenever they like, even while you are working. "
                        "If you are unsure who exists, look here instead of "
                        f"guessing. At the start of this turn there were: {listing}."
                    ),
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ask_peer",
                    "description": (
                        f"Ask a colleague in the roster. Currently: {listing}. "
                        "Use it on your own initiative when the question falls "
                        "in one of their fields, without asking the user first. "
                        "For questions of substance, not about what can be done "
                        "here: your own tool list answers that."
                    ),
                    "parameters": {"type": "object", "properties": {
                        # A closed list puts the choice in front of its eyes at
                        # the moment it decides. Not a cage: if the roster has
                        # changed meanwhile, `resolve` still accepts the name
                        # and, on failure, returns who is actually there now.
                        "name": {"type": "string",
                                 "enum": [p.name for p in others],
                                 "description": "The colleague to consult."},
                        "question": {"type": "string", "description": "What you ask them."},
                    }, "required": ["name", "question"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "ask_everyone",
                    "description": (
                        f"Put the question to ALL colleagues at once ({listing}). "
                        "Use it when you do not know whose field it is, or when it "
                        "touches several: it is how you say \u201cwho can give me a "
                        "hand with this?\u201d. Whoever it does not concern stays "
                        "quiet, so you only get the answers of those who have "
                        "something to say."
                    ),
                    "parameters": {"type": "object", "properties": {
                        "question": {"type": "string",
                                     "description": "The question for everyone."},
                    }, "required": ["question"]},
                },
            },
        ]

    def ask_everyone(self, asker: SwarmMember, question: str,
                     chat_id: str = "") -> str:
        """"Who can help me with this?" put to the whole roster."""
        others = [p for p in self.list_members() if p.id != asker.id]
        if not others:
            return "[There is nobody else in the roster.]"
        risposte = []
        for peer in others:
            try:
                text = self.consult(asker, peer.name, question, chat_id,
                                    broadcast=True)
            except SwarmError as exc:
                risposte.append(f"{peer.name}: [{exc}]")
                continue
            # Whoever it does not concern stays quiet: that is the half that
            # makes a question to everyone useful, otherwise a chorus of
            # apologies comes back.
            if self._said_nothing(text):
                continue
            text = text.strip()
            risposte.append(f"{peer.name} ({peer.role}): {text}")
        if not risposte:
            return "[Nobody answered: none of them handles this.]"
        return "\n\n".join(risposte)

    def consult(self, asker: SwarmMember, peer_name: str, question: str,
                chat_id: str = "", broadcast: bool = False) -> str:
        """One agent asks another. Question and answer both stay visible.

        The consulted agent does NOT get ask_peer in turn: two agents bouncing
        the ball forever are a loop, not a collaboration.
        """
        peer = self.resolve(peer_name)
        if peer.id == asker.id:
            raise SwarmError("Stai chiedendo a te stesso.")

        # ask_peer does not go through the executor, so nothing was published:
        # live, the consultation was invisible and only appeared once the turn
        # was over.
        def segnala(kind: str, **extra):
            if not chat_id:
                return
            try:
                from core import activity
                activity.publish(
                    kind, source="dashboard", chat_id=chat_id,
                    session_key=f"dashboard:chat:{chat_id}",
                    from_id=asker.id, from_name=asker.name,
                    to_id=peer.id, to_name=peer.name, **extra,
                )
            except Exception:
                pass

        segnala("peer", question=question[:400])
        if broadcast:
            # A question put to everyone: the useful half is that whoever it
            # does not concern stays quiet. Without that permission a chorus of
            # apologies comes back.
            request = (
                f"{asker.name} is asking the whole roster, not just you: "
                f"{question}\n\nAnswer ONLY if this is your field or you have "
                f"something useful to add. Otherwise write {self.NOTHING} and "
                f"nothing else: do not apologise, do not explain why it is not "
                f"your area."
            )
        else:
            request = f"{asker.name} asks you: {question}"
        text = self._speak(
            peer, request,
            sender=asker.name, allow_peers=False, steps=[], persist=False,
        )
        segnala("peer_done", answer=text[:600])
        if broadcast and self._said_nothing(text):
            return ""   # they passed: do not clutter the conversation
        if chat_id:
            meta = {"peer": {"from": asker.id, "to": peer.id,
                             "from_name": asker.name, "to_name": peer.name}}
            self.store.add_message(
                chat_id, "assistant", question, author_type="agent",
                author_id=asker.id, author_name=asker.name,
                recipient_id=peer.id, metadata=dict(meta, direction="ask"),
            )
            self.store.add_message(
                chat_id, "assistant", text, author_type="agent",
                author_id=peer.id, author_name=peer.name,
                recipient_id=asker.id, metadata=dict(meta, direction="answer"),
            )
        return text

    @staticmethod
    def _live(chat_id: str, author: str):
        """Pubblica i pezzi della risposta mentre arrivano.

        Senza questo l'agente chiama il modello, aspetta tutto, e consegna il
        blocco intero: da fuori sembra bloccato. La dashboard ascolta lo stesso
        bus dell'agente principale, quindi basta parlargli nella sua lingua.
        """
        if not chat_id:
            return None, (lambda: None)
        from core import activity

        meta = {"source": "dashboard", "chat_id": chat_id,
                "session_key": f"dashboard:chat:{chat_id}", "actor_id": author}

        def on_text(delta: str):
            if delta:
                try:
                    activity.publish("token", text=delta, **meta)
                except Exception:
                    pass

        def done():
            try:
                activity.publish("assistant_end", **meta)
            except Exception:
                pass

        return on_text, done

    def _run_with_tools(self, member: SwarmMember, client,
                        messages: list[dict], allow_peers: bool = True,
                        chat_id: str = "", steps: list | None = None) -> str:
        """Fa parlare l'agente lasciandogli usare i suoi strumenti."""
        import config as cfg

        names = self.tool_names()
        parent = self.agent
        peers = allow_peers and len(self.list_members()) > 1
        on_text, done = self._live(chat_id, member.id)
        if (not names and not peers) or (parent is None and not peers):
            return self._plain(client, messages, on_text, done)

        source = f"agent:{member.name}"
        chat = self.store.direct_chat_for_agent(member.id) or {}

        def note(name: str, args: dict, output: str) -> None:
            """Cosa ha fatto, salvato col messaggio.

            Prima i passaggi vivevano solo durante lo streaming: appena la
            risposta veniva riletta dal database sparivano, e non c'era piu'
            modo di sapere se l'agente avesse davvero eseguito qualcosa.
            """
            if steps is None:
                return
            if name == "shell":
                label = str(args.get("command", ""))[:200]
            else:
                label = ", ".join(
                    f"{k}={str(val)[:60]}" for k, val in (args or {}).items()
                )[:200]
            steps.append({"tool": name, "args": label,
                          "out": " ".join(str(output or "").split())[:400]})

        def run_tool(name: str, args: dict) -> str:
            if name == "who_is_there":
                out = self.roster_text()
                note(name, args, out)
                return out
            if name == "ask_peer":
                try:
                    out = self.consult(member, str(args.get("name", "")),
                                       str(args.get("question", "")), chat_id)
                except SwarmError as exc:
                    out = f"[{exc}]"
                note(name, args, out)
                return out
            if name == "ask_everyone":
                out = self.ask_everyone(member, str(args.get("question", "")), chat_id)
                note(name, args, out)
                return out
            # openvurp pubblica ogni azione sul bus leggendo il route attivo.
            # Senza spostarlo, le azioni di questo agente comparirebbero nella
            # conversazione sbagliata (o in nessuna).
            previous_route = getattr(parent, "_active_route", None)
            previous_channel = getattr(parent, "_active_channel", "cli")
            previous_ui = getattr(parent, "ui", None)
            if chat.get("id"):
                parent._active_route = _Route(chat["id"])
                parent._active_channel = "dashboard"
                # Il permesso va chiesto dove l'azione e' stata chiesta: se
                # l'agente e' stato aperto dal browser, la domanda non puo'
                # comparire nel terminale.
                try:
                    from core.approvals import WebApprovalUI
                    parent.ui = WebApprovalUI(previous_ui, chat["id"], member.name)
                except Exception:
                    pass
            try:
                out = str(parent._execute_tool(name, args or {}, source) or "")
            except Exception as exc:
                out = f"[TOOL FALLITO] {exc}"
            note(name, args, out)
            try:
                return out
            finally:
                parent._active_route = previous_route
                parent._active_channel = previous_channel
                if previous_ui is not None:
                    parent.ui = previous_ui

        # Codex/Claude non hanno function calling nativo: gli strumenti passano
        # come dynamic tools e il giro lo chiude il CLI.
        if not getattr(client, "supports_function_calling", False):
            if getattr(client, "supports_tool_transport", False):
                wire = list(self._schema_for(parent, names, client))
                if peers:
                    wire.extend(self._peer_tools(member))
                try:
                    return str(client.call_streamed(
                        messages, tools_schema=wire, on_tool=run_tool,
                        on_text=on_text,
                    ) or "").strip()
                finally:
                    done()
            return self._plain(client, messages, on_text, done)

        schema = list(self._schema_for(parent, names, client))
        if peers:
            schema.extend(self._peer_tools(member))
        rounds = max(1, min(int(getattr(cfg, "SWARM_TOOL_ROUNDS", 4)), 8))
        history = list(messages)
        for _ in range(rounds):
            reply = client.call_with_tools(history, schema)
            if not reply.tool_calls:
                text = str(reply.text or "").strip()
                if on_text and text:
                    on_text(text)   # niente delta da questo percorso: almeno arriva
                done()
                return text
            history.append({
                "role": "assistant", "content": reply.text or "",
                "tool_calls": [{"id": tc.id, "name": tc.name, "args": tc.args}
                               for tc in reply.tool_calls],
            })
            from core.security.untrusted import is_untrusted_tool, wrap_untrusted
            for call in reply.tool_calls:
                out = run_tool(call.name, call.args)[:8000]
                if is_untrusted_tool(call.name):
                    out = wrap_untrusted(call.name, out)
                history.append({
                    "role": "tool_result", "tool_call_id": call.id,
                    "name": call.name, "content": out,
                })
        # Giri esauriti: meglio chiedere una risposta con quello che ha in mano
        # che restituire il vuoto.
        history.append({
            "role": "user",
            "content": "Hai finito i passaggi disponibili. Rispondi ora con quello "
                       "che hai raccolto, dicendo cosa non sei riuscito a verificare.",
        })
        return self._plain(client, history, on_text, done)

    @staticmethod
    def _plain(client, messages: list[dict], on_text, done) -> str:
        """Una risposta senza strumenti, in streaming dove il backend lo permette."""
        try:
            if on_text and hasattr(client, "call_streamed"):
                return str(client.call_streamed(messages, on_text=on_text) or "").strip()
            text = str(client.call(messages) or "").strip()
            if on_text and text:
                on_text(text)
            return text
        finally:
            done()

    @staticmethod
    def _schema_for(parent, names, client) -> list[dict]:
        if parent is None or not names:
            return []
        if getattr(client, "backend", "") == "anthropic":
            return parent.tools.to_anthropic_schema(names)
        return parent.tools.to_openai_schema(names)

    def _client(self, member: SwarmMember):
        import config as cfg
        from core.llm import create_llm_client

        backend = member.backend or str(getattr(cfg, "SWARM_BACKEND", "") or "")
        model = member.model or str(getattr(cfg, "SWARM_MODEL", "") or "")
        active = getattr(self.agent, "_active_llm", None)
        if not backend:
            backend = getattr(active, "backend", "") or str(getattr(cfg, "LLM_BACKEND", "ollama"))
        if not model:
            model = getattr(active, "model", "") or str(getattr(cfg, "LLM_MODEL", ""))
        client = create_llm_client(backend=backend, model=model)
        client.max_tokens = max(256, min(int(getattr(cfg, "SWARM_MAX_TOKENS", 1200)), 4000))
        client.temperature = 0.4
        return client

    def _system_prompt(self, member: SwarmMember, peers: list[SwarmMember]) -> str:
        others = ", ".join(f"{p.name} ({p.role})" for p in peers if p.id != member.id)
        lines = [
            f"You are '{member.name}', a specialist in openvurp's swarm.",
            f"Role: {member.role}",
        ]
        if member.instructions:
            lines.append(f"Standing instructions: {member.instructions}")
        if others:
            lines.append(
                "Colleagues in the roster: " + others + ". "
                "If part of the task is their field, ask them yourself with "
                "`ask_peer` (or `ask_everyone`) before answering: the user "
                "should not have to remember who is there."
            )
        lines.append(
            "Your tools are the ones listed: whatever they can do, you can do. "
            "Do it and show the finished result; only ask about irreversible "
            "or costly choices. Name any file you create with its full path: "
            "the user sees it in a preview. The folder you run in is openvurp's "
            "home, not the subject: the context is what the user and your own "
            "instructions say."
        )
        lines.append(
            "Reply in the language the person writes to you in. Be concrete "
            "and brief: a clear position, the reason, and \u2014 if there is one "
            "\u2014 the main risk. Do not invent facts: if you are missing "
            "something, say so and say how to get it."
        )
        return "\n".join(lines)

    def _history(self, chat_id: str, limit: int = 0) -> str:
        import config as cfg

        limit = limit or int(getattr(cfg, "SWARM_HISTORY_MESSAGES", 12))
        messages = self.store.list_messages(chat_id, limit=max(2, limit))
        return "\n".join(
            f"[{m.get('author_name') or m.get('role')}] {m.get('content', '')}"
            for m in messages
        )[-6000:]

    def _speak(self, member: SwarmMember, prompt: str, context: str = "",
               sender: str = "openvurp", chat_id: str = "",
               allow_peers: bool = True, tools: bool = True,
               steps: list | None = None, persist: bool = True) -> str:
        peers = self.list_members()
        messages = [{"role": "system", "content": self._system_prompt(member, peers)}]
        if context.strip():
            messages.append({
                "role": "user",
                "content": "Contesto della discussione finora:\n" + context.strip(),
            })
        messages.append({"role": "user", "content": f"[{sender}] {prompt}"})

        self._charge()
        try:
            client = self._client(member)
            # La lista arriva da chi ha iniziato il turno. Se la tenessimo in un
            # attributo condiviso, una consulenza annidata (ask_peer) la
            # sovrascriverebbe con i passaggi del collega — di solito vuoti — e
            # cancellerebbe proprio quelli di chi sta rispondendo.
            passaggi: list[dict] = steps if steps is not None else []
            text = (self._run_with_tools(
                member, client, messages, allow_peers=allow_peers,
                chat_id=chat_id, steps=passaggi,
            ) if tools else str(client.call(messages) or "").strip())
        except Exception as exc:
            raise SwarmError(f"{member.name} non ha risposto: {exc}") from exc
        if not text:
            raise SwarmError(f"{member.name} ha risposto a vuoto.")

        # `chat_id` dice DOVE sta succedendo — serve allo streaming, ai
        # passaggi e alle consulenze fra agenti. `persist` dice solo se la
        # coppia domanda/risposta la scriviamo noi: la dashboard la scrive per
        # conto suo. Tenerli insieme spegneva tutto il resto.
        if chat_id and persist:
            self.store.add_message(
                chat_id, "user", prompt, author_type="user",
                author_id=sender, author_name=sender.capitalize(),
                recipient_id=member.id,
            )
            self.store.add_message(
                chat_id, "assistant", text, author_type="agent",
                author_id=member.id, author_name=member.name, recipient_id="room",
                metadata={"steps": passaggi} if passaggi else None,
            )
        return text

    def ask(self, name: str, question: str, sender: str = "openvurp",
            persist: bool = True) -> str:
        """Domanda a un singolo specialista, nel suo filo di conversazione.

        ``persist=False`` per chi tiene gia' lui la contabilita' dei messaggi
        (la dashboard scrive il turno con il proprio run_id): scriverlo due
        volte lascerebbe la domanda duplicata nella chat.
        """
        if not str(question or "").strip():
            raise SwarmError("La domanda è vuota.")
        member = self.resolve(name)
        chat = self.store.direct_chat_for_agent(member.id) or {}
        history = self._history(chat["id"]) if chat.get("id") else ""
        passaggi: list[dict] = []
        try:
            return self._speak(member, question, context=history, sender=sender,
                               chat_id=str(chat.get("id", "")), persist=persist,
                               steps=passaggi)
        finally:
            # Chi persiste il messaggio per conto suo (la dashboard) li legge qui.
            self.last_steps = passaggi

    def broadcast(self, question: str, names: list[str] | None = None,
                  sender: str = "openvurp") -> dict[str, str]:
        """Stessa domanda a più specialisti, in parallelo.

        È il caso "scrivo a tutti e due insieme": ognuno risponde per sé, senza
        vedere gli altri, così i pareri restano indipendenti.
        """
        targets = [self.resolve(n) for n in names] if names else self.list_members()
        if not targets:
            raise SwarmError("Nessuno specialista a cui chiedere.")
        results: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(len(targets), 6)) as pool:
            futures = {}
            for member in targets:
                chat = self.store.direct_chat_for_agent(member.id) or {}
                futures[pool.submit(
                    self._speak, member, question, "", sender, str(chat.get("id", "")),
                )] = member
            for future, member in futures.items():
                try:
                    results[member.name] = future.result()
                except Exception as exc:
                    results[member.name] = f"[errore] {exc}"
        return results

    def _team_room(self) -> dict | None:
        try:
            return self.store.team_room()
        except Exception:
            return None

    def _room(self, create: bool = True) -> dict | None:
        """La stanza comune dove gli specialisti discutono fra loro."""
        for chat in self.store.list_chats():
            if chat.get("title") == self.ROOM_TITLE:
                return chat
        if not create:
            return None
        return self.store.create_chat(title=self.ROOM_TITLE, mode="team")

    def discuss(self, topic: str, names: list[str] | None = None,
                rounds: int = 2, sender: str = "openvurp") -> list[dict]:
        """Fa parlare gli specialisti TRA LORO, a turni.

        Dal secondo giro ognuno legge cosa hanno detto gli altri e risponde a
        quello: è qui che lo sciame smette di essere N monologhi e diventa una
        discussione con disaccordi utili.
        """
        import config as cfg

        if not str(topic or "").strip():
            raise SwarmError("Serve un argomento da discutere.")
        targets = [self.resolve(n) for n in names] if names else self.list_members()
        if len(targets) < 2:
            raise SwarmError(
                "Per discutere servono almeno due specialisti. "
                "Creane un altro con swarm_spawn."
            )
        max_rounds = max(1, min(int(rounds or 2),
                                int(getattr(cfg, "SWARM_MAX_ROUNDS", 3))))
        room = self._room() or {}
        room_id = str(room.get("id", ""))
        if room_id:
            self.store.set_chat_agents(room_id, [m.id for m in targets])
            self.store.add_message(
                room_id, "user", topic, author_type="user",
                author_id=sender, author_name=sender.capitalize(),
            )

        transcript: list[dict] = []
        for round_no in range(1, max_rounds + 1):
            for member in targets:
                context = "\n\n".join(
                    f"[{entry['name']}] {entry['text']}" for entry in transcript
                )[-8000:]
                if round_no == 1:
                    prompt = f"Argomento: {topic}\n\nDai la tua posizione."
                else:
                    prompt = (
                        f"Argomento: {topic}\n\nGiro {round_no}. Leggi cosa hanno "
                        f"detto gli altri: di' dove NON sei d'accordo e perché, "
                        f"e cosa cambia nella conclusione. Se sei d'accordo, dillo "
                        f"in una riga e aggiungi ciò che manca."
                    )
                try:
                    text = self._speak(member, prompt, context=context, sender=sender)
                except SwarmError as exc:
                    text = f"[errore] {exc}"
                else:
                    if room_id:
                        self.store.add_message(
                            room_id, "assistant", text, author_type="agent",
                            author_id=member.id, author_name=member.name,
                            recipient_id="room", metadata={"round": round_no},
                        )
                transcript.append({
                    "round": round_no, "name": member.name,
                    "role": member.role, "text": text,
                })
        return transcript

    # ── Chiacchiere ──────────────────────────────────────────────────────

    # How an agent with nothing to say answers. Silence has to be a possible
    # answer, otherwise they speak out of obligation.
    NOTHING = "—"

    @staticmethod
    def _said_nothing(text: str) -> bool:
        clean = str(text or "").strip().strip('".«»').strip()
        return len(clean) <= 2 or clean in {"-", "--", "—", "..."}

    def small_talk(self) -> list[dict]:
        """Due agenti si dicono qualcosa, se ne hanno voglia.

        Nessun argomento suggerito: non c'e' una lista di spunti da cui pescano.
        Hanno il loro carattere, sanno chi c'e' e cosa si e' detto di recente,
        e da li' decidono da soli se aprire bocca, su cosa, e con chi prendersela.
        Il silenzio e' una risposta prevista: un agente che non ha niente da dire
        non deve inventarsi qualcosa per riempire il turno.
        """
        import random

        members = self.list_members()
        if len(members) < 2:
            return []
        a, b = random.sample(members, 2)
        room = self._team_room()
        room_id = str((room or {}).get("id", ""))
        recente = self._history(room_id) if room_id else ""

        cornice = (
            "Non c'e' niente da fare adesso e nessuno ti ha chiesto niente. "
            "Sei in una stanza con gli altri agenti.\n"
            f"Se ti va di dire qualcosa \u2014 a tutti o a qualcuno in "
            f"particolare, su quello che vuoi \u2014 dillo. Due o tre righe, "
            f"come parleresti davvero.\n"
            f"Se non hai niente da dire, rispondi soltanto: {self.NOTHING}"
        )
        if recente.strip() and recente != "(stanza nuova)":
            cornice = f"Ultime cose dette qui dentro:\n{recente}\n\n" + cornice

        try:
            apertura = self._speak(a, cornice, sender="(nessuno)",
                                   allow_peers=False, tools=False)
        except SwarmError:
            return []
        if self._said_nothing(apertura):
            return []

        try:
            risposta = self._speak(
                b,
                f"{a.name} ha detto: \u00ab{apertura}\u00bb\n\n"
                f"Se ti va di rispondere, rispondi come faresti tu. "
                f"Se la cosa non ti tocca, scrivi soltanto: {self.NOTHING}",
                sender=a.name, allow_peers=False, tools=False,
            )
        except SwarmError:
            risposta = ""

        detto = [(a, apertura)]
        if not self._said_nothing(risposta):
            detto.append((b, risposta))

        scambio: list[dict] = []
        for who, text in detto:
            entry = {"author_name": who.name, "content": text}
            if room_id:
                entry = self.store.add_message(
                    room_id, "assistant", text, author_type="agent",
                    author_id=who.id, author_name=who.name, recipient_id="room",
                    metadata={"idle": True},
                )
            scambio.append(entry)
        return scambio

    def start_small_talk(self, ui=None) -> threading.Thread | None:
        """Avvia le chiacchiere in sottofondo. Solo l'app viva le accende."""
        import config as cfg

        if not bool(getattr(cfg, "SWARM_IDLE_CHAT", True)):
            return None

        def loop():
            import random
            import time as _t

            giorno, fatte = "", 0
            while True:
                lo = max(2, int(getattr(cfg, "SWARM_IDLE_MIN_MINUTES", 25)))
                hi = max(lo, int(getattr(cfg, "SWARM_IDLE_MAX_MINUTES", 90)))
                _t.sleep(random.uniform(lo * 60, hi * 60))
                try:
                    oggi = date.today().isoformat()
                    if oggi != giorno:
                        giorno, fatte = oggi, 0
                    tetto = max(0, int(getattr(cfg, "SWARM_IDLE_DAILY_MAX", 6)))
                    if tetto and fatte >= tetto:
                        continue
                    if self.small_talk():
                        fatte += 1
                except Exception:
                    pass  # una chiacchiera fallita non deve fermare il resto

        thread = threading.Thread(target=loop, daemon=True, name="swarm-small-talk")
        thread.start()
        if ui is not None:
            try:
                ui.console.print(
                    "  [green]Chiacchiere[/green] [dim](gli agenti si parlano ogni tanto)[/dim]"
                )
            except Exception:
                pass
        return thread

    @staticmethod
    def render_discussion(transcript: list[dict]) -> str:
        if not transcript:
            return "(nessuno scambio)"
        lines: list[str] = []
        current = None
        for entry in transcript:
            if entry.get("round") != current:
                current = entry.get("round")
                lines.append(f"\n— Giro {current} —")
            lines.append(f"[{entry.get('name')}] {entry.get('text', '')}")
        return "\n".join(lines).strip()
