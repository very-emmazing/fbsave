#!/usr/bin/env python3
"""Diagnose-Probe: laedt den Post auf www.facebook.com und dumpt die
Kommentar-DOM-Struktur, damit die richtigen Selektoren ermittelt werden koennen.
Schreibt probe_out/: HTML, Screenshot, structure.json. Nicht Teil der Beweissicherung."""

import json
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = ("https://www.facebook.com/neue.szene.augsburg/posts/"
       "pfbid02RgKgpshoRqspLtfHu2zwkmwbSXh5tTqFpxNXmvkDv7e4anhHS1zwHKSbsxKjC89Sl")

OUT = Path("probe_out")
OUT.mkdir(exist_ok=True)

JS = """
() => {
  const arts = [...document.querySelectorAll('div[role="article"]')];
  const articles = arts.slice(0, 8).map(a => ({
    aria: a.getAttribute('aria-label'),
    text_head: a.innerText.slice(0, 200),
    links: [...a.querySelectorAll('a[href]')].slice(0, 6).map(l => ({
      t: l.innerText.trim().slice(0, 60),
      href: l.getAttribute('href').slice(0, 120),
      aria: l.getAttribute('aria-label'),
    })),
  }));
  const buttons = [...document.querySelectorAll('[role="button"]')]
    .map(b => b.innerText.trim().replace(/\\s+/g, ' ').slice(0, 70))
    .filter(t => t);
  return {
    n_articles: arts.length,
    article_arias: arts.map(a => (a.getAttribute('aria-label') || '').slice(0, 80)),
    articles,
    buttons: [...new Set(buttons)].slice(0, 60),
    n_dialog: document.querySelectorAll('div[role="dialog"]').length,
    title: document.title,
  };
}
"""

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(locale="de-DE", storage_state="state.json",
                              viewport={"width": 1280, "height": 1600})
    page = ctx.new_page()
    page.goto(URL, wait_until="load", timeout=60000)
    page.wait_for_timeout(5000)
    print("URL:", page.url)
    print("Titel:", page.title())

    (OUT / "page.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(OUT / "page.png"), full_page=False)

    data = page.evaluate(JS)
    (OUT / "structure.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Artikel-Elemente:", data["n_articles"])
    print("Dialoge:", data["n_dialog"])
    for a in data["article_arias"][:10]:
        print("  aria:", a)

    # Zeit-Link des ersten Kommentars hovern -> Tooltip mit absoluter Zeit?
    try:
        art = page.query_selector('div[role="article"][aria-label]')
        if art:
            for link in art.query_selector_all("a[href]"):
                t = (link.inner_text() or "").strip()
                if re.fullmatch(r"\d+\s*(Sek\.|Min\.|Std\.|T|Tg\.|W|Wo\.|J)\.?|\d+\s*[smhdwy]", t):
                    print("Zeit-Link gefunden:", repr(t))
                    link.hover()
                    page.wait_for_timeout(1500)
                    tip = page.query_selector('[role="tooltip"]')
                    print("Tooltip:", repr(tip.inner_text()) if tip else None)
                    break
    except Exception as e:
        print("Hover-Test fehlgeschlagen:", e)

    browser.close()
print("Fertig -> probe_out/")
