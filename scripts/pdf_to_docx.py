#!/usr/bin/env python3
"""Turn a text PDF into a real Word file, with nothing installed.

Written because an agent was asked three times for a .docx and three times
answered that it could not: no pandoc, no LibreOffice, no python-docx on this
machine. All true, and all beside the point — a .docx is a zip archive with
XML inside, and both are in the standard library.

What it keeps: the text, exactly; the bold; the paragraph breaks; and the page
split, as explicit page breaks, so a five-page PDF stays five pages.

What it does not keep: the exact typographic layout. A PDF places glyphs at
coordinates, a Word document reflows text — nobody can preserve both. This
keeps the content and the structure, which is what "convert to Word" means
when the point is to edit it.

    python3 scripts/pdf_to_docx.py input.pdf [output.docx]
"""

from __future__ import annotations

import os
import sys
import zipfile
from xml.sax.saxutils import escape

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _paragraphs(pdf_path: str) -> list[list[dict]]:
    """One list of paragraphs per page. Each paragraph is a list of runs."""
    import pymupdf

    doc = pymupdf.open(pdf_path)
    pages = []
    for page in doc:
        paragraphs = []
        for block in page.get_text("dict")["blocks"]:
            if "lines" not in block:
                continue                      # immagini: non hanno testo
            runs: list[dict] = []
            for line in block["lines"]:
                for span in line["spans"]:
                    text = span["text"]
                    if not text:
                        continue
                    grassetto = bool(span["flags"] & 16)
                    # Uno span che continua il precedente con lo stesso stile
                    # non merita un run nuovo: Word li unisce comunque, e il
                    # file resta piu' piccolo e piu' pulito da modificare.
                    if runs and runs[-1]["bold"] == grassetto:
                        runs[-1]["text"] += text
                    else:
                        runs.append({"text": text, "bold": grassetto,
                                     "size": span["size"]})
                if runs and not runs[-1]["text"].endswith(" "):
                    runs[-1]["text"] += " "
            if runs:
                runs[-1]["text"] = runs[-1]["text"].rstrip()
                paragraphs.append(runs)
        pages.append(paragraphs)
    doc.close()
    return pages


def _run_xml(run: dict) -> str:
    props = "<w:b/>" if run["bold"] else ""
    mezzi_punti = int(round(run.get("size", 11) * 2))
    props += f'<w:sz w:val="{mezzi_punti}"/><w:szCs w:val="{mezzi_punti}"/>'
    testo = escape(run["text"])
    return (f'<w:r><w:rPr>{props}</w:rPr>'
            f'<w:t xml:space="preserve">{testo}</w:t></w:r>')


def _document_xml(pages: list[list[dict]]) -> str:
    corpo = []
    for numero, paragrafi in enumerate(pages):
        if numero:
            # L'interruzione di pagina esplicita: e' cosi' che cinque pagine
            # restano cinque, invece di diventare un fiume unico.
            corpo.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
        for runs in paragrafi:
            corpo.append("<w:p><w:pPr><w:spacing w:after=\"120\"/></w:pPr>"
                         + "".join(_run_xml(r) for r in runs) + "</w:p>")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}"><w:body>' + "".join(corpo) +
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/>'
        '</w:sectPr></w:body></w:document>'
    )


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '</Types>'
)

_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Target="word/document.xml" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"/>'
    '</Relationships>'
)

_DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
)


def convert(pdf_path: str, docx_path: str = "") -> str:
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(pdf_path)
    docx_path = docx_path or os.path.splitext(pdf_path)[0] + ".docx"
    pages = _paragraphs(pdf_path)
    if not any(pages):
        raise ValueError(
            "Nessun testo estraibile: il PDF e' fatto di immagini scansionate. "
            "Serve un OCR, non una conversione.")
    with zipfile.ZipFile(docx_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/document.xml", _document_xml(pages))
    return docx_path


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 2
    uscita = convert(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "")
    print(uscita)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
