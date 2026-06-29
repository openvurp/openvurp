#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test search_keto.py con print di debug."""

import os
import sys

# Aggiungi la directory principale al path
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

print(f"[DEBUG] script_dir: {script_dir}")
print(f"[DEBUG] project_root: {project_root}")

from scripts.search_web import search_ddg
print("[OK] Import search_web riuscito")

results = search_ddg("ricette keto carboidrati", 3)
print(f"[OK] Risultati: {len(results)}")

# Ora prova con la funzione principale
import search_keto
print("[OK] Import search_keto riuscito")
search_keto.find_keto_recipes()
