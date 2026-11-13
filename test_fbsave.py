import codecs
import csv
import hashlib
import io
import itertools
import json
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import fbsave
from fbsave import (CSV_FIELDS, SORT_BUTTON_RE, SORT_TARGET_RE, build_comment_tree,
                    expand_everything, parse_aria, sort_title, write_outputs)


class CommentSortTest(unittest.TestCase):
    def test_sort_target_matches_the_title_only(self):
        for newest, all_comments, expected in (
            ("Neueste zuerst\nAlle Kommentare anzeigen, die neuesten zuerst.",
             "Alle Kommentare\ufeff\nAlle Kommentare anzeigen, auch potenziellen Spam.",
             "Alle Kommentare"),
            ("Newest\nShow all comments, newest first.",
             "All comments\ufeff\nShow all comments, including potential spam.",
             "All comments"),
        ):
            with self.subTest(title=expected):
                titles = [sort_title(option) for option in (newest, all_comments)]
                self.assertEqual([title for title in titles if SORT_TARGET_RE.fullmatch(title)],
                                 [expected])


class WriteOutputsTest(unittest.TestCase):
    def test_csv_and_json_preserve_comments_and_manifest_hash(self):
        comment = {
            "index": 1,
            "comment_id": "00123",
            "is_reply": "nein",
            "parent_id": "",
            "parent_author": "",
            "author": "Jörg Öster",
            "profile_url": "https://example.test/joerg",
            "timestamp_raw": "vor 5 Min.",
            "timestamp_utc": "2026-06-10T10:00:00+00:00",
            "timestamp_source": "graphql_exact",
            "text": 'Grüße, "Hallo" 👋\nZweite Zeile\r\n最後の行',
            "attachment_alt": "Bild mit Löwe 🦁",
            "screenshot_file": "comment_001.png",
            "aria_label": "Kommentar von Jörg Öster",
        }
        reply = dict(
            comment, index=2, comment_id="00124", is_reply="ja",
            parent_id=comment["comment_id"], parent_author=comment["author"],
            author="Zoë", profile_url="https://example.test/zoe", text="",
            attachment_alt="", screenshot_file="",
            aria_label="Antwort von Zoë an Jörg Öster",
        )
        nested_reply = dict(
            reply, index=3, comment_id="00125", parent_id=reply["comment_id"],
            parent_author=reply["author"], author="Clara", text="Danke!",
            profile_url="https://example.test/clara",
            aria_label="Antwort von Clara auf Zoës Antwort",
        )
        rows = [comment, reply, nested_reply]

        with TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            outdir = Path(directory)
            write_outputs(outdir, rows, {})

            csv_path = outdir / "comments.csv"
            self.assertTrue(csv_path.read_bytes().startswith(codecs.BOM_UTF8))
            with csv_path.open(newline="", encoding="utf-8-sig") as csv_file:
                reader = csv.DictReader(csv_file)
                self.assertEqual(reader.fieldnames, CSV_FIELDS)
                csv_rows = list(reader)

            json_path = outdir / "comments.json"
            json_bytes = json_path.read_bytes()
            json_text = json_bytes.decode("utf-8")
            json_rows = json.loads(json_text)
            self.assertEqual(json_rows, [dict(comment, replies=[
                dict(reply, replies=[dict(nested_reply, replies=[])]),
            ])])
            json_thread = [json_rows[0], json_rows[0]["replies"][0],
                           json_rows[0]["replies"][0]["replies"][0]]
            self.assertEqual(csv_rows, [
                {field: str(node[field]) for field in CSV_FIELDS}
                for node in json_thread
            ])
            self.assertTrue(all("replies" not in row for row in rows))
            self.assertIn("Grüße", json_text)
            self.assertIn("👋", json_text)
            self.assertTrue(json_text.startswith("[\n  {\n"))

            manifest = json.loads((outdir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["files_sha256"]["comments.json"],
                             hashlib.sha256(json_bytes).hexdigest())
            self.assertIn(f"📄 JSON:         {json_path}", output.getvalue())

    def test_tree_keeps_replies_with_missing_or_conflicting_parents(self):
        comment = dict(index=1, comment_id="1", is_reply="nein", parent_id="",
                       author="Anna", parent_author="")
        reply = dict(index=2, comment_id="2", is_reply="ja", parent_id="1",
                     author="Ben", parent_author="Anna")
        conflicting = dict(index=3, comment_id="3", is_reply="ja", parent_id="1",
                           author="Clara", parent_author="Ben")
        orphan = dict(index=4, comment_id="4", is_reply="ja", parent_id="9",
                      author="David", parent_author="Eva")

        self.assertEqual(build_comment_tree([reply, comment, conflicting, orphan]), [
            dict(comment, replies=[dict(reply, replies=[])]),
            dict(conflicting, replies=[]),
            dict(orphan, replies=[]),
        ])

    def test_tree_breaks_cyclic_parent_links(self):
        first = dict(index=1, comment_id="1", is_reply="ja", parent_id="2",
                     author="Anna", parent_author="Ben")
        second = dict(index=2, comment_id="2", is_reply="ja", parent_id="1",
                      author="Ben", parent_author="Anna")

        self.assertEqual(build_comment_tree([first, second]), [
            dict(second, replies=[dict(first, replies=[])]),
        ])

    def test_empty_capture_writes_empty_json_array(self):
        with TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            outdir = Path(directory)
            write_outputs(outdir, [], {})

            with (outdir / "comments.csv").open(newline="", encoding="utf-8-sig") as csv_file:
                reader = csv.DictReader(csv_file)
                self.assertEqual(reader.fieldnames, CSV_FIELDS)
                self.assertEqual(list(reader), [])
            self.assertEqual((outdir / "comments.json").read_text(encoding="utf-8"), "[]")


# ---------------------------------------------------------------------------
# Tests zu den Verdachtsstellen aus der Diff-Tour
# ---------------------------------------------------------------------------


def parent_map(tree, parent=None, out=None):
    """{index: index des Elternknotens oder None} aus dem JSON-Baum."""
    out = {} if out is None else out
    for node in tree:
        out.setdefault(node["index"], []).append(parent)
        parent_map(node["replies"], node["index"], out)
    return out


class CommentTreeRobustnessTest(unittest.TestCase):
    """h12: Ergebnis darf nicht von der Zeilenreihenfolge abhaengen, und kein
    Eintrag darf verloren gehen oder doppelt auftauchen."""

    def rows(self):
        return [
            dict(index=1, comment_id="1", is_reply="nein", parent_id="",
                 author="Anna", parent_author=""),
            dict(index=2, comment_id="2", is_reply="ja", parent_id="1",
                 author="Ben", parent_author="Anna"),
            dict(index=3, comment_id="3", is_reply="ja", parent_id="2",
                 author="Clara", parent_author="Ben"),
            dict(index=4, comment_id="4", is_reply="ja", parent_id="1",
                 author="David", parent_author="Anna"),
        ]

    def test_valid_thread_is_independent_of_row_order(self):
        expected = {1: [None], 2: [1], 3: [2], 4: [1]}
        for order in itertools.permutations(self.rows()):
            with self.subTest(order=[r["index"] for r in order]):
                self.assertEqual(parent_map(build_comment_tree(list(order))), expected)

    def test_every_row_appears_exactly_once_even_with_cycles(self):
        rows = self.rows() + [
            dict(index=5, comment_id="5", is_reply="ja", parent_id="7",
                 author="Eva", parent_author="Gil"),
            dict(index=6, comment_id="6", is_reply="ja", parent_id="5",
                 author="Finn", parent_author="Eva"),
            dict(index=7, comment_id="7", is_reply="ja", parent_id="6",
                 author="Gil", parent_author="Finn"),
        ]
        for order in itertools.permutations(rows):
            with self.subTest(order=[r["index"] for r in order]):
                found = parent_map(build_comment_tree(list(order)))
                self.assertEqual(sorted(found), list(range(1, 8)))
                self.assertTrue(all(len(p) == 1 for p in found.values()))


class ReplyParentAuthorTest(unittest.TestCase):
    """h12 + parse_aria: Antworten muessen beim richtigen Elternkommentar landen,
    auch wenn der Elternname auf -s endet (Genitiv)."""

    def nest(self, parent_name, aria):
        meta = parse_aria(aria)
        self.assertIsNotNone(meta, aria)
        comment = dict(index=1, comment_id="1", is_reply="nein", parent_id="",
                       author=parent_name, parent_author="")
        reply = dict(index=2, comment_id="2", is_reply="ja", parent_id="1",
                     author=meta["author"], parent_author=meta["parent_author"])
        return parent_map(build_comment_tree([comment, reply]))

    def test_reply_nests_under_parent(self):
        for parent_name, aria in (
            ("Zoë", "Antwort von Ben auf Zoës Kommentar (1 Std.)"),
            ("Andreas", "Antwort von Ben auf Andreas’ Kommentar (1 Std.)"),
            ("Andreas", "Antwort von Ben auf Andreas' Kommentar (1 Std.)"),
            # Genitiv ohne Apostroph bei Namen auf -s (ob Facebook das so
            # ausgibt, ist offen):
            ("Andreas", "Antwort von Ben auf Andreas Kommentar (1 Std.)"),
            ("James", "Reply by Ben to James's comment (1h)"),
            ("James", "Reply by Ben to James' comment (1h)"),
        ):
            with self.subTest(aria=aria):
                self.assertEqual(self.nest(parent_name, aria), {1: [None], 2: [1]})


def js_finder_matches(button_text: str) -> bool:
    """Bildet die Textpruefung aus find_sort_button() nach: erste Zeile,
    U+FEFF entfernen, trim(), Whitespace zusammenfassen, Regex mit Flag 'i'."""
    first_line = button_text.split("\n")[0].replace("\ufeff", "")
    normalized = " ".join(first_line.split())
    return re.search(SORT_BUTTON_RE, normalized, re.I) is not None


class SortButtonConsistencyTest(unittest.TestCase):
    """h8/h9: Jeder Button, dessen Titel (erste Zeile) als Sortier-Titel gilt,
    muss von find_sort_button() auch gefunden werden. Sonst bleibt die
    Sortierung unbestaetigt, obwohl sie umgestellt wurde."""

    def test_finder_accepts_every_button_whose_title_is_a_sort_title(self):
        for text in (
            "Relevanteste",
            "Relevanteste zuerst",
            "Alle Kommentare",
            "Alle Kommentare﻿",
            "All comments",
            # Button mit Untertitel, wie bei den Menuepunkten (ob Facebook den
            # Button selbst so rendert, ist offen):
            "Alle Kommentare\nAlle Kommentare anzeigen, auch potenziellen Spam.",
            "Relevanteste\nDie relevantesten Kommentare zuerst anzeigen.",
        ):
            with self.subTest(text=text):
                self.assertTrue(re.fullmatch(SORT_BUTTON_RE, sort_title(text), re.I))
                self.assertTrue(js_finder_matches(text))


class FakeFeed:
    """Simuliert Facebooks Nachladen: Jeder Scroll fordert den naechsten Block
    an, der erst nach `latency_ms` sichtbar wird. Die Zeit laeuft nur ueber
    page.wait_for_timeout()."""

    def __init__(self, batches, latency_ms, endless_button=None):
        self.visible = batches[0]
        self.pending = list(batches[1:])
        self.latency_ms = latency_ms
        self.endless_button = endless_button
        self.now = 0
        self.arrives_at = None
        self.scrolls = 0

    def wait_for_timeout(self, ms):
        self.now += ms
        if self.arrives_at is not None and self.now >= self.arrives_at:
            self.visible += self.pending.pop(0)
            self.arrives_at = None

    def scroll(self, _scope):
        self.scrolls += 1
        if self.pending and self.arrives_at is None:
            self.arrives_at = self.now + self.latency_ms

    def count(self, _scope):
        return self.visible

    def click(self, _scope, _pattern, exclude=None):
        return self.endless_button

    @property
    def total(self):
        return self.visible + sum(self.pending)


class ExpandEverythingTest(unittest.TestCase):
    """h10: Wann hoert die Nachladeschleife auf, und meldet sie das ehrlich?"""

    def run_feed(self, feed, max_rounds=500, count=None):
        stepper = mock.Mock()
        with mock.patch.object(fbsave, "get_scope", return_value=object()), \
                mock.patch.object(fbsave, "click_button_matching", feed.click), \
                mock.patch.object(fbsave, "count_articles", count or feed.count), \
                mock.patch.object(fbsave, "scroll_comment_list_to_end", feed.scroll), \
                redirect_stdout(io.StringIO()):
            return expand_everything(feed, stepper, lambda: None, max_rounds)

    def test_loads_everything_when_facebook_answers_quickly(self):
        feed = FakeFeed([20, 20, 20, 7], latency_ms=1000)
        total = feed.total
        self.assertTrue(self.run_feed(feed))
        self.assertEqual(feed.visible, total)

    def test_survives_a_slow_batch_within_three_scrolls(self):
        feed = FakeFeed([20, 20, 20, 7], latency_ms=4000)
        total = feed.total
        self.assertTrue(self.run_feed(feed))
        self.assertEqual(feed.visible, total)

    @unittest.expectedFailure
    def test_slower_batch_is_not_reported_as_complete(self):
        # Bekannte Grenze der Heuristik: Wartet Facebook laenger als
        # 3 × 1,5 s, gilt der Lauf als vollstaendig, obwohl Kommentare fehlen.
        # Ohne externes Ende-Signal (Zaehler, GraphQL-Cursor) nicht loesbar.
        feed = FakeFeed([20, 20, 20, 7], latency_ms=5000)
        total = feed.total
        finished = self.run_feed(feed)
        self.assertTrue(feed.visible == total or not finished)

    def test_endless_button_hits_max_rounds_and_reports_unfinished(self):
        feed = FakeFeed([20], latency_ms=1000, endless_button="Mehr anzeigen")
        self.assertFalse(self.run_feed(feed, max_rounds=25))

    def test_counting_failure_is_not_reported_as_complete(self):
        # count_articles() liefert bei Fehlern -1. Dann weiss niemand, ob
        # noch etwas nachgeladen wurde, also darf der Lauf nicht als
        # abgeschlossen gelten.
        feed = FakeFeed([20, 20, 20, 7], latency_ms=1000)
        self.assertFalse(self.run_feed(feed, count=lambda _scope: -1))
