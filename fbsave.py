#!/usr/bin/env python3
"""
fbsave.py — Beweissicherung von Kommentaren unter einem oeffentlichen Facebook-Post.

Laedt den Post ueber mbasic.facebook.com (serverseitig gerendert, kein Modal,
kein App-Interstitial), klickt Cookie-Consent weg, faltet alle Kommentare und
Antworten auf und dokumentiert sie:

  - debug_NN_<schritt>.png   Screenshot nach jedem Schritt (Debugging)
  - comment_NNN.png          Einzel-Screenshot pro Kommentar/Antwort
  - full_page.png            Gesamt-Screenshot der fertig aufgefalteten Seite
  - comments.csv             author, profile_url, timestamp_raw, timestamp_utc, text, ...
  - manifest.json            UTC-Zeitstempel des Laufs + SHA256 aller Dateien

Aufruf (im virtualenv mit installiertem Playwright):

    python fbsave.py                          # Standard-URL, sichtbarer Browser
    python fbsave.py --url <POST_URL>
    python fbsave.py --state state.json       # mit gespeicherter Login-Session
    python fbsave.py --headless               # ohne sichtbares Browserfenster
    python fbsave.py --selftest               # testet nur den Zeitstempel-Parser

Hinweis zur Beweiskraft: mbasic zeigt nur relative Zeitangaben ("5 Std.",
"Gestern um 14:32"). timestamp_raw ist der woertliche Seiteninhalt (Beleg),
timestamp_utc wird daraus relativ zur Capture-Zeit berechnet (Naeherung,
Genauigkeit entspricht der Granularitaet der Anzeige).
"""

import argparse
import csv
import hashlib
import itertools
import json
import platform
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

DEFAULT_URL = (
    "https://www.facebook.com/neue.szene.augsburg/posts/"
    "pfbid02RgKgpshoRqspLtfHu2zwkmwbSXh5tTqFpxNXmvkDv7e4anhHS1zwHKSbsxKjC89Sl"
)

# ---------------------------------------------------------------------------
# Text-Muster (deutsch + englisch, da die Sprache vom Consent-Status abhaengt)
# ---------------------------------------------------------------------------

COOKIE_ACCEPT_RE = re.compile(
    r"alle( optionalen)? cookies erlauben"
    r"|erforderliche und optionale cookies erlauben"
    r"|alle akzeptieren"
    r"|allow all cookies"
    r"|allow essential and optional"
    r"|accept all",
    re.I,
)

MORE_COMMENTS_RE = re.compile(
    r"weitere kommentare|vorherige kommentare|mehr kommentare"
    r"|view more comments|view previous comments|see more comments|show more comments",
    re.I,
)

MORE_REPLIES_RE = re.compile(
    r"weitere antworten|vorherige antworten|more replies|previous replies",
    re.I,
)

# Links wie "3 Antworten" / "1 Antwort" / "View 5 replies" oeffnen den
# Antwort-Thread. Der blanke Aktions-Link "Antworten" (= antworten schreiben)
# wird ueber EXCLUDE_EXACT ausgeschlossen.
REPLY_LINK_RE = re.compile(
    r"^\d+\s+antwort(en)?$"
    r"|^\d+\s+repl(y|ies)$"
    r"|alle\s+\d+\s+antworten"
    r"|antworten anzeigen"
    r"|view\s+(all\s+)?\d+\s+(more\s+)?repl(y|ies)",
    re.I,
)
EXCLUDE_EXACT = {"antworten", "reply", "antworten …", "reply …"}

LOGIN_WALL_RE = re.compile(
    r"you must log in|log in to continue|log in or sign up"
    r"|du musst dich anmelden|melde dich an|anmelden oder registrieren",
    re.I,
)

# ---------------------------------------------------------------------------
# Zeitstempel-Parser: relative mbasic-Anzeige -> absolute Zeit
# ---------------------------------------------------------------------------

DE_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
    "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10,
    "november": 11, "dezember": 12,
}
EN_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
DE_WEEKDAYS = {
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
    "freitag": 4, "samstag": 5, "sonntag": 6,
}
EN_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _clock(h: str, m: str, ampm: Optional[str]) -> "tuple[int, int]":
    hour, minute = int(h), int(m)
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    return hour, minute


def parse_fb_time(raw: str, ref: datetime) -> Optional[datetime]:
    """Wandelt eine Facebook-Relativzeit in eine absolute Zeit (Zeitzone von ref) um.

    ref ist der zeitzonenbewusste Zeitpunkt, zu dem die Anzeige erfasst wurde.
    Gibt None zurueck, wenn das Format unbekannt ist.
    """
    try:
        return _parse_fb_time(raw, ref)
    except (ValueError, OverflowError):
        return None


def _parse_fb_time(raw: str, ref: datetime) -> Optional[datetime]:
    s = " ".join(raw.strip().lower().split())
    s = re.sub(r"^vor\s+", "", s)  # "vor 5 Min." -> "5 min."

    if re.fullmatch(r"gerade eben|jetzt|just now", s):
        return ref

    units = [
        (r"(\d+)\s*(sek\.?|sec(?:s)?\.?|s)", "seconds"),
        (r"(\d+)\s*(min\.?|minuten?|mins?\.?|m)", "minutes"),
        (r"(\d+)\s*(std\.?|stunden?|hrs?\.?|hours?|h)", "hours"),
        (r"(\d+)\s*(tg\.?|tag(?:en)?|days?|d)", "days"),
        (r"(\d+)\s*(wo\.?|wochen?|wks?\.?|weeks?|w)", "weeks"),
    ]
    for pattern, unit in units:
        m = re.fullmatch(pattern, s)
        if m:
            return ref - timedelta(**{unit: int(m.group(1))})

    # "gestern um 14:32" / "yesterday at 6:01 pm"
    m = re.fullmatch(r"(gestern|yesterday)\s+(?:um|at)\s+(\d{1,2}):(\d{2})\s*(am|pm)?", s)
    if m:
        hour, minute = _clock(m.group(2), m.group(3), m.group(4))
        return (ref - timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)

    # "montag um 09:15" / "monday at 9:15 am" -> letzter solcher Wochentag
    m = re.fullmatch(r"([a-zäöü]+)\s+(?:um|at)\s+(\d{1,2}):(\d{2})\s*(am|pm)?", s)
    if m and (m.group(1) in DE_WEEKDAYS or m.group(1) in EN_WEEKDAYS):
        wd = DE_WEEKDAYS.get(m.group(1), EN_WEEKDAYS.get(m.group(1)))
        back = (ref.weekday() - wd) % 7 or 7
        hour, minute = _clock(m.group(2), m.group(3), m.group(4))
        return (ref - timedelta(days=back)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)

    # "4. juni um 18:01" / "4. juni 2025 um 18:01" (Uhrzeit optional)
    m = re.fullmatch(
        r"(\d{1,2})\.\s*([a-zäöü]+)(?:\s+(\d{4}))?(?:\s+um\s+(\d{1,2}):(\d{2}))?", s)
    if m and m.group(2) in DE_MONTHS:
        day, month = int(m.group(1)), DE_MONTHS[m.group(2)]
        hour, minute = (int(m.group(4)), int(m.group(5))) if m.group(4) else (0, 0)
        year = int(m.group(3)) if m.group(3) else ref.year
        dt = ref.replace(year=year, month=month, day=day,
                         hour=hour, minute=minute, second=0, microsecond=0)
        if not m.group(3) and dt > ref:
            dt = dt.replace(year=year - 1)
        return dt

    # "june 4 at 6:01 pm" / "june 4, 2025 at 6:01 pm" (Uhrzeit optional)
    m = re.fullmatch(
        r"([a-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?"
        r"(?:\s+at\s+(\d{1,2}):(\d{2})\s*(am|pm)?)?", s)
    if m and m.group(1) in EN_MONTHS:
        month, day = EN_MONTHS[m.group(1)], int(m.group(2))
        hour, minute = _clock(m.group(4), m.group(5), m.group(6)) if m.group(4) else (0, 0)
        year = int(m.group(3)) if m.group(3) else ref.year
        dt = ref.replace(year=year, month=month, day=day,
                         hour=hour, minute=minute, second=0, microsecond=0)
        if not m.group(3) and dt > ref:
            dt = dt.replace(year=year - 1)
        return dt

    return None


def selftest() -> int:
    from datetime import timezone as tz
    ref = datetime(2026, 6, 10, 12, 0, tzinfo=tz(timedelta(hours=2)))
    cases = [
        ("Gerade eben", datetime(2026, 6, 10, 12, 0)),
        ("Just now", datetime(2026, 6, 10, 12, 0)),
        ("45 Sek.", datetime(2026, 6, 10, 11, 59, 15)),
        ("5 Min.", datetime(2026, 6, 10, 11, 55)),
        ("vor 5 Min.", datetime(2026, 6, 10, 11, 55)),
        ("3 Std.", datetime(2026, 6, 10, 9, 0)),
        ("23 hrs", datetime(2026, 6, 9, 13, 0)),
        ("2 Tagen", datetime(2026, 6, 8, 12, 0)),
        ("1 Wo.", datetime(2026, 6, 3, 12, 0)),
        ("Gestern um 14:32", datetime(2026, 6, 9, 14, 32)),
        ("Yesterday at 6:01 PM", datetime(2026, 6, 9, 18, 1)),
        ("Montag um 09:15", datetime(2026, 6, 8, 9, 15)),
        ("4. Juni um 18:01", datetime(2026, 6, 4, 18, 1)),
        ("4. Juni", datetime(2026, 6, 4, 0, 0)),
        ("28. Dezember um 10:00", datetime(2025, 12, 28, 10, 0)),
        ("3. Juni 2024 um 08:30", datetime(2024, 6, 3, 8, 30)),
        ("June 4 at 6:01 PM", datetime(2026, 6, 4, 18, 1)),
        ("December 28 at 10:00 AM", datetime(2025, 12, 28, 10, 0)),
        ("June 3, 2024 at 8:30 AM", datetime(2024, 6, 3, 8, 30)),
    ]
    failed = 0
    for raw, want_naive in cases:
        got = parse_fb_time(raw, ref)
        want = want_naive.replace(tzinfo=ref.tzinfo)
        ok = got == want
        failed += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'FAIL'} {raw!r:32} -> {got}  (erwartet {want})")
    unknown = parse_fb_time("voellig unbekanntes format", ref)
    print(f"  {'OK  ' if unknown is None else 'FAIL'} unbekanntes Format -> {unknown!r} (erwartet None)")
    failed += 0 if unknown is None else 1
    print(f"Selftest: {len(cases) + 1 - failed}/{len(cases) + 1} bestanden")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# URL-Helfer
# ---------------------------------------------------------------------------

def to_mbasic(url: str) -> str:
    p = urlparse(url)
    host = re.sub(r"^(www|m|web)\.facebook\.com$", "mbasic.facebook.com", p.netloc)
    return urlunparse(p._replace(netloc=host))


def normalize_profile_url(href: str, base: str) -> str:
    """Macht aus einem mbasic-Profillink einen kanonischen www-Link ohne Tracking."""
    if not href:
        return ""
    absolute = urljoin(base, href)
    p = urlparse(absolute)
    host = p.netloc.replace("mbasic.facebook.com", "www.facebook.com") \
                   .replace("m.facebook.com", "www.facebook.com")
    # Nur den fuer die Identifikation noetigen id-Parameter behalten
    qs = parse_qs(p.query)
    keep = {k: v for k, v in qs.items() if k in ("id", "story_fbid")}
    query = "&".join(f"{k}={v[0]}" for k, v in keep.items())
    return urlunparse(("https", host, p.path, "", query, ""))


# ---------------------------------------------------------------------------
# Browser-Schritte
# ---------------------------------------------------------------------------

class Stepper:
    """Debug-Helfer: nummerierter Screenshot + Titel/URL-Ausgabe nach jedem Schritt."""

    def __init__(self, outdir: Path):
        self.outdir = outdir
        self.counter = itertools.count(1)

    def step(self, page, name: str) -> None:
        n = next(self.counter)
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        path = self.outdir / f"debug_{n:02d}_{slug}.png"
        try:
            page.screenshot(path=str(path))
        except Exception as exc:
            print(f"[STEP {n:02d}] {name}: Screenshot fehlgeschlagen: {exc}")
        try:
            title = page.title()
        except Exception:
            title = "<nicht lesbar>"
        print(f"[STEP {n:02d}] {name}")
        print(f"          Titel: {title!r}")
        print(f"          URL:   {page.url}", flush=True)


def handle_cookie_consent(page, stepper: Stepper) -> bool:
    """Klickt einen Cookie-/Consent-Button, falls vorhanden. True = geklickt."""
    for el in page.query_selector_all("button, input[type=submit], a"):
        try:
            label = el.get_attribute("value") if el.evaluate(
                "e => e.tagName") == "INPUT" else el.inner_text()
        except Exception:
            continue
        label = " ".join((label or "").split())
        if label and COOKIE_ACCEPT_RE.search(label):
            print(f"    Cookie-Dialog gefunden, klicke: {label!r}")
            try:
                el.click()
                page.wait_for_load_state("load", timeout=20000)
            except Exception as exc:
                print(f"    Klick/Navigation: {exc}")
            stepper.step(page, "nach_cookie_klick")
            return True
    print("    Kein Cookie-Dialog gefunden (evtl. schon akzeptiert).")
    return False


def detect_login_wall(page) -> bool:
    if "/login" in urlparse(page.url).path:
        return True
    try:
        body = page.inner_text("body", timeout=5000)
    except Exception:
        return False
    return bool(LOGIN_WALL_RE.search(body[:3000]))


def click_one_matching_link(page, pattern) -> Optional[str]:
    """Klickt den ersten Link, dessen Text auf pattern passt (mbasic = Navigation).

    Gibt den Linktext zurueck oder None, wenn nichts gefunden wurde.
    """
    for a in page.query_selector_all("a[href]"):
        try:
            text = " ".join((a.inner_text() or "").split())
        except Exception:
            continue
        if not text or text.lower() in EXCLUDE_EXACT:
            continue
        if pattern.search(text):
            try:
                a.click()
                page.wait_for_load_state("load", timeout=30000)
            except Exception as exc:
                print(f"    Klick auf {text!r} fehlgeschlagen: {exc}")
                return None
            return text
    return None


def expand_all(page, stepper: Stepper, pattern, label: str, max_rounds: int) -> None:
    for round_no in range(1, max_rounds + 1):
        clicked = click_one_matching_link(page, pattern)
        if clicked is None:
            print(f"    {label}: nichts mehr aufzufalten (nach {round_no - 1} Klicks).")
            return
        print(f"    {label} Runde {round_no}: {clicked!r} geklickt")
        stepper.step(page, f"{label}_runde_{round_no}")
    print(f"    WARNUNG: --max-rounds ({max_rounds}) erreicht, "
          f"moeglicherweise nicht alles aufgefaltet!")


def collect_reply_links(page) -> "list[str]":
    hrefs, seen = [], set()
    for a in page.query_selector_all("a[href]"):
        try:
            text = " ".join((a.inner_text() or "").split())
        except Exception:
            continue
        if not text or text.lower() in EXCLUDE_EXACT:
            continue
        if REPLY_LINK_RE.search(text):
            href = urljoin(page.url, a.get_attribute("href") or "")
            if href not in seen:
                seen.add(href)
                hrefs.append(href)
    return hrefs


# ---------------------------------------------------------------------------
# Extraktion
# ---------------------------------------------------------------------------

_EXTRACT_JS = """
(el) => {
  const h3 = el.querySelector('h3');
  const a = h3 ? h3.querySelector('a[href]') : null;
  let text = '';
  if (h3) {
    const parts = [];
    let n = h3.nextElementSibling;
    while (n) {
      if (n.querySelector('abbr')) break;   // Footer (Gefaellt mir / Zeit) erreicht
      parts.push(n.innerText);
      n = n.nextElementSibling;
    }
    text = parts.join('\\n').trim();
  }
  const abbr = el.querySelector('abbr');
  return {
    author: a ? a.innerText.trim() : '',
    href: a ? a.getAttribute('href') : '',
    time_raw: abbr ? abbr.innerText.trim() : '',
    text: text,
  };
}
"""


def find_comment_elements(page):
    """mbasic-Heuristik: Kommentar = div mit (langer) numerischer id, das einen
    Autor-Link in einem h3 enthaelt. Falls ein ufi-Container existiert
    (Kommentarbereich), wird nur darin gesucht, damit der Post selbst nicht
    faelschlich als Kommentar erfasst wird."""
    scope = page.query_selector("div[id^='ufi_']") or page
    out = []
    for d in scope.query_selector_all("div[id]"):
        cid = d.get_attribute("id") or ""
        if not re.fullmatch(r"\d{6,}", cid):
            continue
        if d.query_selector("h3 a[href]") is None:
            continue
        out.append((cid, d))
    return out


def extract_from_page(page, rows, seen_ids, stepper: Stepper, outdir: Path,
                      capture_time: datetime, *, is_reply_page: bool,
                      parent_id: str = "") -> None:
    """Extrahiert alle Kommentar-Elemente der aktuellen Seite in rows (dedupe per id)."""
    elements = find_comment_elements(page)
    print(f"    {len(elements)} Kommentar-Element(e) auf dieser Seite gefunden.")
    for cid, el in elements:
        if cid in seen_ids:
            continue
        try:
            data = el.evaluate(_EXTRACT_JS)
        except Exception as exc:
            print(f"    WARNUNG: Extraktion fuer id={cid} fehlgeschlagen: {exc}")
            continue
        index = len(rows) + 1
        shot = f"comment_{index:03d}.png"
        try:
            el.scroll_into_view_if_needed(timeout=5000)
            el.screenshot(path=str(outdir / shot))
        except Exception as exc:
            print(f"    WARNUNG: Screenshot fuer id={cid} fehlgeschlagen: {exc}")
            shot = ""
        parsed = parse_fb_time(data["time_raw"], capture_time) if data["time_raw"] else None
        is_reply = is_reply_page and cid != parent_id
        row = {
            "index": index,
            "comment_id": cid,
            "is_reply": "ja" if is_reply else "nein",
            "parent_id": parent_id if is_reply else "",
            "author": data["author"],
            "profile_url": normalize_profile_url(data["href"], page.url),
            "timestamp_raw": data["time_raw"],
            "timestamp_utc": parsed.astimezone(timezone.utc).isoformat() if parsed else "",
            "text": data["text"],
            "screenshot_file": shot,
            "source_url": page.url,
        }
        rows.append(row)
        seen_ids.add(cid)
        preview = (row["text"][:60] + "…") if len(row["text"]) > 60 else row["text"]
        print(f"    [{index:03d}] {row['author']} ({row['timestamp_raw']}): {preview!r}")


# ---------------------------------------------------------------------------
# Ausgabe: CSV + Manifest
# ---------------------------------------------------------------------------

CSV_FIELDS = ["index", "comment_id", "is_reply", "parent_id", "author",
              "profile_url", "timestamp_raw", "timestamp_utc", "text",
              "screenshot_file", "source_url"]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_outputs(outdir: Path, rows, manifest: dict) -> None:
    csv_path = outdir / "comments.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    manifest["comment_count"] = len(rows)
    manifest["files_sha256"] = {
        p.name: sha256_file(p)
        for p in sorted(outdir.iterdir())
        if p.is_file() and p.name != "manifest.json"
    }
    with open(outdir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\nErgebnis: {len(rows)} Kommentar(e)")
    print(f"  CSV:      {csv_path}")
    print(f"  Manifest: {outdir / 'manifest.json'}")


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------

def run(args) -> int:
    from playwright.sync_api import sync_playwright

    started = datetime.now(timezone.utc)
    outdir = Path(args.out) / f"capture_{started.strftime('%Y%m%dT%H%M%SZ')}"
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"Ausgabeverzeichnis: {outdir}")

    mbasic_url = to_mbasic(args.url)
    print(f"Ziel-URL (mbasic): {mbasic_url}")

    stepper = Stepper(outdir)
    rows: list = []
    seen_ids: set = set()
    manifest = {
        "purpose": "Beweissicherung Facebook-Kommentare",
        "requested_url": args.url,
        "mbasic_url": mbasic_url,
        "capture_started_utc": started.isoformat(),
        "python_version": platform.python_version(),
        "note_timestamps": (
            "timestamp_raw ist der woertliche Anzeigetext der Seite. "
            "timestamp_utc wurde daraus relativ zur Capture-Zeit berechnet "
            "(Naeherung in Granularitaet der Anzeige)."
        ),
    }
    try:
        import playwright as _pw
        manifest["playwright_version"] = getattr(_pw, "__version__", "unbekannt")
    except Exception:
        pass

    exit_code = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        ctx_kwargs = {
            "locale": "de-DE",
            "viewport": {"width": 1280, "height": 1600},
        }
        state = Path(args.state) if args.state else None
        if state and state.exists():
            ctx_kwargs["storage_state"] = str(state)
            print(f"Login-Session aus {state} geladen.")
            manifest["used_storage_state"] = str(state)
        elif args.state:
            print(f"WARNUNG: --state {args.state} existiert nicht, fahre ohne Login fort.")
        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()
        page.set_default_timeout(20000)

        try:
            # 1) Post laden
            page.goto(mbasic_url, wait_until="load", timeout=60000)
            stepper.step(page, "post_geladen")

            # 2) Cookie-Consent; danach Post-URL neu laden (Redirect-Problem)
            if handle_cookie_consent(page, stepper):
                page.goto(mbasic_url, wait_until="load", timeout=60000)
                stepper.step(page, "post_nach_cookie_neu_geladen")

            if "mbasic.facebook.com" not in urlparse(page.url).netloc:
                print("WARNUNG: Seite wurde von mbasic weggeleitet "
                      f"({page.url}). Die Selektoren passen evtl. nicht mehr — "
                      "bitte Debug-Screenshots pruefen.")

            if detect_login_wall(page):
                stepper.step(page, "login_wand")
                print("\nFEHLER: Facebook verlangt einen Login fuer diesen Post.")
                print("Abhilfe: einmalig  python save_login.py  ausfuehren (manuell "
                      "einloggen, Session wird als state.json gespeichert) und dann "
                      "dieses Skript mit  --state state.json  erneut starten.")
                manifest["aborted"] = "login_wall"
                return 2

            # Capture-Zeit als Referenz fuer die Relativzeit-Umrechnung
            capture_time = datetime.now(timezone.utc).astimezone()

            # 3) Alle "weitere Kommentare"-Links wiederholt klicken
            print("\n== Kommentare auffalten ==")
            expand_all(page, stepper, MORE_COMMENTS_RE, "kommentare", args.max_rounds)

            # 4) Gesamt-Screenshot + Top-Level-Kommentare extrahieren
            page.screenshot(path=str(outdir / "full_page.png"), full_page=True)
            stepper.step(page, "vor_extraktion")
            manifest["page_title"] = page.title()
            manifest["final_url"] = page.url
            print("\n== Top-Level-Kommentare extrahieren ==")
            extract_from_page(page, rows, seen_ids, stepper, outdir,
                              capture_time, is_reply_page=False)

            # 5) Antwort-Threads einzeln besuchen
            reply_links = collect_reply_links(page)
            print(f"\n== {len(reply_links)} Antwort-Thread(s) gefunden ==")
            for i, href in enumerate(reply_links, 1):
                print(f"  Thread {i}/{len(reply_links)}: {href}")
                try:
                    page.goto(href, wait_until="load", timeout=60000)
                except Exception as exc:
                    print(f"    WARNUNG: Thread nicht ladbar: {exc}")
                    continue
                stepper.step(page, f"antworten_thread_{i}")
                expand_all(page, stepper, MORE_REPLIES_RE,
                           f"antworten_{i}", args.max_rounds)
                parent_id = parse_qs(urlparse(page.url).query).get(
                    "comment_id", [""])[0]
                extract_from_page(page, rows, seen_ids, stepper, outdir,
                                  capture_time, is_reply_page=True,
                                  parent_id=parent_id)

        except KeyboardInterrupt:
            print("\nAbgebrochen — bisherige Daten werden trotzdem gespeichert.")
            manifest["aborted"] = "keyboard_interrupt"
            exit_code = 130
        except Exception:
            print("\nFEHLER — bisherige Daten werden trotzdem gespeichert:")
            traceback.print_exc()
            manifest["aborted"] = "exception"
            manifest["error"] = traceback.format_exc()
            exit_code = 1
        finally:
            manifest["capture_finished_utc"] = datetime.now(timezone.utc).isoformat()
            try:
                browser.close()
            except Exception:
                pass
            write_outputs(outdir, rows, manifest)

    return exit_code


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Dokumentiert alle Kommentare unter einem oeffentlichen "
                    "Facebook-Post (Screenshots + CSV + Manifest).")
    ap.add_argument("--url", default=DEFAULT_URL, help="URL des Facebook-Posts")
    ap.add_argument("--headless", action="store_true",
                    help="Browser unsichtbar laufen lassen (Standard: sichtbar)")
    ap.add_argument("--state", default="state.json",
                    help="Playwright storage_state mit Login-Session (optional; "
                         "wird ignoriert, wenn die Datei fehlt)")
    ap.add_argument("--out", default="captures", help="Basis-Ausgabeverzeichnis")
    ap.add_argument("--max-rounds", type=int, default=200,
                    help="Max. Klicks pro Auffalt-Schleife")
    ap.add_argument("--selftest", action="store_true",
                    help="Nur den Zeitstempel-Parser testen (kein Browser)")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
