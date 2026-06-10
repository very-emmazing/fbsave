#!/usr/bin/env python3
"""
fbsave.py — Beweissicherung von Kommentaren unter einem oeffentlichen Facebook-Post.

Laedt den Post auf www.facebook.com (mbasic/m. werden von Meta inzwischen
dorthin umgeleitet), stellt die Sortierung auf "Alle Kommentare", faltet alle
Kommentare, Antworten und abgeschnittene Texte ("Mehr anzeigen") auf und
dokumentiert sie:

  - debug_NN_<schritt>.png/.html  Screenshot + HTML-Dump nach jedem Schritt
  - debug_structure_*.json        DOM-Diagnose (Artikel, aria-Labels, Buttons)
  - debug_graphql_times.json      aus GraphQL geerntete exakte Zeitstempel
  - comment_NNN.png               Einzel-Screenshot pro Kommentar/Antwort
  - full_page.png                 Gesamt-Screenshot der aufgefalteten Seite
  - comments.csv                  author, profile_url, timestamps, text, ...
  - manifest.json                 UTC-Zeitstempel des Laufs + SHA256 aller Dateien

Zeitstempel: Facebook liefert die exakte Kommentarzeit (Unix-Epoch) im
eingebetteten Relay-/GraphQL-JSON mit. Wo eine Kommentar-ID zugeordnet werden
kann, ist timestamp_utc EXAKT (timestamp_source=graphql_exact). Nur als
Fallback wird die angezeigte Relativzeit ("vor 5 Tagen") relativ zur
Capture-Zeit umgerechnet (timestamp_source=computed_from_relative).

Aufruf (im Projekt-venv ./fbsave/):

    ./fbsave/bin/python fbsave.py                    # sichtbarer Browser
    ./fbsave/bin/python fbsave.py --headless
    ./fbsave/bin/python fbsave.py --url <POST_URL>
    ./fbsave/bin/python fbsave.py --selftest         # nur Zeitparser testen
"""

import argparse
import base64
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
from urllib.parse import parse_qs, urlparse, urlunparse

DEFAULT_URL = (
    "https://www.facebook.com/neue.szene.augsburg/posts/"
    "pfbid02RgKgpshoRqspLtfHu2zwkmwbSXh5tTqFpxNXmvkDv7e4anhHS1zwHKSbsxKjC89Sl"
)

# ---------------------------------------------------------------------------
# Text-Muster (deutsch + englisch; Texte am 2026-06-10 live verifiziert)
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

# Sortier-Umschalter ueber der Kommentarliste ("Relevanteste zuerst" etc.)
SORT_BUTTON_RE = r"relevanteste|most relevant|neueste zuerst|newest|top comments"
SORT_TARGET_RE = r"alle kommentare|all comments"

# Auffalt-Buttons: weitere Kommentare, Antwort-Threads, abgeschnittene Texte.
# Live beobachtete Texte: "Weitere Kommentare ansehen", "Alle 26 Antworten
# ansehen", "Antwort ansehen", "Mehr anzeigen".
EXPAND_RE = (
    r"weitere kommentare (ansehen|anzeigen)"
    r"|vorherige kommentare"
    r"|view (more|previous) comments"
    r"|antwort(en)? ansehen"
    r"|weitere antwort(en)?"
    r"|view (all )?\d+ (more )?repl(y|ies)"
    r"|view (more )?repl(y|ies)"
    r"|mehr anzeigen"
    r"|see more"
)
# Exakte Texte, die NICHT geklickt werden duerfen (Aktions-Buttons / Menues)
EXPAND_EXCLUDE = ["antworten", "reply", "mehr", "more", "gefällt mir", "like",
                  "teilen", "share", "kommentieren", "comment"]

LOGIN_WALL_RE = re.compile(
    r"you must log in|log in to continue|log in or sign up"
    r"|du musst dich anmelden|melde dich an|anmelden oder registrieren",
    re.I,
)

# aria-Labels der Kommentar-Artikel, live beobachtet:
#   "Kommentar von Uwe Schenk (vor 5 Tagen)"
#   "Antworte mit Anna G. auf Anke-Luzia P.s Kommentar (Vor 27 Minuten)."
ARIA_PATTERNS = [
    re.compile(r"^Kommentar von (?P<author>.+?) \((?P<time>[^()]+)\)\.?$", re.S),
    # "auf Ys Kommentar" = Antwort, "auf Ys Antwort" = Antwort auf eine Antwort
    re.compile(r"^Antwort(?:e)? (?:von|mit) (?P<author>.+?) auf (?P<parent>.+?)s?"
               r" (?:Kommentar|Antwort) \((?P<time>[^()]+)\)\.?$", re.S),
    re.compile(r"^Comment by (?P<author>.+?) \((?P<time>[^()]+)\)\.?$", re.S),
    re.compile(r"^Reply by (?P<author>.+?) to (?P<parent>.+?)(?:['’]s)?"
               r" (?:comment|reply) \((?P<time>[^()]+)\)\.?$", re.S),
]

# created_time + Kommentar-URL aus eingebettetem Relay-/GraphQL-JSON; die ID
# wird aus der URL geparst (bei Antworten zaehlt reply_comment_id, NICHT die
# comment_id des Eltern-Kommentars)
CREATED_TIME_RE = re.compile(r'"created_time":(\d+),"url":"([^"]*?)"')

# ---------------------------------------------------------------------------
# Zeitstempel-Parser: relative Anzeige -> absolute Zeit (nur Fallback)
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
    s = re.sub(r"^vor\s+", "", s)         # "vor 5 Tagen"  -> "5 tagen"
    s = re.sub(r"\s+ago$", "", s)         # "5 days ago"   -> "5 days"
    s = re.sub(r"^einer?m?\s+", "1 ", s)  # "einer Stunde" -> "1 stunde"
    s = re.sub(r"^an?\s+", "1 ", s)       # "an hour"      -> "1 hour"

    if re.fullmatch(r"gerade eben|jetzt|just now", s):
        return ref

    units = [
        (r"(\d+)\s*(sek\.?|sekunden?|sec(?:s)?\.?|s)", "seconds"),
        (r"(\d+)\s*(min\.?|minuten?|mins?\.?|minutes?|m)", "minutes"),
        (r"(\d+)\s*(std\.?|stunden?|hrs?\.?|hours?|h)", "hours"),
        (r"(\d+)\s*(tg\.?|tag(?:e|en)?|days?|d)", "days"),
        (r"(\d+)\s*(wo\.?|wochen?|wks?\.?|weeks?|w)", "weeks"),
    ]
    for pattern, unit in units:
        m = re.fullmatch(pattern, s)
        if m:
            return ref - timedelta(**{unit: int(m.group(1))})
    m = re.fullmatch(r"(\d+)\s*(j\.?|jahr(?:e|en)?|yrs?\.?|years?|y)", s)
    if m:
        return ref - timedelta(days=365 * int(m.group(1)))  # Naeherung

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
        ("Vor 46 Minuten", datetime(2026, 6, 10, 11, 14)),
        ("vor einer Stunde", datetime(2026, 6, 10, 11, 0)),
        ("3 Std.", datetime(2026, 6, 10, 9, 0)),
        ("23 hrs", datetime(2026, 6, 9, 13, 0)),
        ("5 Tage", datetime(2026, 6, 5, 12, 0)),
        ("vor 5 Tagen", datetime(2026, 6, 5, 12, 0)),
        ("2 Tagen", datetime(2026, 6, 8, 12, 0)),
        ("46 minutes ago", datetime(2026, 6, 10, 11, 14)),
        ("1 Wo.", datetime(2026, 6, 3, 12, 0)),
        ("vor einem Jahr", datetime(2025, 6, 10, 12, 0)),
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

def to_www(url: str) -> str:
    p = urlparse(url)
    host = re.sub(r"^(mbasic|m|web)\.facebook\.com$", "www.facebook.com", p.netloc)
    return urlunparse(p._replace(netloc=host))


def normalize_profile_url(href: str) -> str:
    """Kanonischer Profillink ohne Tracking-/Kontext-Parameter."""
    if not href:
        return ""
    p = urlparse(href)
    host = (p.netloc or "www.facebook.com") \
        .replace("mbasic.facebook.com", "www.facebook.com") \
        .replace("m.facebook.com", "www.facebook.com")
    qs = parse_qs(p.query)
    keep = {k: v for k, v in qs.items() if k == "id"}  # profile.php?id=...
    query = "&".join(f"{k}={v[0]}" for k, v in keep.items())
    return urlunparse(("https", host, p.path, "", query, ""))


def decode_comment_id(value: str) -> str:
    """comment_id-Parameter normalisieren: numerisch lassen, Base64-IDs wie
    'Y29tbWVudDo...' ('comment:<post>_<id>') dekodieren."""
    if not value:
        return ""
    if value.isdigit():
        return value
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4)).decode("utf-8", "replace")
        m = re.search(r"_(\d+)$", decoded)
        if m:
            return m.group(1)
    except Exception:
        pass
    return value


def extract_ids_from_links(hrefs: "list[str]") -> "tuple[str, str]":
    """(eigene_id, parent_id) aus den Links eines Kommentar-Artikels."""
    for href in hrefs:
        q = parse_qs(urlparse(href).query)
        if "reply_comment_id" in q:
            return (decode_comment_id(q["reply_comment_id"][0]),
                    decode_comment_id(q.get("comment_id", [""])[0]))
    for href in hrefs:
        q = parse_qs(urlparse(href).query)
        if "comment_id" in q:
            return decode_comment_id(q["comment_id"][0]), ""
    return "", ""


# ---------------------------------------------------------------------------
# Debug-Hilfen (Screenshot + HTML + Strukturdiagnose pro Schritt)
# ---------------------------------------------------------------------------

class Stepper:
    """Nummerierter Screenshot, optional HTML-Dump, Titel/URL-Ausgabe je Schritt."""

    def __init__(self, outdir: Path):
        self.outdir = outdir
        self.counter = itertools.count(1)

    def step(self, page, name: str, save_html: bool = False) -> None:
        n = next(self.counter)
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        try:
            page.screenshot(path=str(self.outdir / f"debug_{n:02d}_{slug}.png"))
        except Exception as exc:
            print(f"[STEP {n:02d}] {name}: Screenshot fehlgeschlagen: {exc}")
        if save_html:
            try:
                (self.outdir / f"debug_{n:02d}_{slug}.html").write_text(
                    page.content(), encoding="utf-8")
            except Exception as exc:
                print(f"[STEP {n:02d}] {name}: HTML-Dump fehlgeschlagen: {exc}")
        try:
            title = page.title()
        except Exception:
            title = "<nicht lesbar>"
        print(f"[STEP {n:02d}] {name}")
        print(f"          Titel: {title!r}")
        print(f"          URL:   {page.url}", flush=True)


_DIAG_JS = """
(el) => {
  const arts = [...el.querySelectorAll('div[role="article"]')];
  const buttons = [...el.querySelectorAll('[role="button"]')]
    .map(b => (b.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 80))
    .filter(t => t);
  return {
    n_articles: arts.length,
    article_arias: arts.map(a => (a.getAttribute('aria-label') || '').slice(0, 120)),
    button_texts: [...new Set(buttons)],
    n_dialogs: document.querySelectorAll('div[role="dialog"]').length,
  };
}
"""


def dump_diagnostics(page, scope, outdir: Path, tag: str) -> dict:
    """Schreibt eine DOM-Strukturdiagnose nach debug_structure_<tag>.json.

    Damit laesst sich ohne erneuten Lauf erkennen, warum Selektoren nicht
    greifen (welche aria-Labels / Button-Texte tatsaechlich vorhanden sind).
    """
    try:
        data = scope.evaluate(_DIAG_JS)
    except Exception as exc:
        data = {"error": str(exc)}
    data["url"] = page.url
    try:
        data["title"] = page.title()
    except Exception:
        pass
    path = outdir / f"debug_structure_{tag}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"    Diagnose -> {path.name}: {data.get('n_articles', '?')} Artikel, "
          f"{len(data.get('button_texts', []))} Button-Texte, "
          f"{data.get('n_dialogs', '?')} Dialog(e)")
    return data


# ---------------------------------------------------------------------------
# GraphQL-Ernte: exakte created_time je comment_id
# ---------------------------------------------------------------------------

class TimeHarvester:
    def __init__(self):
        self.times: "dict[str, int]" = {}
        self.sources = 0

    def feed(self, text: str) -> None:
        if not text or "created_time" not in text:
            return
        self.sources += 1
        for epoch, url in CREATED_TIME_RE.findall(text):
            q = parse_qs(urlparse(url.replace("\\/", "/")).query)
            cid = q.get("reply_comment_id", q.get("comment_id", [""]))[0]
            cid = decode_comment_id(cid)
            if cid:
                self.times[cid] = int(epoch)

    def dump(self, outdir: Path) -> None:
        path = outdir / "debug_graphql_times.json"
        path.write_text(json.dumps(
            {"sources_with_created_time": self.sources,
             "comment_ids": len(self.times),
             "times_utc": {cid: datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
                           for cid, t in sorted(self.times.items())}},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"    GraphQL-Ernte -> {path.name}: exakte Zeit fuer "
              f"{len(self.times)} Kommentar-ID(s) aus {self.sources} Quelle(n)")


# ---------------------------------------------------------------------------
# Browser-Schritte
# ---------------------------------------------------------------------------

def handle_cookie_consent(page, stepper: Stepper) -> bool:
    for el in page.query_selector_all('button, input[type=submit], a, [role="button"]'):
        try:
            tag = el.evaluate("e => e.tagName")
            label = el.get_attribute("value") if tag == "INPUT" else el.inner_text()
        except Exception:
            continue
        label = " ".join((label or "").split())
        if label and COOKIE_ACCEPT_RE.search(label):
            print(f"    Cookie-Dialog gefunden, klicke: {label!r}")
            try:
                el.click()
                page.wait_for_timeout(2000)
            except Exception as exc:
                print(f"    Klick: {exc}")
            stepper.step(page, "nach_cookie_klick", save_html=True)
            return True
    print("    Kein Cookie-Dialog gefunden (eingeloggt oder schon akzeptiert).")
    return False


def get_scope(page):
    """Kommentar-Container: der Dialog mit Artikeln (Post oeffnet als Modal),
    sonst das ganze Dokument."""
    best = None
    for d in page.query_selector_all('div[role="dialog"]'):
        try:
            if d.query_selector('div[role="article"]'):
                best = d
        except Exception:
            continue
    return best or page.query_selector("html")


_CLICK_BUTTON_JS = """
(el, args) => {
  const re = new RegExp(args.pattern, 'i');
  const exclude = new Set(args.exclude);
  for (const b of el.querySelectorAll('[role="button"]')) {
    const t = (b.innerText || '').trim().replace(/\\s+/g, ' ');
    if (!t || exclude.has(t.toLowerCase())) continue;
    if (re.test(t)) {
      b.scrollIntoView({block: 'center'});
      b.click();
      return t;
    }
  }
  return null;
}
"""


def click_button_matching(scope, pattern: str, exclude=None) -> Optional[str]:
    try:
        return scope.evaluate(_CLICK_BUTTON_JS,
                              {"pattern": pattern, "exclude": exclude or EXPAND_EXCLUDE})
    except Exception as exc:
        print(f"    Button-Suche fehlgeschlagen: {exc}")
        return None


def count_articles(scope) -> int:
    try:
        return scope.evaluate("el => el.querySelectorAll('div[role=\"article\"]').length")
    except Exception:
        return -1


def switch_sort_to_all_comments(page, scope, stepper: Stepper) -> bool:
    clicked = click_button_matching(scope, SORT_BUTTON_RE, exclude=[])
    if not clicked:
        print("    Kein Sortier-Umschalter gefunden (evtl. schon 'Alle Kommentare').")
        return False
    print(f"    Sortier-Umschalter geklickt: {clicked!r}")
    page.wait_for_timeout(1500)
    # Menue rendert als Portal auf Dokumentebene -> auf der ganzen Seite suchen
    target = None
    try:
        target = page.evaluate("""(pattern) => {
            const re = new RegExp(pattern, 'i');
            for (const m of document.querySelectorAll('[role="menuitem"]')) {
              const t = (m.innerText || '').trim().replace(/\\s+/g, ' ');
              if (re.test(t)) { m.click(); return t; }
            }
            return null;
        }""", SORT_TARGET_RE)
    except Exception as exc:
        print(f"    Menue-Klick fehlgeschlagen: {exc}")
    if target:
        print(f"    Sortierung umgestellt auf: {target!r}")
        page.wait_for_timeout(2500)
        stepper.step(page, "sortierung_alle_kommentare", save_html=True)
        return True
    print("    WARNUNG: Menuepunkt 'Alle Kommentare' nicht gefunden — "
          "Reihenfolge bleibt 'Relevanteste'; ggf. fehlen ausgeblendete Kommentare!")
    stepper.step(page, "sortier_menue_ohne_treffer", save_html=True)
    return False


def expand_everything(page, stepper: Stepper, drain, max_rounds: int) -> None:
    """Klickt alle Auffalt-Buttons (weitere Kommentare / Antworten / Mehr anzeigen),
    bis nichts mehr da ist. www = AJAX, kein Seitenwechsel."""
    idle = 0
    for round_no in range(1, max_rounds + 1):
        scope = get_scope(page)  # nach React-Re-Renders neu greifen
        clicked = click_button_matching(scope, EXPAND_RE)
        if clicked:
            idle = 0
            page.wait_for_timeout(1200)
            drain()
            print(f"    Runde {round_no}: {clicked!r} geklickt "
                  f"({count_articles(scope)} Artikel sichtbar)")
            if round_no % 10 == 0:
                stepper.step(page, f"auffalten_runde_{round_no}")
            continue
        # nichts gefunden: ans Ende scrollen (Lazy-Loading) und nochmal pruefen
        idle += 1
        try:
            scope.evaluate(
                "el => { const a = el.querySelectorAll('div[role=\"article\"]');"
                " if (a.length) a[a.length-1].scrollIntoView({block:'center'}); }")
        except Exception:
            pass
        page.wait_for_timeout(1500)
        drain()
        if idle >= 3:
            print(f"    Auffalten beendet nach {round_no} Runde(n), "
                  f"{count_articles(get_scope(page))} Artikel sichtbar.")
            return
    print(f"    WARNUNG: --max-rounds ({max_rounds}) erreicht, "
          f"moeglicherweise nicht alles aufgefaltet!")


# ---------------------------------------------------------------------------
# Extraktion
# ---------------------------------------------------------------------------

_EXTRACT_JS = """
(el) => {
  const links = [...el.querySelectorAll('a[href]')].map(a => ({
    t: (a.innerText || '').trim(),
    href: a.href,
  }));
  // Kommentartext: innerste div[dir=auto], die nicht in Links liegen
  // (Autorname und Zeitangabe sind Links)
  const body = [...el.querySelectorAll('div[dir="auto"]')]
    .filter(d => !d.closest('a') && !d.querySelector('div[dir="auto"]'))
    .map(d => d.innerText.trim())
    .filter(t => t);
  // Anhaenge (Bilder/GIFs/Sticker): grosse Bilder mit Alt-Text, keine Avatare
  const attachments = [...el.querySelectorAll('img[alt]')]
    .filter(i => (i.height || 0) > 48 && i.alt)
    .map(i => i.alt.trim());
  return {
    aria: el.getAttribute('aria-label') || '',
    links: links,
    text: [...new Set(body)].join('\\n').trim(),
    attachment_alt: [...new Set(attachments)].join(' | '),
  };
}
"""


def parse_aria(aria: str) -> Optional[dict]:
    aria = " ".join(aria.split())
    for pat in ARIA_PATTERNS:
        m = pat.match(aria)
        if m:
            d = m.groupdict()
            return {"author": d.get("author", ""),
                    "time_raw": d.get("time", ""),
                    "parent_author": d.get("parent", "") or "",
                    "is_reply": bool(d.get("parent"))}
    return None


def extract_comments(page, scope, rows, seen, outdir: Path,
                     harvester: TimeHarvester, capture_time: datetime) -> None:
    articles = scope.query_selector_all('div[role="article"][aria-label]')
    print(f"    {len(articles)} Artikel-Element(e) mit aria-Label gefunden.")
    skipped_aria = []
    for art in articles:
        try:
            data = art.evaluate(_EXTRACT_JS)
        except Exception as exc:
            print(f"    WARNUNG: Artikel nicht lesbar (stale?): {exc}")
            continue
        meta = parse_aria(data["aria"])
        if meta is None:
            if data["aria"]:
                skipped_aria.append(data["aria"][:120])
            continue

        hrefs = [l["href"] for l in data["links"]]
        cid, parent_id = extract_ids_from_links(hrefs)
        key = cid or hashlib.sha256(
            (meta["author"] + data["text"]).encode()).hexdigest()[:16]
        if key in seen:
            continue
        seen.add(key)

        profile_href = next((l["href"] for l in data["links"]
                             if l["t"] == meta["author"]), "")
        if not profile_href:
            profile_href = next((l["href"] for l in data["links"] if l["t"]), "")

        epoch = harvester.times.get(cid)
        if epoch is not None:
            ts_utc = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
            ts_source = "graphql_exact"
        else:
            parsed = parse_fb_time(meta["time_raw"], capture_time)
            ts_utc = parsed.astimezone(timezone.utc).isoformat() if parsed else ""
            ts_source = "computed_from_relative" if parsed else "unparsed"

        index = len(rows) + 1
        shot = f"comment_{index:03d}.png"
        try:
            art.scroll_into_view_if_needed(timeout=5000)
            art.screenshot(path=str(outdir / shot))
        except Exception as exc:
            print(f"    WARNUNG: Screenshot {shot} fehlgeschlagen: {exc}")
            shot = ""

        row = {
            "index": index,
            "comment_id": cid,
            "is_reply": "ja" if meta["is_reply"] else "nein",
            "parent_id": parent_id if meta["is_reply"] else "",
            "parent_author": meta["parent_author"],
            "author": meta["author"],
            "profile_url": normalize_profile_url(profile_href),
            "timestamp_raw": meta["time_raw"],
            "timestamp_utc": ts_utc,
            "timestamp_source": ts_source,
            "text": data["text"],
            "attachment_alt": data.get("attachment_alt", ""),
            "screenshot_file": shot,
            "aria_label": data["aria"],
        }
        rows.append(row)
        preview = (row["text"][:60] + "…") if len(row["text"]) > 60 else row["text"]
        print(f"    [{index:03d}] {row['author']} ({row['timestamp_raw']}, "
              f"{ts_source}): {preview!r}")
    if skipped_aria:
        path = outdir / "debug_skipped_arias.json"
        path.write_text(json.dumps(skipped_aria, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"    {len(skipped_aria)} Artikel ohne Kommentar-aria-Muster "
              f"uebersprungen -> {path.name}")


# ---------------------------------------------------------------------------
# Ausgabe: CSV + Manifest
# ---------------------------------------------------------------------------

CSV_FIELDS = ["index", "comment_id", "is_reply", "parent_id", "parent_author",
              "author", "profile_url", "timestamp_raw", "timestamp_utc",
              "timestamp_source", "text", "attachment_alt", "screenshot_file",
              "aria_label"]


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
    manifest["timestamp_sources"] = {
        src: sum(1 for r in rows if r["timestamp_source"] == src)
        for src in {r["timestamp_source"] for r in rows}
    }
    manifest["files_sha256"] = {
        p.name: sha256_file(p)
        for p in sorted(outdir.iterdir())
        if p.is_file() and p.name != "manifest.json"
    }
    with open(outdir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\nErgebnis: {len(rows)} Kommentar(e)/Antwort(en)")
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

    www_url = to_www(args.url)
    print(f"Ziel-URL: {www_url}")

    stepper = Stepper(outdir)
    harvester = TimeHarvester()
    rows: list = []
    seen: set = set()
    manifest = {
        "purpose": "Beweissicherung Facebook-Kommentare",
        "requested_url": args.url,
        "loaded_url": www_url,
        "capture_started_utc": started.isoformat(),
        "python_version": platform.python_version(),
        "note_timestamps": (
            "timestamp_source=graphql_exact: exakte Kommentarzeit (Unix-Epoch) aus "
            "dem von Facebook mitgelieferten GraphQL-JSON. "
            "timestamp_source=computed_from_relative: aus der angezeigten "
            "Relativzeit (timestamp_raw) relativ zur Capture-Zeit berechnet "
            "(Naeherung in Granularitaet der Anzeige)."
        ),
    }
    try:
        from importlib.metadata import version
        manifest["playwright_version"] = version("playwright")
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

        # GraphQL-Antworten sammeln (liefern created_time beim Nachladen);
        # Body erst ausserhalb des Handlers lesen (sync-API)
        graphql_responses: list = []
        page.on("response",
                lambda r: graphql_responses.append(r) if "/api/graphql" in r.url else None)

        def drain():
            while graphql_responses:
                resp = graphql_responses.pop()
                try:
                    harvester.feed(resp.text())
                except Exception:
                    pass

        try:
            # 1) Post laden
            page.goto(www_url, wait_until="load", timeout=60000)
            page.wait_for_timeout(4000)  # React-Hydration abwarten
            stepper.step(page, "post_geladen", save_html=True)

            # 2) Cookie-Consent (nur ohne Login relevant); danach neu laden
            if handle_cookie_consent(page, stepper):
                page.goto(www_url, wait_until="load", timeout=60000)
                page.wait_for_timeout(4000)
                stepper.step(page, "post_nach_cookie_neu_geladen", save_html=True)

            scope = get_scope(page)
            diag = dump_diagnostics(page, scope, outdir, "nach_laden")

            if diag.get("n_articles", 0) == 0:
                body_text = ""
                try:
                    body_text = page.inner_text("body", timeout=5000)[:3000]
                except Exception:
                    pass
                if LOGIN_WALL_RE.search(body_text):
                    stepper.step(page, "login_wand", save_html=True)
                    print("\nFEHLER: Facebook verlangt einen Login fuer diesen Post.")
                    print("Abhilfe: einmalig  python save_login.py  ausfuehren und "
                          "erneut starten (state.json wird automatisch benutzt).")
                    manifest["aborted"] = "login_wall"
                    return 2
                print("\nWARNUNG: Keine Kommentar-Artikel gefunden — "
                      "Diagnose-Dateien (debug_structure_*.json, *.html) pruefen!")

            capture_time = datetime.now(timezone.utc).astimezone()

            # 3) Sortierung auf "Alle Kommentare" stellen
            print("\n== Sortierung umstellen ==")
            manifest["sorted_to_all_comments"] = \
                switch_sort_to_all_comments(page, get_scope(page), stepper)
            drain()

            # 4) Alles auffalten (Kommentare, Antworten, 'Mehr anzeigen')
            print("\n== Kommentare/Antworten auffalten ==")
            expand_everything(page, stepper, drain, args.max_rounds)
            stepper.step(page, "fertig_aufgefaltet", save_html=True)

            # 5) Gesamt-Screenshots
            try:
                page.screenshot(path=str(outdir / "full_page.png"), full_page=True)
            except Exception as exc:
                print(f"    WARNUNG: full_page.png fehlgeschlagen: {exc}")
            scope = get_scope(page)
            try:
                scope.screenshot(path=str(outdir / "full_dialog.png"))
            except Exception:
                pass
            manifest["page_title"] = page.title()
            manifest["final_url"] = page.url

            # 6) Extraktion
            print("\n== Kommentare extrahieren ==")
            drain()
            harvester.feed(page.content())  # initial eingebettetes Relay-JSON
            harvester.dump(outdir)
            dump_diagnostics(page, scope, outdir, "vor_extraktion")
            extract_comments(page, scope, rows, seen, outdir, harvester, capture_time)

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
    ap.add_argument("--max-rounds", type=int, default=500,
                    help="Max. Klicks beim Auffalten")
    ap.add_argument("--selftest", action="store_true",
                    help="Nur den Zeitstempel-Parser testen (kein Browser)")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
