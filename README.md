# fbsave — Beweissicherung von Facebook-Kommentaren

Dokumentiert alle Kommentare (inkl. Antworten und Antworten auf Antworten)
unter einem öffentlichen Facebook-Post für eine Strafanzeige: Einzel-Screenshot
pro Kommentar, Gesamt-Screenshot, CSV mit Autor/Profillink/Zeitstempel/Text
sowie ein `manifest.json` mit UTC-Zeitstempeln des Laufs und SHA256-Hashes
aller Dateien.

Technik: Playwright steuert Chromium auf `www.facebook.com`. (`mbasic.` und
`m.facebook.com` werden von Meta inzwischen dorthin umgeleitet — der frühere
mbasic-Ansatz funktioniert nicht mehr.) Kommentare sind `div[role="article"]`-
Elemente; Autor und Relativzeit stehen im `aria-label`, die **exakte**
Kommentarzeit (Unix-Epoch) wird aus dem von Facebook mitgelieferten
Relay-/GraphQL-JSON geerntet.

## Setup

Das Projekt-venv liegt in `./fbsave/`. Falls Chromium fehlt:

```bash
./fbsave/bin/python -m playwright install chromium
```

## Verwendung

```bash
cd ~/dev/fbsave

# Standard: sichtbarer Browser, Ziel-URL ist als Default im Skript hinterlegt
./fbsave/bin/python fbsave.py

# Unsichtbar / andere URL / Schnelltest des Zeitparsers
./fbsave/bin/python fbsave.py --headless
./fbsave/bin/python fbsave.py --url "https://www.facebook.com/..."
./fbsave/bin/python fbsave.py --selftest
```

### Login

Eine in `state.json` gespeicherte Session wird automatisch benutzt (erzeugen
mit `./fbsave/bin/python save_login.py`: manuell einloggen, Enter drücken).
Ohne Login zeigt Facebook je nach Region nur eine Login-Wand — das Skript
erkennt das und bricht mit Hinweis ab. `state.json` enthält Session-Cookies —
nicht weitergeben (steht in `.gitignore`).

## Ablauf des Skripts

1. Post laden (URL wird auf `www.facebook.com` normalisiert)
2. Cookie-Consent wegklicken, falls vorhanden (nur ohne Login), danach neu laden
3. Kommentar-Sortierung auf **„Alle Kommentare"** umstellen (sonst blendet
   „Relevanteste zuerst" Kommentare aus!)
4. Auffalt-Schleife: „Weitere Kommentare ansehen", „Alle X Antworten ansehen",
   „Mehr anzeigen" (abgeschnittene Texte) — bis nichts mehr nachlädt
5. Gesamt-Screenshots, dann Extraktion pro `role=article`:
   Autor + Relativzeit aus `aria-label`, Profillink, Kommentar-ID aus den
   Links, Text, Bild-Alt-Texte, Einzel-Screenshot
6. Exakte Zeitstempel aus den gesammelten GraphQL-Antworten zuordnen

## Ausgabe (`captures/capture_<UTC>/`)

| Datei | Inhalt |
|---|---|
| `comments.csv` | `index, comment_id, is_reply, parent_id, parent_author, author, profile_url, timestamp_raw, timestamp_utc, timestamp_source, text, attachment_alt, screenshot_file, aria_label` (UTF-8-BOM, Excel-tauglich) |
| `comment_NNN.png` | Einzel-Screenshot pro Kommentar/Antwort |
| `full_page.png`, `full_dialog.png` | Gesamt-Screenshots |
| `manifest.json` | URLs, Seitentitel, Start/Ende UTC, Versionen, Zähler, SHA256 jeder Datei |

### Debug-Dateien (für Fehlersuche, ebenfalls gehasht)

| Datei | Inhalt |
|---|---|
| `debug_NN_<schritt>.png/.html` | Screenshot + kompletter HTML-Dump nach jedem Schritt |
| `debug_structure_*.json` | DOM-Diagnose: Anzahl Artikel, alle aria-Labels, alle Button-Texte, Dialoge — zeigt sofort, welcher Selektor nicht greift |
| `debug_graphql_times.json` | alle geernteten exakten Zeitstempel je Kommentar-ID |
| `debug_skipped_arias.json` | aria-Labels, die keinem Kommentar-Muster entsprachen (nur falls vorhanden) |

`probe.py` ist ein eigenständiges Diagnose-Werkzeug, das nur die DOM-Struktur
dumpt (`probe_out/`), ohne eine Beweissicherung zu erzeugen.

## Wichtige Hinweise zur Beweiskraft

- **Zeitstempel:** `timestamp_source=graphql_exact` heißt: exakte, von Facebook
  selbst gelieferte Kommentarzeit (sekundengenau, UTC). Nur wenn keine
  Kommentar-ID zuordenbar ist, wird die angezeigte Relativzeit
  (`timestamp_raw`, auch auf den Screenshots sichtbar) relativ zur Capture-Zeit
  umgerechnet (`computed_from_relative`, Näherung). Das Manifest zählt beide
  Quellen aus.
- **Bild-Kommentare:** Kommentare ohne Text (nur Bild/GIF/Sticker) haben ein
  leeres `text`-Feld; das Bild ist im Einzel-Screenshot gesichert und sein
  Alt-Text steht in `attachment_alt`.
- **Hashes:** `manifest.json` wird zuletzt geschrieben und enthält SHA256 aller
  übrigen Dateien. Verzeichnis nach dem Lauf nicht mehr verändern; ideal sofort
  archivieren (ZIP) und den Manifest-Hash separat notieren/versenden.
- **Sortierung:** Wenn das Umstellen auf „Alle Kommentare" fehlschlägt, warnt
  das Skript — dann können von Facebook ausgeblendete Kommentare fehlen
  (`manifest.json: sorted_to_all_comments`).
- Auch bei Fehlern/Abbruch (Strg-C) werden CSV und Manifest mit den bis dahin
  gesammelten Daten geschrieben.
- Facebook ändert sein Markup regelmäßig. Wenn ein Lauf 0 Kommentare liefert:
  `debug_structure_*.json` und die HTML-Dumps zeigen, welche aria-Labels und
  Button-Texte aktuell verwendet werden.
