#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test import search_keto e controllo funzione."""

import os
import sys

script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

from scripts import search_keto
import inspect

print("[DEBUG] search_keto.__file__:", search_keto.__file__)
print("[DEBUG] source file exists:", os.path.exists(search_keto.__file__))

# Leggi il file sorgente
with open(search_keto.__file__, encoding="utf-8") as f:
    source = f.read()

# Controlla se usa search_web
if "search_web" in source:
    print("[OK] search_web trovato nel codice")
else:
    print("[ERRORE] search_web NON trovato nel codice")
    print("[DEBUG] Prime 500 righe del file:")
    print(source[:500])
