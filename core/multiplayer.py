"""Collaborative rooms with persistent peer agents.

This does not use SubagentManager: every profile is a stable participant in the
room, with its own LLM client, role, and messages attributed in the ChatStore.
"""

from __future__ import annotations

import threading

from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.chat_store import ChatStore
from core.llm import create_llm_client


@dataclass
class TeamResult:
    brief: str = ""
    messages: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    errors: list[str] = field(default_factory=list)
    rounds: int = 0
    ended: str = ""      # "silence" | "stop" | "cap" | "budget" | "empty"


NOTHING = "\u2014"   # silence is a valid answer: nobody is obliged to speak


_STOP_LOCK = threading.RLock()
_STOPPED: set[str] = set()


def request_stop(chat_id: str) -> None:
    """"Okay, stop now." Applies to the discussion running in that room."""
    with _STOP_LOCK:
        _STOPPED.add(str(chat_id))


def clear_stop(chat_id: str) -> None:
    with _STOP_LOCK:
        _STOPPED.discard(str(chat_id))


def stop_requested(chat_id: str) -> bool:
    with _STOP_LOCK:
        return str(chat_id) in _STOPPED


def _bare(text: str) -> str:
    return "".join(c for c in str(text or "").lower() if c.isalnum())


def already_said(said, profile, text: str) -> bool:
    """True if this agent is repeating something it has already said.

    The prompt can ask them not to repeat themselves all it likes: when the
    model recites the same closing line word for word, the only serious barrier
    is code. A repetition is not a contribution — it is silence with an echo —
    and treating it as silence is what makes a discussion end on its own
    instead of at the round cap.
    """
    import difflib

    fresh = _bare(text)
    if not fresh:
        return False
    for who, older in said:
        if who["id"] != profile["id"]:
            continue
        before = _bare(older)
        if fresh == before:
            return True
        # "Got it Enzo, settled for me" contains "settled for me": a courtesy
        # wrapped around the same sentence is still the same sentence.
        if len(fresh) >= 12 and (fresh in before or before in fresh):
            return True
        # Similarity applies to SHORT text only: that is how closing formulas
        # are recognised. On a long contribution, changing one digit would be
        # enough to look new — but a long contribution that is nearly identical
        # does not happen: either it is identical (caught above) or genuinely new.
        if len(fresh) < 60 and \
                difflib.SequenceMatcher(None, fresh, before).ratio() > 0.85:
            return True
    return False
    for who, vecchio in said:
        if who["id"] != profile["id"]:
            continue
        prima = _bare(vecchio)
        if nuovo == prima:
            return True
        # «Ricevuto Enzo, per me e' chiusa» contiene «per me e' chiusa»:
        # un'aggiunta di cortesia attorno alla stessa frase resta la stessa frase.
        if len(nuovo) >= 12 and (nuovo in prima or prima in nuovo):
            return True
        # La somiglianza vale solo sul CORTO: le formule di chiusura si
        # riconoscono cosi'. Su un intervento lungo basterebbe cambiare una
        # cifra per sembrare nuovo — ma un intervento lungo quasi identico a
        # occhio non capita: o e' identico (preso sopra) o e' davvero nuovo.
        if len(nuovo) < 60 and                 difflib.SequenceMatcher(None, nuovo, prima).ratio() > 0.85:
            return True
    return False


def has_words(text) -> bool:
    """A turn made only of emoji or punctuation is not a contribution."""
    return any(ch.isalpha() for ch in str(text or ""))


class MultiplayerCoordinator:
    def __init__(self, store: ChatStore):
        self.store = store

    def collaborate(self, chat_id: str, request: str, run_id: str = "",
                    on_turn=None, on_message=None, should_stop=None) -> TeamResult:
        """``on_turn(profile)`` before someone speaks, ``on_message(row)`` as
        soon as they have: a room must be shown while it happens.

        With three agents and two rounds the wait is tens of seconds, and until
        now everything appeared at the end together — it looked stuck. The
        contributions are already sequential: they just have to be let out one
        at a time.

        ``should_stop()`` is asked before every contribution: it is the user's
        "okay, stop now". The discussion no longer has a round count decided in
        advance — it runs while somebody genuinely has something to add, ends
        by itself when a whole round passes in silence, and stops at once if
        you ask it to.
        """
        import config as cfg

        participants = self.store.chat_agents(chat_id)
        max_agents = max(1, min(int(getattr(cfg, "MULTIPLAYER_MAX_AGENTS", 3)), 6))
        participants = participants[:max_agents]
        if not participants:
            return TeamResult(ended="empty", errors=[
                "There is nobody in this room. Add agents from the roster."
            ])
        daily_limit = max(0, int(getattr(cfg, "MULTIPLAYER_DAILY_CALL_BUDGET", 120)))
        used_today = self.store.count_agent_messages_since(
            datetime.now(timezone.utc).date().isoformat()
        )
        if daily_limit and used_today >= daily_limit:
            return TeamResult(ended="budget", errors=[
                f"Daily limit reached: {used_today} contributions out of "
                f"{daily_limit}. You can raise it in Settings, under "
                f"\u201cAgents \u2192 calls per day\u201d."
            ])

        history = self._history(chat_id)
        # A safety cap, not a duration: it exists only so that a room left
        # alone does not run forever at your expense.
        max_rounds = max(1, min(int(getattr(cfg, "MULTIPLAYER_MAX_ROUNDS", 12)), 40))
        output = TeamResult()
        said: list[tuple[dict, str]] = []
        # How many contributions from OTHERS were on the table the last time
        # each of them spoke. This stops anyone from speaking twice when
        # nothing new has arrived for them to answer.
        seen_by: dict[str, int] = {}

        # A discussion is sequential by nature: whoever speaks later must be
        # able to read whoever spoke before. In parallel everyone writes blind
        # and what comes out is N monologues side by side, not a debate.
        round_no = 0
        while True:
            round_no += 1
            if round_no > max_rounds:
                output.ended = "cap"
                break
            spoke_this_round = False
            for profile in participants:
                if should_stop is not None and should_stop():
                    output.ended = "stop"
                    break
                others = sum(1 for who, _ in said if who["id"] != profile["id"])
                # Amanda answered first; the other two stayed quiet; on the
                # next round Amanda found only herself in front of her and
                # repeated her own message word for word. A turn with nothing
                # new in front of it is not a turn: it is skipped.
                if round_no > 1 and others <= seen_by.get(profile["id"], -1):
                    continue
                seen_by[profile["id"]] = others
                if on_turn is not None:
                    try:
                        on_turn(profile, round_no)
                    except Exception:
                        pass   # the room does not stop for a UI problem
                floor = "\n\n".join(
                    f"[{who['name']} · {who['role']}] {text}" for who, text in said
                )[-6000:]
                try:
                    text, tok_in, tok_out = self._speak(
                        profile, request, history, floor, round_no,
                    )
                except Exception as exc:
                    output.errors.append(f"{profile['name']}: {exc}")
                    continue
                output.input_tokens += tok_in
                output.output_tokens += tok_out
                if not text.strip() or text.strip() in {NOTHING, "-", "--"}:
                    continue   # they chose to add nothing
                if round_no > 1 and not has_words(text):
                    continue   # \u201c\U0001f319\u201d is filler, not a contribution
                if round_no > 1 and already_said(said, profile, text):
                    continue   # echoing yourself is silence, not a turn
                said.append((profile, text.strip()))
                row = self.store.add_message(
                    chat_id, "assistant", text.strip(), author_type="agent",
                    author_id=profile["id"], author_name=profile["name"],
                    recipient_id="room", run_id=run_id,
                    metadata={"round": round_no, "role": profile["role"]},
                )
                output.messages.append(row)
                spoke_this_round = True
                if on_message is not None:
                    try:
                        on_message(row)
                    except Exception:
                        pass

            if output.ended == "stop":
                break
            # Nobody opened their mouth for a whole round: the discussion
            # ended by itself. That is how it should close, not by counting.
            if not spoke_this_round:
                output.ended = "silence"
                break

        output.rounds = round_no

        # A discussion with no landing point reads like a quarrel that never
        # ends: N opinions side by side and nobody saying where it got to.
        # Whoever opened it closes it — not a chairman, just a way of not
        # leaving the room half finished.
        if len(said) >= 2 and round_no > 1:
            chair = said[0][0]
            chiusura, tok_in, tok_out = self._close(
                chair, request, said, output.ended)
            output.input_tokens += tok_in
            output.output_tokens += tok_out
            if chiusura:
                row = self.store.add_message(
                    chat_id, "assistant", chiusura, author_type="agent",
                    author_id=chair["id"], author_name=chair["name"],
                    recipient_id="room", run_id=run_id,
                    metadata={"round": round_no, "role": chair["role"],
                              "closing": True},
                )
                output.messages.append(row)
                if on_message is not None:
                    try:
                        on_message(row)
                    except Exception:
                        pass

        output.brief = self._brief(said)
        return output

    def _client(self, profile: dict, request: str = ""):
        import config as cfg

        backend = profile.get("backend") or getattr(cfg, "MULTIPLAYER_BACKEND", "")
        model = profile.get("model") or getattr(cfg, "MULTIPLAYER_MODEL", "")
        if str(backend or "").strip().lower() == "auto":
            from core.model_router import route_chat_prompt
            route = route_chat_prompt(request)
            backend, model = route.backend, route.model
        client = create_llm_client(backend=backend, model=model)
        client.max_tokens = max(128, min(
            int(getattr(cfg, "MULTIPLAYER_MAX_TOKENS", 900)), 2000
        ))
        client.temperature = 0.35
        return client

    def _roster(self, exclude_id: str) -> str:
        """Chi altro esiste. Un agente che non sa chi c'e' non puo' passare la
        palla a chi ne sa di piu': puo' solo improvvisare o tacere."""
        others = [
            a for a in self.store.list_agents(enabled_only=True)
            if a["id"] != exclude_id
        ]
        if not others:
            return ""
        rows = "\n".join(f"- {a['name']}: {a['role']}" for a in others)
        return (
            "\nNella rubrica ci sono anche questi agenti:\n" + rows +
            "\nSe una domanda ricade nel campo di uno di loro, dillo apertamente "
            "('questo lo saprebbe X meglio di me') invece di improvvisare."
        )

    def _speak(self, profile: dict, request: str, history: str,
               floor: str, round_no: int) -> tuple[str, int, int]:
        """Un intervento nella stanza, con davanti quello che hanno gia' detto."""
        rules = (
            f"Sei {profile['name']}, ruolo persistente: {profile['role']}.\n"
            f"{profile['instructions']}\n"
            "Sei un membro paritario di una stanza multi-agente, non un subagent. "
            "Non fingere di aver usato tool: lavori sul contesto fornito.\n"
            "Parla come parlerebbe una persona con quel carattere: rivolgiti agli altri "
            "per nome, contraddicili quando serve, cambia idea se qualcuno ti convince. "
            "Niente formule da assistente ('Come posso aiutarti?', 'Ottima domanda'): "
            "sei uno che sta discutendo, non uno che serve.\n"
            "Attieniti a cio' che e' stato davvero detto: non attribuire agli altri "
            "posizioni che non hanno espresso, e non dichiarare un accordo che non c'e'.\n"
            # Tre tic che rivelano la macchina: annunciare di essere d'accordo,
            # riassumere chi ha appena parlato, chiudere ogni battuta con una
            # «conclusione». In una chat vera non li fa nessuno.
            "Non dire che sei d'accordo tanto per dirlo, non riassumere quello che "
            "hanno appena scritto gli altri e non chiudere con 'Conclusione:'. "
            f"Se non hai niente da aggiungere, rispondi solo {NOTHING} e taci: "
            "e' una risposta legittima."
            + self._roster(profile["id"])
        )
        # Nessun classificatore decide qui che tipo di messaggio sia. Sapere se
        # «io vado a dormire ragazzi» e' un congedo e' esattamente cio' che un
        # modello sa fare meglio di una lista di parole — che infatti lo mancava.
        # Al codice resta solo di NON obbligarlo a parlare.
        if not floor:
            task = (
                "Rispondi come risponderesti davvero: puo' essere una posizione "
                "argomentata, una battuta o due righe, secondo cosa ha scritto. "
                f"Se non e' cosa tua o non hai niente da dire, scrivi {NOTHING}."
            )
        elif round_no == 1:
            task = (
                "Hai letto chi ha parlato prima di te. Aggiungi qualcosa di TUO: "
                f"un punto nuovo, un dubbio, un dato. Se non ce l'hai, scrivi {NOTHING}."
            )
        else:
            # Una discussione serve a decidere. Chiedendo solo «dove NON sei
            # d'accordo», e senza piu' un numero di giri che la chiudeva
            # d'ufficio, l'unico modo di avere diritto di parola diventava
            # trovare un altro disaccordo: litigavano all'infinito per
            # costruzione. Qui il disaccordo va detto, ma va detto anche cosa
            # servirebbe per chiuderlo — e convergere e' un finale, non una resa.
            task = (
                f"Giro {round_no}. Hai gia' parlato: NON ripetere quello che hai "
                f"gia' detto, nemmeno riformulato.\n"
                f"Se resta un disaccordo vero, dillo agli altri per nome, spiega "
                f"perche', e aggiungi COSA SERVIREBBE per scioglierlo (un dato, "
                f"una prova, una scelta dell'utente).\n"
                # Niente frasi-esempio fra virgolette: i modelli le recitano
                # identiche a ogni giro, e la recita conta come aver parlato —
                # e' nato cosi' un loop di 27 «per me si fa cosi', non ho altro».
                f"Se per te la questione e' chiusa, scrivi {NOTHING}: l'hai "
                f"gia' detta, ripeterla non la chiude di piu'.\n"
                f"Non cercare un disaccordo nuovo solo per avere qualcosa da dire: "
                f"quando avete detto tutto, scrivi {NOTHING} e basta. Chiudere e' "
                f"una risposta legittima, riempire il turno no."
            )
            if round_no >= 4:
                # Dopo qualche giro, se non ci si avvicina non ci si avvicinera'.
                task += (
                    f"\nSiete al giro {round_no}: se le posizioni non si stanno "
                    f"avvicinando, non insistere. Di' in una riga cosa resta "
                    f"aperto e chi deve deciderlo, poi chiudi."
                )
        content = f"Contesto recente della stanza:\n{history}\n\nRichiesta:\n{request}"
        if floor:
            content += f"\n\nQuello che e' stato detto finora in questa discussione:\n{floor}"
        content += f"\n\n{task}"
        text, _duration, tok_in, tok_out = self._client(
            profile, request,
        ).call_with_timing([
            {"role": "system", "content": rules},
            {"role": "user", "content": content},
        ])
        return text, tok_in, tok_out

    def _close(self, chair: dict, request: str,
               said: list[tuple[dict, str]],
               ended: str) -> tuple[str, int, int]:
        """Where you got to. Without inventing an agreement that isn't there."""
        tavolo = "\n\n".join(f"[{who['name']}] {text}" for who, text in said)[-6000:]
        why = {
            "stop": "The user has just stopped you.",
            "silence": "Nobody had anything left to add.",
            "cap": "The discussion has gone on for a long time.",
        }.get(ended, "")
        rules = (
            f"Sei {chair['name']}, ruolo: {chair['role']}. Hai aperto tu questa "
            f"discussione e adesso la chiudi per chi vi legge.\n"
            f"{chair.get('instructions', '')}"
        )
        content = (
            f"Richiesta di partenza:\n{request}\n\n"
            f"Quello che vi siete detti:\n{tavolo}\n\n"
            f"{why}\n"
            "Scrivi la chiusura in poche righe, parlando all'utente:\n"
            "1. Su cosa siete d'accordo (solo se lo siete davvero).\n"
            "2. Su cosa NO, e chi la pensa diversamente — per nome.\n"
            "3. Cosa serve per decidere, o cosa deve scegliere l'utente.\n"
            "Vincoli: non dichiarare un accordo che non c'e', non introdurre "
            "opzioni che nessuno ha proposto, non riassumere battuta per "
            "battuta. Se la conclusione e' «non abbiamo deciso», dillo."
        )
        try:
            text, _d, tok_in, tok_out = self._client(chair, request).call_with_timing([
                {"role": "system", "content": rules},
                {"role": "user", "content": content},
            ])
        except Exception:
            return "", 0, 0
        text = (text or "").strip()
        if not has_words(text) or text in {NOTHING, "-", "--"}:
            return "", tok_in, tok_out
        return text, tok_in, tok_out

    def _history(self, chat_id: str) -> str:
        messages = self.store.list_messages(chat_id, limit=10)
        rows = []
        for item in messages:
            who = item.get("author_name") or item.get("author_type") or item.get("role")
            text = " ".join(str(item.get("content", "")).split())[:600]
            if text:
                rows.append(f"[{who}] {text}")
        return "\n".join(rows)[:6000] or "(stanza nuova)"

    @staticmethod
    def _brief(contributions: list[tuple[dict, str]]) -> str:
        if not contributions:
            return ""
        packet = "\n\n".join(
            f"### {profile['name']} ({profile['role']})\n{text[:1800]}"
            for profile, text in contributions
        )
        return (
            "## DISCUSSIONE DELLA STANZA MULTI-AGENTE\n"
            "Questi sono messaggi reali dei tuoi peer. Riporta all'utente cosa hanno "
            "detto DAVVERO.\n"
            "Vincoli, non consigli:\n"
            "- NON introdurre opzioni che nessuno ha proposto. Se hai un'idea tua, "
            "presentala come tua, separata dalle loro.\n"
            "- NON dichiarare accordo o unanimita' se non c'e': se restano in "
            "disaccordo, dillo e spiega su cosa.\n"
            "- Attribuisci ogni posizione a chi l'ha espressa, per nome.\n\n"
            + packet
        )[:7000]
