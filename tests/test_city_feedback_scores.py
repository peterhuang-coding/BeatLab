"""Direction-specific tests for the v2 feedback revision scores.

Fixtures are synthetic: ten prepared payloads are produced by deep-copying the
compact prototype and overlaying each case (per continuation-a2 contract).
"""
from __future__ import annotations

import copy
import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from examples import ten_beat_scores as orig
from examples import city_feedback_scores as rev

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "city-prepared-compact.json")
DRUM_IDS = set(rev.DRUM_IDS)
KEY_IDS = {"piano", "vibes"}


def load_prepared():
    with open(FIXTURE, encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    for case in data["cases"]:
        p = copy.deepcopy(data["prototype"])
        p.update(case)
        out.append(p)
    return out


def by_id(score):
    return {t["id"]: t for t in score["tracks"]}


def phrase_tracks(score):
    return [t for t in score["tracks"] if t.get("drum") is False]


def placements_by_bar(score, section_name):
    sec = next(s for s in score["sections"] if s["name"] == section_name)
    base = sec["start_bar"] * 4
    bars = {}
    for p in score["sample_flip"]["placements"]:
        if base <= p["beat"] < base + sec["bars"] * 4:
            bars.setdefault(int((p["beat"] - base) // 4), []).append(p)
    return bars


def section_beats(score, section_name):
    sec = next(s for s in score["sections"] if s["name"] == section_name)
    return sec["start_bar"] * 4, sec["bars"] * 4


def drum_events_in_bar(tracks, bar):
    out = []
    for t in tracks:
        if t["id"] in DRUM_IDS:
            out += [e for e in t["events"]
                    if bar * 4 <= e["beat"] < (bar + 1) * 4]
    return out


class FeedbackScoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prepared = load_prepared()
        cls.v1 = [orig.build_score(p) for p in cls.prepared]
        cls.v2 = [rev.build_score(p) for p in cls.prepared]

    # -- generic validity ---------------------------------------------------
    def test_ten_scores_valid(self):
        self.assertEqual(len(self.v2), 10)
        for p, v1, s in zip(self.prepared, self.v1, self.v2):
            self.assertEqual(s["bpm"], float(p["bpm"]))
            self.assertEqual(s["bars"], int(p["bars"]))
            self.assertEqual(s["sections"], v1["sections"])
            self.assertEqual(s["sample_flip"]["slices"],
                             v1["sample_flip"]["slices"])
            self.assertEqual(s["sample_flip"]["source_records"],
                             v1["sample_flip"]["source_records"])
            self.assertEqual(s["sample_root"], p["sample_root"])
            self.assertEqual(s["tail_seconds"], v1["tail_seconds"])
            self.assertEqual(s["fade_seconds"], v1["fade_seconds"])
            self.assertEqual(s["license"], v1["license"])
            total = s["bars"] * 4
            for tr in s["tracks"]:
                self.assertTrue(math.isfinite(tr["gain_db"]))
                beats = [e["beat"] for e in tr["events"]]
                self.assertEqual(beats, sorted(beats))
                for e in tr["events"]:
                    self.assertTrue(math.isfinite(e["velocity"]))
                    self.assertTrue(0 < e["velocity"] <= 1)
                    self.assertTrue(0 <= e["beat"] <= total + 1e-9)

    def test_placements_sorted_bounded_unwrapped_and_mirrored(self):
        for s in self.v2:
            pls = s["sample_flip"]["placements"]
            slices = {x["id"]: x for x in s["sample_flip"]["slices"]}
            bounds = {x["name"]: (x["start_bar"] * 4,
                                  (x["start_bar"] + x["bars"]) * 4)
                      for x in s["sections"]}
            self.assertEqual(pls, sorted(pls, key=lambda p: p["beat"]))
            prev_end = 0.0
            for p in pls:
                self.assertLessEqual(p["duration_beats"],
                                     slices[p["slice_id"]]["target_beats"])
                sec = next(n for n, (a, b) in bounds.items()
                           if a - 1e-9 <= p["beat"] < b)
                a, b = bounds[sec]
                self.assertGreaterEqual(p["beat"], a - 1e-9)
                self.assertLessEqual(p["beat"] + p["duration_beats"],
                                     b + 1e-9)
                self.assertGreaterEqual(p["beat"], prev_end - 1e-9)
                prev_end = p["beat"] + p["duration_beats"]
            self.assertLessEqual(prev_end, s["bars"] * 4 + 1e-9)
            seen = sorted(
                (round(e["beat"], 6), round(e["duration_beats"], 6),
                 round(e["velocity"], 6), t["id"])
                for t in phrase_tracks(s) for e in t["events"])
            want = sorted((round(p["beat"], 6), round(p["duration_beats"], 6),
                           round(p["velocity"], 6), p["slice_id"])
                          for p in pls)
            self.assertEqual(seen, want)

    def test_bass_monophonic_and_active_slice_notes(self):
        for s in self.v2:
            bass = by_id(s).get("bass")
            self.assertIsNotNone(bass)
            slices = {x["id"]: int(x["bass_midi"])
                      for x in s["sample_flip"]["slices"]}
            pls = s["sample_flip"]["placements"]
            prev = None
            for e in bass["events"]:
                active = [p for p in pls if p["beat"] <= e["beat"]][-1]
                self.assertEqual(e["note"], slices[active["slice_id"]])
                if prev is not None:
                    self.assertGreaterEqual(
                        e["beat"], prev[0] + prev[1] - 1e-9)
                prev = (e["beat"], e["duration_beats"])

    def test_no_added_keys_over_phrases(self):
        for s in self.v2:
            windows = [(p["beat"], p["beat"] + p["duration_beats"])
                       for p in s["sample_flip"]["placements"]]
            for tr in s["tracks"]:
                if tr["id"] not in KEY_IDS:
                    continue
                for e in tr["events"]:
                    self.assertFalse(
                        any(a - 1e-9 <= e["beat"] < b - 1e-9
                            for a, b in windows),
                        msg=f"key event over phrase in {s['metadata']}")

    def test_metadata_v2(self):
        for s in self.v2:
            md = s["metadata"]
            self.assertEqual(md["collection_id"],
                             "city-afterimages-20261013-v2")
            self.assertEqual(md["version"], "v2")
            self.assertIsNone(md["keep"])
            self.assertTrue(md["revision_summary"])
            self.assertTrue(any("一" <= ch <= "鿿"
                                for ch in md["revision_summary"]))
            self.assertNotIn("808", md["revision_summary"])

    def test_middle_switch_preserved(self):
        for v1, s in zip(self.v1, self.v2):
            self.assertEqual(s["metadata"]["middle_switch"],
                             v1["metadata"]["middle_switch"])
            self.assertEqual(s["metadata"]["direction"],
                             v1["metadata"]["direction"])

    def test_only_existing_samples_used(self):
        for p, s in zip(self.prepared, self.v2):
            files = {x["file"] for x in p["slices"]} | \
                    {i["sample"] for i in p["instruments"]}
            for tr in s["tracks"]:
                self.assertIn(tr["sample"], files)

    # -- isolation / determinism --------------------------------------------
    def test_input_and_parent_isolation(self):
        snapshot_feels = copy.deepcopy(orig.FEELS)
        snapshot_recipes = copy.deepcopy(orig.RECIPES)
        for p in self.prepared:
            before = copy.deepcopy(p)
            orig.build_score(p)
            rev.build_score(p)
            self.assertEqual(p, before)
        self.assertEqual(orig.FEELS, snapshot_feels)
        self.assertEqual(orig.RECIPES, snapshot_recipes)

    def test_deterministic_and_distinct_from_v1(self):
        for p, v1 in zip(self.prepared, self.v1):
            self.assertEqual(rev.build_score(p), rev.build_score(copy.deepcopy(p)))
            self.assertNotEqual(rev.build_score(p), v1)

    def test_ten_arrangements_pairwise_distinct(self):
        sigs = set()
        for s in self.v2:
            sig = (tuple((p["slice_id"], p["beat"]) for p in
                         s["sample_flip"]["placements"]),
                   tuple(sorted((t["id"], tuple(
                       round(e["beat"], 4) for e in t["events"]))
                       for t in s["tracks"] if t["id"] in DRUM_IDS)))
            self.assertNotIn(sig, sigs)
            sigs.add(sig)

    # -- direction 01 --------------------------------------------------------
    def test_01_drill_halftime(self):
        v1, s = self.v1[0], self.v2[0]
        self.assertEqual(s["sample_flip"]["placements"],
                         v1["sample_flip"]["placements"])  # motif retained
        t = by_id(s)
        base, span = section_beats(s, "verse")
        snares = [e["beat"] for e in t["snare"]["events"]
                  if base <= e["beat"] < base + span]
        self.assertTrue(snares)
        for b in snares:
            self.assertAlmostEqual((b - base) % 4, 2.0, places=4)
        kicks = [e["velocity"] for e in t["kick"]["events"]
                 if base <= e["beat"] < base + span]
        self.assertGreaterEqual(max(kicks), 0.95)
        self.assertLessEqual(min(kicks), 0.6)  # transient contrast
        rbase, rspan = section_beats(s, "return")
        last = rbase + rspan - 4
        roll = [e for e in t["hat"]["events"]
                if last + 3 - 1e-9 <= e["beat"] < last + 4]
        self.assertGreaterEqual(len(roll), 3)

    # -- direction 02 --------------------------------------------------------
    def test_rephrases_are_section_surgical(self):
        cases = {
            1: ("02", {"middle"}),
            5: ("06", {"verse", "drop", "return"}),
            6: ("07", {"funk", "switch", "return"}),
            9: ("10", {"verse", "middle", "reprise"}),
        }
        for idx, (_name, changed) in cases.items():
            v1, s = self.v1[idx], self.v2[idx]
            for sec in s["sections"]:
                a, b = sec["start_bar"] * 4, (sec["start_bar"] + sec["bars"]) * 4
                old = [p for p in v1["sample_flip"]["placements"]
                       if a <= p["beat"] < b]
                new = [p for p in s["sample_flip"]["placements"]
                       if a <= p["beat"] < b]
                if sec["name"] not in changed:
                    self.assertEqual(
                        new, old,
                        msg=f"track {idx+1} section {sec['name']} changed")

    def test_02_call_answer_middle_with_gaps(self):
        v1, s = self.v1[0], self.v2[1]
        bars = placements_by_bar(s, "middle")
        lead_bars = [i for i, ps in bars.items()
                     if any(p["slice_id"] == "lead_b" for p in ps)]
        ans_bars = [i for i, ps in bars.items()
                    if any(p["slice_id"] == "answer_b" for p in ps)]
        self.assertGreaterEqual(len(lead_bars), 2)
        self.assertEqual(sorted(lead_bars), sorted(ans_bars)[:len(lead_bars)])
        mbase, mspan = section_beats(s, "middle")
        used = {p["slice_id"] for p in s["sample_flip"]["placements"]
                if mbase <= p["beat"] < mbase + mspan}
        self.assertEqual(used, {"lead_b", "answer_b"})
        self.assertIn(2, set(range(4)) - set(bars))  # full-bar rest
        t = by_id(s)
        self.assertNotIn("piano", t)
        self.assertNotIn("vibes", t)
        self.assertNotIn("snare", t)
        self.assertIn("rim", t)
        v1_drums = sum(len(x["events"]) for x in self.v1[1]["tracks"]
                       if x["id"] in DRUM_IDS)
        v2_drums = sum(len(x["events"]) for x in s["tracks"]
                       if x["id"] in DRUM_IDS)
        self.assertLess(v2_drums, v1_drums)

    # -- direction 03 --------------------------------------------------------
    def test_03_frozen_phrases_and_primary_kit_timing(self):
        v1, s = self.v1[2], self.v2[2]
        self.assertEqual(s["sample_flip"]["placements"],
                         v1["sample_flip"]["placements"])
        a, b = by_id(v1), by_id(s)
        for layer in ("kick", "snare"):
            self.assertEqual([e["beat"] for e in a[layer]["events"]],
                             [e["beat"] for e in b[layer]["events"]])
        for tr in s["tracks"]:
            if tr["id"] in DRUM_IDS:
                self.assertAlmostEqual(
                    tr["gain_db"], a[tr["id"]]["gain_db"] - 4)
        self.assertAlmostEqual(b["bass"]["gain_db"],
                               a["bass"]["gain_db"] - 2)
        self.assertEqual(b["bass"]["events"], a["bass"]["events"])
        for sid in {p["slice_id"] for p in
                    s["sample_flip"]["placements"]}:
            self.assertEqual(b[sid], a[sid])
        # small variations really happened
        self.assertGreater(len(b["hat"]["events"]),
                           len(a["hat"]["events"]))
        self.assertLess(len(b["rim"]["events"]), len(a["rim"]["events"]))

    # -- direction 04 --------------------------------------------------------
    def test_04_sparse_no_clap_wall(self):
        v1, s = self.v1[3], self.v2[3]
        self.assertEqual(s["sample_flip"]["placements"],
                         v1["sample_flip"]["placements"])
        a, b = by_id(v1), by_id(s)
        self.assertNotIn("clap", b)
        for tr in s["tracks"]:
            if tr["id"] in DRUM_IDS:
                self.assertLessEqual(tr["gain_db"],
                                     a[tr["id"]]["gain_db"] - 6 + 1e-9)
        self.assertLess(
            sum(len(t["events"]) for t in s["tracks"] if t["id"] == "hat"),
            sum(len(t["events"]) for t in v1["tracks"] if t["id"] == "hat"))

    # -- direction 05 --------------------------------------------------------
    def test_05_halftime_anchor_pocket_quiet_turn(self):
        v1, s = self.v1[4], self.v2[4]
        self.assertEqual(s["sample_flip"]["placements"],
                         v1["sample_flip"]["placements"])
        t = by_id(s)
        base, span = section_beats(s, "verse")
        for e in t["snare"]["events"]:
            if base <= e["beat"] < base + span:
                self.assertAlmostEqual((e["beat"] - base) % 4, 2.0, places=4)
        kicks = {}
        for e in t["kick"]["events"]:
            if base <= e["beat"] < base + span:
                bar = int((e["beat"] - base) // 4)
                kicks.setdefault(bar % 2, set()).add(
                    round((e["beat"] - base) % 4, 3))
        self.assertNotEqual(kicks[0], kicks[1])  # varied two-bar pocket
        msec = next(x for x in s["sections"] if x["name"] == "middle")
        silent = [bar for bar in range(msec["start_bar"],
                                       msec["start_bar"] + msec["bars"])
                  if not drum_events_in_bar(s["tracks"], bar)]
        self.assertTrue(silent)  # intentional quiet turn
        self.assertLess(len(t["bass"]["events"]),
                        len(by_id(v1)["bass"]["events"]))
        keys_v1 = sum(len(x["events"]) for x in v1["tracks"]
                      if x["id"] in KEY_IDS)
        keys_v2 = sum(len(x["events"]) for x in s["tracks"]
                      if x["id"] in KEY_IDS)
        self.assertLess(keys_v2, keys_v1)

    # -- direction 06 --------------------------------------------------------
    def test_06_repeatable_motif_pauses_and_gain(self):
        v1, s = self.v1[5], self.v2[5]
        a, b = by_id(v1), by_id(s)
        for tr in s["tracks"]:
            if tr["id"] in DRUM_IDS:
                self.assertLessEqual(tr["gain_db"],
                                     a[tr["id"]]["gain_db"] - 6 + 1e-9)
        self.assertNotIn("piano", b)
        used = [p["slice_id"] for p in s["sample_flip"]["placements"]]
        self.assertLessEqual(set(used), {"lead_b", "answer_b", "turn_b"})
        self.assertGreaterEqual(used.count("lead_b"), 4)
        bars = placements_by_bar(s, "verse")
        self.assertTrue(set(range(4)) - set(bars))  # pauses inside verse
        v1_drums = sum(len(x["events"]) for x in v1["tracks"]
                       if x["id"] in DRUM_IDS)
        v2_drums = sum(len(x["events"]) for x in s["tracks"]
                       if x["id"] in DRUM_IDS)
        self.assertLess(v2_drums, v1_drums)

    # -- direction 07 --------------------------------------------------------
    def test_07_two_bar_question_answer(self):
        v1, s = self.v1[6], self.v2[6]
        b = by_id(s)
        self.assertNotIn("clap", b)
        used = {p["slice_id"] for p in s["sample_flip"]["placements"]}
        self.assertLessEqual(len(used), 8)
        bars = placements_by_bar(s, "funk")
        for bar in range(8):
            self.assertEqual(len(bars[bar]), 1)
        for bar in (0, 2, 4, 6):
            self.assertEqual(bars[bar][0]["slice_id"], "lead_c")
        for bar in (1, 3, 5):
            self.assertEqual(bars[bar][0]["slice_id"], "answer_c")
        self.assertLess(
            sum(len(t["events"]) for t in s["tracks"] if t["id"] == "hat"),
            sum(len(t["events"]) for t in v1["tracks"] if t["id"] == "hat"))

    # -- direction 08 --------------------------------------------------------
    def test_08_only_drums_change(self):
        v1, s = self.v1[7], self.v2[7]
        a, b = by_id(v1), by_id(s)
        for sid in set(a) & set(b):
            if sid not in DRUM_IDS:
                self.assertEqual(b[sid], a[sid])
        for sid, tr in a.items():
            if sid in DRUM_IDS and sid in b:
                self.assertLess(b[sid]["gain_db"], tr["gain_db"])
        # drum patterns were actually revised
        self.assertNotEqual(
            [e["beat"] for e in a["kick"]["events"]],
            [e["beat"] for e in b["kick"]["events"]])
        msec = next(x for x in s["sections"] if x["name"] == "middle")
        silent = [bar for bar in range(msec["start_bar"],
                                       msec["start_bar"] + msec["bars"])
                  if not drum_events_in_bar(s["tracks"], bar)]
        self.assertTrue(silent)  # a stop
        self.assertEqual(s["sample_flip"]["placements"],
                         v1["sample_flip"]["placements"])

    # -- direction 09 --------------------------------------------------------
    def test_09_only_gains_change(self):
        v1, s = self.v1[8], self.v2[8]
        a, b = by_id(v1), by_id(s)
        self.assertEqual(set(a), set(b))
        for sid in a:
            self.assertEqual(b[sid]["events"], a[sid]["events"])
            if sid in DRUM_IDS:
                self.assertAlmostEqual(b[sid]["gain_db"],
                                       a[sid]["gain_db"] - 2)
            elif sid == "bass":
                self.assertAlmostEqual(b[sid]["gain_db"],
                                       a[sid]["gain_db"] - 1)
            else:
                self.assertEqual(b[sid]["gain_db"], a[sid]["gain_db"])
                self.assertEqual(b[sid], a[sid])
        self.assertEqual(s["sample_flip"]["placements"],
                         v1["sample_flip"]["placements"])

    # -- direction 10 --------------------------------------------------------
    def test_10_long_theme_returns(self):
        v1, s = self.v1[9], self.v2[9]
        b = by_id(s)
        used = [p["slice_id"] for p in s["sample_flip"]["placements"]]
        self.assertLessEqual(set(used),
                             {"lead_a", "answer_a", "turn_a", "turn_b"})
        self.assertGreaterEqual(used.count("lead_a"), 4)
        longs = [p for p in s["sample_flip"]["placements"]
                 if p["duration_beats"] == 4]
        self.assertGreaterEqual(len(longs), 2)
        self.assertNotIn("open_hat", b)
        self.assertLess(
            sum(len(t["events"]) for t in s["tracks"] if t["id"] == "hat"),
            sum(len(t["events"]) for t in v1["tracks"] if t["id"] == "hat"))
        keys_v1 = sum(len(x["events"]) for x in v1["tracks"]
                      if x["id"] in KEY_IDS)
        keys_v2 = sum(len(x["events"]) for x in s["tracks"]
                      if x["id"] in KEY_IDS)
        self.assertLess(keys_v2, keys_v1)


if __name__ == "__main__":
    unittest.main()
