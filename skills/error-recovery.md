---
id: official/error-recovery
type: skill
title: "Recover from a failing command, don't loop"
description: "When a command or tool fails, debug it instead of retrying blindly: read the real error, form one hypothesis, try the smallest fix, and escalate if stuck."
tags: [error, debug, recovery, failure, retry, troubleshooting, errore, ripresa, fallimento, blocco, comando, fallisce, fallito, riprova, loop]
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

A command, tool, or step just failed — and the instinct to immediately rerun the
same thing is wrong.

## How

1. **Read the actual error**, not just the exit code. The message usually names
   the cause (missing binary, wrong path, permission, bad flag, network).
2. **One hypothesis** about why it failed. State it.
3. **Smallest fix** that tests that hypothesis — change one thing, not five.
4. Re-run. If it works, move on. If it fails *the same way*, your hypothesis was
   wrong — form a new one; do not retry the identical command a third time.
5. **Escalate** when stuck: say the real blocker and the smallest decision you
   need, instead of looping or pretending it worked.

## Notes

- Two identical failures = stop guessing and inspect (read the file, check the
  tool exists, print the variable).
- Never hide a failure behind a confident-sounding success.
