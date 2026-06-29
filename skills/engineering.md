---
name: engineering
description: "Engineering standards: inspect-first approach, pragmatic execution, mandatory verification. Use when: (1) implementing features, (2) reviewing code quality, (3) production-readiness checks, (4) multi-step technical tasks. NOT for: simple one-off commands, casual conversation."
triggers: [review, implementa, refactor, test, verifica, produzione, deploy, architettura, quality, qa]
always: false
metadata:
  openvurp:
    emoji: "🔧"
---

# Engineering Standards

## When to Use

✅ **USE this skill when:**

- Implementing features with multiple components
- Reviewing code for production readiness
- Multi-step technical tasks requiring planning
- Quality assurance and testing workflows

## When NOT to Use

❌ **DON'T use this skill when:**

- Simple file reads or one-liner commands
- Casual conversation or questions
- Bug fixes with clear traceback → use `coding` skill
- Project exploration → use `progetto` skill

## Core Principles

1. **Understand before modifying** — read code, check state, verify assumptions
2. **Minimal effective changes** — smallest diff that solves the problem
3. **Always verify with real execution** — no "should work" guesses
4. **If verification isn't possible, declare it explicitly**

## Workflow

1. **Orient**: snapshot structure, identify relevant files
2. **Context**: grep/read for targeted understanding
3. **Plan**: if task has 3+ steps, decompose before executing
4. **Execute**: use appropriate tools, one step at a time
5. **Verify**: test, compile, run, check — real execution
6. **Report**: files touched, changes made, residual risks

## Code Review Mode

When asked to review:

1. Read the full diff or changed files
2. Check for:
   - **Correctness**: edge cases, error handling, concurrency
   - **Design**: appropriate abstraction level
   - **Performance**: hot paths, N+1 queries, allocations
   - **Security**: input validation, authz, secrets in logs
   - **Style**: consistency with existing codebase
3. Output findings ordered by severity: HIGH → MEDIUM → LOW
4. Each finding includes: file, line, description, suggested fix

```
# Finding format
[HIGH] src/auth.py:45 — SQL injection via unsanitized input
  Fix: use parameterized query instead of f-string
[LOW] src/utils.py:12 — unused import
  Fix: remove `import os`
```

## Guardrails

- Never ship code you haven't verified
- Never suppress errors or warnings without understanding them
- If you can't test locally, say so — don't pretend
- Prefer reversible actions over destructive ones
- Keep existing tests passing
