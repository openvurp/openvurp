#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test import search_web da search_keto.py."""

import os
import sys

script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

print(f"[DEBUG] script_dir: {script_dir}")
print(f"[DEBUG] project_root: {project_root}")
print(f"[DEBUG] sys.path[0]: {sys.path[0]}")

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
