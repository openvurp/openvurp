"""Quello che l'agente scrive, la pagina deve disegnarlo.

Regressione reale: a una domanda di confronto ("le cinque azioni migliori")
l'agente ha risposto con una tabella markdown — che e' il formato che un
modello sceglie da solo ogni volta che mette a confronto delle cose — e la
pagina l'ha stampata com'era: cinque righe di barre verticali e trattini,
illeggibili. Stessa sorte per gli elenchi numerati, che diventavano puntati
perdendo l'ordine, unico contenuto di una classifica.

Questi test ESEGUONO il renderer della pagina con node, invece di cercare
sottostringhe: una tabella o si disegna o non si disegna, e la differenza si
vede solo eseguendola.
"""

import json
import re
import shutil
import subprocess

import pytest

from tests.test_dashboard_page import _page, _script

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node non disponibile")

# Il renderer non tocca il DOM: si puo' eseguire da solo, con la sola `esc`.
HARNESS = """
import fs from "fs";
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const js = fs.readFileSync(process.argv[2], "utf-8");
const inizio = js.indexOf("const CODICI=[]");
const fine = js.indexOf("/* ── streaming");
if (inizio < 0 || fine < inizio) { console.error("markdown section not found"); process.exit(2); }
const md = new Function("esc", js.slice(inizio, fine) + "; return md;")(esc);
console.log(md(JSON.parse(fs.readFileSync(process.argv[3], "utf-8"))));
"""


@pytest.fixture(scope="module")
def rendi(tmp_path_factory):
    """md(testo) eseguito davvero, dal codice servito ai browser."""
    workspace = tmp_path_factory.mktemp("md")
    script = workspace / "dash.js"
    script.write_text(_script(_page()), encoding="utf-8")
    harness = workspace / "harness.mjs"
    harness.write_text(HARNESS, encoding="utf-8")

    def run(source: str) -> str:
        incoming = workspace / "in.json"
        incoming.write_text(json.dumps(source), encoding="utf-8")
        done = subprocess.run(
            ["node", str(harness), str(script), str(incoming)],
            capture_output=True, text=True, timeout=60,
        )
        assert done.returncode == 0, done.stderr[:2000]
        return done.stdout.strip()

    return run


TABELLA = """| Classifica | Titolo | Perche' interessante |
|---|---|---|
| 1 | Brady - BRC | FY26 ricavi +9,8%. [Risultati](https://example.com/brc) |
| 2 | Kinsale - KNSL | combined ratio **75,5%** |
"""


def test_a_markdown_table_becomes_a_table(rendi):
    out = rendi(TABELLA)
    assert "<table>" in out and "</table>" in out
    assert "<th>Classifica</th>" in out
    assert out.count("<tr>") == 3, "intestazione piu' due righe"
    assert "<td>Brady - BRC</td>" in out
    # Nessuna barra verticale sopravvive come testo.
    assert "|---|" not in out


def test_the_cells_keep_their_own_formatting(rendi):
    out = rendi(TABELLA)
    assert '<a href="https://example.com/brc" target="_blank"' in out
    assert "<b>75,5%</b>" in out


def test_a_wide_table_scrolls_instead_of_stretching_the_conversation(rendi):
    """Il thread e' largo 46rem: una tabella a cinque colonne deve scorrere
    dentro il suo riquadro, non allargare la pagina."""
    assert '<div class="tw">' in rendi(TABELLA)
    css = _page()
    assert ".tw{overflow-x:auto" in css


def test_alignment_is_honoured(rendi):
    out = rendi("| a | b | c |\n|:--|:-:|--:|\n| 1 | 2 | 3 |\n")
    assert '<th style="text-align:center">b</th>' in out
    assert '<th style="text-align:right">c</th>' in out
    assert "<th>a</th>" in out


def test_a_ragged_row_does_not_lose_a_cell(rendi):
    """Meglio una colonna senza titolo che una cella buttata."""
    out = rendi("| a | b |\n|---|---|\n| 1 | 2 | 3 |\n")
    assert "<td>3</td>" in out
    assert out.count("<th>") + out.count("<th ") == 3, "manca la colonna in piu'"


def test_a_pipe_inside_a_cell_stays_a_pipe(rendi):
    out = rendi("| comando | cosa fa |\n|---|---|\n"
                "| `ls \\| wc -l` | conta i file |\n")
    assert "ls | wc -l" in out
    assert out.count("<td") == 2


def test_something_that_is_not_a_table_is_left_alone(rendi):
    """Senza la riga di trattini non e' una tabella: e' testo con delle barre."""
    out = rendi("prima | seconda | terza")
    assert "<table>" not in out
    assert "prima | seconda | terza" in out


def test_a_numbered_list_keeps_its_numbers(rendi):
    out = rendi("1. primo\n2. secondo\n3. terzo")
    assert out.startswith("<ol>") and "<li>primo</li>" in out
    assert "<ul>" not in out


def test_a_bulleted_list_is_still_bulleted(rendi):
    out = rendi("- uno\n- due")
    assert out.startswith("<ul>") and "<li>uno</li>" in out


def test_quotes_and_rules(rendi):
    out = rendi("> citato\n> ancora\n\n---\n\ndopo")
    assert "<blockquote>citato<br>ancora</blockquote>" in out
    assert "<hr>" in out
    assert "<p>dopo</p>" in out


def test_a_block_is_never_wrapped_in_a_paragraph(rendi):
    """Un <div> dentro un <p> il browser lo butta fuori, e resta un paragrafo
    vuoto in mezzo al testo: succedeva a ogni blocco di codice."""
    out = rendi("Ecco il comando:\n```bash\nls -la\n```\nFine.")
    assert "<p></p>" not in out
    assert not re.search(r"<p>[^<]*<div", out)
    assert '<div class="cb">' in out
    assert "<p>Ecco il comando:</p>" in out and "<p>Fine.</p>" in out


def test_code_is_still_escaped_and_openable(rendi):
    out = rendi('```python\nprint("<ciao>")\n```')
    assert "&lt;ciao&gt;" in out and "<script>" not in out
    assert 'data-copy="' in out and 'data-code="' in out


def test_a_table_cannot_smuggle_html(rendi):
    out = rendi("| a |\n|---|\n| <img src=x onerror=alert(1)> |\n")
    assert "<img" not in out
    assert "&lt;img" in out


def test_a_nested_list_stays_one_list(rendi):
    """Le voci rientrate non venivano riconosciute: l'elenco si spezzava in
    due, con un paragrafo di trattini in mezzo."""
    out = rendi("- primo\n  - dentro\n  - ancora\n- secondo")
    assert out == ("<ul><li>primo<ul><li>dentro</li><li>ancora</li></ul></li>"
                   "<li>secondo</li></ul>"), out
    assert "<p>" not in out


def test_a_numbered_list_can_hold_bullets(rendi):
    out = rendi("1. primo\n   - nota\n2. secondo")
    assert out.startswith("<ol>")
    assert "<ul><li>nota</li></ul>" in out


def test_struck_out_text(rendi):
    assert "<del>vecchio</del>" in rendi("~~vecchio~~ nuovo")
