"""What kind of session is this.

Once this module read openvurp's own workspace files — SOUL.md, IDENTITY.md,
USER.md and the rest — and put them in the prompt at every turn. They were
there to give openvurp a character.

openvurp is the platform: the agents are the ones you create, and each of them
carries its own name, role and instructions. The platform has no soul file to
read, so the loader went with the files.

What remains is the one thing the rest of the runtime still asks: whether a
turn is private, a group, a subagent, a scheduled job or the heartbeat. Memory,
the privacy router and the channels all branch on that answer.
"""

from __future__ import annotations


def resolve_session_type(source: str, sender: str, chat_type: str = "") -> str:
    """Determina il tipo di sessione in base a source, sender e chat_type.

    Args:
        chat_type: tipo di chat del canale ("group"/"supergroup" per i gruppi).
            È il segnale autorevole: il `sender` è il nome di una persona, non
            contiene "group", quindi senza questo i gruppi finivano per sbaglio
            in sessione "main" (e ricevevano memoria/profilo privati dell'owner).

    Returns:
        "main" per CLI e DM, "group" per chat di gruppo,
        "subagent" per sub-agenti, "heartbeat" per heartbeat
    """
    if source == "heartbeat":
        return "heartbeat"
    if source == "subagent":
        return "subagent"
    if source == "cron":
        return "cron"
    # Chat di gruppo (qualsiasi canale): contesto pubblico/ridotto, mai privato.
    if (chat_type or "").lower() in ("group", "supergroup"):
        return "group"
    if source == "cli":
        return "main"
    # Fallback storico: alcuni canali codificano il gruppo nel sender/source.
    if "group" in sender.lower() or "group" in source.lower():
        return "group"
    # DM su canale esterno
    return "main"
