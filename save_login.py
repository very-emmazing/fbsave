#!/usr/bin/env python3
"""
save_login.py — Einmalig manuell bei Facebook einloggen und die Session
als state.json speichern, damit fbsave.py sie mit --state state.json
wiederverwenden kann (nur noetig, wenn der Post ohne Login nicht erreichbar ist).

Ablauf:
  1. python save_login.py
  2. Im geoeffneten Browserfenster normal einloggen (inkl. 2FA falls aktiv)
  3. Zurueck im Terminal Enter druecken -> state.json wird geschrieben

state.json enthaelt deine Session-Cookies — nicht weitergeben, nicht committen
(steht in .gitignore).
"""

import sys
from pathlib import Path

STATE_FILE = "state.json"


def main() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(locale="de-DE")
        page = context.new_page()
        page.goto("https://mbasic.facebook.com/login", wait_until="load")
        print("Bitte im Browserfenster einloggen.")
        input("Danach hier Enter druecken, um die Session zu speichern... ")
        context.storage_state(path=STATE_FILE)
        browser.close()

    print(f"Session gespeichert: {Path(STATE_FILE).resolve()}")
    print("Jetzt:  python fbsave.py --state state.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
