"""Synthetic-fixture tests for pipeline/supply_cuts.py.

All audio is generated under a per-test temporary directory; no project
files, network or real music are touched.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import supply_cuts as sc  # noqa: E402


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _one_window(start=0.2, end=2.2, wtype="melody", basis="phrase_v1"):
    return {"start_sec": start, "end_sec": end,
            "type": wtype, "basis": basis}


class SupplyCutsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    # -- fixtures -----------------------------------------------------------
    def _wav_path(self, name):
        return os.path.join(self.tmp, name + ".wav")

    def _write_wav(self, name, data, sample_rate, subtype="FLOAT"):
        path = self._wav_path(name)
        sf.write(path, data, sample_rate, format="WAV", subtype=subtype)
        return path

    def _source(self, asset_id, data, sample_rate, windows=None,
                subtype="FLOAT", sha_override=None, path=None):
        if path is None:
            path = self._write_wav(asset_id, data, sample_rate, subtype)
        with open(path, "rb") as handle:
            sha = _sha256_bytes(handle.read())
        return {
            "asset_id": asset_id,
            "path": path,
            "sha256": sha_override if sha_override is not None else sha,
            "source_url": "https://example.org/item/" + asset_id,
            "rights_evidence_url":
                "https://example.org/rights/" + asset_id,
            "windows": windows if windows is not None else [_one_window()],
        }

    def _job(self, sources, batch_id="batch-test"):
        return {"batch_id": batch_id, "sources": sources}

    def _run(self, job, out_dir):
        job_path = os.path.join(self.tmp, "job.json")
        with open(job_path, "w", encoding="utf-8") as handle:
            json.dump(job, handle)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sc.main(["supply_cuts.py", job_path, out_dir])
        return code, out.getvalue(), err.getvalue()

    # -- exact cut + fade data ---------------------------------------------
    def test_exact_samples_with_5ms_fade_mono(self):
        sr = 1000  # 5 ms fade -> exactly 5 frames
        data = np.full(3000, 0.5, dtype=np.float32)
        source = self._source(
            "synthetic1", data, sr,
            windows=[_one_window(start=0.5, end=2.5)])
        out_dir = os.path.join(self.tmp, "out1")

        code, stdout, _ = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 0)

        path = os.path.join(out_dir, "synthetic1_00.wav")
        cut, cut_sr = sf.read(path, dtype="float64", always_2d=True)
        info = sf.info(path)
        self.assertEqual(cut_sr, 1000)
        self.assertEqual(info.samplerate, 1000)
        self.assertEqual(info.channels, 1)
        self.assertEqual(info.subtype, "FLOAT")
        self.assertEqual(cut.shape, (2000, 1))

        expected = np.full(2000, 0.5, dtype=np.float64)
        expected[:5] *= np.linspace(0.0, 1.0, 5)
        expected[-5:] *= np.linspace(1.0, 0.0, 5)
        np.testing.assert_allclose(cut[:, 0], expected, atol=1e-7)
        # Endpoints are exactly silent after the linear fades.
        self.assertEqual(cut[0, 0], 0.0)
        self.assertEqual(cut[-1, 0], 0.0)
        # Interior samples are untouched: no normalization or gain change.
        self.assertTrue(np.all(cut[5:-5, 0] == 0.5))
        self.assertIn("candidates=1", stdout)
        self.assertIn("skipped=0", stdout)

        with open(os.path.join(out_dir, "manifest.json"),
                  encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["batch"], "batch-test")
        self.assertEqual(manifest["content_identity"], "unverified")
        self.assertEqual(manifest["listening_review"], "pending")
        self.assertEqual(manifest["rights_scope"],
                         "requires_per-use-review")
        self.assertEqual(len(manifest["candidates"]), 1)
        cand = manifest["candidates"][0]
        self.assertEqual(cand["filename"], "synthetic1_00.wav")
        self.assertEqual(cand["start_frame"], 500)
        self.assertEqual(cand["end_frame"], 2500)
        self.assertEqual(cand["frames"], 2000)
        self.assertEqual(cand["sample_rate"], 1000)
        self.assertEqual(cand["channels"], 1)
        self.assertEqual(cand["source_sha256"], source["sha256"])
        self.assertEqual(cand["source_url"], source["source_url"])
        self.assertEqual(cand["rights_evidence_url"],
                         source["rights_evidence_url"])
        with open(path, "rb") as handle:
            self.assertEqual(cand["output_sha256"],
                             _sha256_bytes(handle.read()))
        self.assertAlmostEqual(cand["peak"], 0.5, places=6)
        self.assertGreaterEqual(cand["rms_db"], -50.0)
        self.assertEqual(manifest["skipped"], [])

    # -- sample rate / channels preserved ----------------------------------
    def test_sample_rate_and_stereo_channels_preserved(self):
        sr = 22050
        t = np.arange(sr * 3, dtype=np.float64) / sr
        left = 0.4 * np.ones_like(t)
        right = -0.2 * np.ones_like(t)
        data = np.column_stack([left, right]).astype(np.float32)
        source = self._source(
            "stereo1", data, sr,
            windows=[_one_window(start=0.2, end=2.2)])
        out_dir = os.path.join(self.tmp, "out2")

        code, _, _ = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 0)

        path = os.path.join(out_dir, "stereo1_00.wav")
        cut, cut_sr = sf.read(path, dtype="float64", always_2d=True)
        info = sf.info(path)
        self.assertEqual(cut_sr, 22050)
        self.assertEqual(info.channels, 2)
        self.assertEqual(info.subtype, "FLOAT")
        self.assertEqual(cut.shape, (44100, 2))
        n_fade = int(0.005 * sr)  # 110 frames
        np.testing.assert_allclose(
            cut[:n_fade, 0],
            0.4 * np.linspace(0.0, 1.0, n_fade), atol=1e-6)
        np.testing.assert_allclose(
            cut[:n_fade, 1],
            -0.2 * np.linspace(0.0, 1.0, n_fade), atol=1e-6)
        np.testing.assert_allclose(cut[n_fade:-n_fade, 0], 0.4, atol=1e-7)
        np.testing.assert_allclose(cut[n_fade:-n_fade, 1], -0.2, atol=1e-7)

        with open(os.path.join(out_dir, "manifest.json"),
                  encoding="utf-8") as handle:
            cand = json.load(handle)["candidates"][0]
        self.assertEqual(cand["sample_rate"], 22050)
        self.assertEqual(cand["channels"], 2)
        self.assertEqual(cand["start_frame"], int(0.2 * sr))
        self.assertEqual(cand["end_frame"], int(2.2 * sr))

    # -- source never modified, hash recorded ------------------------------
    def test_source_file_preserved_and_hash_recorded(self):
        sr = 8000
        data = (0.25 * np.ones(sr * 3)).astype(np.float32)
        path = self._write_wav("keepme", data, sr)
        with open(path, "rb") as handle:
            before = handle.read()
        source = self._source("keepme", data, sr, path=path)
        out_dir = os.path.join(self.tmp, "out3")

        code, _, _ = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 0)
        with open(path, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)  # source bytes untouched

        with open(os.path.join(out_dir, "manifest.json"),
                  encoding="utf-8") as handle:
            cand = json.load(handle)["candidates"][0]
        self.assertEqual(cand["source_sha256"],
                         _sha256_bytes(after))

    # -- source SHA mismatch ------------------------------------------------
    def test_source_sha_mismatch_rejected_without_output(self):
        sr = 8000
        data = (0.2 * np.ones(sr * 3)).astype(np.float32)
        source = self._source("badsha", data, sr,
                              sha_override="a" * 64)
        out_dir = os.path.join(self.tmp, "out4")
        code, _, err = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out_dir))
        self.assertIn("sha256 mismatch", err)

    # -- NaN / non-finite bounds -------------------------------------------
    def test_nan_bounds_rejected(self):
        sr = 8000
        data = (0.2 * np.ones(sr * 3)).astype(np.float32)
        source = self._source(
            "nanwin", data, sr,
            windows=[{"start_sec": float("nan"), "end_sec": 2.2,
                      "type": "melody", "basis": "phrase_v1"}])
        out_dir = os.path.join(self.tmp, "out5")
        code, _, err = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out_dir))
        self.assertIn("finite", err)

    def test_infinite_bounds_rejected(self):
        sr = 8000
        data = (0.2 * np.ones(sr * 3)).astype(np.float32)
        source = self._source(
            "infwin", data, sr,
            windows=[{"start_sec": 0.2, "end_sec": float("inf"),
                      "type": "melody", "basis": "phrase_v1"}])
        out_dir = os.path.join(self.tmp, "outinf")
        code, _, _ = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out_dir))

    # -- out of range / bad durations / ordering ---------------------------
    def test_out_of_range_and_bad_window_bounds_rejected(self):
        sr = 1000
        data = (0.2 * np.ones(3000)).astype(np.float32)
        bad_windows = [
            _one_window(start=0.5, end=3.5),      # end beyond duration
            _one_window(start=-0.1, end=2.0),     # negative start
            _one_window(start=1.0, end=2.9),      # under 2 s
            _one_window(start=0.0, end=16.5),     # over 16 s / OOR
            _one_window(start=2.0, end=1.0),      # start >= end
        ]
        for i, window in enumerate(bad_windows):
            source = self._source("oor%d" % i, data, sr,
                                  windows=[window])
            out_dir = os.path.join(self.tmp, "outoor%d" % i)
            code, _, _ = self._run(self._job([source]), out_dir)
            self.assertEqual(code, 2, "window %r should fail" % window)
            self.assertFalse(os.path.exists(out_dir))

    # -- traversal / unsafe asset ids --------------------------------------
    def test_traversal_and_duplicate_asset_ids_rejected(self):
        sr = 8000
        data = (0.2 * np.ones(sr * 3)).astype(np.float32)
        for bad_id in ["../evil", "..", "a/b", "x\\y", "", "a b",
                       "with.dot", "nul:"]:
            source = self._source("unused", data, sr)
            source["asset_id"] = bad_id
            out_dir = os.path.join(self.tmp, "out_" +
                                   "".join(c if c.isalnum() else "_"
                                           for c in bad_id))
            code, _, _ = self._run(self._job([source]), out_dir)
            self.assertEqual(code, 2, "asset_id %r should fail" % bad_id)
            self.assertFalse(os.path.exists(out_dir))

        first = self._source("dup", data, sr)
        second = self._source("unused", data, sr)
        second["asset_id"] = "dup"
        out_dir = os.path.join(self.tmp, "outdup")
        code, _, err = self._run(self._job([first, second]), out_dir)
        self.assertEqual(code, 2)
        self.assertIn("duplicate asset_id", err)
        self.assertFalse(os.path.exists(out_dir))

    # -- silent skip --------------------------------------------------------
    def test_silent_window_skipped_but_run_succeeds(self):
        sr = 8000
        silence = np.zeros(sr * 3, dtype=np.float32)
        source = self._source("quiet1", silence, sr)
        out_dir = os.path.join(self.tmp, "outsilent")
        code, stdout, _ = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 0)
        entries = sorted(os.listdir(out_dir))
        self.assertEqual(entries, ["manifest.json"])
        with open(os.path.join(out_dir, "manifest.json"),
                  encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["candidates"], [])
        self.assertEqual(len(manifest["skipped"]), 1)
        skipped = manifest["skipped"][0]
        self.assertEqual(skipped["asset_id"], "quiet1")
        self.assertEqual(skipped["window_index"], 0)
        self.assertEqual(skipped["reason"], "silence_rms_below_-50dbfs")
        self.assertIsNone(skipped["rms_db"])
        self.assertIn("candidates=0", stdout)
        self.assertIn("skipped=1", stdout)

    def test_near_silence_skipped_loud_kept(self):
        sr = 8000
        # One quiet source (below -50 dBFS RMS) and one normal source.
        quiet_data = np.full(sr * 3, 1e-4, dtype=np.float32)  # -80 dBFS
        loud_data = np.full(sr * 3, 0.3, dtype=np.float32)
        sources = [
            self._source("quiet2", quiet_data, sr),
            self._source("loud1", loud_data, sr),
        ]
        out_dir = os.path.join(self.tmp, "outmix")
        code, stdout, _ = self._run(self._job(sources), out_dir)
        self.assertEqual(code, 0)
        with open(os.path.join(out_dir, "manifest.json"),
                  encoding="utf-8") as handle:
            manifest = json.load(handle)
        names = [c["filename"] for c in manifest["candidates"]]
        self.assertEqual(names, ["loud1_00.wav"])
        self.assertEqual(len(manifest["skipped"]), 1)
        self.assertIn("candidates=1", stdout)
        self.assertIn("skipped=1", stdout)

    # -- existing output directory protected -------------------------------
    def test_existing_output_directory_protected(self):
        sr = 8000
        data = (0.2 * np.ones(sr * 3)).astype(np.float32)
        source = self._source("prot", data, sr)
        out_dir = os.path.join(self.tmp, "outprot")
        os.makedirs(out_dir)
        marker = os.path.join(out_dir, "marker.txt")
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("do not touch\n")

        code, _, err = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        with open(marker, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "do not touch\n")
        self.assertFalse(os.path.exists(
            os.path.join(out_dir, "manifest.json")))
        self.assertFalse(os.path.exists(
            os.path.join(out_dir, "prot_00.wav")))

    # -- invalid later source leaves no output -----------------------------
    def test_invalid_later_source_leaves_no_output(self):
        sr = 8000
        good = (0.2 * np.ones(sr * 3)).astype(np.float32)
        bad = (0.2 * np.ones(sr * 3)).astype(np.float32)
        source1 = self._source("first", good, sr)
        source2 = self._source("second", bad, sr,
                               sha_override="b" * 64)
        out_dir = os.path.join(self.tmp, "outlater")
        code, _, err = self._run(self._job([source1, source2]), out_dir)
        self.assertEqual(code, 2)
        self.assertIn("second", err)
        self.assertFalse(os.path.exists(out_dir))

    def test_invalid_later_audio_leaves_no_output(self):
        sr = 8000
        good = (0.2 * np.ones(sr * 3)).astype(np.float32)
        hot = np.full(sr * 3, 1.0001, dtype=np.float32)
        source1 = self._source("first", good, sr)
        source2 = self._source("hot", hot, sr)
        out_dir = os.path.join(self.tmp, "outhot")
        code, _, _ = self._run(self._job([source1, source2]), out_dir)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out_dir))

    # -- input audio rules --------------------------------------------------
    def test_peak_above_one_rejected(self):
        sr = 8000
        data = np.full(sr * 3, 1.000001, dtype=np.float32)
        source = self._source("peak", data, sr)
        out_dir = os.path.join(self.tmp, "outpeak")
        code, _, err = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 2)
        self.assertIn("peak", err)
        self.assertFalse(os.path.exists(out_dir))

    def test_nonfinite_audio_rejected(self):
        sr = 8000
        data = np.full(sr * 3, 0.5, dtype=np.float32)
        data[10] = np.nan
        source = self._source("nansrc", data, sr)
        out_dir = os.path.join(self.tmp, "outnansrc")
        code, _, _ = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out_dir))

    def test_pcm_input_accepted(self):
        sr = 16000
        data = (0.3 * np.ones(sr * 3)).astype(np.float32)
        source = self._source("pcm", data, sr, subtype="PCM_16")
        out_dir = os.path.join(self.tmp, "outpcm")
        code, _, _ = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 0)
        info = sf.info(os.path.join(out_dir, "pcm_00.wav"))
        self.assertEqual(info.subtype, "FLOAT")  # output always float
        self.assertEqual(info.samplerate, 16000)

    # -- size caps ----------------------------------------------------------
    def test_source_size_caps_enforced(self):
        sr = 8000
        data = (0.1 * np.ones(sr)).astype(np.float32)
        source = self._source("small", data, sr)
        out_dir = os.path.join(self.tmp, "outcap")
        with mock.patch.object(sc, "MAX_SOURCE_BYTES", 1024):
            code, _, err = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 2)
        self.assertIn("bytes", err)
        self.assertFalse(os.path.exists(out_dir))

    def test_total_size_cap_enforced(self):
        sr = 8000
        data = (0.1 * np.ones(sr)).astype(np.float32)
        sources = [self._source("one", data, sr),
                   self._source("two", data, sr)]
        out_dir = os.path.join(self.tmp, "outtotal")
        with mock.patch.object(sc, "MAX_SOURCE_BYTES", 2 ** 30), \
                mock.patch.object(sc, "MAX_TOTAL_SOURCE_BYTES", 1024):
            code, _, _ = self._run(self._job(sources), out_dir)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out_dir))

    # -- structural limits / provenance ------------------------------------
    def test_counts_and_provenance_limits(self):
        sr = 8000
        data = (0.1 * np.ones(sr * 3)).astype(np.float32)

        too_many_sources = [
            self._source("s%d" % i, data, sr) for i in range(5)]
        out_dir = os.path.join(self.tmp, "out5src")
        code, _, _ = self._run(self._job(too_many_sources), out_dir)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out_dir))

        seven = [_one_window(start=0.0, end=2.0) for _ in range(7)]
        source = self._source("seven", data, sr, windows=seven)
        out_dir = os.path.join(self.tmp, "out7win")
        code, _, _ = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out_dir))

        source = self._source("norights", data, sr)
        del source["rights_evidence_url"]
        out_dir = os.path.join(self.tmp, "outnorights")
        code, _, _ = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out_dir))

        source = self._source("badurl", data, sr)
        source["source_url"] = "ftp://example.org/x"
        out_dir = os.path.join(self.tmp, "outbadurl")
        code, _, _ = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out_dir))

        source = self._source("relative", data, sr)
        source["path"] = "relative.wav"
        out_dir = os.path.join(self.tmp, "outrel")
        code, _, _ = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out_dir))

    # -- rerun safety -------------------------------------------------------
    def test_rerun_to_same_directory_fails_without_changes(self):
        sr = 8000
        data = (0.2 * np.ones(sr * 3)).astype(np.float32)
        source = self._source("rerun", data, sr)
        out_dir = os.path.join(self.tmp, "outrerun")
        code, _, _ = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 0)

        def _snapshot():
            result = {}
            for name in os.listdir(out_dir):
                with open(os.path.join(out_dir, name), "rb") as handle:
                    result[name] = handle.read()
            return result

        before = _snapshot()
        code, _, err = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertEqual(_snapshot(), before)

    # -- usage / bad JSON ---------------------------------------------------
    def test_usage_and_bad_job_json(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sc.main(["supply_cuts.py"])
        self.assertEqual(code, 2)
        self.assertIn("usage", err.getvalue())

        job_path = os.path.join(self.tmp, "broken.json")
        with open(job_path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        out_dir = os.path.join(self.tmp, "outbroken")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sc.main(["supply_cuts.py", job_path, out_dir])
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out_dir))

    # -- multiple windows indices preserved through skips ------------------
    def test_window_indices_match_job_position(self):
        sr = 8000
        loud = (0.4 * np.ones(sr * 3)).astype(np.float32)
        quiet = np.full(sr * 3, 1e-4, dtype=np.float32)
        windows = [
            _one_window(start=0.0, end=2.0),      # 0 loud -> kept
            _one_window(start=0.2, end=2.3),      # 1 quiet data source? no
        ]
        # Both windows come from the loud source; force window 1 quiet by
        # using a second source instead is overkill: give one loud source
        # two loud windows and assert zero-based index filenames.
        source = self._source("idx", loud, sr, windows=windows)
        out_dir = os.path.join(self.tmp, "outidx")
        code, _, _ = self._run(self._job([source]), out_dir)
        self.assertEqual(code, 0)
        names = sorted(os.listdir(out_dir))
        self.assertIn("idx_00.wav", names)
        self.assertIn("idx_01.wav", names)
        with open(os.path.join(out_dir, "manifest.json"),
                  encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual([c["window_index"] for c in manifest["candidates"]],
                         [0, 1])

        # A skipped window keeps the run successful with exit code 0.
        source2 = self._source(
            "gap", quiet, sr,
            windows=[_one_window(start=0.0, end=2.0)])
        job2 = self._job([source2], batch_id="gap-batch")
        out_dir2 = os.path.join(self.tmp, "outgap")
        code, stdout, _ = self._run(job2, out_dir2)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(os.listdir(out_dir2)), ["manifest.json"])
        self.assertIn("sources=1", stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
