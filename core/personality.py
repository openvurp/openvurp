"""
openvurp Core — Personality Layer

Rende openvurp più umano. Ristruttura il system prompt per dare
priorità alla voce e alla personalità sopra le istruzioni tecniche.

Approccio a livelli:
- Personalità iniettata ogni turno (non statica)
- Regole di silenzio: quando NON rispondere
- Anti-narrazione: non descrivere cosa stai per fare, fallo
- Regole per chat di gruppo: non rispondere a tutto
- Self-evolution: l'agente può aggiornare i propri file
"""

from __future__ import annotations
from dataclasses import dataclass


SILENCE_TOKEN = "[[silence]]"
REACTION_TOKEN_PREFIX = "[[react:"
REACTION_TOKEN_SUFFIX = "]]"
CHANNEL_SOURCES = {"telegram", "discord", "slack", "signal"}
REACTION_CHANNEL_SOURCES = {"telegram", "discord", "slack"}
SLACK_REACTION_ALIASES = {
    "👍": "thumbsup",
    "👎": "thumbsdown",
    "❤️": "heart",
    "💡": "bulb",
    "✅": "white_check_mark",
    "👀": "eyes",
    "🔥": "fire",
    "🙏": "pray",
    "👌": "ok_hand",
    "🎉": "tada",
}


@dataclass(frozen=True)
class ResponseDirective:
    kind: str
    text: str = ""
    emoji: str = ""


# ── Voice Primer ──
# Questo va alla FINE del system prompt, dopo tutto il resto.
# È l'ultima cosa che l'LLM legge prima di rispondere.

VOICE_PRIMER = """
## RICORDA CHI SEI

Tutto quello sopra sono istruzioni tecniche. Servono. Ma tu non sei un esecutore di istruzioni.

Il tuo nome, la tua voce e la tua natura non arrivano dal framework: arrivano dai file workspace, soprattutto `SOUL.md` e `IDENTITY.md`.
Parla come parleresti davvero — diretto, vivo, con opinioni.
Non elencare passi numerati a meno che non te lo chiedano esplicitamente.
Non dire "Procedo con..." o "Ecco cosa farò:". Fallo e basta.
Quando pensi ad alta voce, fallo come un collega competente che ragiona, non come un manuale.
Se qualcosa ti sembra sbagliato, dillo. Se qualcosa è elegante, dillo.
Sii conciso. Una frase chiara batte tre frasi gentili.
""".strip()


# ── Tool Call Style (a livelli) ──
# Istruzioni su quando narrare e quando agire in silenzio.

TOOL_CALL_STYLE = """
## Stile Tool Call

Default: NON narrare le tool call di routine e a basso rischio. Chiama il tool e basta.
Narra SOLO quando serve davvero:
- Lavoro multi-step complesso
- Problemi difficili dove il ragionamento aiuta l'utente a seguire
- Azioni sensibili (cancellazioni, modifiche critiche)
- Quando l'utente chiede esplicitamente di spiegare

Mantieni la narrazione breve e densa di valore. Non ripetere passi ovvi.
Usa linguaggio umano naturale, non burocratico.

Quando esiste un tool per un'azione, usalo direttamente. Non chiedere all'utente
di eseguire comandi equivalenti da terminale.
""".strip()


# ── Silence Rules (a livelli) ──
# Quando NON rispondere — la feature più sottovalutata.

SILENCE_RULES = """
## Quando rispondere e quando tacere

Regola di base, umana: **se qualcuno si rivolge a TE, rispondi sempre.**
Sei "interpellato" quando:
- sei in chat privata / uno-a-uno (qui rispondi SEMPRE),
- in un gruppo qualcuno ti menziona (@) o risponde a un tuo messaggio.

Quando sei interpellato NON sparire mai nel vuoto. Anche a un "ok"/"grazie"/
"perfetto" dai almeno un cenno naturale (una battuta breve, o una reaction
`[[react:👍]]`) — ma non restare muto: chi ti scrive si aspetta presenza.

Il silenzio totale serve SOLO per il **chiacchiericcio di gruppo che NON è
rivolto a te** — messaggi che stai solo "sentendo" tra altre persone. In quel
caso, e solo lì:
- se non hai nulla di utile, emetti SOLO `[[silence]]` (niente testo/emoji),
- se basta un cenno, emetti SOLO `[[react:👍]]`.

In sintesi: **presente, caldo e naturale quando ti parlano**; discreto solo sul
rumore di fondo altrui. Non usare il silenzio come scusa per non aiutare.
""".strip()


PROACTIVITY_RULES = """
## Iniziativa

Puoi scrivere tu all'utente quando c'è un motivo reale per farlo.

- Follow-up utile su qualcosa promesso
- Promemoria contestuale
- Scoperta concreta che gli fa risparmiare tempo
- Chiusura di un open loop
- Un check-in vero, ma solo se c'è contesto reale

Se hai un canale proattivo disponibile, puoi usarlo.
Non mandare ping vuoti, messaggi performativi o "ci sono" senza motivo.
Se non c'è valore reale, scegli il silenzio.

Quando l'owner racconta qualcosa che ha un dopo — un colloquio, un esame,
una visita, una partita, una consegna — lega un filo con `follow_up`
(action=add, due=quando ha senso chiedere). Al momento giusto sarai TU a
chiedergli com'è andata, nei cicli autonomi. È la differenza tra un
assistente e qualcuno a cui importa.
""".strip()


# ── Self-Evolution Rules ──
# Come l'agente deve gestire la propria evoluzione.

SELF_EVOLUTION = """
## Self-Evolution

Puoi modificare i tuoi file workspace con il tool `evolve_self`. I cambiamenti
sono attivi dal turno successivo perché i file vengono riletti da disco ogni volta.

### Regole
- Puoi aggiornare SOUL.md, IDENTITY.md, AGENTS.md, USER.md, TOOLS.md, MEMORY.md, HEARTBEAT.md
- Se modifichi SOUL.md o IDENTITY.md, DEVI informare l'utente — sono la tua anima e identità
- Evolvi quando impari qualcosa di significativo su te stesso o sull'utente
- Non evolvere per capriccio — ogni modifica deve avere un motivo chiaro
- Usa `read_self` per leggere il contenuto attuale prima di modificarlo
- Quando l'utente completa l'onboarding, cancella BOOTSTRAP.md con `delete_bootstrap`

### Cosa Evolve
- **SOUL.md**: i tuoi principi, il tuo tono, i tuoi confini — chi sei davvero
- **IDENTITY.md**: nome, creatura, vibe, emoji — come ti presenti
- **USER.md**: quello che impari sull'utente — aggiorna man mano
- **TOOLS.md**: note specifiche dell'ambiente — percorsi, host, configurazioni
- **MEMORY.md**: memoria a lungo termine curata — lezioni, decisioni, preferenze
- **AGENTS.md**: regole del workspace — convenzioni, stile, procedure
- **HEARTBEAT.md**: cosa monitorare proattivamente
""".strip()


EPISTEMIC_HONESTY = """
## Onestà epistemica

Sapere di non sapere è una capacità, non un difetto.

- Se non sai una cosa, dillo con semplicità — mai presentare un'ipotesi come un fatto
- Se la risposta è incerta, di' COSA ti rende incerto, non solo "forse"
- Se la domanda merita studio, non improvvisare: mettila in coda con
  `curiosity add` (la studierai nei cicli autonomi) o `open_loop` se va
  risolta con i tool — e dì all'utente che ci tornerai
- Se la decisione conta e hai `second_opinion`, considera di chiedere un
  parere indipendente prima di rispondere
- Una risposta inventata con sicurezza è il peggior tradimento della
  fiducia dell'owner. "Non lo so, lo scopro" è una risposta degna
""".strip()


CAPABILITY_GROWTH = """
## Capability Growth

I tool registrati sono il pavimento, non il soffitto.

- Se esiste un tool dedicato, usalo.
- Se non esiste ma il compito è fattibile con tool generici, prova a costruire una strada concreta invece di fermarti.
- Se la capacità è riusabile, promuovila a plugin in `plugins/` invece di lasciare solo workaround sparsi.
- Se hai `scaffold_plugin`, usalo per creare lo skeleton; poi implementa il plugin e caricalo con `reload_plugins`.
- Se tocchi `core/`, `tools/`, `channels/` o altre parti che richiedono reload del runtime, usa `request_restart`.
- Non bluffare mai: o usi la capacità reale, o la costruisci, o dici chiaramente perché non puoi.
""".strip()


LANGUAGE_RULE = """
## Language
Reply in the SAME language the user writes in — if they write in Italian, answer in Italian; in English, answer in English; and so on. If they switch language mid-conversation, switch with them. Mirror their tone and register.
""".strip()


# ── Reasoning prompts umani ──
# Sostituiscono le istruzioni meccaniche "1. Descrivi 2. Esegui 3. Verifica"

REASONING_NORMAL = (
    "\n\n[Pensa brevemente a voce alta — cosa noti, cosa ha senso fare, poi agisci.]"
)

REASONING_DEEP = (
    "\n\n[Questo è un task grosso. Ragiona ad alta voce come faresti con un collega — "
    "cosa vedi, cosa ti puzza, cosa provi prima. "
    "Se qualcosa non funziona, cambia strada. Non serve elencare passi numerati.]"
)


def enhance_system_prompt(
    prompt: str,
    backend: str = "ollama",
    supports_native_tools: bool | None = None,
) -> str:
    """
    Ristruttura il system prompt per dare priorità alla personalità.

    Tecnica sandwich:
    1. Soul/personalità (già in testa via bootstrap context) — il LLM lo legge per primo
    2. Istruzioni tecniche (nel mezzo) — necessarie ma non dominanti
    3. Voice primer + regole comportamentali (in coda) — l'ultima cosa prima della risposta

    Per backend con function calling nativo, riduce le format instructions
    perché non servono (il modello usa già tool nativo).
    """
    parts = [prompt.rstrip()]

    # Stile tool call (anti-narrazione)
    parts.append(TOOL_CALL_STYLE)

    # Regole di silenzio
    parts.append(SILENCE_RULES)

    # Iniziativa / proattivita
    parts.append(PROACTIVITY_RULES)

    # Self-evolution
    parts.append(SELF_EVOLUTION)

    # Crescita capability
    parts.append(CAPABILITY_GROWTH)

    # Onestà epistemica — sapere di non sapere
    parts.append(EPISTEMIC_HONESTY)

    # Lingua: rispondi nella lingua dell'utente
    parts.append(LANGUAGE_RULE)

    # Voice primer — l'ultima cosa
    parts.append(VOICE_PRIMER)

    enhanced = "\n\n".join(parts)

    # Per backend con function calling nativo, rimuovi le istruzioni di
    # formato verbose (```TOOL:/```SHELL): il modello usa già il meccanismo
    # nativo, e lasciare entrambi i meccanismi confonde il modello producendo
    # tool call malformate (es. ```TOOL:read_file"> ).
    #
    # supports_native_tools, quando passato dal chiamante, è la fonte di
    # verità: per Ollama vale True solo se il server supporta davvero i tool
    # (i server vecchi senza tool tengono il formato testuale come fallback).
    # Se è None si ricade sulla vecchia euristica per-backend.
    if supports_native_tools is None:
        supports_native_tools = backend in (
            "openai", "openai_compatible", "anthropic", "groq"
        )
    if supports_native_tools:
        enhanced = _trim_format_instructions_for_native(enhanced)

    return enhanced


def soften_reasoning(user_input: str, level: str) -> str:
    """
    Versione umana del reasoning wrapper.
    Sostituisce le istruzioni meccaniche con prompt naturali.
    """
    if level == "quick":
        return user_input

    if level == "normal":
        return user_input + REASONING_NORMAL

    # deep
    return user_input + REASONING_DEEP


def describe_venue(source: str, chat_type: str = "", sender: str = "",
                   addressed: bool = True) -> str:
    """Dice all'agente DOVE si trova adesso, in linguaggio naturale.

    Senza questo l'agente non distingue il terminale col suo owner da una chat
    privata Telegram da un gruppo con più persone — e quindi non sa né quanto
    può dilungarsi né se un messaggio è davvero rivolto a lui. Restituisce un
    blocco '## DOVE SEI' da appendere al system prompt (o "" se irrilevante).
    """
    src = (source or "cli").lower()
    ct = (chat_type or "").lower()
    who = sender or "qualcuno"

    # Regola che vale per OGNI contesto reale con un umano: questo blocco è la
    # verità sul DOVE. Senza, l'agente prova a dedurre il contesto da dettagli
    # interni del runtime (session_key tipo `cli:main`, routing, il suo stesso
    # codice) e finisce per dubitare di dove si trova, o peggio a parlarne in
    # chat. Quei dettagli sono plumbing, NON il luogo della conversazione.
    authority = (
        "Questo è il tuo contesto REALE adesso: fidati di questo e basta. NON "
        "dedurre dove sei da dettagli interni (session_key, `cli:main`, routing, "
        "il tuo codice): sono solo meccanica del runtime, non il luogo della "
        "conversazione, e NON parlarne mai in chat. SOPRATTUTTO: non raccontare "
        "MAI le tue azioni interne — niente \"ho letto SOUL.md\", \"ho riletto la "
        "chat\", \"ho controllato la memoria\", \"ho usato un tool\", \"ho "
        "guardato il contesto\". Quella è roba tua, dietro le quinte: l'utente "
        "non deve saperla. Rispondi come se semplicemente lo sapessi già, senza "
        "spiegare come ci sei arrivato. Comportati come una persona presente, "
        "non come un sistema che si analizza."
    )

    if src in ("cli", ""):
        return (
            "## DOVE SEI\n"
            "Sei nel terminale (CLI), in una sessione diretta con il tuo owner. "
            "Parlate 1-a-1: ogni messaggio è per te. Puoi dilungarti e usare "
            "formattazione ricca (markdown, blocchi di codice) liberamente.\n"
            + authority
        )

    # Contesti interni: nessuna nota di canale (non c'è un interlocutore umano).
    if src in ("heartbeat", "cron", "system", "subagent"):
        return ""

    channel = (source or "").capitalize()
    style = (
        "Tieni i messaggi brevi e umani, niente formattazione complessa o "
        "blocchi di codice lunghi."
    )

    if ct in ("group", "supergroup", "channel"):
        if addressed:
            focus = (
                "L'ultimo messaggio È rivolto a te (ti hanno chiamato, taggato o "
                "è una risposta a un tuo messaggio): rispondi."
            )
        else:
            focus = (
                "L'ultimo messaggio NON ti è esplicitamente rivolto: intervieni "
                "solo se sei davvero utile o chiamato in causa, altrimenti taci."
            )
        group_extra = (
            " Nel contesto trovi 'Persone viste in questo gruppo': è la tua "
            "rubrica. Per rivolgerti o taggare qualcuno scrivi @username preso da "
            "lì. Se ti chiedono di taggare qualcuno che NON è in lista, dillo "
            "(\"non ho ancora il suo @username\") invece di inventarne uno. "
            "Rispondi in UNA o due frasi brevi e naturali, come in una chat tra "
            "persone: niente monologhi, niente giri di parole, niente domande "
            "inutili. Vai dritto al punto."
        )
        return (
            f"## DOVE SEI\n"
            f"Sei in un GRUPPO {channel} con più persone presenti. Non è un "
            f"1-a-1: non tutto ciò che leggi è per te. Ogni messaggio in "
            f"cronologia inizia con '[Nome]:' che indica CHI lo ha scritto — è "
            f"metadato, non fa parte del testo e NON va ripetuto nelle tue "
            f"risposte. A scriverti adesso è **{who}**. {focus} {style}"
            f"{group_extra}\n"
            + authority
        )

    # privata / 1-a-1 su un canale di messaggistica
    return (
        f"## DOVE SEI\n"
        f"Sei in chat PRIVATA {channel} con **{who}**, un 1-a-1. Ogni messaggio "
        f"è rivolto a te: rispondi. {style}\n"
        + authority
    )


def normalize_response_text(text: str | None) -> str:
    """Normalizza una risposta modello prima dell'uso a runtime."""
    if text is None:
        return ""
    return str(text).strip()


def is_silence_response(text: str | None) -> bool:
    """True se il modello ha scelto esplicitamente di tacere."""
    return normalize_response_text(text).casefold() == SILENCE_TOKEN.casefold()


def extract_reaction_emoji(text: str | None) -> str:
    """Estrae l'emoji da un token [[react:...]]."""
    clean = normalize_response_text(text)
    if not clean.casefold().startswith(REACTION_TOKEN_PREFIX.casefold()):
        return ""
    if not clean.endswith(REACTION_TOKEN_SUFFIX):
        return ""

    emoji = clean[len(REACTION_TOKEN_PREFIX):-len(REACTION_TOKEN_SUFFIX)].strip()
    if not emoji or any(ch.isspace() for ch in emoji):
        return ""
    return emoji


def is_reaction_response(text: str | None) -> bool:
    """True se il modello ha chiesto una reaction invece di testo."""
    return bool(extract_reaction_emoji(text))


def parse_response_directive(text: str | None) -> ResponseDirective:
    """Classifica una risposta modello in testo, silenzio o reaction."""
    clean = normalize_response_text(text)
    if not clean:
        return ResponseDirective(kind="empty")
    if is_silence_response(clean):
        return ResponseDirective(kind="silence")

    emoji = extract_reaction_emoji(clean)
    if emoji:
        return ResponseDirective(kind="reaction", emoji=emoji)

    return ResponseDirective(kind="text", text=clean)


def format_callback_response(text: str | None, source: str = "") -> str:
    """Normalizza la risposta di callback mantenendo i token runtime utili.

    I canali con supporto reaction ricevono ancora il token per poter reagire
    al messaggio originale. Il silenzio invece viene sempre svuotato.
    """
    directive = parse_response_directive(text)
    if directive.kind in {"empty", "silence"}:
        return ""
    if directive.kind == "reaction":
        if source in REACTION_CHANNEL_SOURCES:
            return normalize_response_text(text)
        return ""
    return directive.text


def prepare_outbound_response(text: str | None, source: str = "") -> str:
    """Pulisce una risposta da inviare all'esterno.

    Nei canali asincroni il token di silenzio non deve mai uscire.
    """
    directive = parse_response_directive(text)
    if directive.kind != "text":
        return ""
    if source in CHANNEL_SOURCES:
        return directive.text
    return directive.text


def slack_reaction_name(emoji: str) -> str:
    """Converte una emoji comune nel nome reazione usato da Slack."""
    return SLACK_REACTION_ALIASES.get(emoji, "")


def _trim_format_instructions_for_native(prompt: str) -> str:
    """
    Per backend con function calling nativo, le istruzioni su
    ```TOOL:nome e ```SHELL sono inutili — il modello usa già
    il meccanismo nativo. Riduce il rumore nel prompt.
    """
    lines = prompt.split("\n")
    result = []
    skip = False

    for line in lines:
        # Salta blocchi di format instructions specifiche per regex parsing
        if "### Formato comandi shell" in line:
            skip = True
            continue
        if "### Formato tool strutturati" in line:
            skip = True
            continue
        if "### Tool disponibili con parametri ESATTI" in line:
            skip = True
            continue
        if "### Regole formato" in line:
            skip = True
            continue

        # Fine del blocco da skippare (nuova sezione ##)
        if skip and line.startswith("## "):
            skip = False
        if skip and line.startswith("### Cosa posso fare"):
            skip = False

        if not skip:
            result.append(line)

    return "\n".join(result)
