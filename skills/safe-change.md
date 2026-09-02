---
id: official/safe-change
type: skill
title: "Safe change: checkpoint before risky edits"
description: "Before any hard-to-reverse change (overwrite, delete, mass edit, config change), make a quick local checkpoint so you can roll back. A reversibility habit."
tags: [safety, backup, rollback, refactor, files, git, sicurezza, ripristino, checkpoint, modifica, annullare]
openvurp_version: ">=4.0"
trust: official
capabilities:
  shell: true
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

Before an action that is hard to undo: overwriting or deleting a file, a mass
refactor, editing config, or anything the owner would hate to lose.

## How

1. Make the cheapest reversible checkpoint available first:
   - in a git repo: `git stash` to set the change aside, or commit a quick
     work-in-progress on a scratch branch so it is recoverable.
   - not in git: copy the target aside, e.g. `cp path/to/file path/to/file.bak`.
2. Say briefly what you backed up and where.
3. Make the change.
4. Verify it works. If it did not, restore from the checkpoint and report what
   happened instead of leaving things half-broken.

## Notes

- Never remove a backup in the same step you create it.
- For a directory, prefer a timestamped copy over an in-place edit.
- This is a habit, not a ceremony: skip it only for trivially reversible edits.
