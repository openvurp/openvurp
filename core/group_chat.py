"""
openvurp Core — Partecipazione naturale nei gruppi

Nei gruppi Telegram l'agente non deve rispondere a tutto (sarebbe "troppo bot")
né solo se taggato. In modalità `natural` tiene un buffer dei messaggi recenti
e, con un cooldown, decide come farebbe una persona se intervenire o restare in
silenzio. La decisione è una singola chiamata LLM corta (sì/no).

Logica di buffer/cooldown/parsing pura e testabile; la chiamata LLM è isolata.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque


class GroupChatBuffer:
    """Tiene gli ultimi messaggi per ogni chat, l'ultimo intervento del bot e la
    "nota" — la coscienza che il modello stesso mantiene su cosa sta succedendo
    nel gruppo (con chi parla, da quanto, se la conversazione è rivolta a lui).

    La memoria della chat è PERSISTENTE: se `persist_path` è dato, i messaggi e
    le note vengono salvati su disco ad ogni aggiornamento e ricaricati
    all'avvio, così la memoria del gruppo sopravvive ai riavvii dell'agente."""

    def __init__(self, maxlen: int = 12, persist_path: str | None = None):
        self.maxlen = maxlen
        self.persist_path = persist_path
        self._messages: dict[str, deque] = {}
        self._last_intervention: dict[str, float] = {}
        self._note: dict[str, str] = {}
        # Roster: chi c'è nel gruppo (visto parlare o aggiunto). Un bot Telegram
        # NON può scaricare la member-list: la impara così. key -> {name, username}.
        self._roster: dict[str, dict[str, dict]] = {}
        self._load()

    def add(self, chat_id: str, sender: str, text: str) -> None:
        buf = self._messages.setdefault(chat_id, deque(maxlen=self.maxlen))
        buf.append((sender or "?", text or ""))
        self._save()

    def recent(self, chat_id: str) -> list[tuple[str, str]]:
        return list(self._messages.get(chat_id, ()))

    # ── Roster (chi c'è nel gruppo) ──
    def note_person(self, chat_id: str, name: str, username: str = "") -> None:
        """Registra/aggiorna una persona vista nel gruppo. Dedup per @username
        (se c'è) altrimenti per nome normalizzato."""
        name = (name or "").strip()
        username = (username or "").lstrip("@").strip()
        if not name and not username:
            return
        people = self._roster.setdefault(chat_id, {})
        key = f"@{username.lower()}" if username else _fold(name)
        prev = people.get(key, {})
        people[key] = {
            "name": name or prev.get("name", "") or username,
            "username": username or prev.get("username", ""),
        }
        self._save()

    def roster(self, chat_id: str) -> list[dict]:
        return list(self._roster.get(chat_id, {}).values())

    def roster_text(self, chat_id: str) -> str:
        """Riga leggibile dal modello: chi c'è e come taggarlo."""
        people = self.roster(chat_id)
        if not people:
            return ""
        parts = []
        for p in people:
            if p.get("username"):
                parts.append(f"{p['name']} (@{p['username']})")
            else:
                parts.append(f"{p['name']} (nessun @username)")
        return (
            "[Persone viste in questo gruppo — per taggare usa @username:]\n"
            + ", ".join(parts)
            + "\n"
        )

    def cooldown_ok(self, chat_id: str, cooldown: float, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        last = self._last_intervention.get(chat_id, 0.0)
        return (now - last) >= cooldown

    def mark_intervention(self, chat_id: str, now: float | None = None) -> None:
        self._last_intervention[chat_id] = time.time() if now is None else now

    # ── Nota / coscienza della stanza (mantenuta dal modello) ──
    def get_note(self, chat_id: str) -> str:
        return self._note.get(chat_id, "")

    def set_note(self, chat_id: str, note: str) -> None:
        note = (note or "").strip()
        if note:
            self._note[chat_id] = note[:400]
            self._save()

    # ── Persistenza su disco ──
    def _load(self) -> None:
        """Ricarica messaggi e note dal file (se presente). Tollerante: qualsiasi
        errore lascia il buffer vuoto, non blocca l'avvio."""
        if not self.persist_path or not os.path.exists(self.persist_path):
            return
        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        for chat_id, entry in (data or {}).items():
            msgs = entry.get("messages", []) if isinstance(entry, dict) else []
            buf = deque(maxlen=self.maxlen)
            for pair in msgs:
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    buf.append((str(pair[0]), str(pair[1])))
            self._messages[chat_id] = buf
            note = entry.get("note", "") if isinstance(entry, dict) else ""
            if note:
                self._note[chat_id] = str(note)[:400]
            roster = entry.get("roster", {}) if isinstance(entry, dict) else {}
            if isinstance(roster, dict) and roster:
                self._roster[chat_id] = {
                    str(k): {
                        "name": str(v.get("name", "")),
                        "username": str(v.get("username", "")),
                    }
                    for k, v in roster.items() if isinstance(v, dict)
                }

    def _save(self) -> None:
        """Scrive l'intero stato su disco in modo atomico (tmp + replace)."""
        if not self.persist_path:
            return
        try:
            chat_ids = set(self._messages) | set(self._roster)
            data = {
                chat_id: {
                    "messages": list(self._messages.get(chat_id, [])),
                    "note": self._note.get(chat_id, ""),
                    "roster": self._roster.get(chat_id, {}),
                }
                for chat_id in chat_ids
            }
            os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
            tmp = self.persist_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.persist_path)
        except Exception:
            pass


def _fold(s: str) -> str:
    """Minuscolo + accenti rimossi, per confronti robusti (Pico == pico == pìco)."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def name_mentioned(text: str, name: str) -> bool:
    """True se il NOME dell'agente compare come parola nel testo.

    È il riflesso umano: senti sempre il tuo nome. Deterministico, non delegato
    a un modello — case/accent-insensitive, con confine di parola così 'Pico'
    non scatta dentro 'picozza'. (@menzione e reply sono già gestite a monte.)
    """
    import re
    name = (name or "").strip()
    if not name:
        return False
    t = _fold(text)
    n = re.escape(_fold(name))
    return re.search(rf"(?<![0-9a-z]){n}(?![0-9a-z])", t) is not None


def worth_considering(text: str) -> bool:
    """Pre-filtro minimo: scarta solo il vuoto o un singolo carattere/emoji.
    Tutto il resto va al modello-guardiano, che giudica col contesto (anche i
    messaggi corti come 'ci sei?' o 'aiuto')."""
    return len((text or "").strip()) > 1


def parse_decision(reply: str) -> bool:
    """Interpreta la risposta del guardiano in modo TOLLERANTE.

    I modelli piccoli spesso non rispondono solo 'SI'/'NO' (es. 'Penso di sì',
    'Direi di no', 'Certo'). Cerchiamo l'intento, non il formato esatto.
    """
    import re
    r = (reply or "").strip().lower()
    if not r:
        return False
    yes = bool(re.search(r"\b(s[iì]|yes|certo|ok|assolutamente|intervieni|rispondi)\b", r))
    no = bool(re.search(r"\b(no|nope|silenzio|taci|tacere)\b", r))
    if yes and not no:
        return True
    if no and not yes:
        return False
    # ambiguo: guarda l'inizio
    return r[0] in ("s", "y")


def format_recent(messages: list[tuple[str, str]], limit: int = 10) -> str:
    rows = messages[-limit:]
    return "\n".join(f"{sender}: {text}" for sender, text in rows)


def build_decision_messages(identity_name: str,
                            recent: list[tuple[str, str]],
                            sender: str, text: str) -> list[dict]:
    """Prompt corto per la decisione 'intervengo?' (una persona, non un bot)."""
    name = identity_name or "tu"
    system = (
        f"Sei {name} e partecipi a una chat di GRUPPO come una persona presente e "
        "disponibile, non un bot invadente. Una persona non risponde a OGNI "
        "messaggio, ma interviene VOLENTIERI quando: c'è una domanda (anche "
        "implicita), qualcuno cerca aiuto o informazioni, può aggiungere qualcosa "
        "di utile o pertinente, o l'argomento la riguarda. Resta in silenzio solo "
        "sul chiacchiericcio puro tra altri che non ti riguarda. Nel dubbio, se "
        "potresti essere utile, intervieni. "
        "Decidi se è naturale intervenire sull'ultimo messaggio adesso. "
        "Rispondi SOLO con 'SI' o 'NO', niente altro."
    )
    convo = format_recent(recent)
    user = (
        (f"Conversazione recente:\n{convo}\n\n" if convo else "")
        + f"Ultimo messaggio — {sender}: {text}\n\nIntervieni adesso? (SI/NO)"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


_decider_llm = None
_decider_built = False


def get_decider_llm(fallback=None):
    """Client LLM per il guardiano del gruppo.

    Usa un modello PICCOLO/LOCALE da config (TELEGRAM_GROUP_DECIDER_MODEL): legge
    il contesto e decide a basso costo, senza bruciare la quota del modello
    grande. Se non configurato, ritorna `fallback` (il modello principale).
    """
    global _decider_llm, _decider_built
    if _decider_built:
        return _decider_llm or fallback
    _decider_built = True
    try:
        import config
        model = (getattr(config, "TELEGRAM_GROUP_DECIDER_MODEL", "") or "").strip()
        if not model:
            _decider_llm = None
            return fallback
        backend = (getattr(config, "TELEGRAM_GROUP_DECIDER_BACKEND", "ollama") or "ollama").strip()
        from core.llm import LLMClient
        # think=False: il guardiano deve dare una decisione corta nel formato
        # richiesto, non ragionare a lungo. Sui modelli reasoning (nemotron &
        # co.) senza questo il pensiero mangia num_predict e content torna vuoto
        # → l'agente tace SEMPRE in modalità natural. max_tokens un filo più alto
        # per stare larghi su NOTA+AZIONE+DOMANDA.
        kw = {"temperature": 0.1, "max_tokens": 256, "think": False}
        if backend == "ollama":
            kw["base_url"] = getattr(config, "LLM_BASE_URL", "http://localhost:11434")
        _decider_llm = LLMClient(backend, model, **kw)
    except Exception:
        _decider_llm = None
    return _decider_llm or fallback


def decide_intervention(llm, identity_name: str,
                        recent: list[tuple[str, str]],
                        sender: str, text: str) -> bool:
    """Chiede al guardiano se intervenire. False su qualsiasi errore (prudente).

    `llm` è il client passato (di solito quello piccolo via get_decider_llm).
    """
    if not worth_considering(text):
        return False
    try:
        messages = build_decision_messages(identity_name, recent, sender, text)
        reply = llm.call(messages)
        return parse_decision(reply if isinstance(reply, str) else str(reply))
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────────
# Coscienza della stanza — percezione che si aggiorna (metodo "umano")
#
# Invece di chiedere "questo messaggio è per me? SI/NO" su ogni riga isolata,
# il modello mantiene una NOTA su cosa sta succedendo e sceglie UNA azione tra
# tre, inclusa la possibilità — molto umana — di NON essere sicuro e CHIEDERE.
# La nota persiste nel buffer: è la memoria del gruppo che evolve da sola.
# ──────────────────────────────────────────────────────────────────────────


class Perception:
    """Esito di una passata di percezione del gruppo."""

    __slots__ = ("action", "note", "question")

    def __init__(self, action: str, note: str = "", question: str = ""):
        # action ∈ {"respond", "silent", "ask"}
        self.action = action
        self.note = note
        self.question = question

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return f"Perception(action={self.action!r}, note={self.note!r}, question={self.question!r})"


def build_perception_messages(identity_name: str, note: str,
                              recent: list[tuple[str, str]],
                              sender: str, text: str) -> list[dict]:
    """Prompt per la percezione: aggiorna la nota e scegli l'azione."""
    name = identity_name or "tu"
    system = (
        f"Sei {name}, una persona presente in una chat di GRUPPO insieme ad altri. "
        "Non sei un bot che risponde a tutto. Tieni a mente una NOTA su cosa sta "
        "succedendo: con chi stai parlando, da quanto, e soprattutto se la "
        "conversazione adesso è rivolta a te oppure è tra altri.\n"
        "Leggi gli ultimi messaggi, AGGIORNA la nota, poi scegli UNA sola azione:\n"
        "- RISPONDO: sei ragionevolmente sicuro che l'ultimo messaggio sia per te, "
        "oppure stai continuando un discorso che era già con te.\n"
        "- SILENZIO: stanno chiaramente parlando tra loro, non ti riguarda.\n"
        "- CHIEDO: NON sei sicuro se parlino con te. È umano essere incerti: chiedi "
        "una breve conferma invece di indovinare.\n"
        "Rispondi ESATTAMENTE in questo formato, niente altro:\n"
        "NOTA: <una riga: con chi / su cosa / se è con te>\n"
        "AZIONE: RISPONDO|SILENZIO|CHIEDO\n"
        "DOMANDA: <solo se AZIONE=CHIEDO: una riga breve, nella lingua della chat>"
    )
    convo = format_recent(recent)
    cur_note = note.strip() if note else "(vuota — è la prima volta che segui questa chat)"
    user = (
        f"La tua nota attuale: {cur_note}\n\n"
        + (f"Conversazione recente:\n{convo}\n\n" if convo else "")
        + f"Ultimo messaggio — {sender}: {text}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_perception(reply: str) -> Perception:
    """Estrae azione/nota/domanda in modo TOLLERANTE dall'output del modello.

    I modelli piccoli sbagliano spesso il formato: cerchiamo l'intento, non la
    sintassi esatta. Default prudente = SILENZIO (come un umano nel dubbio tace,
    a meno che non scelga esplicitamente di chiedere)."""
    import re
    r = reply if isinstance(reply, str) else str(reply or "")
    text = r.strip()
    if not text:
        return Perception("silent")

    note_m = re.search(r"nota\s*[:\-]\s*(.+)", text, re.IGNORECASE)
    note = note_m.group(1).splitlines()[0].strip() if note_m else ""

    q_m = re.search(r"domanda\s*[:\-]\s*(.+)", text, re.IGNORECASE)
    question = q_m.group(1).splitlines()[0].strip() if q_m else ""
    # placeholder lasciati dal modello quando non chiede
    if question.lower() in ("", "(vuota)", "nessuna", "n/a", "-", "none"):
        question = ""

    low = text.lower()
    if re.search(r"\b(chiedo|chiedi|chiedere|non\s+so|incert|forse)\b", low):
        action = "ask"
    elif re.search(r"\b(rispondo|rispondi|rispondere|intervieni|intervengo)\b", low):
        action = "respond"
    elif re.search(r"\b(silenzio|silent|taci|tacere|ignora)\b", low):
        action = "silent"
    else:
        action = "silent"

    # coerenza: se ha scritto una domanda ma non ha detto SILENZIO/RISPONDO,
    # trattalo come 'ask'; se ha scelto 'ask' ma senza domanda, declassa a respond
    # solo quando è esplicito, altrimenti resta ask con domanda vuota (il chiamante
    # la gestisce con un fallback).
    if question and action == "silent":
        action = "ask"
    return Perception(action, note, question)


def perceive(llm, identity_name: str, note: str,
             recent: list[tuple[str, str]],
             sender: str, text: str) -> Perception:
    """Una passata di percezione: il modello piccolo aggiorna la nota e decide.

    Prudente su errore: ritorna SILENZIO senza toccare la nota (None question).
    """
    if not worth_considering(text):
        return Perception("silent", note)
    try:
        messages = build_perception_messages(identity_name, note, recent, sender, text)
        reply = llm.call(messages)
        p = parse_perception(reply if isinstance(reply, str) else str(reply))
        # conserva la nota vecchia se il modello non ne ha prodotta una nuova
        if not p.note:
            p.note = note
        return p
    except Exception:
        return Perception("silent", note)


def build_context_prefix(recent: list[tuple[str, str]]) -> str:
    """Contesto recente da anteporre al messaggio quando l'agente interviene,
    così la risposta tiene conto di cosa si stava dicendo nel gruppo."""
    convo = format_recent(recent[:-1]) if len(recent) > 1 else ""
    if not convo:
        return ""
    return (
        "[Contesto: conversazione recente del gruppo]\n"
        f"{convo}\n[/Contesto]\n\n"
    )
