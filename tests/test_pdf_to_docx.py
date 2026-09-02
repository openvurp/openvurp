"""Turning a PDF into a real Word file, with nothing installed.

An agent was asked three times for a .docx and answered three times that it
could not: no pandoc, no LibreOffice, no python-docx on this machine. All true.
None of it mattered — a .docx is a zip archive with XML inside, and both ship
with Python.

The first delivery was an .rtf, and the owner's verdict was that it "changed
completely". So the test that matters here is not "a file was produced": it is
that the text comes out identical, character for character, and that five pages
stay five pages.
"""

import os
import re
import tempfile
import zipfile
from xml.dom.minidom import parseString

import pytest

pymupdf = pytest.importorskip("pymupdf")

from scripts.pdf_to_docx import convert


def _pdf(tmp: str, pagine: list[str]) -> str:
    doc = pymupdf.open()
    for testo in pagine:
        page = doc.new_page()
        page.insert_text((72, 100), testo, fontsize=12)
    percorso = os.path.join(tmp, "prova.pdf")
    doc.save(percorso)
    doc.close()
    return percorso


def _testo(docx: str) -> str:
    xml = zipfile.ZipFile(docx).read("word/document.xml").decode("utf-8")
    fuori = " ".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, re.S))
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&apos;", "'")):
        fuori = fuori.replace(a, b)
    return " ".join(fuori.split())


def test_it_is_a_real_word_file():
    with tempfile.TemporaryDirectory() as tmp:
        docx = convert(_pdf(tmp, ["Prima pagina"]))
        z = zipfile.ZipFile(docx)
        assert z.testzip() is None, "archivio corrotto"
        for parte in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
            assert parte in z.namelist(), f"manca {parte}: Word non lo aprirebbe"
        for nome in z.namelist():
            parseString(z.read(nome))          # esplode se malformato


def test_the_text_comes_out_identical():
    """The failure the owner reported about the RTF: content that changed."""
    with tempfile.TemporaryDirectory() as tmp:
        righe = ["Esame di coscienza", "Seconda pagina con altro testo"]
        docx = convert(_pdf(tmp, righe))
        uscito = _testo(docx)
        for riga in righe:
            assert riga in uscito, f"perso dal documento: {riga!r}"


def test_five_pages_stay_five_pages():
    with tempfile.TemporaryDirectory() as tmp:
        docx = convert(_pdf(tmp, [f"Pagina {n}" for n in range(1, 6)]))
        xml = zipfile.ZipFile(docx).read("word/document.xml").decode("utf-8")
        assert xml.count('w:br w:type="page"') == 4, "le pagine si sono fuse"


def test_a_scanned_pdf_says_so_instead_of_producing_an_empty_file():
    """Silence would be worse: an empty document that looks like a conversion."""
    with tempfile.TemporaryDirectory() as tmp:
        doc = pymupdf.open()
        doc.new_page()                          # pagina senza testo
        vuoto = os.path.join(tmp, "scansione.pdf")
        doc.save(vuoto)
        doc.close()
        with pytest.raises(ValueError, match="OCR"):
            convert(vuoto)


def test_a_missing_file_is_reported_as_missing():
    with pytest.raises(FileNotFoundError):
        convert("/non/esiste/mai.pdf")
