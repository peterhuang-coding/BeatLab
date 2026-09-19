import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from beatgrid import BeatGrid, fit_beat_grid, slice_n_boundaries


class BeatGridTests(unittest.TestCase):
    def test_equivalent_onset_grids_use_prior_with_float_tolerance(self):
        grid = fit_beat_grid([.1 + k for k in range(5)], start_sec=0,
                             end_sec=5, expected_bpm=60, source_kind="onsets")
        self.assertAlmostEqual(grid.beat_period_sec, 1.0)
        self.assertEqual(grid.ambiguity, "octave_possible")
        self.assertLessEqual(grid.confidence, .75)

    def test_exact_provided_beats(self):
        events = [1.1 + 0.5 * k for k in range(8)]
        g = fit_beat_grid(events, start_sec=1.0, end_sec=5.0)
        self.assertIsInstance(g, BeatGrid)
        self.assertEqual(g.source_kind, "provided_beats")
        self.assertAlmostEqual(g.beat_period_sec, 0.5)
        self.assertAlmostEqual(g.offset_sec, 0.1)
        self.assertEqual(g.ambiguity, "none")
        self.assertGreater(g.confidence, 0.9)
        self.assertLess(g.residual_sec, 1e-9)
        self.assertGreaterEqual(g.beat_count, 8)

    def test_jittered_beats_period_and_residual(self):
        rng = np.random.default_rng(4)
        events = list(
            1.0
            + np.cumsum(np.full(15, 0.6))
            + rng.normal(0, 0.008, 15)
        )
        g = fit_beat_grid(events, start_sec=1.0, end_sec=10.0)
        self.assertAlmostEqual(g.beat_period_sec, 0.6, delta=0.01)
        self.assertLess(g.residual_sec, 0.03)
        self.assertGreater(g.confidence, 0.5)

    def test_missing_beat_keeps_robust_median_period(self):
        events = [2.0, 2.6, 3.2, 4.4, 5.0, 5.6, 6.2]
        g = fit_beat_grid(events, start_sec=2.0, end_sec=6.3)
        self.assertAlmostEqual(g.beat_period_sec, 0.6, delta=0.001)
        self.assertAlmostEqual(g.offset_sec, 0.0)

    def test_missing_beat_normalizes_floating_phase_to_zero(self):
        period = 0.6000000000000001
        events = [period * k for k in range(8)]
        events.pop(4)
        g = fit_beat_grid(events, start_sec=0.0, end_sec=5.0)
        self.assertAlmostEqual(g.beat_period_sec, period, delta=1e-10)
        self.assertEqual(g.offset_sec, 0.0)
        self.assertLess(g.residual_sec, 1e-9)

    def test_sparse_and_all_outside(self):
        g = fit_beat_grid(
            [1.2, 1.8],
            start_sec=1.0,
            end_sec=5.0,
            expected_bpm=120,
        )
        self.assertEqual(g.source_kind, "fallback_equal")
        self.assertEqual(g.confidence, 0.0)
        self.assertAlmostEqual(g.beat_period_sec, 0.5)
        self.assertEqual(g.offset_sec, 0.0)

        with self.assertRaises(ValueError):
            fit_beat_grid([8.0, 9.0], start_sec=1.0, end_sec=5.0)

    def test_fallback_uses_caller_bpm_range(self):
        g = fit_beat_grid(
            [1.0, 2.0, 3.0],
            start_sec=0.0,
            end_sec=10.0,
            expected_bpm=20.0,
            min_bpm=15.0,
            max_bpm=30.0,
        )
        self.assertEqual(g.source_kind, "fallback_equal")
        self.assertAlmostEqual(g.beat_period_sec, 3.0)
        self.assertEqual(g.offset_sec, 0.0)

    def test_invalid_values_and_normalized_duplicates(self):
        with self.assertRaises(ValueError):
            fit_beat_grid(
                [float("nan"), 1.0, 2.0, 3.0],
                start_sec=0,
                end_sec=4,
            )
        with self.assertRaises(ValueError):
            fit_beat_grid(
                [1.0, float("inf"), 2.0, 3.0],
                start_sec=0,
                end_sec=4,
            )
        with self.assertRaises(ValueError):
            fit_beat_grid(
                [1.0, 2.0, 3.0, 4.0],
                start_sec=float("nan"),
                end_sec=4,
            )
        with self.assertRaises(ValueError):
            fit_beat_grid(
                [1.0, 2.0, 3.0, 4.0],
                start_sec=4,
                end_sec=4,
            )
        with self.assertRaises(ValueError):
            fit_beat_grid(
                [1.0, 2.0, 3.0, 4.0],
                start_sec=0,
                end_sec=4,
                min_bpm=120,
                max_bpm=60,
            )

    def test_unsorted_duplicates_are_normalized(self):
        events = [4.0, 1.0, 3.0, 2.0, 2.0, 1.0]
        g = fit_beat_grid(events, start_sec=0.0, end_sec=5.0)
        self.assertEqual(g.source_kind, "provided_beats")
        self.assertAlmostEqual(g.beat_period_sec, 1.0)
        self.assertAlmostEqual(g.offset_sec, 0.0)
        self.assertLess(g.residual_sec, 1e-10)

    def test_events_must_be_one_dimensional(self):
        with self.assertRaises(ValueError):
            fit_beat_grid(
                [[1.0, 2.0], [3.0, 4.0]],
                start_sec=0.0,
                end_sec=5.0,
            )

    def test_event_count_is_bounded(self):
        with self.assertRaises(ValueError):
            fit_beat_grid(
                np.arange(10001, dtype=float),
                start_sec=0.0,
                end_sec=10001.0,
            )

    def test_invalid_source_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            fit_beat_grid(
                [0.25 + 0.3 * k for k in range(8)],
                start_sec=0.0,
                end_sec=3.0,
                source_kind="junk",
            )

    def test_onset_half_time_not_forced_by_expected_bpm(self):
        events = [0.25 + 0.3 * k for k in range(20)]
        g = fit_beat_grid(
            events,
            start_sec=0.0,
            end_sec=6.0,
            expected_bpm=100.0,
            source_kind="onsets",
        )
        self.assertEqual(g.source_kind, "onsets")
        self.assertAlmostEqual(g.beat_period_sec, 0.3, delta=0.001)
        self.assertAlmostEqual(g.offset_sec, 0.25, delta=1e-9)
        self.assertLess(g.residual_sec, 0.001)

    def test_octave_ambiguity_and_confidence(self):
        # Alternating loud/soft-looking but equally supplied onset times are
        # genuinely compatible with quarter and doubled-half-time grids.
        events = np.arange(0.0, 8.0, 0.5)
        g = fit_beat_grid(
            events,
            start_sec=0.0,
            end_sec=8.0,
            expected_bpm=60.0,
            source_kind="onsets",
        )
        self.assertEqual(g.source_kind, "onsets")
        self.assertEqual(g.ambiguity, "octave_possible")
        self.assertLessEqual(g.confidence, 0.75)

    def test_poor_residual_and_coverage_reduce_confidence(self):
        events = np.sort(
            np.concatenate(
                [
                    np.arange(0.0, 8.0, 0.5),
                    np.arange(0.17, 8.0, 1.0),
                ]
            )
        )
        g = fit_beat_grid(
            events,
            start_sec=0.0,
            end_sec=8.0,
            source_kind="onsets",
        )
        self.assertEqual(g.source_kind, "onsets")
        self.assertLess(g.confidence, 0.9)

    def test_boundaries_exact_seventeen(self):
        g = fit_beat_grid(
            [1.1 + 0.5 * k for k in range(8)],
            start_sec=1.0,
            end_sec=5.0,
        )
        bounds = slice_n_boundaries(g, n=16)
        self.assertEqual(len(bounds), 17)
        self.assertEqual(bounds[0], 1.0)
        self.assertEqual(bounds[-1], 5.0)
        np.testing.assert_allclose(bounds, np.linspace(1.0, 5.0, 17))

        custom = slice_n_boundaries(g, end_sec=9.0, n=4)
        self.assertEqual(custom, [1.0, 3.0, 5.0, 7.0, 9.0])

    def test_boundaries_are_independent_of_phase(self):
        g1 = fit_beat_grid(
            [1.0 + 0.5 * k for k in range(8)],
            start_sec=1.0,
            end_sec=5.0,
        )
        g2 = fit_beat_grid(
            [1.1 + 0.5 * k for k in range(8)],
            start_sec=1.0,
            end_sec=5.0,
        )
        self.assertEqual(
            slice_n_boundaries(g1, n=8),
            slice_n_boundaries(g2, n=8),
        )

    def test_boundary_validation(self):
        g = fit_beat_grid(
            [1.1 + 0.5 * k for k in range(8)],
            start_sec=1.0,
            end_sec=5.0,
        )
        with self.assertRaises(ValueError):
            slice_n_boundaries(g, n=0)
        with self.assertRaises(ValueError):
            slice_n_boundaries(g, n=-1)
        with self.assertRaises(ValueError):
            slice_n_boundaries(g, n=2.0)
        with self.assertRaises(ValueError):
            slice_n_boundaries(g, n=True)
        with self.assertRaises(ValueError):
            slice_n_boundaries(g, end_sec=float("nan"))
        with self.assertRaises(ValueError):
            slice_n_boundaries(g, end_sec=0.5)


if __name__ == "__main__":
    unittest.main()
