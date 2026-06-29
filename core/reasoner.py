"""
openvurp Core — Reasoner

Thinking levels, chain-of-thought, complexity classification.
"""

from __future__ import annotations

import re
from enum import Enum


class ThinkingLevel(Enum):
    QUICK = "quick"
    NORMAL = "normal"
    DEEP = "deep"


# Keyword per classificazione complessità
QUICK_PATTERNS = [
    r'^(ciao|hey|ehi|salve|buongiorno|buonasera)',
    r'^(grazie|ok|si|no|va bene|perfetto|esatto)',
    r'^(cosa|chi|dove|quando|quanto)\s+(sei|è|sono)\b',
    r'^/\w+$',  # Slash commands
]

DEEP_PATTERNS = [
    r'(crea|sviluppa|implementa|costruisci|fai|scrivi)\s+.*(progetto|applicazione|app|sistema|servizio|api)',
    r'(refactor|rifattorizza|ristruttura)',
    r'(migra|converti|porta)\s+.*\s+(a|da|verso)',
    r'(analizza|debugga|investiga)\s+.*\s+(problema|errore|bug|crash)',
    r'(configura|setup|installa)\s+.*(server|database|docker|kubernetes|ci|cd)',
    r'(multi[- ]?step|passo\s+per\s+passo|step\s+by\s+step)',
    r'\b(completo|completa|tutto|intera)\b.*\b(pipeline|workflow|stack)\b',
]


class Reasoner:
    def classify(self, user_input: str) -> ThinkingLevel:
        """Classifica la complessità dell'input."""
        text = user_input.strip().lower()

        # Check quick patterns
        for pattern in QUICK_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return ThinkingLevel.QUICK

        # Check deep patterns
        for pattern in DEEP_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return ThinkingLevel.DEEP

        # Euristica: lunghezza e complessità
        words = len(text.split())
        if words < 5:
            return ThinkingLevel.QUICK
        if words > 30:
            return ThinkingLevel.DEEP

        # Presenza di multiple richieste (e, poi, anche, inoltre)
        conjunctions = len(re.findall(r'\b(e poi|poi|inoltre|anche|infine|dopo)\b', text))
        if conjunctions >= 2:
            return ThinkingLevel.DEEP

        return ThinkingLevel.NORMAL

    def wrap_prompt(self, user_input: str, level: ThinkingLevel) -> str:
        """Inietta istruzioni di ragionamento nel prompt."""
        if level == ThinkingLevel.QUICK:
            return user_input  # Nessuna modifica

        if level == ThinkingLevel.NORMAL:
            return (
                f"{user_input}\n\n"
                "[Ragiona brevemente prima di agire. "
                "Spiega cosa farai e perché, poi procedi.]"
            )

        # DEEP
        return (
            f"{user_input}\n\n"
            "[Questo è un task complesso. Scomponilo in passi chiari.\n"
            "Per ogni passo:\n"
            "1. Descrivi cosa farai\n"
            "2. Esegui l'azione\n"
            "3. Verifica il risultato\n"
            "4. Decidi il passo successivo\n\n"
            "Se qualcosa fallisce, analizza il perché e adatta il piano.\n"
            "Alla fine, riassumi cosa è stato fatto.]"
        )
