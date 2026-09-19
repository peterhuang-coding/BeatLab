import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "pipeline")))

from phrase_candidates import phrase_windows


class TestPhraseCandidates(unittest.TestCase):
    def key(self, rows):
        return [
            (r["start_sec"], r["end_sec"], r["bars"], r["basis"])
            for r in rows
        ]

    def test_exact_bound_pair_three_to_seven_is_included_chronologically(self):
        out = phrase_windows(10.0, [0.0, 3.0, 7.0, 10.0])
        pairs = [(r["start_sec"], r["end_sec"]) for r in out]
        # [3, 7] must be present, but the contract does not require it to be
        # first; output is chronological.
        self.assertIn((3.0, 7.0), pairs)
        three_seven = next(r for r in out if (r["start_sec"], r["end_sec"]) == (3.0, 7.0))
        self.assertEqual(three_seven["basis"], "boundary_pair")
        self.assertIsNone(three_seven["bars"])
        self.assertEqual(pairs, sorted(pairs))
        self.assertEqual(
            out,
            phrase_windows(10.0, [10.0, 7.0, 3.0, 0.0]),
        )

    def test_near_boundary_merge_preserves_contiguous_exact_end(self):
        out = phrase_windows(10.0, [0.0, 2.07, 4.11, 10.0], bpm=120)
        pairs = [(r["start_sec"], r["end_sec"], r["bars"]) for r in out]
        self.assertIn((2.07, 4.11, 1.02), pairs)
        self.assertEqual(out[-1]["end_sec"], 10.0)
        for row in out:
            self.assertGreaterEqual(row["end_sec"] - row["start_sec"], 1.999)

    def test_short_segment_merged_and_no_tiny_window(self):
        out = phrase_windows(10.0, [0.0, 1.2, 1.25, 4.0, 10.0], merge_tol_s=0.12)
        self.assertNotIn(
            (1.2, 1.25),
            [(r["start_sec"], r["end_sec"]) for r in out],
        )
        for row in out:
            self.assertGreaterEqual(row["end_sec"] - row["start_sec"], 2.0 - 1e-9)

    def test_no_boundaries_thirty_seconds_has_fallbacks(self):
        out = phrase_windows(30.0, [])
        self.assertGreater(len(out), 0)
        self.assertLessEqual(len(out), 500)
        self.assertTrue(all(r["basis"] == "duration_fallback" for r in out))
        self.assertEqual(out[0]["start_sec"], 0.0)
        self.assertEqual(out[-1]["end_sec"], 30.0)
        for row in out:
            width = row["end_sec"] - row["start_sec"]
            self.assertGreaterEqual(width, 2.0 - 1e-9)
            self.assertLessEqual(width, 16.0 + 1e-9)

    def test_duration_equal_to_merge_tolerance_preserves_both_endpoints(self):
        out = phrase_windows(10.0, [], merge_tol_s=20.0)
        self.assertTrue(out)
        self.assertEqual(out[0]["start_sec"], 0.0)
        self.assertEqual(out[-1]["end_sec"], 10.0)
        for row in out:
            self.assertGreaterEqual(row["end_sec"] - row["start_sec"], 2.0 - 1e-9)
            self.assertLessEqual(row["end_sec"] - row["start_sec"], 16.0 + 1e-9)

    def test_short_duration_still_validates_boundaries(self):
        with self.assertRaises(ValueError):
            phrase_windows(1.0, [0.0, float("nan"), 1.0])
        with self.assertRaises(ValueError):
            phrase_windows(1.0, [0.0, float("inf"), 1.0])

    def test_targets_are_clipped_for_small_gap_fallback(self):
        out = phrase_windows(
            10.0,
            [],
            bpm=10.0,
            min_s=2.0,
            max_s=16.0,
        )
        self.assertTrue(out)
        self.assertEqual(out[0]["start_sec"], 0.0)
        self.assertEqual(out[-1]["end_sec"], 10.0)
        for row in out:
            width = row["end_sec"] - row["start_sec"]
            self.assertGreaterEqual(width, 2.0 - 1e-9)
            self.assertLessEqual(width, 10.0 + 1e-9)

    def test_global_candidate_cap_for_long_duration_without_np_arange_allocation(self):
        # A pathological duration must not allocate an np.arange-sized array
        # and must cap the complete result, not each gap independently.
        out = phrase_windows(1.0e12, [])
        self.assertEqual(len(out), 500)
        self.assertLessEqual(len(out), 500)
        self.assertTrue(all(r["basis"] == "duration_fallback" for r in out))

    def test_long_gap_uses_fallback_without_detected_label(self):
        out = phrase_windows(20.0, [0.0, 2.0, 20.0])
        long_rows = [r for r in out if r["start_sec"] >= 2.0 - 1e-9]
        self.assertTrue(long_rows)
        self.assertTrue(all(r["basis"] == "duration_fallback" for r in long_rows))

    def test_one_and_zero_second_return_empty(self):
        self.assertEqual(phrase_windows(1.0, [0.0, 1.0]), [])
        self.assertEqual(phrase_windows(0.0, []), [])

    def test_invalid_finite_inputs(self):
        with self.assertRaises(ValueError):
            phrase_windows(-1.0, [])
        with self.assertRaises(ValueError):
            phrase_windows(10.0, [], bpm=0.0)
        with self.assertRaises(ValueError):
            phrase_windows(10.0, [], bpm=float("nan"))
        with self.assertRaises(ValueError):
            phrase_windows(10.0, [0.0, float("inf"), 10.0])
        with self.assertRaises(ValueError):
            phrase_windows(10.0, [], min_s=4.0, max_s=2.0)
        with self.assertRaises(ValueError):
            phrase_windows(10.0, [], merge_tol_s=-0.1)
        with self.assertRaises(ValueError):
            phrase_windows(float("inf"), [])

    def test_boundary_limit_is_explicit_and_deterministic(self):
        bounds = [i * 0.001 for i in range(100_000)]
        with self.assertRaises(ValueError):
            phrase_windows(10.0, bounds)
        a = phrase_windows(12.0, [0.0, 3.0, 5.0, 8.0, 12.0], bpm=120)
        b = phrase_windows(12.0, [12.0, 8.0, 5.0, 3.0, 0.0], bpm=120)
        self.assertEqual(self.key(a), self.key(b))


if __name__ == "__main__":
    unittest.main()
