#!/usr/bin/env python3
"""Genera le immagini del README da un workspace finto.

Mai dal database vero: le schermate di un progetto pubblico non devono
contenere le conversazioni di chi lo sviluppa.

Serve Chromium con le sue librerie di sistema:

    pip install playwright
    python -m playwright install chromium
    sudo python -m playwright install-deps chromium   # <- serve root

Poi:  python scripts/make_screenshots.py
"""

from __future__ import annotations

import os
import tempfile
import threading
import time

from core.chat_store import ChatStore
import dashboard as D

AGENTI = [
    ("amanda", "cerca offerte su Amazon"),
    ("ciccio", "bollette"),
    ("dev", "programmatore"),
    ("meteo", "previsioni"),
]


def vetrina() -> tuple[str, dict]:
    """Un workspace con dentro una storia che si capisce a colpo d\u2019occhio."""
    lavoro = tempfile.mkdtemp(prefix="openvurp-vetrina-")
    store = ChatStore(os.path.join(lavoro, "memory"))
    ids = {n: store.create_agent(n, r, "", "codex", "gpt-5.6-luna")["id"]
           for n, r in AGENTI}

    chat = store.direct_chat_for_agent(ids["dev"])
    store.add_message(chat["id"], "user", "Il mio SSD sta morendo, cosa prendo?",
                      author_type="user", author_id="owner", author_name="Tu")
    store.add_message(
        chat["id"], "assistant",
        "Ho guardato lo SMART del disco: **187 settori riallocati** e in crescita. "
        "Non \u00e8 un allarme futuro, \u00e8 gi\u00e0 iniziato \u2014 farei il backup oggi.\n\n"
        "Per la sostituzione ho sentito amanda, che di acquisti ne sa pi\u00f9 di me.",
        author_type="agent", author_id=ids["dev"], author_name="dev",
        metadata={"steps": [
            {"tool": "shell", "args": "smartctl -A /dev/nvme0n1",
             "out": "Reallocated_Sector_Ct  187"},
            {"tool": "ask_peer", "args": "amanda \u00b7 un NVMe 1TB affidabile sotto i 90 euro",
             "out": ""},
        ]})
    meta = {"peer": {"from": ids["dev"], "to": ids["amanda"],
                     "from_name": "dev", "to_name": "amanda"}}
    store.add_message(chat["id"], "assistant",
                      "Un NVMe 1TB affidabile sotto i 90 euro?",
                      author_type="agent", author_id=ids["dev"], author_name="dev",
                      recipient_id=ids["amanda"], metadata=dict(meta, direction="ask"))
    store.add_message(chat["id"], "assistant",
                      "S\u00ec: **Crucial P3 Plus 1TB a 74 \u20ac**, spedito da Amazon. "
                      "Il Silicon Power costa 8 \u20ac meno ma ha un anno di garanzia in meno.",
                      author_type="agent", author_id=ids["amanda"], author_name="amanda",
                      recipient_id=ids["dev"], metadata=dict(meta, direction="answer"))
    store.add_message(chat["id"], "assistant",
                      "Quindi: **Crucial P3 Plus 1TB, 74 \u20ac**. Backup prima di montarlo.",
                      author_type="agent", author_id=ids["dev"], author_name="dev")

    stanza = store.team_room(create=True)
    store.set_chat_agents(stanza["id"], list(ids.values()))
    return lavoro, {"chat": chat["id"], "dev": ids["dev"]}


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Manca playwright: pip install playwright")
        return 1

    lavoro, riferimenti = vetrina()
    srv = D.DashboardServer(port=0, workspace_dir=lavoro, token="vetrina")
    srv.bind()
    porta = srv._server.server_address[1]
    threading.Thread(target=srv.start, daemon=True).start()
    time.sleep(1.0)

    os.makedirs("docs", exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pagina = browser.new_page(viewport={"width": 1440, "height": 900},
                                  device_scale_factor=2)
        pagina.goto(f"http://127.0.0.1:{porta}/?token=vetrina",
                    wait_until="networkidle")
        pagina.wait_for_timeout(1200)
        pagina.screenshot(path="docs/wallet.png")
        print("  docs/wallet.png")

        pagina.evaluate(f"openChat({riferimenti['dev']!r})")
        pagina.wait_for_timeout(1500)
        pagina.screenshot(path="docs/chat.png")
        print("  docs/chat.png")

        pagina.evaluate("openSettings()")
        pagina.wait_for_timeout(1200)
        pagina.screenshot(path="docs/settings.png")
        print("  docs/settings.png")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
