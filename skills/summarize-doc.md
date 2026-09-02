---
id: official/summarize-doc
type: skill
title: "Summarize a long document"
description: "Turn a long file, PDF or pile of notes into a structured summary: TL;DR, key points, decisions/actions, and open questions. Read-only, no network."
tags: [summary, document, pdf, notes, reading, distill, riassunto, riassumi, sintesi, documento, note, lungo]
openvurp_version: ">=4.0"
trust: official
capabilities:
  shell: false
  file_read: true
  file_write: false
  network: []
provenance:
  author: openvurp
  contributed: 2026-06-30
  reviewed_by: openvurp-maintainers
  review_date: 2026-06-30
verified: true
---

## When to use

The owner points you at something long — a file, a PDF, a transcript, a pile of
notes — and wants the essence, not a re-read.

## How

1. Read the source with the right tool (`read_file`, or `pdf_read` for PDFs).
2. If it is large, read it in parts and keep a running list of points — do not
   guess at content you have not actually read.
3. Produce a compact, structured summary:
   - **TL;DR** — one or two sentences.
   - **Key points** — only the few that actually matter.
   - **Decisions / actions** — anything that implies a next step.
   - **Open questions** — what is unclear, missing, or contradictory.
4. Keep the owner's language. Point to the section or line when a claim needs
   grounding.

## Notes

- Summarize what is there; do not invent or pad.
- If the document is too big to read fully, say what you covered and what you
  skipped — never imply you read more than you did.
