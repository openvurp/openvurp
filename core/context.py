"""
openvurp Core — Context Manager

Gestione contesto a livelli:
- Truncation immediata tool result grandi
- Soft trim tool result vecchi (head + tail)
- Hard clear tool result ancora piu vecchi (placeholder)
- Compaction LLM (riassunto conversazione via modello)
- Budget enforcement prima di ogni chiamata
"""

from __future__ import annotations

import os
import glob as glob_mod
import json
from datetime import datetime
from typing import Optional


# ── Costanti a livelli ──

# Soft trim: tool result > di questa soglia vengono tagliati head+tail
SOFT_TRIM_MAX_CHARS = 4000
SOFT_TRIM_HEAD_CHARS = 1500
SOFT_TRIM_TAIL_CHARS = 1500

# Hard clear: tool result vecchi sostituiti con placeholder
HARD_CLEAR_PLACEHOLDER = "[Tool output rimosso — contesto liberato]"

# Truncation immediata: singolo tool result non puo superare il 30% del contesto
MAX_TOOL_RESULT_CONTEXT_SHARE = 0.08
HARD_MAX_TOOL_RESULT_CHARS = 32_000

# Soglie per le fasi di pruning (rapporto uso/budget)
SOFT_TRIM_RATIO = 0.30    # oltre 30% del budget: soft trim
HARD_CLEAR_RATIO = 0.50   # oltre 50% del budget: hard clear
COMPACT_RATIO = 0.75      # oltre 75% del budget: compaction LLM

# Quanti ultimi messaggi assistant proteggere dal hard clear
KEEP_LAST_ASSISTANTS = 3

# Minimo char di tool output prunable per attivare hard clear
MIN_PRUNABLE_TOOL_CHARS = 50_000


def estimate_tokens(text: str) -> int:
    """Stima token (~4 chars/token)."""
    if not text:
        return 0
    return len(text) // 4


def message_chars(message: dict) -> int:
    """Caratteri totali di un messaggio, inclusi tool_calls nativi.

    Il solo len(content) sottostima i messaggi assistant con tool_calls
    (gli argomenti possono essere grandi) e fallisce su content None/list.
    """
    content = message.get("content") or ""
    if isinstance(content, str):
        total = len(content)
    elif isinstance(content, list):
        total = sum(len(str(block)) for block in content)
    else:
        total = len(str(content))

    for tc in message.get("tool_calls") or []:
        if isinstance(tc, dict):
            try:
                import json as _json
                total += len(_json.dumps(tc.get("args", {}), ensure_ascii=False))
            except Exception:
                total += len(str(tc.get("args", "")))
            total += len(str(tc.get("name", "")))
    return total


def load_file(path: str) -> str:
    """Carica file con gestione errori."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


class Skill:
    """Rappresenta una skill caricata da file .md."""
    def __init__(self, name: str, content: str, description: str = "",
                 triggers: list[str] = None, always: bool = False,
                 priority: int = 0):
        self.name = name
        self.content = content
        self.description = description
        self.triggers = triggers or []
        self.always = always
        self.priority = priority

    @classmethod
    def from_file(cls, path: str) -> "Skill":
        content = load_file(path)
        name = os.path.splitext(os.path.basename(path))[0]

        description = ""
        triggers: list[str] = []
        always = False
        priority = 0

        # Estrai il blocco frontmatter tra i primi due "---"
        front_lines: list[str] = []
        in_front = False
        seen_open = False
        for line in content.split("\n"):
            if line.strip() == "---":
                if not seen_open:
                    seen_open = True
                    in_front = True
                    continue
                if in_front:
                    break
            if in_front:
                front_lines.append(line)

        for raw in front_lines:
            line_s = raw.strip()
            if not line_s or line_s.startswith("#") or ":" not in line_s:
                continue
            key, _, val = line_s.partition(":")
            key_lower = key.strip().lower()
            val = val.strip()
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            elif val.startswith("'") and val.endswith("'"):
                val = val[1:-1]

            if key_lower == "description":
                description = val
            elif key_lower == "triggers":
                val_clean = val.strip().strip("[]")
                triggers = [
                    t.strip().strip('"\'')
                    for t in val_clean.split(",")
                    if t.strip()
                ]
            elif key_lower == "always":
                always = val.lower() in ("true", "yes", "si", "sì")
            elif key_lower == "priority":
                try:
                    priority = int(val)
                except ValueError:
                    pass

        return cls(name=name, content=content, description=description,
                   triggers=triggers, always=always, priority=priority)


# ── Funzioni di truncation (livello 1 — immediata) ──

def truncate_tool_result(output: str, context_window_tokens: int) -> str:
    """Tronca un singolo tool result se troppo grande.

    Approccio a livelli: max 30% del context window, hard cap 400K chars.
    Tiene head + tail con indicatore di troncamento.
    """
    if not output:
        return output

    max_chars = min(
        int(context_window_tokens * 4 * MAX_TOOL_RESULT_CONTEXT_SHARE),
        HARD_MAX_TOOL_RESULT_CHARS
    )

    if len(output) <= max_chars:
        return output

    # Tieni head + tail, taglia al newline piu vicino
    keep = max(max_chars, 2000)
    head_size = int(keep * 0.6)
    tail_size = int(keep * 0.4)

    head = output[:head_size]
    tail = output[-tail_size:]

    # Taglia al newline per non spezzare righe
    nl = head.rfind("\n")
    if nl > head_size // 2:
        head = head[:nl]
    nl = tail.find("\n")
    if nl > 0 and nl < tail_size // 2:
        tail = tail[nl + 1:]

    cut_chars = len(output) - len(head) - len(tail)
    return (
        f"{head}\n\n"
        f"[... {cut_chars} caratteri omessi — output troppo grande ...]\n\n"
        f"{tail}"
    )


# ── Context Manager ──

class ContextManager:
    def __init__(self, openvurp_dir: str, memory_dir: str, skills_dir: str,
                 max_tokens: int = 128000, compact_threshold: float = 0.75):
        self.openvurp_dir = openvurp_dir
        self.memory_dir = memory_dir
        self.skills_dir = skills_dir
        self.max_tokens = max_tokens
        self.compact_threshold = compact_threshold
        self._skills_cache: list[Skill] = []
        self._reload_skills()

    def _reload_skills(self):
        """Ricarica skills da disco."""
        self._skills_cache = []
        if os.path.exists(self.skills_dir):
            for f in sorted(glob_mod.glob(os.path.join(self.skills_dir, "*.md"))):
                self._skills_cache.append(Skill.from_file(f))

    # ── System Prompt — Approccio a livelli ──
    #
    # Architettura a 2 livelli:
    # 1. ISTRUZIONI TECNICHE (hardcoded): tool, safety, formato, runtime
    # 2. PROJECT CONTEXT (iniettato): file workspace riletti da disco ogni turno
    #
    # Il Project Context NON fa parte delle istruzioni — è contesto iniettato
    # che l'agente incarna. Questo è il cuore della "personalità iniettata":
    # i file vengono riletti da disco ad ogni turno, quindi modifiche fatte
    # dall'agente (o dall'utente) sono immediate.

    def build_system_prompt(self, bootstrap_context: str,
                            memory_text: str, tools_section: str,
                            user_input: str = "",
                            environment_text: str = "",
                            method_text: str = "",
                            native_tools: bool = False) -> str:
        """Costruisce il system prompt completo — a livelli.

        Args:
            bootstrap_context: Project Context dai file workspace (costruito dal BootstrapLoader)
            memory_text: memoria strutturata rilevante
            tools_section: schema dei tool disponibili
            user_input: input utente per selezione skills

        L'architettura è:
        1. Identità base (una riga)
        2. Istruzioni tecniche (tool, safety, formato, runtime)
        3. Skills selettive
        4. Memoria strutturata
        5. Self-knowledge
        6. PROJECT CONTEXT (file workspace iniettati — la personalità vive qui)
        """
        sections = []

        # 1. Identità base — una riga (minimale)
        sections.append(
            "Sei un assistente personale che gira dentro questo workspace. "
            "I file workspace sotto definiscono chi sei — incarnali, senza importare un nome predefinito dal framework."
        )

        # 2. Tool disponibili
        if tools_section:
            sections.append(tools_section)

        # 3. Skills — indice sempre visibile + contenuto delle "always"
        skills_text = self._skills_section()
        if skills_text:
            sections.append(skills_text)

        # 4. Memoria strutturata (da keyword retrieval)
        if memory_text:
            sections.append("## MEMORIA FILE\n"
                            f"La cartella memoria è: {self.memory_dir}\n\n"
                            + memory_text)

        # 5. Self-knowledge compatta
        sections.append(
            f"## SELF-KNOWLEDGE\n"
            f"- workspace root: `{self.openvurp_dir}`\n"
            f"- puoi ispezionare e modificare il tuo codice quando serve\n"
            f"- non trattare il tuo codice come contesto dominante se il workspace markdown ha già definito identità, memoria e regole"
        )

        # 6. Runtime / habitat reale
        if environment_text:
            sections.append(environment_text)
        else:
            sections.append(
                f"## RUNTIME\n"
                f"Data: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                f"Workspace: {self.openvurp_dir}"
            )

        # 7. Metodo operativo
        if method_text:
            sections.append(method_text)

        # 8. Istruzioni formato (testuale per regex, o nativo per function calling)
        sections.append(self._format_instructions(native_tools=native_tools))

        # 9. PROJECT CONTEXT — i file workspace iniettati
        # Questa è la sezione più importante: la personalità vive qui.
        # Viene riletta da disco ogni turno dal BootstrapLoader.
        if bootstrap_context:
            sections.append(bootstrap_context)

        full = "\n\n".join(sections)

        # Token budget check — system prompt max 40%
        tokens = estimate_tokens(full)
        if tokens > self.max_tokens * 0.4:
            full = self._trim_system_prompt(full)

        return full

    # ── Pruning a 3 fasi (context-pruning a livelli) ──

    def prune_messages(self, messages: list[dict]) -> list[dict]:
        """Pruning in-memory a 3 fasi prima di inviare al modello.

        Fase 1 (Soft Trim): tool result grandi → head + tail
        Fase 2 (Hard Clear): tool result vecchi → placeholder
        Fase 3: drop messaggi vecchi se ancora sopra budget

        NON modifica la lista originale. Restituisce una copia.
        """
        if not messages:
            return messages

        budget_chars = self.max_tokens * 4  # 1 token ~ 4 chars
        total_chars = sum(message_chars(m) for m in messages)

        # Sotto la soglia di soft trim non serve intervenire. Il vecchio
        # controllo usava l'intero budget e rendeva irraggiungibili le fasi
        # configurate al 30% e 50%.
        if total_chars <= budget_chars * SOFT_TRIM_RATIO:
            return messages

        # Copia per non mutare l'originale
        msgs = [dict(m) for m in messages]
        ratio = total_chars / budget_chars

        # ── FASE 1: Soft Trim ──
        if ratio > SOFT_TRIM_RATIO:
            msgs = self._soft_trim(msgs)
            total_chars = sum(message_chars(m) for m in msgs)
            ratio = total_chars / budget_chars

        # ── FASE 2: Hard Clear ──
        if ratio > HARD_CLEAR_RATIO:
            msgs = self._hard_clear(msgs)
            total_chars = sum(message_chars(m) for m in msgs)
            ratio = total_chars / budget_chars

        # ── FASE 3: Drop messaggi vecchi ──
        if ratio > self.compact_threshold:
            msgs = self._drop_old_messages(msgs)

        return msgs

    def _is_tool_output(self, msg: dict) -> bool:
        """Identifica messaggi che contengono output di tool/comandi."""
        content = msg.get("content", "")
        if msg.get("role") == "tool_result":
            return True
        if msg.get("role") != "user":
            return False
        # I tool output in openvurp iniziano con "Output dei comandi:"
        # oppure contengono risultati di tool
        return (content.startswith("Output dei comandi:") or
                content.startswith("$ ") or
                content.startswith("[read_file]") or
                content.startswith("[grep]") or
                content.startswith("[glob]") or
                content.startswith("[web_fetch]") or
                content.startswith("[web_search]"))

    def _soft_trim(self, msgs: list[dict]) -> list[dict]:
        """Fase 1: tronca tool result grandi con head + tail."""
        result = []
        for msg in msgs:
            content = msg.get("content", "")
            if self._is_tool_output(msg) and len(content) > SOFT_TRIM_MAX_CHARS:
                msg = dict(msg)
                # Tieni head + tail
                head = content[:SOFT_TRIM_HEAD_CHARS]
                tail = content[-SOFT_TRIM_TAIL_CHARS:]
                cut = len(content) - SOFT_TRIM_HEAD_CHARS - SOFT_TRIM_TAIL_CHARS
                msg["content"] = (
                    f"{head}\n\n"
                    f"[... {cut} caratteri omessi per contesto ...]\n\n"
                    f"{tail}"
                )
            result.append(msg)
        return result

    def _hard_clear(self, msgs: list[dict]) -> list[dict]:
        """Fase 2: sostituisci tool result vecchi con placeholder.

        Protegge gli ultimi N messaggi assistant e i loro tool result.
        """
        # Calcola totale chars prunable
        prunable_chars = sum(
            len(m.get("content", ""))
            for m in msgs
            if self._is_tool_output(m)
        )

        if prunable_chars < MIN_PRUNABLE_TOOL_CHARS:
            return msgs

        # Trova indice di protezione: ultimi KEEP_LAST_ASSISTANTS assistant
        protect_from = len(msgs)
        assistant_count = 0
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "assistant":
                assistant_count += 1
                if assistant_count >= KEEP_LAST_ASSISTANTS:
                    protect_from = i
                    break

        result = []
        for i, msg in enumerate(msgs):
            if i < protect_from and self._is_tool_output(msg):
                msg = dict(msg)
                msg["content"] = HARD_CLEAR_PLACEHOLDER
            result.append(msg)
        return result

    def _drop_old_messages(self, msgs: list[dict]) -> list[dict]:
        """Fase 3: elimina messaggi vecchi, tieni system + ultimi N."""
        system = [m for m in msgs if m.get("role") == "system"]
        others = [m for m in msgs if m.get("role") != "system"]

        if len(others) <= 10:
            return msgs

        # Tieni ultimi 10 messaggi
        kept = others[-10:]

        # Crea un riassunto compatto dei messaggi eliminati
        dropped = others[:-10]
        user_msgs = [m for m in dropped if m.get("role") == "user" and
                     not self._is_tool_output(m)]

        if user_msgs:
            topics = []
            for m in user_msgs[-5:]:  # ultimi 5 input utente
                text = m.get("content", "")[:150]
                topics.append(f"- {text}")

            summary = {
                "role": "user",
                "content": (
                    "[Contesto precedente — la conversazione e stata compattata]\n"
                    f"Messaggi rimossi: {len(dropped)}\n"
                    "Ultimi argomenti discussi:\n" + "\n".join(topics)
                )
            }
            return system + [summary] + kept

        return system + kept

    def prune_to_target(self, messages: list[dict], target_tokens: int) -> list[dict]:
        """Tetto economico deterministico, distinto dal context window fisico.

        Conserva il system prompt e almeno gli ultimi sei messaggi. Gli output
        tool vengono ridotti prima di eliminare turni remoti; non richiede una
        chiamata LLM aggiuntiva per produrre un riassunto.
        """
        target_chars = max(4000, int(target_tokens) * 4)
        if sum(message_chars(m) for m in messages) <= target_chars:
            return messages

        msgs = self._soft_trim([dict(m) for m in messages])
        if sum(message_chars(m) for m in msgs) <= target_chars:
            return msgs
        msgs = self._hard_clear(msgs)
        if sum(message_chars(m) for m in msgs) <= target_chars:
            return msgs

        system = [m for m in msgs if m.get("role") == "system"][:1]
        others = [m for m in msgs if m.get("role") != "system"]
        dropped: list[dict] = []
        while len(others) > 6 and (
            sum(message_chars(m) for m in system + others) > target_chars
        ):
            dropped.append(others.pop(0))

        # Non iniziare mai con un tool_result orfano dopo il taglio.
        while len(others) > 6 and others and others[0].get("role") == "tool_result":
            dropped.append(others.pop(0))

        if dropped:
            user_topics = [
                " ".join(str(m.get("content", "")).split())[:140]
                for m in dropped if m.get("role") == "user" and not self._is_tool_output(m)
            ][-3:]
            if user_topics:
                summary = {
                    "role": "user",
                    "content": "[Turni precedenti rimossi per budget]\n" + "\n".join(
                        f"- {topic}" for topic in user_topics
                    ),
                }
                if sum(message_chars(m) for m in system + [summary] + others) <= target_chars:
                    return system + [summary] + others
        return system + others

    # ── Compaction LLM (livello 4 — chiede al modello di riassumere) ──

    def build_compaction_prompt(self, messages: list[dict]) -> list[dict]:
        """Costruisce i messaggi per chiedere al LLM di compattare la conversazione.

        Restituisce una lista di messaggi da inviare al LLM.
        Il risultato sara un riassunto che sostituisce la history.
        """
        # Raccogli solo i messaggi non-system
        conversation_parts = []
        for m in messages:
            if m.get("role") == "system":
                continue
            role = m.get("role", "?")
            content = m.get("content", "")
            # Tronca contenuti enormi per il riassunto
            if len(content) > 2000:
                content = content[:1000] + "\n[...]\n" + content[-500:]
            conversation_parts.append(f"[{role}]: {content}")

        conversation_text = "\n\n".join(conversation_parts)

        return [
            {
                "role": "system",
                "content": (
                    "Sei un assistente che riassume conversazioni. "
                    "Crea un riassunto CONCISO ma COMPLETO della conversazione seguente. "
                    "Includi:\n"
                    "- Cosa ha chiesto l'utente\n"
                    "- Cosa e stato fatto (azioni, tool usati, file modificati)\n"
                    "- Decisioni prese e risultati ottenuti\n"
                    "- Informazioni importanti emerse\n"
                    "- Stato attuale del lavoro (cosa manca, cosa e in corso)\n\n"
                    "NON includere dettagli irrilevanti o output di tool completi.\n"
                    "Rispondi SOLO con il riassunto, nient'altro."
                )
            },
            {
                "role": "user",
                "content": f"Riassumi questa conversazione:\n\n{conversation_text}"
            }
        ]

    def apply_compaction(self, messages: list[dict], summary: str) -> list[dict]:
        """Applica il riassunto LLM alla conversazione.

        Mantiene il system prompt, sostituisce la history con il riassunto,
        e tiene gli ultimi messaggi intatti.
        """
        system = [m for m in messages if m.get("role") == "system"]
        others = [m for m in messages if m.get("role") != "system"]

        # Tieni gli ultimi 6 messaggi (3 turni user+assistant)
        keep_recent = min(6, len(others))
        recent = others[-keep_recent:] if keep_recent > 0 else []

        summary_msg = {
            "role": "user",
            "content": (
                "[Riassunto conversazione precedente — compattata automaticamente]\n\n"
                f"{summary}\n\n"
                "[Fine riassunto — continua la conversazione da qui]"
            )
        }

        # Serve una risposta assistant dopo il summary per mantenere l'alternanza
        ack_msg = {
            "role": "assistant",
            "content": "Ho il contesto della conversazione precedente. Continuo da qui."
        }

        return system + [summary_msg, ack_msg] + recent

    # ── Budget enforcement ──

    @staticmethod
    def schema_chars(tools_schema: list[dict] | None = None) -> int:
        if not tools_schema:
            return 0
        try:
            return len(json.dumps(tools_schema, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            return len(str(tools_schema))

    def check_budget(self, messages: list[dict],
                     tools_schema: list[dict] | None = None) -> dict:
        """Calcola uso del budget e diagnostica.

        Returns dict con:
        - total_tokens: stima token totali
        - budget_tokens: budget massimo
        - ratio: rapporto uso/budget
        - over_budget: True se sfora
        - needs_soft_trim: True se serve soft trim
        - needs_hard_clear: True se serve hard clear
        - needs_compaction: True se serve compaction LLM
        - top_contributors: i 3 messaggi piu grandi
        """
        message_total = sum(message_chars(m) for m in messages)
        tools_chars = self.schema_chars(tools_schema)
        total_chars = message_total + tools_chars
        total_tokens = total_chars // 4
        budget_chars = self.max_tokens * 4
        ratio = total_chars / budget_chars if budget_chars > 0 else 0

        # Top 3 messaggi piu grandi
        sized = [(i, message_chars(m), m.get("role", "?"))
                 for i, m in enumerate(messages)]
        sized.sort(key=lambda x: x[1], reverse=True)
        top = sized[:3]

        return {
            "total_tokens": total_tokens,
            "budget_tokens": self.max_tokens,
            "message_tokens": message_total // 4,
            "tool_schema_tokens": tools_chars // 4,
            "ratio": round(ratio, 2),
            "over_budget": ratio > 1.0,
            "needs_soft_trim": ratio > SOFT_TRIM_RATIO,
            "needs_hard_clear": ratio > HARD_CLEAR_RATIO,
            "needs_compaction": ratio > COMPACT_RATIO,
            "top_contributors": [
                {"index": i, "chars": c, "role": r} for i, c, r in top
            ]
        }

    def should_compact(self, messages: list[dict],
                       tools_schema: list[dict] | None = None) -> bool:
        """Controlla se serve compaction LLM."""
        total = (
            sum(message_chars(m) for m in messages)
            + self.schema_chars(tools_schema)
        ) // 4
        return total > self.max_tokens * self.compact_threshold

    # ── Overflow detection (a livelli) ──

    @staticmethod
    def is_context_overflow_error(error: Exception) -> bool:
        """Rileva errori di context overflow dal provider LLM."""
        msg = str(error).lower()
        patterns = [
            "request_too_large",
            "context length exceeded",
            "exceeds model context window",
            "exceeds the maximum context",
            "prompt is too long",
            "maximum context length",
            "token limit",
            "context_length_exceeded",
            "max_tokens",
            "input is too long",
            "request size exceeds",
        ]
        return any(p in msg for p in patterns)

    # ── Skills e prompt (invariati) ──

    def _skills_section(self) -> str:
        """Costruisce la sezione SKILLS del system prompt.

        Sempre visibile al modello:
        - indice compatto (nome + description) di TUTTE le skill
        - contenuto completo solo delle skill con `always: true`

        Il contenuto completo delle altre skill si carica on-demand con il
        tool `load_skill`. Questo evita drift "non sapevo di avere la skill X"
        e non gonfia il prompt con procedure non richieste.
        """
        if not self._skills_cache:
            return ""

        ordered = sorted(
            self._skills_cache,
            key=lambda s: (-s.priority, s.name),
        )

        lines = ["## SKILLS"]
        lines.append(
            "Queste sono le skill che hai. Per ogni task, prima guarda l'indice: "
            "se una skill copre quello che ti serve, carica la procedura completa "
            "con il tool `load_skill` (param: `name`). "
            "Le skill marcate ★ sono gia' caricate qui sotto integralmente."
        )
        lines.append("")
        lines.append("### Indice skill disponibili")

        always_skills: list[Skill] = []
        for skill in ordered:
            desc = skill.description or "(nessuna descrizione — apri il file per dettagli)"
            marker = " ★" if skill.always else ""
            lines.append(f"- **{skill.name}**{marker}: {desc}")
            if skill.always:
                always_skills.append(skill)

        if always_skills:
            lines.append("")
            lines.append("### Skill sempre attive")
            for skill in always_skills:
                lines.append("")
                lines.append(f"#### skill: {skill.name}")
                lines.append(skill.content)
                lines.append("---")

        return "\n".join(lines)

    def load_skill(self, name: str) -> Optional[Skill]:
        """Ritorna la skill per nome (case-insensitive), o None."""
        if not name:
            return None
        target = name.strip().lower()
        for skill in self._skills_cache:
            if skill.name.lower() == target:
                return skill
        return None

    def _format_instructions(self, native_tools: bool = False) -> str:
        """Istruzioni su come usare i tool.

        Con function calling nativo niente formato testuale ```TOOL:/```SHELL:
        il modello chiama i tool col meccanismo nativo e quei blocchi non
        vengono eseguiti — istruire il formato testuale produce chiamate
        malformate (es. ```TOOL:read_file"> ).
        """
        if native_tools:
            return (
                "## COME FUNZIONO\n\n"
                "Ho accesso a tool strutturati (inclusa la shell) tramite il "
                "function calling nativo del modello. Chiamo i tool con quel "
                "meccanismo.\n\n"
                "IMPORTANTE: NON scrivere blocchi di testo tipo ```TOOL:nome o "
                "```SHELL — non vengono eseguiti, sono solo testo che l'utente "
                "vede. Per usare un tool emetti una vera tool call nativa.\n\n"
                "Tutto il testo che scrivo fuori dalle tool call è ciò che "
                "l'utente legge.\n\n"
                "### Cosa posso fare\n"
                "- Leggere e scrivere file\n"
                "- Installare pacchetti (con conferma)\n"
                "- Eseguire codice\n"
                "- Cercare sul web\n"
                "- Git, docker, qualsiasi tool da terminale\n"
                f"- Leggere e modificare la mia memoria: {self.memory_dir}/\n"
                f"- Leggere e modificare il mio codice: {self.openvurp_dir}/"
            )
        return (
            "## COME FUNZIONO\n\n"
            "Ho accesso a tool strutturati e al terminale.\n"
            "Tutto il testo FUORI dai blocchi SHELL/TOOL e quello che l'utente vede.\n"
            "Tutto DENTRO i blocchi viene eseguito.\n\n"
            "Per i nomi tool e i parametri affidati SOLO a `## TOOL DISPONIBILI`: "
            "quella sezione è la fonte di verità.\n\n"

            "### Formato comandi shell\n"
            "```SHELL\nls -la /home\n```\n\n"

            "### Formato tool strutturati\n"
            "IMPORTANTE: il JSON deve essere valido e completo. "
            "Chiudi SEMPRE le parentesi graffe. "
            "Usa nomi e parametri ESATTI dalla sezione `## TOOL DISPONIBILI`.\n\n"

            '```TOOL:nome_tool\n{"parametro": "valore"}\n```\n\n'

            "### Regole formato\n"
            "- Un tool per blocco. NON combinare piu tool in un blocco.\n"
            "- Il JSON deve essere su UNA riga o multi-riga, ma sempre valido.\n"
            "- Per comandi shell usa ```SHELL, per tutto il resto usa ```TOOL:nome.\n"
            "- NON inventare tool che non esistono nella lista sopra.\n"
            "- Se un parametro e obbligatorio, DEVI includerlo.\n\n"

            "### Cosa posso fare\n"
            "- Leggere e scrivere file\n"
            "- Installare pacchetti (con conferma)\n"
            "- Eseguire codice\n"
            "- Cercare sul web\n"
            "- Git, docker, qualsiasi tool da terminale\n"
            f"- Leggere e modificare la mia memoria: {self.memory_dir}/\n"
            f"- Leggere e modificare il mio codice: {self.openvurp_dir}/"
        )

    def _trim_system_prompt(self, prompt: str) -> str:
        """Taglia il system prompt se troppo grande."""
        target = int(self.max_tokens * 0.35)
        current = estimate_tokens(prompt)

        if current <= target:
            return prompt

        lines = prompt.split("\n")
        result = []
        in_skill = False
        skill_count = 0

        for line in lines:
            if line.startswith("### skill:"):
                skill_count += 1
                if skill_count > 3:
                    in_skill = True
                    continue
                in_skill = False
            if in_skill:
                continue
            result.append(line)

        return "\n".join(result)

    def get_all_skills(self) -> list[Skill]:
        """Restituisce tutte le skills per UI."""
        return self._skills_cache

    # ── Metodo legacy (retrocompatibilita) ──

    def prune_history(self, messages: list[dict], budget_tokens: int) -> list[dict]:
        """Legacy — usa prune_messages() invece."""
        return self.prune_messages(messages)
