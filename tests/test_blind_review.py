"""Tests for the standalone local blinded listening artifact and decision API.

Real short audio, level-match measurement, and an actual loopback HTTP server
(ThreadingHTTPServer, ephemeral port). No browser, no network beyond loopback,
no music-taste claims: structure and bytes are what is verified here.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import blind_review as br  # noqa: E402

SR = 16000
RMS_LABEL = "RMS音量匹配（非LUFS，原版文件不变）"
SECRET_TOKENS = ("parent-original-id-alpha", "edit-one-id-bravo",
                 "edit-two-id-charlie", "parent_vocal_master_7f3a.wav",
                 "vocal_edit_one_9c2e.wav", "vocal_edit_two_41ab.wav")


def _tone(amp, harmonic, seed):
    """Non-trivial distinct waveform: sine + harmonic + deterministic noise."""
    t = np.arange(SR) / SR
    rng = np.random.default_rng(seed)
    return (amp * np.sin(2 * np.pi * 440 * t)
            + amp * 0.08 * np.sin(2 * np.pi * harmonic * t)
            + amp * 0.02 * rng.standard_normal(SR))


class ReviewFixture:
    def __init__(self, root: Path, channels=1, count=3):
        self.root = root
        amps = (0.50, 0.20, 0.80)
        harmonics = (880, 660, 990)
        names = ("parent_vocal_master_7f3a.wav",
                 "vocal_edit_one_9c2e.wav",
                 "vocal_edit_two_41ab.wav")
        ids = ("parent-original-id-alpha", "edit-one-id-bravo",
               "edit-two-id-charlie")
        self.items = []
        for i in range(count):
            y = _tone(amps[i], harmonics[i], 100 + i)
            if channels == 2:
                y = np.stack([y, 0.9 * y[::-1]], axis=1)
            p = root / names[i]
            sf.write(p, y, SR, subtype="FLOAT")
            self.items.append({"id": ids[i], "mix_path": str(p)})

def _snapshot(items):
    snap = {}
    for it in items:
        p = Path(it["mix_path"])
        snap[it["id"]] = (p.read_bytes(), os.stat(str(p)).st_mtime_ns, str(p))
    return snap


def _assert_originals_unchanged(test, snap):
    for _id, (data, mtime, path) in snap.items():
        test.assertEqual(Path(path).read_bytes(), data, f"original changed: {_id}")
        test.assertEqual(os.stat(path).st_mtime_ns, mtime, f"mtime changed: {_id}")


def _rms_db(x):
    return 20 * np.log10(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def build(self, **kw):
        fx = ReviewFixture(self.root, **kw)
        self.snap = _snapshot(fx.items)
        out = self.root / "review"
        info = br.build_blind_review(fx.items, out)
        self.fx, self.out, self.info = fx, out, info
        return fx, out, info

    def test_short_ids_are_not_searched_inside_binary_audio(self):
        fx = ReviewFixture(self.root, count=2)
        for item, identifier in zip(fx.items, ('a', 'b')):
            item['id'] = identifier
        result = br.build_blind_review(fx.items, self.root / 'short-ids')
        self.assertEqual(result['labels'], ['A', 'B'])

    def test_structure_and_return_contract(self):
        fx, out, info = self.build()
        self.assertEqual(set(info), {"review_dir", "labels", "matching"})
        self.assertEqual(info["review_dir"], str(out))
        self.assertEqual(info["labels"], ["A", "B", "C"])
        self.assertTrue((out / "public" / "index.html").is_file())
        for label in "ABC":
            self.assertTrue((out / "public" / f"{label}.wav").is_file())
        self.assertTrue((out / "private" / "mapping.json").is_file())
        self.assertFalse((out / "private" / "decision.json").exists(),
                         "no decision may be fabricated at build time")

    def test_audition_copies_level_match_within_0_05db_and_float(self):
        fx, out, info = self.build()
        dbs = {}
        for label in "ABC":
            p = out / "public" / f"{label}.wav"
            self.assertEqual(sf.info(str(p)).subtype, "FLOAT")
            y, sr = sf.read(str(p), always_2d=True)
            self.assertEqual(sr, SR)
            self.assertEqual(y.shape, (SR, 1))
            self.assertTrue(np.all(np.isfinite(y)))
            dbs[label] = _rms_db(y)
        span = max(dbs.values()) - min(dbs.values())
        self.assertLessEqual(span, 0.05, f"RMS mismatch {span:.3f} dB")
        # private metrics name the technique and the same measured values
        mapping = json.loads((out / "private" / "mapping.json").read_text())
        self.assertIn("RMS", mapping["level_match"]["technique"])
        self.assertIn("LUFS", mapping["level_match"]["technique"])
        for label in "ABC":
            self.assertAlmostEqual(mapping["level_match"]["metrics"][label]["rms_db_after"],
                                   dbs[label], delta=0.01)

    def test_headroom_ceiling_respected(self):
        fx, out, info = self.build()
        for label in "ABC":
            y, _ = sf.read(str(out / "public" / f"{label}.wav"), always_2d=True)
            self.assertLessEqual(np.max(np.abs(y)), br.CEILING + 1e-6)

    def test_private_mapping_resolves_random_assignment(self):
        fx, out, info = self.build()
        mapping = json.loads((out / "private" / "mapping.json").read_text())
        self.assertEqual(sorted(mapping["labels"]), ["A", "B", "C"])
        assigned = {label: mapping["assignment"][label]["id"] for label in "ABC"}
        self.assertEqual(sorted(assigned.values()), sorted(it["id"] for it in fx.items))
        self.assertEqual(info["labels"], mapping["labels"])

    def test_random_mapping_not_leaked_into_public(self):
        fx, out, info = self.build()
        html_text = (out / "public" / "index.html").read_text(encoding="utf-8")
        for token in SECRET_TOKENS:
            for f in (out / "public").rglob("*"):
                if not f.is_file():
                    continue
                self.assertNotIn(token.encode("utf-8"), f.read_bytes(),
                                f"secret token {token!r} leaked in {f.name}")
        self.assertNotIn("mapping.json", html_text)

    def test_originals_byte_and_mtime_retained(self):
        self.build()
        _assert_originals_unchanged(self, self.snap)

    def test_stereo_build(self):
        fx, out, info = self.build(channels=2, count=2)
        self.assertEqual(info["labels"], ["A", "B"])
        for label in "AB":
            y, sr = sf.read(str(out / "public" / f"{label}.wav"), always_2d=True)
            self.assertEqual(y.shape, (SR, 2))
        dbs = [_rms_db(sf.read(str(out / "public" / f"{l}.wav"), always_2d=True)[0])
               for l in "AB"]
        self.assertLessEqual(abs(dbs[0] - dbs[1]), 0.05)
        _assert_originals_unchanged(self, self.snap)

    def test_existing_output_refused_and_untouched(self):
        fx = ReviewFixture(self.root, count=2)
        out = self.root / "review"
        br.build_blind_review(fx.items, out)
        before = (out / "public" / "index.html").read_bytes()
        with self.assertRaises(FileExistsError):
            br.build_blind_review(fx.items, out)
        self.assertEqual((out / "public" / "index.html").read_bytes(), before)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _items(self, count=3):
        return ReviewFixture(self.root, count=count).items

    def test_count_out_of_range(self):
        items = self._items()
        with self.assertRaises(br.ValidationError):
            br.build_blind_review(items[:1], self.root / "r")
        with self.assertRaises(br.ValidationError):
            br.build_blind_review(items + items[:1], self.root / "r2")

    def test_duplicate_ids(self):
        items = self._items()
        items[1]["id"] = items[0]["id"]
        with self.assertRaises(br.ValidationError):
            br.build_blind_review(items, self.root / "r")

    def test_missing_path(self):
        items = self._items()
        items[0]["mix_path"] = str(self.root / "nope.wav")
        with self.assertRaises(br.ValidationError):
            br.build_blind_review(items, self.root / "r")

    def test_sample_rate_mismatch(self):
        items = self._items()
        y, _ = sf.read(items[1]["mix_path"], always_2d=True)
        sf.write(items[1]["mix_path"], y, SR // 2, subtype="FLOAT")
        with self.assertRaises(br.ValidationError):
            br.build_blind_review(items, self.root / "r")

    def test_frames_mismatch(self):
        items = self._items()
        y, _ = sf.read(items[1]["mix_path"], always_2d=True)
        sf.write(items[1]["mix_path"], y[:SR // 2], SR, subtype="FLOAT")
        with self.assertRaises(br.ValidationError):
            br.build_blind_review(items, self.root / "r")

    def test_channels_mismatch(self):
        items = self._items()
        y, _ = sf.read(items[1]["mix_path"], always_2d=True)
        stereo = np.stack([y[:, 0], y[:, 0]], axis=1)
        sf.write(items[1]["mix_path"], stereo, SR, subtype="FLOAT")
        with self.assertRaises(br.ValidationError):
            br.build_blind_review(items, self.root / "r")

    def test_nonfinite_rejected(self):
        items = self._items()
        y, _ = sf.read(items[1]["mix_path"], always_2d=True)
        y[10, 0] = np.nan
        sf.write(items[1]["mix_path"], y, SR, subtype="FLOAT")
        with self.assertRaises(br.ValidationError):
            br.build_blind_review(items, self.root / "r")

    def test_silent_rejected(self):
        items = self._items()
        y, _ = sf.read(items[1]["mix_path"], always_2d=True)
        sf.write(items[1]["mix_path"], np.zeros_like(y), SR, subtype="FLOAT")
        with self.assertRaises(br.ValidationError):
            br.build_blind_review(items, self.root / "r")

    def test_identical_waveform_rejected(self):
        items = self._items()
        y0, _ = sf.read(items[0]["mix_path"], always_2d=True)
        sf.write(items[1]["mix_path"], y0, SR, subtype="FLOAT")
        with self.assertRaises(br.ValidationError):
            br.build_blind_review(items, self.root / "r")


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fx = ReviewFixture(self.root, count=3)
        self.out = self.root / "review"
        br.build_blind_review(self.fx.items, self.out)
        self.snap = _snapshot(self.fx.items)

    def start_server(self, on_decision=None):
        handler = br.make_handler(self.out, on_decision=on_decision)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        hs = dict(resp.getheaders())
        status = resp.status
        conn.close()
        return status, hs, data

    def post_json(self, obj, headers=None, raw=None):
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        body = raw if raw is not None else json.dumps(obj).encode()
        return self.request("POST", "/api/decision", body=body, headers=hdrs)

    # ---------- static page ----------

    def test_index_served_chinese_and_minimal_structure(self):
        self.start_server()
        status, hs, data = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", hs["Content-Type"])
        text = data.decode("utf-8")
        self.assertIn(RMS_LABEL, text)
        self.assertIn("保留A", text)
        self.assertIn("平局", text)
        self.assertIn("都不要", text)
        for token in SECRET_TOKENS:
            self.assertNotIn(token.encode("utf-8"), data)

    def test_javascript_same_time_guard_structure(self):
        # Static structure checks only; no browser behavior is claimed.
        text = (self.out / "public" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(text.count("<audio"), 1, "one shared audio element required")
        self.assertIn("let switchGen=0", text)
        self.assertIn("++switchGen", text)
        self.assertIn("if(my!==switchGen)return", text)
        self.assertIn("loadedmetadata", text)
        self.assertIn("Math.min", text)
        self.assertIn("request_id", text)
        self.assertIn("encodeURIComponent(label)+'.wav'", text)

    # ---------- wav serving + ranges ----------

    def test_wav_full_bytes_and_clean_headers(self):
        self.start_server()
        status, hs, data = self.request("GET", "/A.wav")
        self.assertEqual(status, 200)
        self.assertEqual(hs["Content-Type"], "audio/wav")
        self.assertEqual(hs["Accept-Ranges"], "bytes")
        on_disk = (self.out / "public" / "A.wav").read_bytes()
        self.assertEqual(data, on_disk)
        for value in hs.values():
            for token in SECRET_TOKENS:
                self.assertNotIn(token, value)

    def test_head_wav_supported(self):
        self.start_server()
        status, hs, data = self.request("HEAD", "/A.wav")
        self.assertEqual(status, 200)
        self.assertEqual(data, b"")
        size = (self.out / "public" / "A.wav").stat().st_size
        self.assertEqual(int(hs["Content-Length"]), size)

    def test_range_206_exact_slice(self):
        self.start_server()
        status, hs, data = self.request("GET", "/A.wav",
                                        headers={"Range": "bytes=100-199"})
        self.assertEqual(status, 206)
        size = (self.out / "public" / "A.wav").stat().st_size
        self.assertEqual(hs["Content-Range"], f"bytes 100-199/{size}")
        self.assertEqual(int(hs["Content-Length"]), 100)
        on_disk = (self.out / "public" / "A.wav").read_bytes()
        self.assertEqual(data, on_disk[100:200])

    def test_range_open_ended_and_suffix(self):
        self.start_server()
        size = (self.out / "public" / "A.wav").stat().st_size
        on_disk = (self.out / "public" / "A.wav").read_bytes()
        s1, h1, d1 = self.request("GET", "/A.wav",
                                  headers={"Range": "bytes=100-"})
        self.assertEqual(s1, 206)
        self.assertEqual(h1["Content-Range"], f"bytes 100-{size-1}/{size}")
        self.assertEqual(d1, on_disk[100:])
        s2, h2, d2 = self.request("GET", "/A.wav",
                                  headers={"Range": "bytes=-50"})
        self.assertEqual(s2, 206)
        self.assertEqual(h2["Content-Range"], f"bytes {size-50}-{size-1}/{size}")
        self.assertEqual(d2, on_disk[-50:])

    def test_range_end_beyond_size_clamps(self):
        self.start_server()
        size = (self.out / "public" / "A.wav").stat().st_size
        status, hs, data = self.request("GET", "/A.wav",
                                        headers={"Range": f"bytes={size-10}-99999999"})
        self.assertEqual(status, 206)
        self.assertEqual(hs["Content-Range"], f"bytes {size-10}-{size-1}/{size}")

    def test_range_unsatisfiable_416(self):
        self.start_server()
        size = (self.out / "public" / "A.wav").stat().st_size
        status, hs, data = self.request("GET", "/A.wav",
                                        headers={"Range": f"bytes={size}-"})
        self.assertEqual(status, 416)
        self.assertEqual(hs["Content-Range"], f"bytes */{size}")

    def test_malformed_range_ignored_returns_full(self):
        self.start_server()
        status, hs, data = self.request("GET", "/A.wav",
                                        headers={"Range": "bytes=abc-def"})
        self.assertEqual(status, 200)
        self.assertEqual(data, (self.out / "public" / "A.wav").read_bytes())

    def test_only_generated_names_served(self):
        self.start_server()
        for name in ("/D.wav", "/a.wav", "/A.WAV", "/index.html.bak"):
            status, _, _ = self.request("GET", name)
            self.assertEqual(status, 404, name)

    def test_traversal_and_private_files_404(self):
        self.start_server()
        raw_paths = (
            "/../private/mapping.json",
            "/%2e%2e/private/mapping.json",
            "/private/mapping.json",
            "/mapping.json",
            "/A.wav/../private/mapping.json",
            "/public/../private/decision.json",
            "/private/decision.json",
        )
        for path in raw_paths:
            status, _, data = self.request("GET", path)
            self.assertEqual(status, 404, path)
            for token in SECRET_TOKENS:
                self.assertNotIn(token.encode("utf-8"), data)

    # ---------- decision API ----------

    def test_state_before_vote_reveals_nothing(self):
        self.start_server()
        status, hs, data = self.request("GET", "/api/decision")
        self.assertEqual(status, 200)
        obj = json.loads(data)
        self.assertEqual(obj, {"voted": False})
        for token in SECRET_TOKENS:
            self.assertNotIn(token.encode("utf-8"), data)
        self.assertNotIn(b"selected_id", data)

    def test_post_keep_label_persists_and_reveals_identity(self):
        self.start_server()
        mapping = json.loads((self.out / "private" / "mapping.json").read_text())
        status, hs, data = self.post_json({"choice": "A", "request_id": "req-1"})
        self.assertEqual(status, 200)
        obj = json.loads(data)
        self.assertTrue(obj["keep"])
        self.assertEqual(obj["selected_id"], mapping["assignment"]["A"]["id"])
        self.assertEqual(obj["choice"], "A")
        self.assertFalse(obj["reused"])
        self.assertTrue(obj["created_at"])
        # identity now available through the state endpoint as well
        s2, _, d2 = self.request("GET", "/api/decision")
        self.assertEqual(s2, 200)
        self.assertTrue(json.loads(d2)["voted"])
        self.assertEqual(json.loads(d2)["selected_id"],
                         mapping["assignment"]["A"]["id"])

    def test_tie_and_neither_are_not_keep(self):
        self.start_server()
        s1, _, d1 = self.post_json({"choice": "tie", "request_id": "tie-1"})
        self.assertEqual(s1, 200)
        o1 = json.loads(d1)
        self.assertFalse(o1["keep"])
        self.assertIsNone(o1["selected_id"])
        # a second, different decision is rejected (immutable single vote)
        s2, _, _ = self.post_json({"choice": "neither", "request_id": "tie-1"})
        self.assertEqual(s2, 409)

    def test_invalid_votes_rejected_cleanly(self):
        self.start_server()
        bad_payloads = [
            {"choice": "D", "request_id": "r"},          # unknown label
            {"choice": "KEEP_A", "request_id": "r"},     # bad token
            {"choice": "A", "request_id": "bad id!"},    # unsafe id
            {"choice": "A", "request_id": ""},           # empty id
            {"choice": 42, "request_id": "r"},           # non-string
            {"choice": "tie", "request_id": "r",
             "notes": {"time_seconds": 1, "text": "x"}},  # notes not list
            {"choice": "tie", "request_id": "r",
             "notes": [{"time_seconds": True, "text": "x"}]},  # bool time
            {"choice": "tie", "request_id": "r",
             "notes": [{"time_seconds": -1, "text": "x"}]},     # negative
            {"choice": "tie", "request_id": "r",
             "notes": [{"time_seconds": 1, "text": ""}]},       # empty text
            {"choice": "tie", "request_id": "r",
             "notes": [{"time_seconds": 1, "text": "x" * 1001}]},  # too long
        ]
        for payload in bad_payloads:
            status, _, data = self.post_json(payload)
            self.assertEqual(status, 400, payload)
            self.assertIn("error", json.loads(data))

    def test_nonfinite_note_time_raw_json_rejected(self):
        self.start_server()
        raw = b'{"choice":"A","request_id":"nan-1","notes":[{"time_seconds":NaN,"text":"x"}]}'
        status, _, data = self.post_json(None, raw=raw)
        self.assertEqual(status, 400)

    def test_idempotent_retry_reuses_conflict_rejected(self):
        self.start_server()
        payload = {"choice": "B", "request_id": "idem-1",
                   "notes": [{"time_seconds": 0.5, "text": "备注内容"}]}
        s1, _, d1 = self.post_json(payload)
        self.assertEqual(s1, 200)
        first = json.loads(d1)
        s2, _, d2 = self.post_json(payload)
        self.assertEqual(s2, 200)
        second = json.loads(d2)
        self.assertTrue(second["reused"])
        self.assertEqual(second["created_at"], first["created_at"])
        # same request id, different content -> conflict
        s3, _, _ = self.post_json({"choice": "C", "request_id": "idem-1"})
        self.assertEqual(s3, 409)
        # different request id -> conflict
        s4, _, _ = self.post_json({"choice": "B", "request_id": "idem-2"})
        self.assertEqual(s4, 409)

    def test_persistence_refresh_and_load_helper(self):
        self.start_server()
        self.post_json({"choice": "C", "request_id": "persist-1"})
        loaded = br.load_decision(self.out)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["choice"], "C")
        self.assertEqual(loaded["request_id"], "persist-1")
        self._stop()
        # brand-new handler/server over the same directory sees the vote
        self.start_server()
        status, _, data = self.request("GET", "/api/decision")
        self.assertEqual(status, 200)
        obj = json.loads(data)
        self.assertTrue(obj["voted"])
        self.assertEqual(obj["choice"], "C")
        self.assertIsNone(br.load_decision(self.root / "missing"))

    def test_bad_json_and_non_object(self):
        self.start_server()
        s1, _, _ = self.post_json(None, raw=b"{not-json")
        self.assertEqual(s1, 400)
        s2, _, _ = self.post_json(None, raw=b"[1,2,3]")
        self.assertEqual(s2, 400)

    def test_oversized_body_413(self):
        self.start_server()
        raw = b"x" * (br.MAX_BODY + 1)
        s, _, _ = self.post_json(None, raw=raw)
        self.assertEqual(s, 413)

    def test_cross_origin_rejected_same_origin_accepted(self):
        self.start_server()
        bad = {"choice": "A", "request_id": "origin-1"}
        s, _, _ = self.post_json(bad, headers={"Origin": "http://evil.example"})
        self.assertEqual(s, 403)
        s2, _, d2 = self.post_json(
            bad, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(s2, 200)
        self.assertTrue(json.loads(d2)["keep"])

    def test_unknown_post_route_404(self):
        self.start_server()
        status, _, _ = self.request("POST", "/api/other", body=b"{}",
                                    headers={"Content-Type": "application/json"})
        self.assertEqual(status, 404)

    # ---------- callback ----------

    def test_callback_success_reported(self):
        calls = []
        self.start_server(on_decision=lambda d: calls.append(d["request_id"]))
        s, _, d = self.post_json({"choice": "A", "request_id": "cb-ok-1"})
        self.assertEqual(s, 200)
        obj = json.loads(d)
        self.assertEqual(obj["callback"]["status"], "ok")
        self.assertEqual(calls, ["cb-ok-1"])

    def test_callback_failure_distinct_and_recoverable_without_revote(self):
        calls = []

        def cb(decision):
            calls.append(decision["request_id"])
            if len(calls) == 1:
                raise RuntimeError("boom-5000")

        self.start_server(on_decision=cb)
        payload = {"choice": "A", "request_id": "cb-fail-1"}
        s1, _, d1 = self.post_json(payload)
        self.assertEqual(s1, 200)
        o1 = json.loads(d1)
        self.assertEqual(o1["callback"]["status"], "failed")
        self.assertIn("boom-5000", o1["callback"]["error"])
        # the vote itself is intact despite the callback failure
        self.assertTrue(o1["keep"])
        stored = json.loads((self.out / "private" / "decision.json").read_text())
        self.assertEqual(stored["request_id"], "cb-fail-1")
        # CLI recovery: identical retry reuses the vote and re-attempts callback
        s2, _, d2 = self.post_json(payload)
        self.assertEqual(s2, 200)
        o2 = json.loads(d2)
        self.assertTrue(o2["reused"])
        self.assertEqual(o2["callback"]["status"], "ok")
        self.assertEqual(len(calls), 2)
        # still exactly one immutable decision
        same = json.loads((self.out / "private" / "decision.json").read_text())
        self.assertEqual(same["created_at"], stored["created_at"])

    def test_originals_unchanged_after_http_vote(self):
        self.start_server()
        self.post_json({"choice": "A", "request_id": "final-1"})
        _assert_originals_unchanged(self, self.snap)


if __name__ == "__main__":
    unittest.main()
