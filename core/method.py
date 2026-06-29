"""
openvurp Core — Operating Method

Metodo operativo compatto, derivato dall'ambiente e dai tool disponibili.
"""

from __future__ import annotations

from core.environment import EnvironmentSnapshot


def build_operating_method(snapshot: EnvironmentSnapshot, tool_names: list[str]) -> str:
    """Guida breve su come scegliere i tool e verificare il lavoro."""
    tools = set(tool_names)
    lines = ["## METODO OPERATIVO"]

    if {"find_files", "grep", "read_file"}.issubset(tools):
        lines.append(
            "- Per capire il codice: `find_files` per orientarti, `grep` per localizzare, "
            "`read_file` per leggere davvero. Non partire da shell se devi solo esplorare il workspace."
        )

    if {"edit_file", "write_file"}.issubset(tools):
        lines.append(
            "- Per modifiche nel workspace: preferisci `edit_file` per patch mirate; "
            "usa `write_file` per file nuovi o rewrite completi."
        )

    if "shell" in tools:
        shell_name = snapshot.preferred.get("shell") or snapshot.shell_name or "shell"
        shell_family = snapshot.shell_family or "unknown"
        lines.append(
            f"- Usa `shell` quando devi eseguire tool reali del sistema, test, git o package manager. "
            f"La shell di riferimento qui è `{shell_name}` ({shell_family}). Non mischiare sintassi di shell diverse nello stesso comando."
        )
        if shell_family == "cmd":
            lines.append(
                "- Se sei su Windows con shell `cmd`, usa sintassi `cmd` per i built-in; invoca `powershell -NoProfile -Command ...` solo quando ti serve davvero PowerShell."
            )
        elif shell_family == "powershell":
            lines.append(
                "- Se la shell attiva è PowerShell, resta su cmdlet e quoting PowerShell; non improvvisare sintassi bash o redirect `cmd` nello stesso tentativo."
            )
        elif shell_family == "posix":
            lines.append(
                "- Se la shell attiva è POSIX, usa sintassi bash/sh coerente. Non introdurre `where`, `%PATH%` o redirect `cmd` a meno che tu non stia lanciando esplicitamente un binario Windows."
            )

    if {"shell", "capability_lease"}.issubset(tools):
        lines.append(
            "- Per azioni sensibili ripetute, prima usa `dry_run=true` per mostrare cosa succederebbe. "
            "Se l'utente approva una classe stretta di azioni, crea una `capability_lease` breve con prefisso comando o path; non usare lease generiche."
        )

    verification = _verification_command(snapshot)
    if verification:
        lines.append(f"- Dopo modifiche al codice, verifica con `{verification}` se è pertinente.")

    if {"process_list", "process_kill"}.issubset(tools):
        lines.append(
            "- Se il problema riguarda processi o servizi, usa prima `process_list` e `process_kill`."
        )

    if {"process_start", "process_read", "process_stop"}.issubset(tools):
        lines.append(
            "- Per server, watch mode e comandi lunghi: usa `process_start`, poi controlla con `process_read` e chiudi con `process_stop`. Non bloccare il turno con `shell`."
        )

    if {"scaffold_plugin", "reload_plugins"}.issubset(tools):
        lines.append(
            "- Se manca una capability utile e ripetibile, crea uno skeleton con `scaffold_plugin`, implementalo nel plugin e caricalo con `reload_plugins`."
        )

    if "request_restart" in tools:
        lines.append(
            "- Se modifichi file Python del runtime fuori da `plugins/`, usa `request_restart` per applicare il cambio senza lasciare il sistema in stato incoerente."
        )

    if {"subagent_spawn", "subagent_wait"}.issubset(tools):
        lines.append(
            "- Per task paralleli e bounded research/execution: delega con `subagent_spawn`. Di default usa `mode=\"auto\"`, così il router decide tra worker testuale, executor limitato o executor ereditato. Puoi comunque forzare `backend`, `model`, `thinking` e `mode` (`auto`, `text`, `safe_executor`, `inherit_executor`). I subagent annunciano il risultato automaticamente al requester; usa `subagent_wait` solo quando ti serve bloccare esplicitamente."
        )

    if "doctor" in tools:
        lines.append(
            "- Se il sistema sembra incoerente dopo reset, restart o modifiche strutturali, esegui `doctor` per vedere subito wiring, plugin, audit e integrity."
        )

    if "doctor_fix" in tools:
        lines.append(
            "- Se `doctor` trova problemi di setup iniziale, usa `doctor_fix` per bootstrap di ACL, audit, scaffold runtime e baseline integrity."
        )

    if "desktop_screenshot" in tools:
        lines.append(
            "- Se ti serve vedere lo schermo del computer locale, usa `desktop_screenshot` e poi `image_analyze` sul file creato."
        )

    if "browser" in tools:
        lines.append(
            "- Per pagine web, tab, click, form e lettura contenuto usa `browser` come tool primario. "
            "Usa `mode=\"shared\"` per il Chrome reale dell'utente e `mode=\"isolated\"` per automazione deterministica; "
            "`mode=\"auto\"` sceglie il backend giusto. In `mode=\"isolated\"` puoi scegliere engine `chromium`, `firefox` o `webkit`, "
            "e per Chromium puoi usare anche `channel=\"chromium|chrome|chrome-beta|chrome-dev|chrome-canary|msedge|msedge-beta|msedge-dev|msedge-canary\"`."
        )
        lines.append(
            "- Se `browser action=\"status\"` segnala browser aperto senza remote debugging, chiedi una sola approvazione e usa "
            "`browser action=\"relaunch\"`. Non partire con discovery shell del path del browser."
        )

    if "browser_devtools" in tools:
        lines.append(
            "- Usa `browser_devtools` solo per debugging più profondo di Chrome: console, network, snapshot e tool MCP specifici. "
            "Per navigazione e interazione normale preferisci `browser`."
        )

    if "browser_setup" in tools:
        lines.append(
            "- Se `browser action=\"status\"` o `doctor` dicono che Playwright manca o che il driver non e pronto, usa `browser_setup` con approvazione per installare package, runtime browser e opzionalmente browser branded."
        )

    if {"notify_file", "notify_photo"}.intersection(tools):
        lines.append(
            "- Se devi consegnare un file o un'immagine all'utente su Telegram, usa `notify_file` o `notify_photo` invece di dire che non puoi inviare allegati."
        )

    if "memory_consolidate" in tools:
        lines.append(
            "- Per trasformare note giornaliere grezze in memoria lunga, usa `memory_consolidate` e aggiorna `MEMORY.md`."
        )

    if {"learning_feedback", "learning_review", "learning_promote"}.intersection(tools):
        lines.append(
            "- Quando l'utente corregge una preferenza o un errore ricorrente, registra il segnale con `learning_feedback`; usa `learning_review` per candidati e `learning_promote` solo dopo verifica/approvazione."
        )

    if {"task_journal", "reflection_note", "open_loop"}.intersection(tools):
        lines.append(
            "- Per lavori lunghi o promesse future, usa `task_journal` per decisioni/progresso, `reflection_note` per lezioni operative e `open_loop` per aggiungere/chiudere follow-up durevoli."
        )

    if "agent_state" in tools:
        lines.append(
            "- Per task non banali, mantieni l'autonomy loop: goal, plan, act, observe, revise, finish. "
            "Usa `agent_state` per annotare blocker o osservazioni quando il cambio di stato è importante."
        )

    if {"web_search", "web_fetch"}.issubset(tools):
        lines.append(
            "- Usa `web_search` e `web_fetch` solo quando serve davvero contesto esterno o verifica online."
        )

    if "notify" in tools:
        lines.append(
            "- Se c'è un motivo reale per contattare l'utente fuori turno, puoi usare `notify`. Niente ping vuoti o messaggi solo performativi."
        )

    if "image_analyze" in tools:
        lines.append(
            "- Se l'utente manda immagini o screenshot, usa `image_analyze` invece di dire che non puoi vedere."
        )

    if "audio_transcribe" in tools:
        lines.append(
            "- Se arrivano vocali o file audio, usa `audio_transcribe`; non passare da shell per Whisper se esiste già il tool."
        )

    if "pdf_read" in tools:
        lines.append(
            "- Se arriva un PDF, usa `pdf_read` prima di rispondere sul contenuto."
        )

    return "\n".join(lines)


def _verification_command(snapshot: EnvironmentSnapshot) -> str:
    preferred = snapshot.preferred
    project_types = set(snapshot.project_types)

    if "python" in project_types and preferred.get("python_tests"):
        return preferred["python_tests"]

    if "node" in project_types and preferred.get("js_package_manager"):
        return f"{preferred['js_package_manager']} test"

    return ""
