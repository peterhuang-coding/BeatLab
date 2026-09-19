from __future__ import annotations

import random
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import soundfile as sf

PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import arrangement  # noqa: E402
import common  # noqa: E402
import compose  # noqa: E402
import phrase_schedule  # noqa: E402
import render  # noqa: E402


class PhraseRenderIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bpm = 120.0
        self.sr = int(render.SR)
        self.sections = [
            {"name": "intro", "bars": 2},
            {"name": "hook", "bars": 2},
            {"name": "outro", "bars": 2},
        ]
        self.hero_rel = Path("library/hero.wav")
        self.hero_path = self.root / self.hero_rel
        self.hero_path.parent.mkdir(parents=True, exist_ok=True)
        t = np.arange(int(3.0 * self.sr), dtype=np.float64) / self.sr
        self.source_audio = (0.35 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)
        sf.write(self.hero_path, self.source_audio, self.sr, subtype="FLOAT")
        self.chops = [
            {"file": str(self.hero_rel), "start_sec": 0.15, "end_sec": 0.90,
             "pad": 1, "midi_note": 60},
            {"file": str(self.hero_rel), "start_sec": 1.05, "end_sec": 1.80,
             "pad": 2, "midi_note": 62},
        ]
        self.hero = {"id": "hero-moment", "asset_id": "asset-hero",
                     "start_sec": 0.15, "end_sec": 1.80, "stem": "source"}
        self.assets = {"asset-hero": {"id": "asset-hero",
                                      "library_path": str(self.hero_rel),
                                      "bpm": self.bpm}}

    def _spec(self, placements, vocals):
        return SimpleNamespace(
            run_id="integration-run", recipe_kind="chop", bpm=self.bpm, total_bars=6,
            beat_id="fixture", drum_pattern={}, bass_pattern={},
            chop_placements=placements, vocal_placements=vocals,
            sections=[SimpleNamespace(name=s["name"], bars=s["bars"], energy=0.7)
                      for s in self.sections],
        )

    def _stretch_target(self, event):
        for op in event["operations"]:
            if op.get("op") == "time_stretch":
                return int(round(float(op["target_seconds"]) * self.sr))
        return event["source_end_frame"] - event["source_start_frame"]

    def test_v1_phrase_round_trip_renders_real_scheduled_audio(self):
        recipe = {
            "kind": "chop", "seed": 24680, "groove_profile": "boom-bap",
            "bass_root": "A", "chops": [dict(c) for c in self.chops],
            "transform": {"bpm": self.bpm},
            "arrangement": {"phrase_scheduler": "v1",
                            "sections": [dict(s) for s in self.sections]},
        }
        before = self.hero_path.read_bytes()
        beat_s = 60.0 / self.bpm
        step_s = beat_s / 4.0
        phrase_frames = int(round(step_s * 4.0 * self.sr))

        with mock.patch.object(common, "ROOT", self.root):
            placements, vocals = compose.place_chops(
                random.Random(13), recipe, self.hero, [], self.assets, str(self.hero_rel))
            plan = phrase_schedule.plan_phrase_events(
                [dict(s) for s in self.sections], n_motifs=2,
                seed=recipe["seed"], phrase_steps=4)
            planned = plan["events"]

            self.assertEqual(vocals, [])
            self.assertEqual(len(placements), len(planned))
            self.assertGreaterEqual({int(ev["motif_id"]) for ev in planned}, {0, 1})
            self.assertEqual(
                {int(p["chop_index"]) for p in placements}, {0, 1})
            self.assertTrue(all(p["timing_basis"] == "phrase_schedule_v1"
                                for p in placements))

            bounds = []
            for ev in planned:
                start = (int(ev["bar"]) * 16 + int(ev["step"])) * step_s
                self.assertEqual(int(ev.get("duration_steps", 4)), 4)
                self.assertLessEqual(start + 4.0 * step_s, 6.0 * 4.0 * beat_s + 1e-9)
                bounds.append((start, start + 4.0 * step_s))
            for earlier, later in zip(bounds, bounds[1:]):
                self.assertLessEqual(earlier[1], later[0] + 1e-9)

            arr = arrangement.build_arrangement(
                self._spec(placements, vocals),
                {"recipe_id": "fixture:phrase", "kind": "chop"}, {})
            events = arrangement.track(arr, "track-samples")["events"]
            self.assertEqual(len(events), len(planned))
            self.assertEqual(arrangement.track(arr, "track-vocals")["events"], [])
            self.assertEqual(arrangement.track(arr, "track-drums")["events"], [])
            self.assertEqual(arrangement.track(arr, "track-bass")["events"], [])

            total_frames = int(round(24.0 * beat_s * self.sr))
            layers = render._render_layers(arr, self.root, total_frames)
            left, right = layers["chops"]
            mix = left + right
            self.assertEqual(left.shape, (total_frames,))
            self.assertEqual(right.shape, (total_frames,))
            self.assertGreater(np.count_nonzero(mix), 0)
            self.assertTrue(np.all(np.isfinite(mix)))
            for name in ("vocal", "drums", "bass"):
                x, y = layers[name]
                self.assertEqual(x.shape, (total_frames,))
                self.assertEqual(y.shape, (total_frames,))
                self.assertTrue(np.all(np.isfinite(x + y)))
                self.assertEqual(np.count_nonzero(x) + np.count_nonzero(y), 0)

            asset_map = arrangement.assets_by_id(arr)
            active_by_section = {"intro": 0, "hook": 0, "outro": 0}
            rendered_bounds = []
            for event, planned_event, placement in zip(events, planned, placements):
                self.assertEqual(event["section"], planned_event["section"])
                self.assertAlmostEqual(
                    float(event["start_beat"]),
                    int(planned_event["bar"]) * 4.0 + int(planned_event["step"]) / 4.0,
                    places=5)
                self.assertAlmostEqual(float(event["duration_beats"]), 1.0, places=5)
                self.assertAlmostEqual(float(placement["stretch_to"]), 4.0 * step_s, places=5)

                media = asset_map[event["media_asset_id"]]
                decoded, decoded_sr = sf.read(self.root / media["source_path"], always_2d=True)
                self.assertEqual(decoded_sr, self.sr)
                target_frames = self._stretch_target(event)
                self.assertEqual(target_frames, phrase_frames)
                self.assertLessEqual(abs(len(decoded) - target_frames), 1)
                self.assertTrue(np.all(np.isfinite(decoded)))
                self.assertGreater(np.count_nonzero(np.abs(decoded) > 1e-8), 0)

                start = int(round(float(event["start_beat"]) * beat_s * self.sr))
                stop = min(total_frames, start + target_frames)
                window = np.abs(mix[start:stop]) > 1e-8
                self.assertGreater(np.count_nonzero(window), 0)
                active = np.flatnonzero(window)
                active_start, active_end = start + int(active[0]), start + int(active[-1]) + 1
                rendered_bounds.append((active_start, active_end))
                section_no = {"intro": 0, "hook": 2, "outro": 4}[event["section"]]
                sec_lo = int(round(section_no * 4.0 * beat_s * self.sr))
                sec_hi = int(round((section_no + 2) * 4.0 * beat_s * self.sr))
                active_by_section[event["section"]] += max(
                    0, min(active_end, sec_hi) - max(active_start, sec_lo))

            for earlier, later in zip(rendered_bounds, rendered_bounds[1:]):
                self.assertLessEqual(earlier[1], later[0] + 2)

            for bar in range(6):
                bar_start = int(round(bar * 4.0 * beat_s * self.sr))
                rest_start = int(round((bar * 4.0 + 3.0) * beat_s * self.sr))
                bar_end = int(round((bar + 1) * 4.0 * beat_s * self.sr))
                starts_here = {
                    int(round(start * self.sr)) for start, _ in bounds
                    if bar_start <= int(round(start * self.sr)) < bar_end}
                if rest_start not in starts_here:
                    self.assertEqual(np.count_nonzero(np.abs(mix[rest_start:bar_end])), 0)

            self.assertGreater(active_by_section["hook"], active_by_section["intro"])
            self.assertEqual(self.hero_path.read_bytes(), before)
            np.testing.assert_array_equal(sf.read(self.hero_path)[0], self.source_audio)
            processed = list((self.root / "processed").glob("*.wav"))
            self.assertTrue(processed)
            self.assertEqual(len(processed), len({e["media_asset_id"] for e in events}))


if __name__ == "__main__":
    unittest.main()
