#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ricerca web con DuckDuckGo (senza JS)."""

import requests
from bs4 import BeautifulSoup
import re
import urllib.parse


def search_ddg(query: str, max_results: int = 5):
    """Cerca su DuckDuckGo con query diretta (senza JS)."""
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
    
    for link in soup.select("a.result__a")[:max_results]:
        title = link.get_text(strip=True)
        href = link.get("href", "")
        
        # DDG usa link di redirect — estrai l'URL reale
        if href.startswith("/l/?"):
            match = re.search(r"uddg=(.+?)&", href)
            if match:
                href = urllib.parse.unquote(match.group(1))
        
        # Accetta URL con o senza schema
        if href.startswith("http") or href.startswith("//"):
            results.append({"title": title, "url": href})
    
    return results


if __name__ == "__main__":
    # Test
    results = search_ddg("ricette keto carboidrati", max_results=3)
    print(f"[OK] Risultati: {len(results)}")
    for r in results:
        print(f"- {r['title']}")
        print(f"  {r['url']}")
