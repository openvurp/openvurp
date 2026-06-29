"""
openvurp Core — Anima

L'identità come organismo, non come file di testo.

Altri framework iniettano markdown statici scritti a mano (SOUL.md,
IDENTITY.md, USER.md). L'Anima di openvurp è diversa: tratti strutturati e
versionati, ognuno con origine (bootstrap/owner/learned), confidenza,
età e storia completa. L'agente la fa evolvere con mutazioni VERIFICATE
(niente segreti, niente duplicati, budget giornaliero anti-drift) e ogni
cambiamento è ispezionabile e reversibile.

Quando l'Anima ha tratti attivi, sostituisce SOUL.md/IDENTITY.md/USER.md
nel prompt. I file markdown restano semi leggibili per la nascita; l'Anima
è dove l'identità vive e cresce.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime

ANIMA_FILE = "anima.json"

SECTIONS = ("identity", "voice", "boundaries", "owner", "method")

SECTION_LABELS = {
    "identity": "Who I am",
    "voice": "How I speak",
    "boundaries": "Boundaries",
    "owner": "My owner",
    "method": "How I work",
}

# Budget anti-drift: l'identità evolve, non sfarfalla.
MAX_MUTATIONS_PER_DAY = 12


@dataclass
class Trait:
    id: str
    section: str
    text: str
    origin: str = "learned"          # bootstrap | owner | learned
    confidence: float = 0.7
    created: str = ""
    updated: str = ""
    version: int = 1
    retired: bool = False
    history: list = field(default_factory=list)

    def age_days(self) -> int:
        try:
            created = datetime.fromisoformat(self.created)
            return max(0, (datetime.now() - created).days)
        except Exception:
            return 0


class AnimaError(Exception):
    pass


class Anima:
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir
        self.path = os.path.join(workspace_dir, ANIMA_FILE)
        self._traits: list[Trait] = []
        self._created = ""
        self._mtime: float = -1.0
        self._load()

    # ── Persistenza ──

    def _load(self):
        try:
            stat = os.stat(self.path)
        except OSError:
            self._traits = []
            self._mtime = -1.0
            return
        if stat.st_mtime == self._mtime:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._created = data.get("created", "")
            self._traits = [Trait(**t) for t in data.get("traits", [])]
            self._mtime = stat.st_mtime
        except Exception:
            self._traits = []

    def _save(self):
        data = {
            "version": 1,
            "created": self._created or datetime.now().isoformat(timespec="seconds"),
            "traits": [asdict(t) for t in self._traits],
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        try:
            self._mtime = os.stat(self.path).st_mtime
        except OSError:
            pass
        self._created = data["created"]

    # ── Query ──

    def active(self) -> bool:
        """L'Anima è attiva quando ha almeno un tratto vivo."""
        self._load()
        return any(not t.retired for t in self._traits)

    def active_traits(self, section: str = "") -> list[Trait]:
        self._load()
        traits = [t for t in self._traits if not t.retired]
        if section:
            traits = [t for t in traits if t.section == section]
        return traits

    def find(self, trait_id: str) -> Trait | None:
        self._load()
        for t in self._traits:
            if t.id == trait_id:
                return t
        return None

    # ── Verifica mutazioni ──

    def _verify_text(self, text: str) -> list[str]:
        problems = []
        clean = " ".join((text or "").split())
        if len(clean) < 8:
            problems.append("testo troppo corto per essere un tratto")
        if len(clean) > 500:
            problems.append("testo troppo lungo: un tratto è una frase o due, non un saggio")
        try:
            from core.security.audit import redact
            if redact(clean) != clean:
                problems.append("contiene materiale che sembra un segreto")
        except Exception:
            pass
        return problems

    def _mutations_today(self) -> int:
        today = datetime.now().date().isoformat()
        count = 0
        for t in self._traits:
            if (t.updated or t.created or "").startswith(today):
                count += 1
            for h in t.history:
                if str(h.get("date", "")).startswith(today):
                    count += 1
        return count

    def _check_budget(self):
        if self._mutations_today() >= MAX_MUTATIONS_PER_DAY:
            raise AnimaError(
                f"Identity budget exhausted ({MAX_MUTATIONS_PER_DAY} mutations/day). "
                "Identity evolves, it doesn't flicker: try again tomorrow or talk to the owner."
            )

    # ── Mutazioni ──

    def add_trait(self, section: str, text: str, origin: str = "learned",
                  reason: str = "", confidence: float = 0.7) -> Trait:
        self._load()
        section = (section or "").strip().lower()
        if section not in SECTIONS:
            raise AnimaError(f"Sezione sconosciuta: {section}. Disponibili: {', '.join(SECTIONS)}")
        problems = self._verify_text(text)
        clean = " ".join(text.split())
        for t in self.active_traits(section):
            if t.text.strip().lower() == clean.lower():
                problems.append(f"tratto identico già presente ({t.id})")
        if problems:
            raise AnimaError("Mutazione rifiutata: " + "; ".join(problems))
        self._check_budget()

        now = datetime.now().isoformat(timespec="seconds")
        trait = Trait(
            id=hashlib.sha1(f"{section}:{clean}:{now}".encode()).hexdigest()[:8],
            section=section,
            text=clean,
            origin=origin if origin in ("bootstrap", "owner", "learned") else "learned",
            confidence=max(0.0, min(1.0, float(confidence))),
            created=now,
            updated=now,
        )
        if reason:
            trait.history.append({"date": now, "event": "born", "reason": reason[:200]})
        self._traits.append(trait)
        self._save()
        self._record_growth(f"nuovo tratto [{section}]: {clean[:80]}")
        return trait

    def revise_trait(self, trait_id: str, text: str, reason: str = "") -> Trait:
        trait = self.find(trait_id)
        if trait is None or trait.retired:
            raise AnimaError(f"Tratto non trovato o ritirato: {trait_id}")
        problems = self._verify_text(text)
        if problems:
            raise AnimaError("Mutazione rifiutata: " + "; ".join(problems))
        self._check_budget()

        now = datetime.now().isoformat(timespec="seconds")
        trait.history.append({
            "date": now, "event": "revised",
            "previous": trait.text, "reason": (reason or "")[:200],
        })
        trait.text = " ".join(text.split())
        trait.updated = now
        trait.version += 1
        self._save()
        self._record_growth(f"tratto rivisto [{trait.section}] v{trait.version}: {trait.text[:80]}")
        return trait

    def retire_trait(self, trait_id: str, reason: str = "") -> Trait:
        trait = self.find(trait_id)
        if trait is None or trait.retired:
            raise AnimaError(f"Tratto non trovato o già ritirato: {trait_id}")
        now = datetime.now().isoformat(timespec="seconds")
        trait.retired = True
        trait.updated = now
        trait.history.append({
            "date": now, "event": "retired", "reason": (reason or "")[:200],
        })
        self._save()
        self._record_growth(f"tratto ritirato [{trait.section}]: {trait.text[:80]}")
        return trait

    def restore_trait(self, trait_id: str, reason: str = "") -> Trait:
        trait = self.find(trait_id)
        if trait is None or not trait.retired:
            raise AnimaError(f"Tratto non trovato o non ritirato: {trait_id}")
        now = datetime.now().isoformat(timespec="seconds")
        trait.retired = False
        trait.updated = now
        trait.history.append({
            "date": now, "event": "restored", "reason": (reason or "")[:200],
        })
        self._save()
        self._record_growth(f"tratto ripristinato [{trait.section}]: {trait.text[:80]}")
        return trait

    def _record_growth(self, detail: str):
        try:
            from core.growth import record_growth_event
            record_growth_event(
                os.path.join(self.workspace_dir, "memory"),
                "identity_updated", detail,
            )
        except Exception:
            pass

    # ── Compilazione nel prompt ──

    def compile_prompt(self, session_type: str = "main") -> str:
        """Compila l'Anima nella sezione identità del system prompt.

        Sostituisce SOUL.md/IDENTITY.md/USER.md: stessa funzione, ma
        l'identità qui è cresciuta dall'esperienza, non incollata da
        un template.
        """
        traits = self.active_traits()
        if not traits:
            return ""

        lines = [
            "## ANIMA",
            "Questa è la tua identità: non un template, ma tratti che hai "
            "costruito vivendo con il tuo owner. Incarnali. Puoi farli "
            "evolvere con il tool `anima_update` quando impari qualcosa "
            "di vero su di te o sull'owner (informa l'owner quando tocchi "
            "chi sei).",
            "",
        ]
        for section in SECTIONS:
            # In gruppo (contesto pubblico, visibile a estranei) non esporre i
            # tratti privati sull'owner: identità/voce/confini/metodo sì, profilo
            # dell'owner no. In DM (main) e cicli interni resta tutto.
            if session_type == "group" and section == "owner":
                continue
            section_traits = [t for t in traits if t.section == section]
            if not section_traits:
                continue
            lines.append(f"### {SECTION_LABELS.get(section, section)}")
            for t in section_traits:
                lines.append(f"- {t.text}")
            lines.append("")
        return "\n".join(lines).rstrip()

    # ── Vista umana (/anima) ──

    def render_status(self) -> str:
        self._load()
        traits = [t for t in self._traits if not t.retired]
        retired = [t for t in self._traits if t.retired]
        if not traits and not retired:
            return (
                "Empty anima: identity still lives in the markdown files.\n"
                "It fills up via the anima_update tool — each trait is born "
                "from experience, with origin, age and history."
            )

        lines = [f"{len(traits)} active traits"
                 + (f" · {len(retired)} retired" if retired else "")]
        muts = self._mutations_today()
        lines.append(f"mutations today: {muts}/{MAX_MUTATIONS_PER_DAY}")
        lines.append("")
        for section in SECTIONS:
            section_traits = [t for t in traits if t.section == section]
            if not section_traits:
                continue
            lines.append(f"{SECTION_LABELS.get(section, section)}:")
            for t in section_traits:
                age = t.age_days()
                age_str = "today" if age == 0 else f"{age}d"
                ver = f" v{t.version}" if t.version > 1 else ""
                lines.append(
                    f"  [{t.id}] {t.text}  ({t.origin}, {age_str}{ver})"
                )
            lines.append("")
        return "\n".join(lines).rstrip()
