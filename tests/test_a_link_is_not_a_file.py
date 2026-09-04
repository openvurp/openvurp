"""Un nome non e' un file, e un indirizzo web non e' un percorso.

Guasto reale, dopo una ricerca sul web. L'agente ha risposto citando le sue
fonti come link. In fondo alla risposta sono comparse due schede,
`enghouse-releases-second-quarter-results-802425061.html` e
`dati-completi.html`, e una si e' aperta da sola: vuota. Sembrava che
l'agente avesse creato dei file e che poi si svuotassero.

Non era mai stato creato niente. La coda di un link
(`https://www.newswire.ca/news-releases/enghouse-...-802425061.html`) somiglia
a un percorso: la pagina ne faceva una scheda «apri questo file», e aprirla
mostrava una cornice vuota, perche' quel file sul disco non c'e' mai stato.

Due difetti distinti, due rimedi:
1. gli indirizzi web non vengono piu' scambiati per percorsi;
2. quello che non c'e' non si apre e non si offre — e se ci provi, la scheda
   dice perche', invece di restare bianca.
"""

import json
import os
import shutil
import subprocess
import tempfile

import pytest

import dashboard as D
from tests.test_dashboard_page import _page, _script

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node non disponibile")

HARNESS = """
import fs from "fs";
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const js = fs.readFileSync(process.argv[2], "utf-8");
const inizio = js.indexOf("function senzaUrl(");
const fine = js.indexOf("/* Quali blocchi");
if (inizio < 0 || fine < inizio) { console.error("path section not found"); process.exit(2); }
const api = new Function("esc",
  js.slice(inizio, fine) + "; return {trovaPercorsi, percorsiApribili, senzaUrl};")(esc);
const testo = JSON.parse(fs.readFileSync(process.argv[3], "utf-8"));
console.log(JSON.stringify({
  trovati: api.trovaPercorsi(testo),
  inline: api.percorsiApribili(testo),
}));
"""


@pytest.fixture(scope="module")
def percorsi(tmp_path_factory):
    """trovaPercorsi/percorsiApribili eseguiti, dal codice servito ai browser."""
    workspace = tmp_path_factory.mktemp("paths")
    script = workspace / "dash.js"
    script.write_text(_script(_page()), encoding="utf-8")
    harness = workspace / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")

    def run(text: str) -> dict:
        incoming = workspace / "in.json"
        incoming.write_text(json.dumps(text), encoding="utf-8")
        done = subprocess.run(
            ["node", str(harness), str(script), str(incoming)],
            capture_output=True, text=True, timeout=60,
        )
        assert done.returncode == 0, done.stderr[:2000]
        return json.loads(done.stdout)

    return run


RISPOSTA = (
    "Enghouse Systems — ENGH: ricavi ricorrenti circa 69%.\n"
    "Fonte: https://www.newswire.ca/news-releases/"
    "enghouse-releases-second-quarter-results-802425061.html\n"
    "Vedi anche [il report](https://example.com/dati/dati-completi.html) "
    "e www.borsaitaliana.it/quotazioni/scheda.html"
)


def test_a_link_does_not_become_a_file_to_open(percorsi):
    assert percorsi(RISPOSTA)["trovati"] == []


def test_the_link_stays_readable_in_the_text(percorsi):
    """Coprire l'indirizzo serve a cercarci dentro, non a cancellarlo."""
    inline = percorsi(RISPOSTA)["inline"]
    assert "enghouse-releases-second-quarter-results-802425061.html" in inline
    assert 'class="fileref"' not in inline


def test_a_real_path_is_still_offered(percorsi):
    out = percorsi("Ho scritto il riepilogo in memory/uploads/riepilogo.html, "
                   "e il grafico in /mnt/c/Users/User/Desktop/vurp/grafico.png")
    assert out["trovati"] == ["memory/uploads/riepilogo.html",
                              "/mnt/c/Users/User/Desktop/vurp/grafico.png"]
    assert out["inline"].count('class="fileref"') == 2


def test_a_path_next_to_a_link_survives(percorsi):
    """Coprire l'indirizzo non deve mangiarsi quello che gli sta accanto."""
    out = percorsi("da https://x.com/a/b.html l'ho salvato in note/estratto.md")
    assert out["trovati"] == ["note/estratto.md"]


# ── il server: «c'e'?» e' una domanda, non un errore ─────────────────────

def _handler(workspace: str):
    server = D.DashboardServer(port=0, workspace_dir=workspace, token="t")
    risposte = []

    class _Fake(server.handler_class):
        def __init__(self, query):
            self.path = "/api/file?" + query
            self.wfile = type("W", (), {"write": lambda _s, b: None})()

        def _json_response(self, payload, status=200):
            risposte.append((status, payload))

    return _Fake, risposte


def test_asking_for_a_file_that_is_not_there_answers_no_not_404():
    with tempfile.TemporaryDirectory() as workspace:
        Fake, risposte = _handler(workspace)
        Fake("as=exists&path=dati-completi.html")._serve_preview()
        assert risposte == [(200, {"exists": False})]


def test_asking_for_a_file_that_is_there_answers_yes():
    with tempfile.TemporaryDirectory() as workspace:
        with open(os.path.join(workspace, "vero.html"), "w", encoding="utf-8") as f:
            f.write("<p>ciao</p>")
        Fake, risposte = _handler(workspace)
        Fake("as=exists&path=vero.html")._serve_preview()
        status, payload = risposte[0]
        assert status == 200 and payload["exists"] is True
        assert payload["name"] == "vero.html" and payload["size"] == 11


def test_a_link_cannot_be_used_to_read_outside_the_workspace():
    with tempfile.TemporaryDirectory() as workspace:
        Fake, risposte = _handler(workspace)
        Fake("as=exists&path=//www.newswire.ca/news/x.html")._serve_preview()
        assert risposte == [(200, {"exists": False})]


def test_the_page_checks_before_opening_and_says_what_is_missing():
    js = _script(_page())
    assert "async function esisteFile(" in js
    assert "as=exists&path=" in js
    # Non si apre da sola una cosa che non c'e'.
    assert "apriSeEsiste(belli[belli.length-1])" in js
    # E se ci provi tu, la scheda spiega invece di restare bianca.
    assert "if(!await esisteFile(percorso)){" in js
    assert "non sul tuo disco" in js
