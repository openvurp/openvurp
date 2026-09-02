"""Approvals asked where the action was asked for.

openvurp requests permission through the agent's UI. While the terminal was the
only interface that was fine. With the wallet in the browser it is not: an
agent opened from the web would start a sensitive action and the question would
appear in the terminal, where nobody was looking — and the request would hang
until somebody noticed by chance.

Here the question becomes an event on the bus (the page shows it inside the
conversation) and the tool waits for the answer. If nobody answers within the
allotted time it counts as a no: better an action not taken than an action
authorised by silence.
"""

from __future__ import annotations

import threading
import uuid


class _Pending:
    __slots__ = ("event", "choice")

    def __init__(self):
        self.event = threading.Event()
        self.choice = "no"


_LOCK = threading.RLock()
_PENDING: dict[str, _Pending] = {}


def timeout_seconds() -> int:
    try:
        import config as cfg
        return max(15, min(int(getattr(cfg, "WEB_APPROVAL_TIMEOUT", 180)), 3600))
    except Exception:
        return 180


def request(prompt: str, chat_id: str, tool: str = "", actor: str = "") -> str:
    """Asks for permission in the chat and waits. Returns yes / no / always."""
    from core import activity

    token = uuid.uuid4().hex[:12]
    pending = _Pending()
    with _LOCK:
        _PENDING[token] = pending

    try:
        activity.publish(
            "approval", source="dashboard", chat_id=chat_id,
            session_key=f"dashboard:chat:{chat_id}",
            approval_id=token, text=str(prompt or "")[:1200],
            tool=tool, actor=actor, timeout=timeout_seconds(),
        )
    except Exception:
        # If we cannot even ask, it does not run.
        with _LOCK:
            _PENDING.pop(token, None)
        return "no"

    granted = pending.event.wait(timeout=timeout_seconds())
    with _LOCK:
        _PENDING.pop(token, None)
    if not granted:
        try:
            activity.publish(
                "approval_done", source="dashboard", chat_id=chat_id,
                session_key=f"dashboard:chat:{chat_id}",
                approval_id=token, choice="timeout",
            )
        except Exception:
            pass
        return "no"
    return pending.choice


def answer(token: str, choice: str) -> bool:
    """The user's answer from the page. True if somebody was waiting."""
    choice = str(choice or "").strip().lower()
    if choice not in {"yes", "no", "always"}:
        choice = "no"
    with _LOCK:
        pending = _PENDING.get(token)
    if pending is None:
        return False
    pending.choice = choice
    pending.event.set()
    try:
        from core import activity
        activity.publish("approval_done", source="dashboard",
                         approval_id=token, choice=choice)
    except Exception:
        pass
    return True


def pending_count() -> int:
    with _LOCK:
        return len(_PENDING)


class WebApprovalUI:
    """The UI used by turns that originate from the web.

    Everything is delegated to the real UI (so the terminal keeps seeing what
    happens) except the permission question, which goes to whoever actually
    asked for the action.
    """

    def __init__(self, real_ui, chat_id: str, actor: str = ""):
        self.__dict__["_real"] = real_ui
        self.__dict__["_chat_id"] = chat_id
        self.__dict__["_actor"] = actor

    def confirm_choice(self, prompt: str) -> str:
        return request(prompt, self.__dict__["_chat_id"], actor=self.__dict__["_actor"])

    def confirm(self, prompt: str) -> bool:
        return self.confirm_choice(prompt) in {"yes", "always"}

    def __getattr__(self, name):
        return getattr(self.__dict__["_real"], name)

    def __setattr__(self, name, value):
        setattr(self.__dict__["_real"], name, value)
