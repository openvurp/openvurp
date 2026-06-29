---
name: coding
description: "Code assistance: write, debug, refactor, review code. Use when: (1) writing or modifying code, (2) fixing bugs from errors/tracebacks, (3) refactoring, (4) code review. NOT for: project exploration (use progetto), deployment, system administration."
triggers: [codice, programma, bug, fix, refactor, classe, funzione, errore, debug, code, python, javascript, html, css, test, compile, build, script]
always: false
metadata:
  openvurp:
    emoji: "💻"
    requires:
      anyBins: [python3, node, go, rustc]
---

# Coding

## When to Use

✅ **USE this skill when:**

- Writing new code or creating source files
- Fixing bugs from tracebacks or error messages
- Refactoring existing code for clarity or performance
- Reviewing code quality, finding issues
- Running and testing code

## When NOT to Use

❌ **DON'T use this skill when:**

- Exploring project structure → use `progetto` skill
- Git operations (commit, push, branch) → direct shell
- System/hardware tasks → use `habitat` skill
- GitHub PRs/issues → use `github` skill

## Workflow: Bug Fix

1. **Reproduce**: execute and capture the exact error output
2. **Locate**: identify `file:line` from traceback
3. **Read context**: read the file around the error, understand the flow
4. **Search references**: grep for the function/variable involved — who calls it, how
5. **Root cause**: find the real cause, not the symptom. Walk the call chain if needed
6. **Fix**: surgical edit — change only what's necessary
7. **Verify**: run again, confirm the fix
8. **Regression**: run test suite if one exists

```bash
# Typical bug fix flow
python3 script.py                          # 1. reproduce
grep -rn "function_name" src/             # 4. find references
python3 -m pytest tests/ -x              # 8. regression
```

## Workflow: New File

1. Create directory if needed: `mkdir -p src/components`
2. Write functional code — no empty skeletons
3. Check syntax: `python3 -m py_compile file.py`
4. Execute: `python3 file.py` or `node file.js`

## Workflow: Refactor

1. Read ALL involved files before any modification
2. Grep all usages of the target function/class/variable
3. Plan modification order (dependencies first)
4. Edit one file at a time, verify after each
5. Full test at the end

## Workflow: Debug (subtle bugs)

1. Add temporary logging at critical points
2. Execute and observe the flow
3. Isolate: comment sections to narrow the break point
4. Verify assumptions: `python3 -c "import X; print(type(X.method))"`
5. Fix + remove temporary logging
6. Verify clean execution

## Workflow: Performance

```bash
# Measure first — numbers, not feelings
time python3 script.py

# Profile
python3 -m cProfile -s cumtime script.py 2>&1 | head -30

# Optimize only the bottleneck, then re-measure
```

## Guardrails

- Don't create files unless necessary — prefer editing existing code
- Don't add unrequested complexity or features
- Confirm before destructive modifications to user's code
- Comment only where code isn't self-evident
- Maintain existing code style (indentation, naming, patterns)
- Always test after writing or modifying code
- If a test fails, fix it or explain why — never ignore
