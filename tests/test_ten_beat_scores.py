"""Focused tests for the 07-10 arrangement repair (single extra continuation).

Frozen review-gate defects repaired here:
  * 07 - overlapping phrase windows near beat 64 (return section)
  * 08 - phrase placed outside its section (intro; stale absolute beat 10)
  * 09 - overlapping phrase windows near beat 16 (twostep section)
  * 10 - overlapping phrase windows near beat 8 (verse section)

The repair only rewrites the four recipes' bar-cell placement; declared
bars/BPM/source data are untouched and scores 01-06 stay byte-semantically
identical (frozen-first-six.json).
"""
import copy
import hashlib
import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_candidate():
    spec = importlib.util.spec_from_file_location(
        "ten_beat_scores", ROOT / "examples" / "ten_beat_scores.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = _load_candidate()
FIXTURES = json.loads((ROOT / "tests" / "fixtures" / "ten-beat-synthetic-prepared.json").read_text())
BY_NUMBER = {p["number"]: p for p in FIXTURES}
FROZEN = json.loads((ROOT / "tests" / "fixtures" / "city-first-six-score-hashes.json").read_text())
REPAIRED = ["07", "08", "09", "10"]


class RepairBuildTests(unittest.TestCase):
    def test_07_through_10_build_cleanly(self):
        for number in REPAIRED:
            with self.subTest(number=number):
                # Raised "Overlapping phrase windows" / "outside its section"
                # before the repair; must now return a score deterministically.
                score = M.build_score(BY_NUMBER[number])
                self.assertEqual(score, M.build_score(BY_NUMBER[number]))

    def test_placements_never_overlap_for_07_through_10(self):
        for number in REPAIRED:
            with self.subTest(number=number):
                score = M.build_score(BY_NUMBER[number])
                placements = sorted(score["sample_flip"]["placements"],
                                    key=lambda x: x["beat"])
                self.assertGreaterEqual(
                    len({p["slice_id"] for p in placements}), 8)
                for earlier, later in zip(placements, placements[1:]):
                    self.assertLessEqual(
                        earlier["beat"] + earlier["duration_beats"],
                        later["beat"] + 1e-6)

    def test_every_placement_lives_inside_its_section(self):
        for number in REPAIRED:
            with self.subTest(number=number):
                score = M.build_score(BY_NUMBER[number])
                bounds = [(s["start_bar"] * 4,
                           (s["start_bar"] + s["bars"]) * 4)
                          for s in score["sections"]]
                for p in score["sample_flip"]["placements"]:
                    start, end = p["beat"], p["beat"] + p["duration_beats"]
                    covering = [b for b in bounds if b[0] <= start < b[1]]
                    self.assertEqual(len(covering), 1, p)
                    self.assertLessEqual(end, covering[0][1])

    def test_declared_bars_bpm_and_sources_preserved(self):
        for number in REPAIRED:
            with self.subTest(number=number):
                payload = copy.deepcopy(BY_NUMBER[number])
                score = M.build_score(payload)
                self.assertEqual(score["bars"], payload["bars"])
                self.assertEqual(score["bpm"], float(payload["bpm"]))
                self.assertEqual(score["sample_flip"]["source_records"],
                                 payload["source_records"])
                # Prepared payload is never mutated by the builder.
                self.assertEqual(payload, BY_NUMBER[number])

    def test_no_authored_phrases_dropped_or_added(self):
        for number in REPAIRED:
            with self.subTest(number=number):
                score = M.build_score(BY_NUMBER[number])
                expected = sum(len(section[2])
                               for section in M.RECIPES[number]["sections"])
                self.assertEqual(
                    len(score["sample_flip"]["placements"]), expected)

    def test_frozen_defect_points_repaired(self):
        # 07 return (bar 16 / beat 64): lead_c pair then a genuinely later bar.
        p07 = M.build_score(BY_NUMBER["07"])["sample_flip"]["placements"]
        at = {p["beat"]: p for p in p07}
        self.assertEqual(at[64]["slice_id"], "lead_c")
        self.assertEqual(at[64]["duration_beats"], 2.0)
        self.assertEqual(at[66]["slice_id"], "answer_c")

        # 08 intro: all three cell phrases inside the 2-bar (8-beat) section;
        # the stale beat-10 answer was relocated to local beat 2 (absolute 6).
        p08 = M.build_score(BY_NUMBER["08"])["sample_flip"]["placements"]
        intro = [p for p in p08 if p["beat"] < 8]
        self.assertTrue(
            all(p["beat"] + p["duration_beats"] <= 8 for p in intro))
        relocated = [p for p in intro
                     if p["slice_id"] == "answer_d"
                     and p["duration_beats"] == 0.75]
        self.assertEqual([p["beat"] for p in relocated], [6])
        self.assertNotIn(10, {p["beat"] for p in intro})

        # 09 twostep (bar 4 / beat 16): lead_a/answer_a pair on the downbeat.
        p09 = M.build_score(BY_NUMBER["09"])["sample_flip"]["placements"]
        at = {p["beat"]: p for p in p09}
        self.assertEqual(at[16]["slice_id"], "lead_a")
        self.assertEqual(at[16]["duration_beats"], 2.0)
        self.assertEqual(at[18]["slice_id"], "answer_a")

        # 10 verse (bar 2 / beat 8): lead_a/answer_a pair on the downbeat.
        p10 = M.build_score(BY_NUMBER["10"])["sample_flip"]["placements"]
        at = {p["beat"]: p for p in p10}
        self.assertEqual(at[8]["slice_id"], "lead_a")
        self.assertEqual(at[8]["duration_beats"], 2.0)
        self.assertEqual(at[10]["slice_id"], "answer_a")

    def test_ten_drum_signatures_remain_distinct(self):
        signatures = set()
        for payload in FIXTURES:
            score = M.build_score(payload)
            signature = [(t["id"],
                          [(e["beat"], e["duration_beats"])
                           for e in t["events"]])
                         for t in score["tracks"]
                         if t["id"] in ("kick", "snare", "rim", "clap")]
            signatures.add(
                hashlib.sha256(json.dumps(signature).encode()).hexdigest())
        self.assertEqual(len(signatures), 10)


class FirstSixPreservationTests(unittest.TestCase):
    def test_first_six_hashes_match_frozen(self):
        for payload in FIXTURES[:6]:
            with self.subTest(number=payload["number"]):
                actual = hashlib.sha256(
                    json.dumps(M.build_score(payload),
                               sort_keys=True).encode()).hexdigest()
                self.assertEqual(actual, FROZEN[payload["number"]])


if __name__ == "__main__":
    unittest.main()
