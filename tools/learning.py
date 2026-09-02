"""
openvurp Tools - Verified Learning Loop
"""

from __future__ import annotations

import os

from core.learning import LearningLoop
from core.tools import Tool, ToolResult


OPENVURP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_DIR = os.path.join(OPENVURP_DIR, "memory")


def _learning() -> LearningLoop:
    """L'archivio di chi sta chiamando, non quello di tutti.

    Prima era uno solo: qualunque agente chiamasse learning_feedback scriveva
    nello stesso mucchio. Lo scope lo mette lo sciame prima di eseguire il
    tool; senza scope si ricade sulla piattaforma, che e' il caso del terminale.
    """
    from core.learning import current_scope
    return LearningLoop(MEMORY_DIR, scope=current_scope())


def learning_feedback_handler(topic: str = "", feedback: str = "",
                              rating: int = 0, desired_change: str = "",
                              tags: list[str] | None = None) -> ToolResult:
    if not (feedback or "").strip():
        return ToolResult.fail("Parametro feedback obbligatorio.")

    event = _learning().record_feedback(
        topic=topic,
        feedback=feedback,
        rating=rating,
        desired_change=desired_change,
        tags=tags or [],
        actor="agent",
        source="tool",
    )
    return ToolResult.ok(
        f"Feedback registrato: {event.topic} ({event.signal}, {event.source})."
    )


def learning_review_handler(max_events: int = 200,
                            min_repeats: int = 2) -> ToolResult:
    report = _learning().review(
        max_events=max(1, int(max_events or 200)),
        min_repeats=max(1, int(min_repeats or 2)),
    )
    return ToolResult.ok(report.render())


def learning_promote_handler(candidate_id: str = "", topic: str = "",
                             content: str = "",
                             tags: list[str] | None = None,
                             force: bool = False) -> ToolResult:
    result = _learning().promote_candidate(
        candidate_id=candidate_id,
        topic=topic,
        content=content,
        tags=tags or [],
        actor="agent",
        source="tool",
        force=bool(force),
    )
    if result.startswith("[ERROR]") or result.startswith("[VERIFICA FALLITA]"):
        return ToolResult.fail(result)
    return ToolResult.ok(result)


def learning_rollback_handler(filename: str = "", reason: str = "") -> ToolResult:
    result = _learning().rollback_lesson(
        filename=filename,
        reason=reason,
        actor="agent",
        source="tool",
    )
    if result.startswith("[ERROR]"):
        return ToolResult.fail(result)
    return ToolResult.ok(result)


LEARNING_FEEDBACK_TOOL = Tool(
    name="learning_feedback",
    description=(
        "Registra feedback esplicito dell'utente come evento locale del learning loop."
    ),
    parameters={
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Area o preferenza a cui si riferisce il feedback.",
            },
            "feedback": {
                "type": "string",
                "description": "Feedback dell'utente da conservare.",
            },
            "rating": {
                "type": "integer",
                "description": "-1 negativo, 0 neutro, 1 positivo.",
            },
            "desired_change": {
                "type": "string",
                "description": "Cambiamento desiderato se il feedback indica un problema.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tag brevi per retrieval futuro.",
            },
        },
        "required": ["feedback"],
    },
    handler=learning_feedback_handler,
)

LEARNING_REVIEW_TOOL = Tool(
    name="learning_review",
    description=(
        "Analizza eventi locali di apprendimento e genera candidati verificabili."
    ),
    parameters={
        "type": "object",
        "properties": {
            "max_events": {
                "type": "integer",
                "description": "Numero massimo di eventi recenti da considerare.",
            },
            "min_repeats": {
                "type": "integer",
                "description": "Ripetizioni minime per promuovere errori tool a candidato.",
            },
        },
    },
    handler=learning_review_handler,
)

LEARNING_PROMOTE_TOOL = Tool(
    name="learning_promote",
    description=(
        "Promuove un candidato learning o una lezione manuale in memory/lessons. "
        "La promozione è VERIFICATA: viene bloccata se confidenza/evidenze sono "
        "insufficienti, se contiene segreti o se duplica una lezione esistente. "
        "La lezione scritta include la provenienza (candidato, evidenze, verifica)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "candidate_id": {
                "type": "string",
                "description": "ID candidato prodotto da learning_review.",
            },
            "topic": {
                "type": "string",
                "description": "Titolo lezione se non si usa candidate_id.",
            },
            "content": {
                "type": "string",
                "description": "Contenuto lezione se non si usa candidate_id.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tag brevi della lezione.",
            },
            "force": {
                "type": "boolean",
                "description": "Promuovi anche se la verifica fallisce. Solo con accordo esplicito dell'owner.",
            },
        },
    },
    requires_approval=True,
    handler=learning_promote_handler,
)

LEARNING_ROLLBACK_TOOL = Tool(
    name="learning_rollback",
    description=(
        "Ritira una lezione promossa che si è rivelata sbagliata o dannosa "
        "(rollback). Il file viene spostato in memory/lessons/.retired/ con il "
        "motivo, così la storia resta ispezionabile."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Nome file della lezione in memory/lessons/ (es: 2026-06-10_topic_ab12cd34.md).",
            },
            "reason": {
                "type": "string",
                "description": "Perché la lezione viene ritirata.",
            },
        },
        "required": ["filename"],
    },
    requires_approval=True,
    handler=learning_rollback_handler,
)
