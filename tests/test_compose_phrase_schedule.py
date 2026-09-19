import copy
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

import common as project_common
import compose
import phrase_schedule


class ComposePhraseScheduleTests(unittest.TestCase):
    def setUp(self):
        self.sections = [
            {"name": "intro", "bars": 2},
            {"name": "verse", "bars": 2},
            {"name": "hook", "bars": 2},
            {"name": "verse_variation", "bars": 2},
            {"name": "outro", "bars": 2},
        ]
        self.hero = {
            "id": "hero-moment",
            "asset_id": "asset-1",
            "start_sec": 10.0,
            "end_sec": 12.0,
            "stem": "source",
        }
        self.asset = {
            "id": "asset-1",
            "library_path": "library/hero.wav",
            "bpm": 120,
        }
        self.chops = [
            {"file": f"library/chop-{i}.wav", "start_sec": float(i), "end_sec": float(i + 1),
             "pad": i + 1, "midi_note": 60 + i}
            for i in range(4)
        ]

    def recipe(self, scheduler=None):
        recipe = {
            "kind": "chop",
            "seed": 12345,
            "groove_profile": "boom-bap",
            "bass_root": "A",
            "chops": copy.deepcopy(self.chops),
            "arrangement": {"sections": copy.deepcopy(self.sections)},
        }
        if scheduler is not None:
            recipe["arrangement"]["phrase_scheduler"] = scheduler
        return recipe

    def hero_only(self, placements):
        return [p for p in placements if p["sample_id"] == "asset-1"]

    def test_v1_schedules_intro_verse_hook_event_counts(self):
        recipe = self.recipe("v1")
        placements, vocals = compose.place_chops(
            random.Random(0), recipe, self.hero, [], {"asset-1": self.asset}, "library/hero.wav"
        )
        hero_events = self.hero_only(placements)
        counts = {name: 0 for name in ("intro", "verse", "hook")}
        for p in hero_events:
            counts[p["section"]] = counts.get(p["section"], 0) + 1
        self.assertEqual(counts["intro"], 2)
        self.assertEqual(counts["verse"], 4)
        self.assertEqual(counts["hook"], 6)
        self.assertEqual(vocals, [])

    def test_v1_terminal_hero_rest_and_stretch_spans_four_steps(self):
        recipe = self.recipe("v1")
        placements, _ = compose.place_chops(
            random.Random(0), recipe, self.hero, [], {"asset-1": self.asset}, "library/hero.wav"
        )
        outro = [p for p in self.hero_only(placements) if p["section"] == "outro"]
        self.assertEqual([(p["bar"], p["step"]) for p in outro], [(8, 0), (9, 0)])
        for p in outro:
            self.assertAlmostEqual(p["stretch_to"], 4.0 * (60.0 / 92.0 / 4.0), places=4)
        occupied = {(p["bar"], p["step"]) for p in self.hero_only(placements)}
        self.assertNotIn((9, 4), occupied)
        self.assertNotIn((9, 8), occupied)
        self.assertNotIn((9, 12), occupied)

    def test_v1_theme_anchor_and_no_section_run_over_two(self):
        recipe = self.recipe("v1")
        placements, _ = compose.place_chops(
            random.Random(0), recipe, self.hero, [], {"asset-1": self.asset}, "library/hero.wav"
        )
        hero_events = self.hero_only(placements)
        anchors = {(p["section"], p["bar"], p["step"]): p["chop_index"]
                   for p in hero_events if p["step"] == 0}
        self.assertEqual(anchors[("intro", 0, 0)], 0)
        self.assertEqual(anchors[("hook", 4, 0)], 0)
        self.assertEqual(anchors[("outro", 8, 0)], 0)
        by_section = {}
        for p in hero_events:
            by_section.setdefault(p["section"], []).append(p["chop_index"])
        for motifs in by_section.values():
            for a, b, c in zip(motifs, motifs[1:], motifs[2:]):
                self.assertFalse(a == b == c)

    def test_same_seed_same_placements(self):
        def make():
            recipe = self.recipe("v1")
            placements, _ = compose.place_chops(
                random.Random(999), recipe, self.hero, [], {"asset-1": self.asset}, "library/hero.wav"
            )
            return self.hero_only(placements)

        self.assertEqual(make(), make())

    def test_n_motifs_one_rests_before_third_consecutive_event(self):
        recipe = self.recipe("v1")
        recipe["chops"] = [copy.deepcopy(self.chops[0])]
        placements, _ = compose.place_chops(
            random.Random(0), recipe, self.hero, [], {"asset-1": self.asset}, "library/hero.wav"
        )
        hook = [p for p in self.hero_only(placements) if p["section"] == "hook"]
        self.assertEqual(
            [(p["bar"], p["step"]) for p in hook],
            [(4, 0), (4, 4), (5, 0), (5, 4)],
        )
        self.assertTrue(all(p["chop_index"] == 0 for p in hook))

    def test_v1_maps_planned_motifs_to_chops_and_marks_timing_basis(self):
        recipe = self.recipe("v1")
        placements, _ = compose.place_chops(
            random.Random(0), recipe, self.hero, [], {"asset-1": self.asset}, "library/hero.wav"
        )
        plan = phrase_schedule.plan_phrase_events(
            self.sections, n_motifs=4, seed=12345, phrase_steps=4
        )
        hero_events = self.hero_only(placements)
        self.assertEqual(len(hero_events), len(plan["events"]))
        for p, ev in zip(hero_events, plan["events"]):
            chop = self.chops[ev["motif_id"]]
            self.assertEqual(p["section"], ev["section"])
            self.assertEqual(p["bar"], ev["bar"])
            self.assertEqual(p["step"], ev["step"])
            self.assertEqual(p["chop_index"], ev["motif_id"])
            self.assertEqual(p["file"], chop["file"])
            self.assertEqual(p["start_sec"], chop["start_sec"])
            self.assertEqual(p["end_sec"], chop["end_sec"])
            self.assertEqual(p["timing_basis"], "phrase_schedule_v1")
        metrics = recipe["arrangement"]["phrase_schedule_metrics"]
        added_fields = {"scope", "density_note"}
        self.assertEqual(len(metrics), len(plan["metrics"]))
        for actual, pure in zip(metrics, plan["metrics"]):
            for key, value in pure.items():
                self.assertEqual(actual[key], value)
            self.assertEqual(actual["scope"], "hero_only")
            self.assertIn("supporting samples may still sound", actual["density_note"])
            self.assertEqual(set(actual) - set(pure), added_fields)

    def test_v1_uses_section_index_for_duplicate_section_names_and_mutations(self):
        sections = [
            {"name": "verse", "bars": 1, "mutation": {"lp_hz": 1000.0}},
            {"name": "verse", "bars": 1, "mutation": {"fade_out": True}},
        ]
        recipe = self.recipe("v1")
        recipe["arrangement"]["sections"] = copy.deepcopy(sections)
        recipe["mutations"] = [
            {"lp_hz": 1000.0},
            {"fade_out": True},
        ]
        placements, _ = compose.place_chops(
            random.Random(0), recipe, self.hero, [], {"asset-1": self.asset}, "library/hero.wav"
        )
        hero_events = self.hero_only(placements)
        plan = phrase_schedule.plan_phrase_events(sections, n_motifs=4, seed=12345, phrase_steps=4)
        self.assertEqual([(p["bar"], p["step"]) for p in hero_events],
                         [(e["bar"], e["step"]) for e in plan["events"]])
        first = [p for p in hero_events if p["bar"] == 0]
        second = [p for p in hero_events if p["bar"] == 1]
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertTrue(all(p["lp_hz"] == 1000.0 for p in first))
        self.assertTrue(all(p.get("lp_hz") is None for p in second))
        self.assertTrue(all(p["gain"] == .75 for p in first))
        self.assertTrue(all(abs(p["gain"] - .75) < 1e-9 for p in second))

    def test_legacy_default_and_explicit_off_are_identical(self):
        def make(scheduler):
            rng = random.Random(7)
            recipe = self.recipe(scheduler)
            placements, vocals = compose.place_chops(
                rng, recipe, self.hero, [], {"asset-1": self.asset}, "library/hero.wav"
            )
            return recipe, placements, vocals

        default_recipe, default_placements, default_vocals = make(None)
        off_recipe, off_placements, off_vocals = make("off")
        self.assertEqual(default_placements, off_placements)
        self.assertEqual(default_vocals, off_vocals)
        self.assertEqual(len(self.hero_only(default_placements)), 10)
        for recipe, placements in (
            (default_recipe, default_placements),
            (off_recipe, off_placements),
        ):
            self.assertNotIn("phrase_schedule_metrics", recipe["arrangement"])
            self.assertTrue(all("timing_basis" not in p for p in self.hero_only(placements)))
            self.assertTrue(all("stretch_to" not in p for p in self.hero_only(placements)))

    def test_loop_and_stem_onset_avoidance_uses_source_bars_eight(self):
        for kind in ("loop", "stem"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                relative_stem = "library/source.wav" if kind == "loop" else "library/source_drums.wav"
                stem_path = root / relative_stem
                stem_path.parent.mkdir(parents=True, exist_ok=True)
                stem_path.write_bytes(b"fake-audio")

                recipe = {
                    "kind": kind,
                    "groove_profile": "boom-bap",
                    "bass_root": 33,
                    "chops": [],
                    "transform": {"bpm": 92},
                    "arrangement": {"sections": [{"name": "intro", "bars": 1}]},
                }
                hero = {
                    "id": "m1",
                    "asset_id": "a1",
                    "start_sec": 0.0,
                    "end_sec": 16.0,
                    "stem": "source" if kind == "loop" else "drums",
                }
                asset = {"id": "a1", "library_path": "library/source.wav", "bpm": 120}

                class FakeSection:
                    def __init__(self, name, bars):
                        self.name = name
                        self.bars = bars

                def fake_build_sections(_kind, _bars_map):
                    self.assertEqual(_kind, kind)
                    self.assertEqual(_bars_map, {"intro": 1})
                    return [FakeSection("intro", 1)]

                def fake_stem_file(_path, stem):
                    self.assertEqual(_path, "library/source.wav")
                    self.assertEqual(stem, hero["stem"])
                    return relative_stem

                def fake_generate_drum_pattern(_rng, _sections, _profile, _swing, _tick, avoid):
                    self.assertEqual(avoid, {0: {0}})
                    return []

                def fake_generate_bass(_rng, _sections, root_value):
                    self.assertEqual(root_value, 33)
                    return []

                with mock.patch.object(compose.common, "ROOT", root), \
                        mock.patch.object(compose.recipes, "_seed", return_value=1), \
                        mock.patch.object(compose.recipes, "_stem_file", side_effect=fake_stem_file), \
                        mock.patch.object(
                            compose.recipes, "hero_onset_steps", return_value={0}
                        ) as onset, \
                        mock.patch.object(
                            compose, "build_sections", side_effect=fake_build_sections
                        ), \
                        mock.patch.object(
                            compose, "generate_drum_pattern", side_effect=fake_generate_drum_pattern
                        ), \
                        mock.patch.object(
                            compose, "generate_bass", side_effect=fake_generate_bass
                        ):
                    spec = compose.build_spec_for_recipe(
                        recipe, kind, "run-1", hero, [], {"a1": asset}, 92.0, None
                    )

                self.assertEqual(spec.total_bars, 1)
                self.assertEqual(spec.bpm, 92.0)
                self.assertEqual(onset.call_count, 1)
                self.assertEqual(onset.call_args.args[:3], (stem_path, 0.0, 16.0))
                self.assertEqual(onset.call_args.args[3], 92.0)
                self.assertEqual(onset.call_args.kwargs["source_bars"], 8)
                self.assertTrue(stem_path.is_file())


if __name__ == "__main__":
    unittest.main()
