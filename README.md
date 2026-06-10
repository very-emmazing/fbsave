# fbsave — Beweissicherung von Facebook-Kommentaren

Dokumentiert alle Kommentare (inkl. Antworten) unter einem öffentlichen
Facebook-Post für eine Strafanzeige: Einzel-Screenshot pro Kommentar,
Gesamt-Screenshot, CSV mit Autor/Profillink/Zeitstempel/Text sowie ein
`manifest.json` mit UTC-Zeitstempeln des Laufs und SHA256-Hashes aller Dateien.

Technik: Playwright steuert Chromium und lädt den Post über
`mbasic.facebook.com` (serverseitig gerendert — kein Modal wie auf
`www.facebook.com`, kein App-Interstitial wie auf `m.facebook.com`).

## Setup

Playwright ist via pipx in `~/.venv/playwright/` installiert. Falls die
Chromium-Binaries noch fehlen:

```bash
~/.venv/playwright/bin/python -m playwright install chromium
```

## Verwendung

```bash
cd fbsave

# Standard: sichtbarer Browser (zum Zuschauen/Debuggen), Ziel-URL ist
# als Default im Skript hinterlegt
~/.venv/playwright/bin/python fbsave.py

# Andere Post-URL
~/.venv/playwright/bin/python fbsave.py --url "https://www.facebook.com/..."

# Ohne sichtbares Fenster (erst wenn alles zuverlässig läuft)
~/.venv/playwright/bin/python fbsave.py --headless
```

### Falls Facebook einen Login verlangt

Manche Posts/Regionen zeigen ohne Login nur eine Login-Wand. Dann einmalig:

```bash
~/.venv/playwright/bin/python save_login.py   # manuell einloggen, Enter drücken
~/.venv/playwright/bin/python fbsave.py       # state.json wird automatisch benutzt
```

`state.json` enthält Session-Cookies — nicht weitergeben (steht in `.gitignore`).

## Ablauf des Skripts

1. URL wird auf `mbasic.facebook.com` umgeschrieben und geladen
2. Cookie-/Consent-Dialog wird weggeklickt; weil danach oft eine Weiterleitung
   stattfindet, wird die Post-URL anschließend **neu geladen**
3. Alle „Weitere Kommentare anzeigen"-Links werden wiederholt geklickt,
   bis nichts mehr nachzuladen ist (Limit: `--max-rounds`, Default 200)
4. Gesamt-Screenshot + Extraktion der Top-Level-Kommentare
5. Jeder Antwort-Thread („3 Antworten" …) wird einzeln geöffnet, aufgefaltet
   und extrahiert

Nach **jedem** Schritt entsteht ein nummerierter Debug-Screenshot
(`debug_NN_<schritt>.png`) und Seitentitel + URL werden ausgegeben — wenn etwas
hakt, zeigen diese Dateien genau, an welchem Schritt es war.

## Ausgabe

Pro Lauf entsteht `captures/capture_<UTC-Zeit>/` mit:

| Datei | Inhalt |
|---|---|
| `comments.csv` | `index, comment_id, is_reply, parent_id, author, profile_url, timestamp_raw, timestamp_utc, text, screenshot_file, source_url` (UTF-8 mit BOM, Excel-tauglich) |
| `comment_NNN.png` | Einzel-Screenshot pro Kommentar/Antwort |
| `full_page.png` | Gesamt-Screenshot der aufgefalteten Seite |
| `debug_NN_*.png` | Debug-Screenshots je Schritt |
| `manifest.json` | Ziel-URL, final geladene URL, Seitentitel, Start/Ende des Laufs in UTC (ISO 8601), Versionen, Kommentaranzahl, SHA256 jeder Datei |

## Wichtige Hinweise zur Beweiskraft

- **Zeitstempel:** mbasic zeigt nur *relative* Zeiten („5 Std.", „Gestern um
  14:32"). `timestamp_raw` ist der wörtliche Seiteninhalt (der eigentliche
  Beleg, auch auf den Screenshots sichtbar). `timestamp_utc` wird daraus
  relativ zur Capture-Zeit **berechnet** und ist eine Näherung in der
  Granularität der Anzeige (z. B. ±30 min bei „5 Std."). Nicht parsebare
  Formate lassen `timestamp_utc` leer — `timestamp_raw` bleibt erhalten.
- **Hashes:** `manifest.json` wird zuletzt geschrieben und enthält SHA256 aller
  übrigen Dateien. Verzeichnis nach dem Lauf nicht mehr verändern; ideal
  zusätzlich sofort archivieren (z. B. ZIP) und den Manifest-Hash separat
  notieren/versenden, um den Zeitpunkt zu belegen.
- **mbasic-Abschaltung:** Meta schaltet mbasic schrittweise ab. Leitet die
  Seite auf `www.facebook.com` um, gibt das Skript eine Warnung aus — dann
  bitte melden, die Selektoren müssen dann angepasst werden.
- Auch bei Fehlern/Abbruch (Strg-C) werden CSV und Manifest mit den bis dahin
  gesammelten Daten geschrieben.
