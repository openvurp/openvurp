"""Chi sta lavorando, adesso, su questo filo di esecuzione.

Le lezioni erano di tutti: un unico archivio, scritto da qualunque agente
chiamasse learning_feedback. Una correzione data a chi cerca offerte finiva nel
bagaglio di chi scrive codice, e nessuna delle due era piu' verificabile,
perche' non si sapeva a chi appartenesse. Con la memoria e' lo stesso.

La chiave e' l'id e non il nome: l'owner puo' rinominare un agente quando
vuole, e quello che ha imparato deve restare suo.

E' una variabile di contesto, non un attributo: gli agenti girano anche in
parallelo (``broadcast`` usa un pool di thread), e un attributo condiviso lo
sovrascriverebbe il collega che parte un istante dopo.
"""

from __future__ import annotations

import contextvars
import os

_SCOPE: contextvars.ContextVar = contextvars.ContextVar("agent_scope", default="")


def set_scope(scope: str):
    """Da qui in avanti si lavora per conto di questo agente."""
    return _SCOPE.set(str(scope or ""))


def reset_scope(token) -> None:
    _SCOPE.reset(token)


def current_scope() -> str:
    return _SCOPE.get()


def scoped_dir(memory_dir: str, name: str, scope: str = "") -> str:
    """La cartella di un archivio, dell'agente o della piattaforma.

    Senza scope resta dov'era (``memory/<name>``): la piattaforma non trasloca,
    e quello che c'e' gia' sul disco continua a leggersi.
    """
    scope = str(scope or "").strip()
    if not scope:
        return os.path.join(memory_dir, name)
    return os.path.join(memory_dir, "agents", scope, name)


def agent_home(memory_dir: str, scope: str = "") -> str:
    """La radice di tutto quello che appartiene a un agente."""
    scope = str(scope or "").strip()
    if not scope:
        return memory_dir
    return os.path.join(memory_dir, "agents", scope)
