import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from contextlib import ExitStack

import numpy as np
import soundfile as sf

PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

import common
import moments
import phrase_diversity


class MomentsPhraseIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(self.tmp)
        self.src = self.root / "source.wav"
        sr = moments.SR
        t = np.arange(sr * 10, dtype=np.float32) / sr
        sf.write(self.src, 0.01 * np.sin(2 * np.pi * 220 * t), sr)
        self.asset = {"id": "a1", "category": "x", "library_path": str(self.src), "bpm": None}
        self.stack.enter_context(mock.patch.object(common, "ROOT", self.root))
        self.stack.enter_context(mock.patch.object(
            common, "load_audio_mono",
            lambda p, sr: (sf.read(str(p), dtype="float32")[0], sr),
        ))
        self.stack.enter_context(mock.patch.object(moments, "structure_bounds", lambda y, sr: [0.0, 3.0, 7.0, 10.0]))
        self.stack.enter_context(mock.patch.object(moments, "_load_stems", lambda asset_dir, sr: {}))
        self.stack.enter_context(mock.patch.object(moments, "_build_risks", lambda *a, **k: []))
        self.stack.enter_context(mock.patch.object(moments, "moment_total", lambda scores: float(sum(scores.values()))))
        self.stack.enter_context(mock.patch.object(common, "get_db", side_effect=AssertionError("dry run opened DB")))
        self.stack.enter_context(mock.patch.object(common, "upsert_moment", side_effect=AssertionError("dry run wrote DB")))

    def tearDown(self):
        self.stack.close()

    def classify_by_window(self, labels):
        def classify(seg, stems, sr, w, bounds):
            key = (round(w["start_sec"], 3), round(w["end_sec"], 3))
            return labels.get(key, "texture"), {}
        self.stack.enter_context(mock.patch.object(moments, "classify_window", side_effect=classify))
        self.stack.enter_context(mock.patch.object(
            moments, "score_window",
            lambda seg, stems, sr, w, bounds, mtype: {"loopability": 1.0},
        ))

    def test_phrase_generates_real_boundary_windows_and_logs_basis(self):
        self.classify_by_window({(0.0, 3.0): "melody", (3.0, 7.0): "texture", (7.0, 10.0): "transition"})
        kept, _ = moments.analyze_asset(self.asset, write_db=False, candidate_mode="phrase_v1")
        intervals = {(m["start_sec"], m["end_sec"]) for m in kept}
        self.assertEqual(intervals, {(0.0, 3.0), (3.0, 7.0), (7.0, 10.0)})
        target = next(m for m in kept if (m["start_sec"], m["end_sec"]) == (3.0, 7.0))
        self.assertTrue(any("phrase_v1/boundary_pair" in line for line in target["explain"]))
        self.assertNotIn("interval", target)

    def test_same_source_overlapping_cross_type_is_globally_filtered(self):
        windows = [
            {"start_sec": 3.0, "end_sec": 7.0, "bars": 4.0, "basis": "boundary_pair"},
            {"start_sec": 3.5, "end_sec": 7.0, "bars": 3.5, "basis": "test_overlap"},
            {"start_sec": 7.0, "end_sec": 10.0, "bars": 3.0, "basis": "boundary_pair"},
        ]
        labels = {(3.0, 7.0): "texture", (3.5, 7.0): "melody", (7.0, 10.0): "drum_break"}
        self.classify_by_window(labels)
        self.stack.enter_context(mock.patch.object(moments, "structure_bounds", lambda y, sr: [0.0, 3.0, 3.5, 7.0, 10.0]))
        self.stack.enter_context(mock.patch.object(phrase_candidates_patch_target(), "phrase_windows", lambda dur, bounds, bpm: windows))
        kept, _ = moments.analyze_asset(self.asset, write_db=False, candidate_mode="phrase_v1")
        rows = [m for m in kept if m["end_sec"] == 7.0]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "texture")

    def test_separate_stems_allow_overlapping_regions(self):
        windows = [{"start_sec": 3.0, "end_sec": 7.0, "bars": 4.0, "basis": "boundary_pair"}]
        def classify(seg, stems, sr, w, bounds):
            return ("drum_break" if stems else "bass_phrase"), {}
        self.stack.enter_context(mock.patch.object(moments, "classify_window", side_effect=classify))
        self.stack.enter_context(mock.patch.object(
            moments, "score_window",
            lambda seg, stems, sr, w, bounds, mtype: {"loopability": 1.0},
        ))
        self.stack.enter_context(mock.patch.object(
            moments, "_load_stems",
            lambda asset_dir, sr: {"drums": np.zeros(sr * 10, dtype=np.float32),
                                    "bass": np.zeros(sr * 10, dtype=np.float32)},
        ))
        with mock.patch.object(phrase_candidates_patch_target(), "phrase_windows", lambda dur, bounds, bpm: windows):
            source_asset = dict(self.asset)
            drum_kept, _ = moments.analyze_asset(source_asset, write_db=False, candidate_mode="phrase_v1")
            self.stack.enter_context(mock.patch.object(moments, "_load_stems", lambda asset_dir, sr: {}))
            bass_kept, _ = moments.analyze_asset(source_asset, write_db=False, candidate_mode="phrase_v1")
        self.assertEqual({m["stem"] for m in drum_kept}, {"drums"})
        self.assertEqual({m["stem"] for m in bass_kept}, {"bass"})
        self.assertEqual({m["type"] for m in drum_kept + bass_kept}, {"drum_break", "bass_phrase"})

    def test_legacy_output_dryrun_and_invalid_mode(self):
        self.classify_by_window({})
        kept, _ = moments.analyze_asset(self.asset, write_db=False)
        self.assertTrue(kept)
        self.assertTrue(all("候选依据" not in line for m in kept for line in m["explain"]))
        with self.assertRaises(ValueError):
            moments.analyze_asset(self.asset, write_db=False, candidate_mode="bad")


def phrase_candidates_patch_target():
    import phrase_candidates
    return phrase_candidates


if __name__ == "__main__":
    unittest.main()
