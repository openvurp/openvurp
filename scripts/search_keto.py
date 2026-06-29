#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ricerca ricette keto su DuckDuckGo con carboidrati netti ≤5g."""

import requests
from bs4 import BeautifulSoup
from urllib.parse import urlencode
import re
import json


def search_ddg(query: str, max_results: int = 5):
    """Cerca su DuckDuckGo con query diretta (senza JS)."""
    # Usa l'endpoint di ricerca interno di DDG
    url = "https://html.duckduckgo.com/html/"
    params = {"q": query}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"Errore nella richiesta: {e}")
        return []
    
    soup = BeautifulSoup(response.text, "html.parser")
    results = []
    
    # Estrai i link dai risultati (classe 'result__a')
    for link in soup.select("a.result__a")[:max_results]:
        title = link.get_text(strip=True)
        href = link.get("href", "")
        # DDG usa link di redirect — estrai l'URL reale
        if href.startswith("/l/?"):
            # Estrai l'URL dalla query string
            match = re.search(r"uddg=(.+?)&", href)
            if match:
                import urllib.parse
                href = urllib.parse.unquote(match.group(1))
        if href.startswith("http"):
            results.append({"title": title, "url": href})
    
    return results


def extract_net_carbs(text: str) -> float | None:
    """Estrai carboidrati netti da un testo (es. '5g netti', 'carboidrati: 4g')."""
    # Pattern per carboidrati netti
    patterns = [
        r"(\d+\.?\d*)\s*g\s*(?:netti?|netto|netti|carboidrati\s*netti)",
        r"carboidrati\s*netti?:\s*(\d+\.?\d*)\s*g",
        r"netti?:\s*(\d+\.?\d*)\s*g",
        r"carboidrati:\s*(\d+\.?\d*)\s*g",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def fetch_page(url: str) -> str:
    """Scarica il contenuto di una pagina."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        # Estrai il testo visibile (rimuovi script/style)
        for script in soup(["script", "style"]):
            script.decompose()
        text = soup.get_text(separator=" ", strip=True)
        return text[:3000]  # Limita a 3000 caratteri
    except Exception:
        return ""


def find_keto_recipes():
    """Trova ricette keto con carboidrati netti <=5g."""
    query = "ricette keto carboidrati"
    print(f"[SEARCH] Ricerca: {query}\n")
    
    results = search_ddg(query, max_results=5)
    if not results:
        print("[INFO] Nessun risultato trovato.")
        return
    
    print(f"[OK] Trovati {len(results)} risultati. Verifica carboidrati netti...\n")
    
    recipes = []
    for i, res in enumerate(results, 1):
        print(f"[{i}] {res['title']}")
        print(f"    {res['url']}")
        
        # Scarica la pagina
        page_text = fetch_page(res["url"])
        net_carbs = extract_net_carbs(page_text)
        
        if net_carbs is not None and net_carbs <= 5:
            print(f"    [OK] Carboidrati netti: {net_carbs}g\n")
            recipes.append({
                "title": res["title"],
                "url": res["url"],
                "net_carbs": net_carbs
            })
        else:
            print(f"    [WARN] Carboidrati netti: {net_carbs or 'non trovato'}g\n")
    
    # Salva i risultati in JSON
    with open("memory/keto_recipes.json", "w", encoding="utf-8") as f:
        json.dump(recipes, f, indent=2, ensure_ascii=False)
    
    print(f"\n[OK] Ricette con <=5g netti: {len(recipes)}")
    for r in recipes:
        print(f"- {r['title']} ({r['net_carbs']}g netti)")


if __name__ == "__main__":
    find_keto_recipes()
