#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Debug search_keto.py."""

import os
import sys

# Aggiungi la directory principale al path
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

print(f"[DEBUG] script_dir: {script_dir}")
print(f"[DEBUG] project_root: {project_root}")

# Verifica file
print(f"[DEBUG] search_web.py esiste? {os.path.exists(os.path.join(project_root, 'scripts', 'search_web.py'))}")

try:
    from scripts.search_web import search_ddg
    print("[OK] Import riuscito")
except Exception as e:
    print(f"[ERRORE] Import fallito: {e}")
    import traceback
    traceback.print_exc()
    exit(1)

results = search_ddg("ricette keto carboidrati", 3)
print(f"[OK] Risultati: {len(results)}")
