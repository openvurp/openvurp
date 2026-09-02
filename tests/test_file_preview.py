"""Guardare un file senza scaricarlo — e senza aprire una porta sul disco.

Un endpoint che serve un file a partire da un percorso e' un buco di lettura
se non e' chiuso bene: `..`, percorsi assoluti, collegamenti simbolici che
escono dalla cartella. E ci sono cose che stanno DENTRO il workspace e non
vanno servite comunque: il .env, i database, le chiavi.
"""

import os
import tempfile

import pytest

import dashboard as D


@pytest.fixture
def posto(tmp_path, monkeypatch):
    lavoro = tmp_path / "lavoro"
    (lavoro / "memory").mkdir(parents=True)
    (lavoro / "hello.py").write_text("def ciao():\n    return 'mondo'\n")
    (lavoro / ".env").write_text("TELEGRAM_TOKEN=segretissimo\n")
    (lavoro / "memory" / "chats.db").write_text("dati")
    (lavoro / "note.pdf").write_bytes(b"%PDF-1.4 finto")

    fuori = tmp_path / "fuori"
    fuori.mkdir()
    (fuori / "rubato.txt").write_text("roba privata")
    os.symlink(fuori / "rubato.txt", lavoro / "scorciatoia.txt")

    H = type("H", (D.DashboardHandler,), {"workspace_dir": str(lavoro)})
    return H, str(lavoro), str(fuori)


# ── quello che deve passare ──────────────────────────────────────────────

def test_a_normal_file_is_served(posto):
    H, lavoro, _ = posto
    assert H.anteprima_percorso("hello.py") == os.path.realpath(f"{lavoro}/hello.py")
    assert H.anteprima_percorso(f"{lavoro}/hello.py")


def test_the_language_is_guessed_from_the_name(posto):
    H, _, _ = posto
    assert H.anteprima_linguaggio("a.py") == "python"
    assert H.anteprima_linguaggio("a.rs") == "rust"
    assert H.anteprima_linguaggio("a.sconosciuto") == ""


# ── quello che NON deve passare ──────────────────────────────────────────

def test_the_env_file_is_never_served(posto):
    """Dentro il workspace, ma contiene i token: non e' un file come gli altri."""
    H, _, _ = posto
    assert H.anteprima_percorso(".env") == ""


def test_databases_and_keys_are_never_served(posto):
    H, lavoro, _ = posto
    assert H.anteprima_percorso("memory/chats.db") == ""
    (open(f"{lavoro}/privata.pem", "w")).write("chiave")
    assert H.anteprima_percorso("privata.pem") == ""


def test_climbing_out_with_dots_is_refused(posto):
    H, _, _ = posto
    for tentativo in ("../fuori/rubato.txt", "../../etc/passwd",
                      "memory/../../fuori/rubato.txt"):
        assert H.anteprima_percorso(tentativo) == "", tentativo


def test_an_absolute_path_outside_is_refused(posto):
    H, _, fuori = posto
    assert H.anteprima_percorso(f"{fuori}/rubato.txt") == ""


def test_a_symlink_that_escapes_is_refused(posto):
    """Il percorso viene RISOLTO: un collegamento non e' una scorciatoia legale."""
    H, _, _ = posto
    assert H.anteprima_percorso("scorciatoia.txt") == ""


def test_a_sibling_directory_with_the_same_prefix_is_refused(tmp_path):
    """«/casa/vurp-altro» comincia per «/casa/vurp» ma non ci sta dentro.

    E' il motivo per cui il controllo non puo' essere un confronto di prefisso.
    """
    (tmp_path / "vurp").mkdir()
    vicino = tmp_path / "vurp-altro"
    vicino.mkdir()
    (vicino / "segreto.txt").write_text("x")
    H = type("H", (D.DashboardHandler,), {"workspace_dir": str(tmp_path / "vurp")})
    assert H.anteprima_percorso(str(vicino / "segreto.txt")) == ""


def test_a_directory_is_not_a_file(posto):
    H, _, _ = posto
    assert H.anteprima_percorso("memory") == ""


def test_nothing_is_served_for_an_empty_path(posto):
    H, _, _ = posto
    assert H.anteprima_percorso("") == ""
    assert H.anteprima_percorso("   ") == ""


# ── il giro HTTP vero ────────────────────────────────────────────────────

def test_over_http_the_refusals_hold_and_auth_is_required(tmp_path):
    import json
    import threading
    import time
    import urllib.error
    import urllib.request

    lavoro = tmp_path / "w"
    (lavoro / "memory").mkdir(parents=True)
    (lavoro / "hello.py").write_text("print('ciao')\n")
    (lavoro / ".env").write_text("SEGRETO=1\n")

    srv = D.DashboardServer(port=0, workspace_dir=str(lavoro), token="t")
    srv.bind()
    porta = srv._server.server_address[1]
    threading.Thread(target=srv.start, daemon=True).start()
    time.sleep(0.6)

    def chiedi(query, cookie=True):
        req = urllib.request.Request(
            f"http://127.0.0.1:{porta}/api/file?{query}",
            headers={"Cookie": "ovtok=t"} if cookie else {})
        try:
            risposta = urllib.request.urlopen(req, timeout=10)
            return risposta.status, risposta.read()
        except urllib.error.HTTPError as exc:
            return exc.code, b""

    stato, corpo = chiedi("path=hello.py&as=text")
    assert stato == 200 and json.loads(corpo)["text"] == "print('ciao')\n"

    assert chiedi("path=.env&as=text")[0] == 404
    assert chiedi("path=../../etc/passwd&as=text")[0] == 404
    assert chiedi("path=hello.py", cookie=False)[0] == 401, "servito senza token"


def test_the_browser_is_told_not_to_guess_the_type(tmp_path):
    """Un .txt interpretato come HTML eseguirebbe quello che c'e' dentro."""
    import threading
    import time
    import urllib.request

    lavoro = tmp_path / "w"
    (lavoro / "memory").mkdir(parents=True)
    (lavoro / "nota.txt").write_text("<script>alert(1)</script>")
    srv = D.DashboardServer(port=0, workspace_dir=str(lavoro), token="t")
    srv.bind()
    porta = srv._server.server_address[1]
    threading.Thread(target=srv.start, daemon=True).start()
    time.sleep(0.6)

    req = urllib.request.Request(f"http://127.0.0.1:{porta}/api/file?path=nota.txt",
                                 headers={"Cookie": "ovtok=t"})
    risposta = urllib.request.urlopen(req, timeout=10)
    assert risposta.headers.get("X-Content-Type-Options") == "nosniff"
    assert risposta.headers.get("Content-Disposition") == "inline"
    assert "sandbox" in (risposta.headers.get("Content-Security-Policy") or "")
    assert risposta.headers.get("Content-Type") == "application/octet-stream"



def test_pdf_pages_are_rendered_by_us_not_by_the_browser(tmp_path):
    """Il visore del browser dentro la scheda portava la sua barra grigia.

    Le pagine si rendono con PyMuPDF e arrivano come PNG: fogli puliti sul
    fondo scuro, e la barra di Chrome resta fuori.
    """
    fitz = pytest.importorskip("fitz")
    lavoro = tmp_path / "w"
    (lavoro / "memory").mkdir(parents=True)
    doc = fitz.open()
    for k in range(3):
        doc.new_page().insert_text((72, 100), f"pagina {k + 1}")
    doc.save(str(lavoro / "doc.pdf"))
    doc.close()

    H = type("H", (D.DashboardHandler,), {"workspace_dir": str(lavoro)})
    percorso = H.anteprima_percorso("doc.pdf")
    assert percorso, "il PDF non passa nemmeno il controllo percorsi"

    import json
    import threading
    import time
    import urllib.error
    import urllib.request

    srv = D.DashboardServer(port=0, workspace_dir=str(lavoro), token="t")
    srv.bind()
    porta = srv._server.server_address[1]
    threading.Thread(target=srv.start, daemon=True).start()
    time.sleep(0.6)

    def chiedi(q):
        req = urllib.request.Request(f"http://127.0.0.1:{porta}/api/file?{q}",
                                     headers={"Cookie": "ovtok=t"})
        return urllib.request.urlopen(req, timeout=20)

    meta = json.loads(chiedi("as=pdfmeta&path=doc.pdf").read())
    assert meta["pages"] == 3
    risposta = chiedi("as=pdfpage&page=2&path=doc.pdf")
    corpo = risposta.read()
    assert risposta.headers["Content-Type"] == "image/png"
    assert corpo[:4] == b"\x89PNG"
    with pytest.raises(urllib.error.HTTPError):
        chiedi("as=pdfpage&page=99&path=doc.pdf")


def test_the_pdf_branch_in_the_page_has_no_browser_viewer():
    from tests.test_dashboard_page import _page, _script
    js = _script(_page())
    assert "view=FitH" not in js, "e' tornato il visore del browser"
    assert "as=pdfmeta" in js and "as=pdfpage" in js
    assert 'class="sheet"' in js and 'loading="lazy"' in js


def test_the_css_braces_are_balanced():
    """Una graffa in piu' lasciata da un innesto ha rotto tutte le regole a
    valle: il box dell'anteprima e' semplicemente sparito. Il parser CSS non
    urla — salta e va avanti — quindi il pareggio va contato."""
    import re as _re
    from tests.test_dashboard_page import _page
    css = _re.search(r"<style>(.*?)</style>", _page(), _re.S).group(1)
    assert css.count("{") == css.count("}"), (
        f"CSS sbilanciato: {css.count('{')} aperte, {css.count('}')} chiuse")
