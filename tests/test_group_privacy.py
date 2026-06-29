"""In gruppo (contesto pubblico, visibile a estranei) l'agente NON deve esporre
i dati privati dell'owner: niente USER.md, niente MEMORY.md, niente sezione
"owner" dell'anima. In DM (main) invece resta tutto.

Questi test bloccano la regressione del bug per cui i messaggi di gruppo
finivano in sessione "main" (perché resolve_session_type guardava solo il
sender, che è il nome di una persona) e ricevevano il profilo privato.
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.bootstrap import BootstrapLoader, resolve_session_type
from core.anima import Anima


def test_resolve_session_type_group_from_chat_type():
    # Il sender è il nome di una persona, NON contiene "group": è chat_type a decidere.
    assert resolve_session_type("telegram", "Mario", "group") == "group"
    assert resolve_session_type("telegram", "Mario", "supergroup") == "group"
    # DM e CLI restano "main".
    assert resolve_session_type("telegram", "Mario", "private") == "main"
    assert resolve_session_type("telegram", "Mario", "") == "main"
    assert resolve_session_type("cli", "user", "") == "main"


def _workspace_with_files(tmp):
    files = {
        "SOUL.md": "voce interna dell'agente",
        "IDENTITY.md": "identita pubblica dell'agente",
        "AGENTS.md": "istruzioni operative",
        "TOOLS.md": "elenco tool",
        "USER.md": "Profilo privato: l'owner si chiama Mario, vive a Taranto.",
        "MEMORY.md": "Memoria privata: fatti durevoli sull'owner.",
    }
    for name, content in files.items():
        with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
            f.write(content)
    return BootstrapLoader(tmp)


def test_group_session_excludes_private_files():
    with tempfile.TemporaryDirectory() as tmp:
        loader = _workspace_with_files(tmp)

        group_names = {f.name for f in loader.load_all(session_type="group")}
        assert "USER.md" not in group_names, "USER.md (profilo owner) non va in gruppo"
        assert "MEMORY.md" not in group_names, "MEMORY.md (memoria privata) non va in gruppo"
        # L'identità pubblica e il metodo operativo invece servono.
        assert {"IDENTITY.md", "SOUL.md", "AGENTS.md", "TOOLS.md"} <= group_names

        main_names = {f.name for f in loader.load_all(session_type="main")}
        assert "USER.md" in main_names, "in DM il profilo owner deve esserci"
        assert "MEMORY.md" in main_names


def test_anima_hides_owner_section_in_group():
    with tempfile.TemporaryDirectory() as tmp:
        anima = Anima(tmp)
        anima.add_trait("identity", "Sono un agente locale, diretto e utile.")
        anima.add_trait("owner", "Il mio owner si chiama Mario e vive a Taranto.")

        main_prompt = anima.compile_prompt("main")
        group_prompt = anima.compile_prompt("group")

        # In DM l'anima conosce l'owner; in gruppo no.
        assert "Mario" in main_prompt
        assert "Mario" not in group_prompt
        # L'identità (non privata) resta in entrambi.
        assert "agente locale" in group_prompt
