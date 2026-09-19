import copy
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from phrase_diversity import interval_iou, interval_overlap, normalize_source_id, select_diverse_moments


def m(start, end, total=1.0, asset="a", stem="vox", mid=None):
    out = {"interval": [start, end], "total": total, "asset_id": asset, "stem": stem}
    if mid is not None:
        out["id"] = mid
    return out


class PhraseDiversityTests(unittest.TestCase):
    def test_interval_helpers(self):
        self.assertEqual(interval_overlap((0, 4), (3.5, 8)), 0.5)
        self.assertAlmostEqual(interval_iou((0, 4), (3.5, 8)), 0.5 / 8.0)
        self.assertEqual(interval_iou((2, 2), (2, 2)), 0.0)

        with self.assertRaises(ValueError):
            interval_overlap((1, 0), (0, 1))
        with self.assertRaises(ValueError):
            interval_iou((float("nan"), 1), (0, 1))
        with self.assertRaises(ValueError):
            interval_overlap((0,), (0, 1))
        with self.assertRaises(ValueError):
            interval_iou(None, (0, 1))

    def test_source_identity(self):
        self.assertEqual(
            normalize_source_id({"asset_id": 7}),
            normalize_source_id({"asset_id": "7", "stem": "defaultsource"}),
        )
        self.assertEqual(
            normalize_source_id({"asset_id": 7, "stem": 3}),
            normalize_source_id({"asset_id": "7", "stem": "3"}),
        )
        self.assertNotEqual(
            normalize_source_id({"asset_id": "a|b", "stem": "c"}),
            normalize_source_id({"asset_id": "a", "stem": "b|c"}),
        )
        self.assertIsNone(normalize_source_id({"asset_id": None, "stem": "x"}))
        self.assertIsNone(normalize_source_id({"stem": "x"}))
        self.assertIsNone(normalize_source_id({"asset_id": "", "stem": "x"}))
        self.assertIsNone(normalize_source_id({"asset_id": "a", "stem": float("nan")}))

    def test_high_overlap_keeps_highest_score(self):
        data = [m(0, 2), m(0.1, 2.1), m(0.2, 2.2, 5.0)]
        self.assertEqual(select_diverse_moments(data, 5), [data[2]])

    def test_overlap_threshold_boundaries_on_long_intervals(self):
        exact = [
            {"interval": [0, 4], "total": 1.0, "asset_id": "a", "stem": "x", "id": "a"},
            {"interval": [3.5, 8], "total": 0.9, "asset_id": "a", "stem": "x", "id": "b"},
        ]

        exact_at_limit = select_diverse_moments(
            copy.deepcopy(exact), 5, max_overlap_seconds=0.5, max_iou=1.0
        )
        self.assertEqual(exact_at_limit, exact)

        exact_just_below = select_diverse_moments(
            copy.deepcopy(exact), 5, max_overlap_seconds=0.49, max_iou=1.0
        )
        self.assertEqual(exact_just_below, [exact[0]])

    def test_iou_threshold_boundary_on_long_intervals(self):
        data = [
            {"interval": [0, 4], "total": 1.0, "asset_id": "a", "stem": "x", "id": "a"},
            {"interval": [3.5, 8], "total": 0.9, "asset_id": "a", "stem": "x", "id": "b"},
        ]

        kept = select_diverse_moments(copy.deepcopy(data), 5, max_overlap_seconds=10, max_iou=0.0625)
        self.assertEqual(kept, data)

        rejected = select_diverse_moments(copy.deepcopy(data), 5, max_overlap_seconds=10, max_iou=0.0624)
        self.assertEqual(rejected, [data[0]])

    def test_separated_adjacent_and_cross_identity(self):
        separated = [m(0, 1, 3), m(2, 3, 2), m(4, 5, 1)]
        self.assertEqual(select_diverse_moments(separated, 5), separated)

        adjacent = [m(0, 4, 1.0, mid="a"), m(4, 8, 1.0, mid="b")]
        self.assertEqual(len(select_diverse_moments(adjacent, 5)), 2)

        cross = [
            {**m(0, 2, 1.0, "a", "vox"), "id": "a"},
            {**m(0, 2, 1.0, "b", "vox"), "id": "b"},
        ]
        self.assertEqual(len(select_diverse_moments(cross, 5)), 2)

        stems = [
            {**m(0, 2, 1.0, "a", "vox"), "id": "a"},
            {**m(0, 2, 1.0, "a", "bass"), "id": "b"},
        ]
        self.assertEqual(len(select_diverse_moments(stems, 5)), 2)

    def test_iou_limit_and_same_source_count(self):
        first = {"interval": [0, 10], "total": 1.0, "asset_id": "a", "stem": "x", "id": "a"}
        second = {"interval": [0, 2.5], "total": 0.9, "asset_id": "a", "stem": "x", "id": "b"}
        third = {"interval": [20, 22], "total": 0.8, "asset_id": "a", "stem": "x", "id": "c"}

        self.assertEqual(select_diverse_moments([first, second, third], 5, max_iou=0.25), [first, third])

        kept = select_diverse_moments([first, second, third], 5, max_same_source=1)
        self.assertEqual(kept, [first])
        self.assertEqual(select_diverse_moments([first, second], 0), [])

    def test_scores_ties_missing_and_invalid(self):
        nan_bad = m(0, 1, float("nan"), "a", "x", "bad")
        inf_bad = m(2, 3, float("inf"), "a", "x", "inf")
        neg_inf_bad = m(4, 5, float("-inf"), "a", "x", "neg-inf")
        missing = m(10, 11, "ignored", "a", "x", "missing")
        finite = m(0, 1, -100.0, "a", "x", "finite")

        kept = select_diverse_moments([nan_bad, inf_bad, neg_inf_bad, missing, finite], 5)
        self.assertEqual(kept, [finite, missing])

        invalid = [
            {"interval": [-1, 2], "total": 9, "asset_id": "a", "stem": "x"},
            {"interval": [2, 1], "total": 9, "asset_id": "a", "stem": "x"},
            {"interval": [float("nan"), 2], "total": 9, "asset_id": "a", "stem": "x"},
            {"interval": [0, 0], "total": 9, "asset_id": "a", "stem": "x"},
            {"interval": [0, 1], "total": 9, "asset_id": "", "stem": "x"},
        ]
        self.assertEqual(select_diverse_moments(invalid, 5), [])

        tie_a = m(0, 1, 1.0, "a", "x", "a")
        tie_b = m(2, 3, 1.0, "a", "x", "b")
        self.assertEqual(select_diverse_moments([tie_b, tie_a], 1), [tie_a])

        original = [m(5, 6), m(0, 1)]
        original_copy = copy.deepcopy(original)
        result = select_diverse_moments(original, 5)
        self.assertEqual(result, [original[1], original[0]])
        self.assertEqual(original, original_copy)

    def test_equal_score_start_end_same_identity_uses_id_tie_break(self):
        first = {"interval": [0, 1], "total": 1.0, "asset_id": "a", "stem": "x", "id": "b"}
        second = {"interval": [0, 1], "total": 1.0, "asset_id": "a", "stem": "x", "id": "a"}
        self.assertEqual(select_diverse_moments([first, second], 1), [second])

    def test_missing_total_sorts_after_finite_scores_and_chronologically(self):
        later_scored = {"interval": [3, 4], "total": -100.0, "asset_id": "a", "stem": "x", "id": "low"}
        earlier_missing = {"interval": [1, 2], "asset_id": "a", "stem": "y", "id": "earlier"}
        later_missing = {"interval": [5, 6], "asset_id": "a", "stem": "y", "id": "later"}

        kept = select_diverse_moments([later_missing, later_scored, earlier_missing], 5)
        self.assertEqual(kept, [earlier_missing, later_scored, later_missing])

        first_only = select_diverse_moments([later_missing, later_scored, earlier_missing], 1)
        self.assertEqual(first_only, [later_scored])

    def test_non_numeric_total_is_treated_as_missing(self):
        finite = {"interval": [1, 2], "total": 0.0, "asset_id": "a", "stem": "x", "id": "finite"}
        non_numeric = {"interval": [0, 1], "total": "ignored", "asset_id": "a", "stem": "y", "id": "missing"}
        self.assertEqual(select_diverse_moments([non_numeric, finite], 5), [non_numeric, finite])

    def test_interval_field_is_supported(self):
        moment = {"interval": [0, 1], "total": 1.0, "asset_id": "a", "stem": "x", "id": "a"}
        self.assertEqual(select_diverse_moments([moment], 5), [moment])

    def test_bad_arguments(self):
        with self.assertRaises(ValueError):
            select_diverse_moments([], -1)
        with self.assertRaises(ValueError):
            select_diverse_moments([], 1, max_same_source=-1)
        with self.assertRaises(ValueError):
            select_diverse_moments([], 1, max_overlap_seconds=-0.1)
        with self.assertRaises(ValueError):
            select_diverse_moments([], 1, max_iou=1.1)
        with self.assertRaises(ValueError):
            select_diverse_moments([], 1, max_iou=float("nan"))
        with self.assertRaises(ValueError):
            select_diverse_moments([], 1, max_overlap_seconds=float("inf"))


if __name__ == "__main__":
    unittest.main()
