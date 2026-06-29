---
name: progetto
description: "Project analysis and exploration: file structure, dependencies, architecture, documentation. Use when: (1) exploring an unknown codebase, (2) understanding project architecture, (3) analyzing dependencies, (4) onboarding to a new project. NOT for: writing code (use coding), deploying (use shell)."
triggers: [progetto, struttura, architettura, dipendenze, organizza, esplora, analizza, codebase, onboarding]
always: false
metadata:
  openvurp:
    emoji: "📂"
---

# Project Analysis

## When to Use

✅ **USE this skill when:**

- Exploring an unknown codebase for the first time
- Understanding project architecture and file layout
- Analyzing dependencies and build systems
- Onboarding — "how does this project work?"
- Finding entry points, configs, test suites

## When NOT to Use

❌ **DON'T use this skill when:**

- Writing or modifying code → use `coding` skill
- Bug fixing with known location → use `coding` skill
- Git operations → direct shell
- System/hardware exploration → use `habitat` skill

## Quick Exploration

```bash
# 1. Structure — map all files (excluding noise)
find . -type f -not -path '*/\.*' -not -path '*/node_modules/*' \
  -not -path '*/__pycache__/*' -not -path '*/venv/*' | head -50

# 2. Languages present
find . -type f \( -name '*.py' -o -name '*.js' -o -name '*.ts' \
  -o -name '*.go' -o -name '*.rs' \) | wc -l

# 3. Dependencies
cat requirements.txt 2>/dev/null || cat Pipfile 2>/dev/null || \
  cat package.json 2>/dev/null || cat go.mod 2>/dev/null || \
  cat Cargo.toml 2>/dev/null || echo "No dependency file found"

# 4. README
cat README.md 2>/dev/null | head -60

# 5. Git history
git log --oneline -10 2>/dev/null
git remote -v 2>/dev/null
```

## Deep Analysis Checklist

1. **Entry point** — `main.py`, `index.js`, `app.py`, `manage.py`, `cmd/main.go`
2. **Configuration** — where are configs? env vars, .env, config files
3. **Dependencies** — what libraries, what versions, any outdated?
4. **Tests** — do they exist? how to run them? what coverage?
5. **Build** — how to build/run? Makefile, scripts, CI config?
6. **Architecture** — monolith vs modular? layers? data flow?

## Internal Dependency Mapping

```bash
# Python — find internal imports
grep -rn "^from \|^import " src/ --include="*.py" | \
  grep -v "site-packages" | sort

# Node — find requires/imports
grep -rn "require(\|from '" src/ --include="*.js" --include="*.ts" | sort

# Entry point analysis
head -30 main.py  # or equivalent
```

## Output Format

Present results as structured summary:

```
## Project: <name>
- Language: Python 3.x
- Framework: Flask
- Entry point: app.py
- Config: config.py, .env
- Dependencies: 12 packages (requirements.txt)
- Tests: pytest, 45 test files
- Build: `pip install -e .` → `python app.py`
- Architecture: MVC, 3 main modules
```

## Guardrails

- Read before recommending — never suggest changes to code you haven't read
- Present facts first, opinions only if asked
- Suggest improvements only when requested
- Don't modify project files during exploration
