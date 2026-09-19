import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import recipes
from beatgrid import BeatGrid


SR = 22050


def _silence(duration, sr=SR):
    return np.zeros(int(round(duration * sr)), dtype=np.float32)


def _add_spike(audio, at, sr=SR, amp=0.8, width=80):
    center = int(round(at * sr))
    lo = max(0, center - width)
    hi = min(len(audio), center + width + 1)
    if hi > lo:
        audio[lo:hi] += amp * np.hanning(hi - lo).astype(np.float32)


def _render_120bpm_16s(sr=SR):
    audio = _silence(16.0, sr)
    for t in np.arange(0.5, 16.0, 0.5):
        _add_spike(audio, float(t), sr, amp=1.0)
    for bar in range(8):
        for sixteenth in range(1, 4):
            t = 0.5 + bar * 2.0 + sixteenth * 0.125
            _add_spike(audio, t, sr, amp=0.45, width=35)
    return audio


class TimingRecipesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.source = Path(tmp.name) / "source.wav"
        self.source.touch()

    def test_slice_exact_count_endpoints_and_mocked_snaps(self):
        audio = _silence(30.0)
        targets = [10.0 + 8.0 * i / 16 for i in range(1, 16)]
        snapped = [t + 0.035 for t in targets]

        def fake_onset_detect(*args, **kwargs):
            self.assertEqual(kwargs.get("units"), "samples")
            y = kwargs["y"]
            sr_out = kwargs["sr"]
            crop_start = int(round(10.0 * sr_out))
            return np.array([round(t * sr_out) - crop_start for t in snapped])

        with mock.patch.object(recipes.common, "load_audio_mono", return_value=(audio, SR)), \
             mock.patch("librosa.onset.onset_detect", side_effect=fake_onset_detect):
            bounds = recipes.slice_hero_chops(self.source, 10.0, 18.0, n=16)

        self.assertEqual(len(bounds), 17)
        self.assertEqual(bounds[0], 10.0)
        self.assertEqual(bounds[-1], 18.0)
        self.assertEqual(len(set(round(x, 12) for x in bounds)), 17)
        self.assertTrue(all(bounds[i] < bounds[i + 1] for i in range(16)))
        np.testing.assert_allclose(bounds[1:-1], snapped, atol=1.0/SR)

    def test_slice_repeated_onsets_do_not_create_zero_width_slices(self):
        audio = _silence(30.0)
        for t in (10.7, 10.71, 12.2, 12.21):
            _add_spike(audio, t, SR, amp=1.0)

        with mock.patch.object(recipes.common, "load_audio_mono", return_value=(audio, SR)):
            bounds = recipes.slice_hero_chops(self.source, 10.0, 14.0, n=17)

        self.assertEqual(len(bounds), 18)
        self.assertEqual(bounds[0], 10.0)
        self.assertEqual(bounds[-1], 14.0)
        self.assertEqual(len(set(round(x, 12) for x in bounds)), 18)
        self.assertTrue(all(bounds[i] < bounds[i + 1] for i in range(17)))

    def test_slice_invalid_arguments_raise_before_io(self):
        with mock.patch.object(recipes.common, "load_audio_mono", side_effect=AssertionError("unexpected I/O")):
            with self.assertRaises(ValueError):
                recipes.slice_hero_chops(Path("missing.wav"), -1.0, 1.0, 4)
            with self.assertRaises(ValueError):
                recipes.slice_hero_chops(Path("missing.wav"), 1.0, 1.0, 4)
            with self.assertRaises(ValueError):
                recipes.slice_hero_chops(Path("missing.wav"), float("nan"), 1.0, 4)
            with self.assertRaises(ValueError):
                recipes.slice_hero_chops(Path("missing.wav"), 0.0, 1.0, 0)
            with self.assertRaises(ValueError):
                recipes.slice_hero_chops(Path("missing.wav"), 0.0, 1.0, 129)
            with self.assertRaises(ValueError):
                recipes.slice_hero_chops(Path("missing.wav"), 0.0, 1.0, True)

    def test_slice_load_failure_uses_equal_grid(self):
        with mock.patch.object(recipes.common, "load_audio_mono", side_effect=RuntimeError("boom")):
            bounds = recipes.slice_hero_chops(Path("missing.wav"), 2.0, 6.0, n=8)
        self.assertEqual(bounds, [2.0 + 0.5 * i for i in range(9)])

    def test_slice_does_not_compress_truncated_region(self):
        audio = _silence(20.0)
        with mock.patch.object(recipes.common, "load_audio_mono", return_value=(audio, SR)):
            with self.assertRaises(ValueError):
                recipes.slice_hero_chops(self.source, 0.0, 30.0, n=8)

    def test_hero_onset_source_bars_maps_to_step_four_not_three(self):
        audio = _render_120bpm_16s()
        with mock.patch.object(recipes.common, "load_audio_mono", return_value=(audio, SR)), \
             mock.patch("librosa.onset.onset_detect", return_value=np.array([.5, 1., 1.5, 2.])):
            steps = recipes.hero_onset_steps(
                self.source, 0.0, 16.0, bpm=92.0, source_bars=8
            )

        self.assertIn(0, steps)
        self.assertIn(4, steps)
        self.assertIn(8, steps)
        self.assertIn(12, steps)
        self.assertNotIn(3, steps)
        self.assertTrue(all(0 <= step <= 15 for step in steps))

    def test_hero_onset_nonzero_crop_start_uses_relative_times(self):
        audio = _silence(30.0)

        def fake_onset_detect(*args, **kwargs):
            seg = kwargs["y"]
            self.assertAlmostEqual(len(seg) / SR, 16.0, delta=2.0 / SR)
            return np.array([0.5, 1.0])

        with mock.patch.object(recipes.common, "load_audio_mono", return_value=(audio, SR)), \
             mock.patch("librosa.onset.onset_detect", side_effect=fake_onset_detect):
            steps = recipes.hero_onset_steps(
                self.source, 10.0, 26.0, bpm=92.0, source_bars=8
            )
        self.assertEqual(steps, {4, 8})

    def test_hero_onset_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing.wav"
            self.assertEqual(
                recipes.hero_onset_steps(missing, 0.0, 4.0, 120.0), set()
            )

    def test_hero_onset_invalid_source_bars_returns_empty(self):
        audio = _silence(4.0)
        with mock.patch.object(recipes.common, "load_audio_mono", return_value=(audio, SR)):
            self.assertEqual(
                recipes.hero_onset_steps(
                    self.source, 0.0, 4.0, 120.0, source_bars=0
                ),
                set(),
            )

    def test_hero_onset_filters_bad_detections(self):
        audio = _silence(4.0)

        def fake_onset_detect(*args, **kwargs):
            return np.array([-0.5, float("nan"), 5.0, 0.5])

        with mock.patch.object(recipes.common, "load_audio_mono", return_value=(audio, SR)), \
             mock.patch("librosa.onset.onset_detect", side_effect=fake_onset_detect):
            steps = recipes.hero_onset_steps(self.source, 0.0, 4.0, 120.0)
        self.assertEqual(steps, {4})

    def test_hero_onset_legacy_target_bpm_remains_supported(self):
        audio = _silence(4.0)
        _add_spike(audio, 0.5, SR, amp=1.0)
        with mock.patch.object(recipes.common, "load_audio_mono", return_value=(audio, SR)), \
             mock.patch("librosa.onset.onset_detect", return_value=np.array([.5, 1., 1.5, 2.])):
            steps = recipes.hero_onset_steps(self.source, 0.0, 4.0, bpm=120.0)
        self.assertIn(4, steps)

    def test_source_grid_converts_mocked_local_beats_to_absolute_times(self):
        audio = _silence(8.0)
        local_beats = np.array([0.0, 0.5, 1.0, 1.5, 2.0])
        captured = {}

        def fake_fit(events, **kwargs):
            captured["events"] = list(events)
            captured["kwargs"] = kwargs
            return BeatGrid(kwargs["start_sec"], kwargs["end_sec"], 0.5, 0.0, 0.9,
                            "provided_beats", "none", 5, 0.01)

        with mock.patch.object(recipes.common, "load_audio_mono", return_value=(audio, SR)), \
             mock.patch("librosa.beat.beat_track", return_value=(None, local_beats)), \
             mock.patch("beatgrid.fit_beat_grid", side_effect=fake_fit):
            grid = recipes.source_grid_for_region(
                self.source, 1.1, 3.1, expected_bpm=120.0
            )

        self.assertEqual(grid.start_sec, 1.1)
        self.assertEqual(grid.end_sec, 3.1)
        np.testing.assert_allclose(
            captured["events"], [1.1, 1.6, 2.1, 2.6, 3.1], atol=2.0 / SR
        )
        self.assertEqual(captured["kwargs"]["expected_bpm"], 120.0)
        self.assertEqual(captured["kwargs"]["source_kind"], "provided_beats")

    def test_source_grid_nonzero_crop_uses_actual_crop_start(self):
        audio = _silence(20.0)
        captured = {}

        def fake_fit(events, **kwargs):
            captured["events"] = list(events)
            return BeatGrid(10.0, 12.0, 0.5, 0.0, 0.9,
                            "provided_beats", "none", 5, 0.01)

        with mock.patch.object(recipes.common, "load_audio_mono", return_value=(audio, SR)), \
             mock.patch("librosa.beat.beat_track", return_value=(None, np.array([0.0, 0.5]))), \
             mock.patch("beatgrid.fit_beat_grid", side_effect=fake_fit):
            recipes.source_grid_for_region(self.source, 10.0, 12.0)

        np.testing.assert_allclose(captured["events"], [10.0, 10.5], atol=1.0 / SR)

    def test_source_grid_invalid_arguments_raise_before_io(self):
        with mock.patch.object(recipes.common, "load_audio_mono", side_effect=AssertionError("unexpected I/O")):
            with self.assertRaises(ValueError):
                recipes.source_grid_for_region(Path("missing.wav"), -1.0, 1.0)
            with self.assertRaises(ValueError):
                recipes.source_grid_for_region(Path("missing.wav"), 1.0, 1.0)
            with self.assertRaises(ValueError):
                recipes.source_grid_for_region(Path("missing.wav"), 0.0, 1.0, expected_bpm=0)
            with self.assertRaises(ValueError):
                recipes.source_grid_for_region(Path("missing.wav"), 0.0, 1.0, sr=0)

    def test_source_grid_missing_file_returns_confidence_zero_fallback(self):
        with mock.patch.object(recipes.common, "load_audio_mono", side_effect=FileNotFoundError()):
            grid = recipes.source_grid_for_region(Path("missing.wav"), 1.0, 3.0, expected_bpm=120)

        self.assertEqual(grid.start_sec, 1.0)
        self.assertEqual(grid.end_sec, 3.0)
        self.assertEqual(grid.confidence, 0.0)
        self.assertEqual(grid.source_kind, "fallback_equal")
        self.assertAlmostEqual(grid.beat_period_sec, 0.5)


if __name__ == "__main__":
    unittest.main()
