import copy
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from phrase_schedule import (
    plan_phrase_events,
    SLOTS,
    RESERVED_STEP,
    STEPS_PER_BAR,
)


SECTIONS = [
    {"name": "intro", "bars": 4},
    {"name": "verse", "bars": 4},
    {"name": "hook", "bars": 4},
    {"name": "outro", "bars": 4},
]


class PhraseScheduleTests(unittest.TestCase):
    def plan(self, sections=SECTIONS, n_motifs=4, seed=7, **kwargs):
        return plan_phrase_events(sections, n_motifs, seed, **kwargs)

    def test_fixed_seed_identity_and_expected_counts(self):
        first = self.plan()
        second = self.plan()
        self.assertEqual(first, second)
        self.assertEqual([m["event_count"] for m in first["metrics"]], [4, 8, 12, 4])
        self.assertEqual(len(first["events"]), 28)
        self.assertEqual(first["events"][0]["motif_id"], 0)
        self.assertEqual(first["events"][12]["motif_id"], 0)
        self.assertEqual(first["events"][24]["motif_id"], 0)

    def test_other_seed_can_differ_and_fields_are_json_friendly(self):
        a = self.plan(seed=1)["events"]
        b = self.plan(seed=999)["events"]
        self.assertNotEqual(
            [e["motif_id"] for e in a], [e["motif_id"] for e in b]
        )
        for event in a + b:
            self.assertEqual(
                set(event),
                {
                    "section_index",
                    "section",
                    "bar",
                    "section_bar",
                    "step",
                    "motif_id",
                    "duration_steps",
                },
            )
            for value in event.values():
                self.assertIsInstance(value, (str, int))

    def test_slots_unique_terminal_rest_and_no_overlap(self):
        result = self.plan()
        by_bar = {}
        for event in result["events"]:
            self.assertIn(event["step"], SLOTS)
            self.assertLessEqual(
                event["step"] + event["duration_steps"], RESERVED_STEP
            )
            by_bar.setdefault(event["bar"], []).append(event)

        for bar_events in by_bar.values():
            self.assertLessEqual(len(bar_events), 3)
            ordered = sorted(bar_events, key=lambda e: e["step"])
            steps = [e["step"] for e in ordered]
            self.assertEqual(steps, sorted(set(steps)))
            for earlier, later in zip(ordered, ordered[1:]):
                self.assertLessEqual(
                    earlier["step"] + earlier["duration_steps"], later["step"]
                )

    def test_motif_range_and_section_local_max_run(self):
        result = self.plan(n_motifs=3)
        for event in result["events"]:
            self.assertIn(event["motif_id"], range(3))

        for metric in result["metrics"]:
            motifs = [e["motif_id"] for e in result["events"]
                      if e["section_index"] == metric["section_index"]]
            for a, b, c in zip(motifs, motifs[1:], motifs[2:]):
                self.assertFalse(a == b == c)

    def test_anchors_restart_on_theme_zero(self):
        result = self.plan()
        for section_index in (0, 2, 3):
            first = next(
                e
                for e in result["events"]
                if e["section_index"] == section_index
            )
            self.assertEqual(first["motif_id"], 0)
            self.assertEqual(first["step"], 0)
            self.assertEqual(first["section_bar"], 0)

    def test_hook_density_and_indexed_duplicate_section_metrics(self):
        result = self.plan(n_motifs=2, sections=SECTIONS[:3])
        metrics = result["metrics"]
        self.assertGreater(metrics[2]["density"], metrics[0]["density"])
        self.assertEqual(metrics[0]["density"], 4 * 4 / (4 * STEPS_PER_BAR))

        duplicate = [
            {"name": "verse", "bars": 1},
            {"name": "verse", "bars": 1},
        ]
        repeated = self.plan(sections=duplicate, n_motifs=2)
        self.assertEqual(
            [m["section_index"] for m in repeated["metrics"]], [0, 1]
        )
        self.assertEqual(
            [m["event_count"] for m in repeated["metrics"]], [2, 2]
        )

    def test_hook_actually_returns_to_theme_after_opening_bar(self):
        result = self.plan(
            sections=[
                {"name": "intro", "bars": 1},
                {"name": "hook", "bars": 4},
            ],
            n_motifs=2,
            seed=123,
        )
        hook_events = [e for e in result["events"] if e["section"] == "hook"]
        self.assertEqual(hook_events[0]["section_bar"], 0)
        self.assertEqual(hook_events[0]["step"], 0)
        self.assertEqual(hook_events[0]["motif_id"], 0)

        later_theme_events = [
            e for e in hook_events if e["section_bar"] >= 1 and e["motif_id"] == 0
        ]
        self.assertTrue(
            later_theme_events,
            "hook must recall motif 0 after its opening bar when possible",
        )

        for section_index in {0, 1}:
            for bar in range(4):
                motifs = [
                    e["motif_id"]
                    for e in hook_events
                    if e["section_bar"] == bar
                ]
                self.assertLessEqual(len(motifs), 3)
                if len(motifs) == 2:
                    self.assertNotEqual(motifs[0], motifs[1])

    def test_one_motif_hook_uses_rests_in_third_slot(self):
        result = self.plan(
            sections=[{"name": "hook", "bars": 2}], n_motifs=1, seed=3
        )
        self.assertEqual(len(result["events"]), 4)
        self.assertEqual({e["step"] for e in result["events"]}, {0, 4})
        self.assertTrue(all(e["motif_id"] == 0 for e in result["events"]))

    def test_one_motif_verse_uses_two_events_per_bar(self):
        result = self.plan(
            sections=[{"name": "verse", "bars": 3}], n_motifs=1, seed=4
        )
        self.assertEqual(len(result["events"]), 6)
        for bar in range(3):
            steps = [
                e["step"]
                for e in result["events"]
                if e["section_bar"] == bar
            ]
            self.assertEqual(steps, [0, 4])
        self.assertTrue(all(e["motif_id"] == 0 for e in result["events"]))

    def test_empty_section_list_is_valid(self):
        result = self.plan(sections=[], n_motifs=1)
        self.assertEqual(result["events"], [])
        self.assertEqual(result["metrics"], [])

    def test_zero_bars_section_is_empty_with_zero_density(self):
        sections = [
            {"name": "intro", "bars": 0},
            {"name": "hook", "bars": 1},
        ]
        result = self.plan(sections=sections, n_motifs=2)
        self.assertEqual(result["metrics"][0]["event_count"], 0)
        self.assertEqual(result["metrics"][0]["active_steps"], 0)
        self.assertEqual(result["metrics"][0]["density"], 0.0)
        self.assertEqual(result["events"][0]["bar"], 0)
        self.assertEqual(result["events"][0]["section_bar"], 0)

    def test_overrides_and_sustain_length(self):
        result = self.plan(
            sections=[{"name": "bridge", "bars": 1}],
            n_motifs=2,
            events_per_bar={"bridge": 0},
            phrase_steps=2,
        )
        self.assertEqual(result["events"], [])
        self.assertEqual(result["metrics"][0]["nominal_events_per_bar"], 0)

        active = self.plan(
            sections=[{"name": "bridge", "bars": 1}],
            n_motifs=2,
            events_per_bar={"bridge": 2},
            phrase_steps=2,
        )
        self.assertEqual(
            [e["duration_steps"] for e in active["events"]], [2, 2]
        )
        self.assertEqual(active["metrics"][0]["density"], 4 / STEPS_PER_BAR)

    def test_no_input_mutation(self):
        sections = copy.deepcopy(SECTIONS)
        overrides = {"hook": 2}
        self.plan(sections=sections, events_per_bar=overrides)
        self.assertEqual(sections, SECTIONS)
        self.assertEqual(overrides, {"hook": 2})

    def test_invalid_inputs(self):
        with self.assertRaises(TypeError):
            plan_phrase_events("not-sections", 1, 0)
        with self.assertRaises(TypeError):
            plan_phrase_events([{"bars": 1}], 1, 0)
        with self.assertRaises(ValueError):
            plan_phrase_events([{"name": "a", "bars": -1}], 1, 0)
        with self.assertRaises(ValueError):
            plan_phrase_events(SECTIONS, 0, 0)
        with self.assertRaises(ValueError):
            plan_phrase_events(SECTIONS, 1, math.nan)
        with self.assertRaises(ValueError):
            plan_phrase_events(SECTIONS, 1, 0, phrase_steps=5)
        with self.assertRaises(ValueError):
            plan_phrase_events(SECTIONS, 1, 0, events_per_bar={"hook": 4})
        with self.assertRaises(ValueError):
            plan_phrase_events(SECTIONS, 1, 0, events_per_bar={"hook": 2.0})


if __name__ == "__main__":
    unittest.main()
