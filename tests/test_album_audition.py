"""Actual-HTTP tests for the BeatLab ten-beat audition server.

Covers: index page, collection/feedback APIs, save+reload, strict rating
validation (bool/float/out-of-range/unknown fields), stale version/hash,
oversize bodies, media GET/HEAD with single byte ranges 206/416, path
traversal / symlink escape rejection, undeclared-file rejection, idempotent
duplicate submissions, concurrent saves, and guaranteeing that server-side
write errors never destroy previously persisted feedback.
"""

import hashlib
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

# Make the workspace root importable regardless of unittest discovery cwd.
_WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(_WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE_ROOT))

from examples import album_audition as aa  # noqa: E402

HOST = "127.0.0.1"
MEDIA_BYTES = bytes((i * 7 + 3) % 256 for i in range(512))
WAV_BYTES = bytes((i * 13 + 1) % 256 for i in range(400))


def make_collection(base: Path, n=3, titles=None):
    """Populate *base* with a valid collection + declared media files."""
    audio_dir = base / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    tracks = []
    titles = titles or {}
    for i in range(n):
        tid = "t%02d" % (i + 1)
        mp3_rel = f"audio/{tid}.mp3"
        wav_rel = f"audio/{tid}.wav"
        (audio_dir / f"{tid}.mp3").write_bytes(MEDIA_BYTES)
        (audio_dir / f"{tid}.wav").write_bytes(WAV_BYTES)
        mix_sha = hashlib.sha256(b"mix:" + tid.encode()).hexdigest()
        tracks.append(
            {
                "id": tid,
                "number": i + 1,
                "title": titles.get(i, f"第 {i + 1} 首草稿"),
                "bpm": 88 + i,
                "duration_seconds": 60 + i * 5,
                "direction": "先保留鼓组骨架，副歌处做对比切换" + ("。" * i),
                "audio": mp3_rel,
                "original_audio": wav_rel,
                "source_summary": [
                    {"title": f"来源唱片 {i + 1}", "source_url": "https://example.invalid/src"}
                ],
                "editable_path": str(base / "stems" / tid),
                "version": "v1",
                "mix_sha256": mix_sha,
                "change_point_seconds": 15 + i,
            }
        )
    manifest = {
        "id": "col-test-10",
        "title": "十首改编试听（测试合集）",
        "subtitle": "暖光编辑主题 · 首轮草稿",
        "tracks": tracks,
    }
    (base / "collection.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


class ServerHarness:
    def __init__(self, base: Path, port=0):
        self.base = Path(base)
        self.server = aa.create_server(self.base, host=HOST, port=port)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection(HOST, self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        result = (resp.status, dict(resp.getheaders()), data)
        conn.close()
        return result

    def get_json(self, path):
        status, headers, data = self.request("GET", path)
        return status, headers, json.loads(data.decode("utf-8"))

    def post_json(self, obj, path="/api/feedback", raw=False):
        body = obj if raw else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return self.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json"},
        )


class AuditionServerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.manifest = make_collection(self.base)
        self.h = ServerHarness(self.base)
        self.track = self.manifest["tracks"][0]

    def tearDown(self):
        self.h.stop()
        self._tmp.cleanup()

    def valid_payload(self, **overrides):
        payload = {
            "track_id": self.track["id"],
            "version": self.track["version"],
            "mix_sha256": self.track["mix_sha256"],
            "ratings": {k: None for k in aa.RATING_KEYS},
            "keep": "undecided",
            "notes": "",
            "timestamp_notes": [],
        }
        payload.update(overrides)
        return payload

    # -- page / collection --------------------------------------------------

    def test_index_page_is_chinese_html(self):
        status, headers, data = self.h.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        text = data.decode("utf-8")
        self.assertIn('lang="zh-CN"', text)
        for phrase in ("律动", "采样记忆点", "变化", "清晰度", "留给人声的空间"):
            self.assertIn(phrase, text)
        self.assertIn("第一阶段试听草稿", text)
        self.assertIn("段落变化", text)
        self.assertIn("textContent", text)
        self.assertNotIn("Access-Control-Allow-Origin", text)

    def test_collection_api_matches_manifest(self):
        status, headers, data = self.h.get_json("/api/collection")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(data["id"], self.manifest["id"])
        self.assertEqual(len(data["tracks"]), 3)
        self.assertEqual(data["tracks"][1]["mix_sha256"], self.manifest["tracks"][1]["mix_sha256"])

    def test_feedback_initially_empty(self):
        status, _, data = self.h.get_json("/api/feedback")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"collection_id": self.manifest["id"], "ratings": {}})

    def test_unknown_route_404(self):
        status, _, data = self.h.request("GET", "/nope")
        self.assertEqual(status, 404)
        err = json.loads(data)["error"]
        self.assertEqual(err["code"], "not_found")

    # -- save / reload / download ------------------------------------------

    def test_save_and_reload_persists_record(self):
        payload = self.valid_payload(
            ratings={
                "groove": 4,
                "sample": 3,
                "variation": None,
                "clarity": 5,
                "rap_space": 2,
            },
            keep="revise",
            notes="副歌前鼓太满；希望多留一点人声空间。",
            timestamp_notes=[
                {"seconds": 15.0, "text": "这里切换对比明显"},
                {"seconds": 42.25, "text": "采样有点糊"},
            ],
        )
        status, _, data = self.h.post_json(payload)
        self.assertEqual(status, 200)
        record = json.loads(data.decode("utf-8"))
        self.assertEqual(record["track_id"], self.track["id"])
        self.assertEqual(record["ratings"]["groove"], 4)
        self.assertEqual(record["ratings"]["variation"], None)
        self.assertEqual(record["keep"], "revise")
        self.assertTrue(record["updated_at"].endswith("Z"))
        self.assertEqual(len(record["timestamp_notes"]), 2)

        on_disk = json.loads(
            (self.base / "feedback.json").read_text(encoding="utf-8")
        )
        self.assertIn(self.track["id"], on_disk["ratings"])

        status, _, reloaded = self.h.get_json("/api/feedback")
        self.assertEqual(status, 200)
        got = reloaded["ratings"][self.track["id"]]
        self.assertEqual(got["notes"], payload["notes"])
        self.assertEqual(got["timestamp_notes"][1]["seconds"], 42.25)
        self.assertEqual(got["version"], "v1")
        self.assertEqual(got["mix_sha256"], self.track["mix_sha256"])

    def test_null_ratings_and_optional_fields_accepted(self):
        status, _, data = self.h.post_json(
            {
                "track_id": self.track["id"],
                "version": "v1",
                "mix_sha256": self.track["mix_sha256"],
                "ratings": {k: None for k in aa.RATING_KEYS},
                "keep": "undecided",
            }
        )
        self.assertEqual(status, 200, data)
        record = json.loads(data)
        self.assertTrue(all(v is None for v in record["ratings"].values()))

    def test_download_is_attachment(self):
        status, headers, _ = self.h.post_json(self.valid_payload(keep="keep"))
        self.assertEqual(status, 200)
        status, headers, data = self.h.request("GET", "/api/feedback/download")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("feedback.json", headers["Content-Disposition"])
        parsed = json.loads(data)
        self.assertIn(self.track["id"], parsed["ratings"])

    def test_exact_duplicate_is_idempotent(self):
        payload = self.valid_payload(
            ratings={
                "groove": 3,
                "sample": None,
                "variation": 4,
                "clarity": None,
                "rap_space": None,
            },
            notes="重复提交测试",
        )
        s1, _, d1 = self.h.post_json(payload)
        self.assertEqual(s1, 200)
        r1 = json.loads(d1)
        s2, _, d2 = self.h.post_json(payload)
        self.assertEqual(s2, 200)
        r2 = json.loads(d2)
        self.assertEqual(r1["updated_at"], r2["updated_at"])
        _, _, feedback = self.h.get_json("/api/feedback")
        self.assertEqual(len(feedback["ratings"]), 1)

    def test_update_same_track_keeps_single_record(self):
        p1 = self.valid_payload(keep="undecided", notes="第一版意见")
        s, _, _ = self.h.post_json(p1)
        self.assertEqual(s, 200)
        p2 = self.valid_payload(
            keep="keep",
            notes="第二版意见",
            ratings={
                "groove": 5,
                "sample": 4,
                "variation": 4,
                "clarity": 5,
                "rap_space": 3,
            },
        )
        s, _, d = self.h.post_json(p2)
        self.assertEqual(s, 200)
        record = json.loads(d)
        self.assertEqual(record["keep"], "keep")
        _, _, feedback = self.h.get_json("/api/feedback")
        self.assertEqual(len(feedback["ratings"]), 1)
        self.assertEqual(
            feedback["ratings"][self.track["id"]]["ratings"]["groove"], 5
        )

    # -- invalid submissions -----------------------------------------------

    def _assert_rejected_without_mutation(self, payload, expected_status=400):
        before = (self.base / "feedback.json").read_bytes()
        status, _, data = self.h.post_json(payload)
        self.assertEqual(status, expected_status, data)
        err = json.loads(data)["error"]
        self.assertTrue(err["code"])
        after = (self.base / "feedback.json").read_bytes()
        self.assertEqual(before, after, "被拒绝的请求不得改动 feedback.json")

    def test_invalid_submissions_never_mutate_feedback(self):
        # seed a valid record first
        seed = self.valid_payload(keep="revise", notes="既有意见，不得丢失")
        status, _, _ = self.h.post_json(seed)
        self.assertEqual(status, 200)

        bad_ratings = [
            6, 0, True, False, 3.5, "4",
        ]
        for bad in bad_ratings:
            with self.subTest(bad=bad):
                ratings = {k: None for k in aa.RATING_KEYS}
                ratings["groove"] = bad
                self._assert_rejected_without_mutation(
                    self.valid_payload(ratings=ratings)
                )

        with self.subTest("unknown rating field"):
            ratings = {k: None for k in aa.RATING_KEYS}
            ratings["energy"] = 5
            self._assert_rejected_without_mutation(
                self.valid_payload(ratings=ratings)
            )

        with self.subTest("missing rating field"):
            ratings = {k: None for k in aa.RATING_KEYS}
            del ratings["clarity"]
            self._assert_rejected_without_mutation(
                self.valid_payload(ratings=ratings)
            )

        with self.subTest("bad keep"):
            self._assert_rejected_without_mutation(
                self.valid_payload(keep="maybe")
            )

        with self.subTest("unknown top-level field"):
            self._assert_rejected_without_mutation(
                self.valid_payload(extra="nope")
            )

        with self.subTest("notes too long"):
            self._assert_rejected_without_mutation(
                self.valid_payload(notes="长" * (aa.MAX_NOTES_CHARS + 1))
            )

        with self.subTest("bad timestamp seconds"):
            self._assert_rejected_without_mutation(
                self.valid_payload(
                    timestamp_notes=[{"seconds": 99999, "text": "超出时长"}]
                )
            )
            self._assert_rejected_without_mutation(
                self.valid_payload(
                    timestamp_notes=[{"seconds": -1, "text": "负数"}]
                )
            )
            self._assert_rejected_without_mutation(
                self.valid_payload(
                    timestamp_notes=[{"seconds": True, "text": "布尔"}]
                )
            )

        with self.subTest("timestamp text wrong type"):
            self._assert_rejected_without_mutation(
                self.valid_payload(
                    timestamp_notes=[{"seconds": 10, "text": 123}]
                )
            )

        # prior feedback survived everything
        _, _, feedback = self.h.get_json("/api/feedback")
        surviving = feedback["ratings"][self.track["id"]]
        self.assertEqual(surviving["notes"], "既有意见，不得丢失")
        self.assertEqual(surviving["keep"], "revise")

    def test_unknown_track_rejected(self):
        payload = self.valid_payload(track_id="does-not-exist")
        status, _, data = self.h.post_json(payload)
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(data)["error"]["code"], "unknown_track")

    def test_stale_hash_and_version_rejected(self):
        payload = self.valid_payload(mix_sha256="0" * 64)
        status, _, data = self.h.post_json(payload)
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(data)["error"]["code"], "stale_version")

        payload = self.valid_payload(version="v9")
        status, _, data = self.h.post_json(payload)
        self.assertEqual(status, 409)
        _, _, feedback = self.h.get_json("/api/feedback")
        self.assertEqual(feedback["ratings"], {})

    def test_malformed_json_rejected(self):
        status, _, data = self.h.request(
            "POST",
            "/api/feedback",
            body=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(data)["error"]["code"], "invalid_json")

    def test_body_too_large_rejected(self):
        big = self.valid_payload(notes="x" * (aa.MAX_BODY_BYTES))
        body = json.dumps(big).encode("utf-8")
        self.assertGreater(len(body), aa.MAX_BODY_BYTES)
        status, headers, data = self.h.request(
            "POST",
            "/api/feedback",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(data)["error"]["code"], "body_too_large")
        self.assertFalse((self.base / "feedback.json").exists())

    # -- media --------------------------------------------------------------

    def test_media_get_declared(self):
        status, headers, data = self.h.request("GET", "/media/audio/t01.mp3")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "audio/mpeg")
        self.assertEqual(headers["Content-Length"], str(len(MEDIA_BYTES)))
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(data, MEDIA_BYTES)

        status, headers, data = self.h.request("GET", "/media/audio/t01.wav")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "audio/wav")
        self.assertEqual(data, WAV_BYTES)

    def test_media_head(self):
        status, headers, data = self.h.request("HEAD", "/media/audio/t01.mp3")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Length"], str(len(MEDIA_BYTES)))
        self.assertEqual(headers["Content-Type"], "audio/mpeg")
        self.assertEqual(data, b"")

    def test_media_ranges_206_and_seek(self):
        status, headers, data = self.h.request(
            "GET",
            "/media/audio/t01.mp3",
            headers={"Range": "bytes=10-19"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], f"bytes 10-19/{len(MEDIA_BYTES)}")
        self.assertEqual(headers["Content-Length"], "10")
        self.assertEqual(data, MEDIA_BYTES[10:20])

        status, headers, data = self.h.request(
            "GET", "/media/audio/t01.mp3", headers={"Range": "bytes=500-"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], f"bytes 500-511/{len(MEDIA_BYTES)}")
        self.assertEqual(data, MEDIA_BYTES[500:])

        status, headers, data = self.h.request(
            "GET", "/media/audio/t01.mp3", headers={"Range": "bytes=-20"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], f"bytes 492-511/{len(MEDIA_BYTES)}")
        self.assertEqual(data, MEDIA_BYTES[-20:])

        status, headers, data = self.h.request(
            "GET",
            "/media/audio/t01.mp3",
            headers={"Range": "bytes=100-999"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], f"bytes 100-511/{len(MEDIA_BYTES)}")
        self.assertEqual(data, MEDIA_BYTES[100:])

    def test_media_range_416(self):
        status, headers, data = self.h.request(
            "GET",
            "/media/audio/t01.mp3",
            headers={"Range": "bytes=1000000-1000001"},
        )
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], f"bytes */{len(MEDIA_BYTES)}")

        status, _, _ = self.h.request(
            "GET",
            "/media/audio/t01.mp3",
            headers={"Range": "bytes=0-9,20-29"},
        )
        self.assertEqual(status, 416)

        status, _, _ = self.h.request(
            "GET",
            "/media/audio/t01.mp3",
            headers={"Range": "chars=0-9"},
        )
        self.assertEqual(status, 416)

    def test_media_path_traversal_and_undeclared_rejected(self):
        for bad in (
            "/media/..%2fcollection.json",
            "/media/audio/../../collection.json",
            "/media/audio/../t01.wav/../../collection.json",
            "/media/collection.json",
            "/media/feedback.json",
            "/media/audio/nonexistent.mp3",
            "/media/audio",
        ):
            with self.subTest(bad=bad):
                status, _, _ = self.h.request("GET", bad)
                self.assertIn(status, (403, 404))

        status, _, _ = self.h.request("HEAD", "/media/feedback.json")
        self.assertEqual(status, 404)

    def test_media_missing_declared_file_404(self):
        (self.base / "audio" / "t03.mp3").unlink()
        status, _, data = self.h.request("GET", "/media/audio/t03.mp3")
        self.assertEqual(status, 404)
        err = json.loads(data)["error"]
        self.assertEqual(err["code"], "media_missing")
        self.assertIn("t03.mp3", err["message"])

    def test_media_symlink_escape_rejected(self):
        outside_dir = Path(tempfile.mkdtemp())
        try:
            outside_file = outside_dir / "secret.mp3"
            outside_file.write_bytes(b"escaped-bytes")
            link = self.base / "audio" / "evil.mp3"
            link.symlink_to(outside_file)
            # declare the link in the manifest as an extra track
            manifest = json.loads(
                (self.base / "collection.json").read_text(encoding="utf-8")
            )
            manifest["tracks"].append(
                {
                    "id": "evil",
                    "number": 4,
                    "title": "逃逸链接",
                    "bpm": 90,
                    "duration_seconds": 10,
                    "direction": "x",
                    "audio": "audio/evil.mp3",
                    "original_audio": "audio/evil.mp3",
                    "source_summary": [],
                    "editable_path": str(self.base),
                    "version": "v1",
                    "mix_sha256": hashlib.sha256(b"evil").hexdigest(),
                    "change_point_seconds": 0,
                }
            )
            (self.base / "collection.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            # restart so the running server picks up the amended manifest
            self.h.stop()
            self.h = ServerHarness(self.base)
            status, _, data = self.h.request("GET", "/media/audio/evil.mp3")
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(data)["error"]["code"], "media_escape")
            status, _, _ = self.h.request(
                "GET",
                "/media/audio/evil.mp3",
                headers={"Range": "bytes=0-3"},
            )
            self.assertEqual(status, 403)
        finally:
            import shutil

            shutil.rmtree(outside_dir, ignore_errors=True)

    # -- concurrency / durability ------------------------------------------

    def test_concurrent_saves_distinct_tracks(self):
        self.h.stop()
        self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.manifest = make_collection(self.base, n=10)
        self.h = ServerHarness(self.base)

        def worker(idx):
            tid = "t%02d" % (idx + 1)
            track = self.manifest["tracks"][idx]
            payload = {
                "track_id": tid,
                "version": "v1",
                "mix_sha256": track["mix_sha256"],
                "ratings": {
                    "groove": (idx % 5) + 1,
                    "sample": None,
                    "variation": None,
                    "clarity": None,
                    "rap_space": None,
                },
                "keep": "undecided",
                "notes": f"线程 {idx}",
                "timestamp_notes": [],
            }
            status, _, _ = self.h.post_json(payload)
            self.assertEqual(status, 200)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            self.assertFalse(t.is_alive())

        _, _, feedback = self.h.get_json("/api/feedback")
        self.assertEqual(len(feedback["ratings"]), 10)
        # file itself is valid JSON
        json.loads((self.base / "feedback.json").read_text(encoding="utf-8"))

    def test_concurrent_saves_same_track_ends_consistent(self):
        track = self.manifest["tracks"][1]

        def worker(idx):
            payload = {
                "track_id": track["id"],
                "version": "v1",
                "mix_sha256": track["mix_sha256"],
                "ratings": {
                    "groove": (idx % 5) + 1,
                    "sample": None,
                    "variation": None,
                    "clarity": None,
                    "rap_space": None,
                },
                "keep": "undecided",
                "notes": f"v{idx}",
                "timestamp_notes": [],
            }
            status, _, _ = self.h.post_json(payload)
            self.assertEqual(status, 200)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        _, _, feedback = self.h.get_json("/api/feedback")
        self.assertEqual(len(feedback["ratings"]), 1)
        rec = feedback["ratings"][track["id"]]
        self.assertIn(rec["ratings"]["groove"], (1, 2, 3, 4, 5))
        self.assertTrue(rec["notes"].startswith("v"))

    def test_write_error_does_not_lose_previous_feedback(self):
        seed = self.valid_payload(keep="keep", notes="重要的既有记录")
        status, _, _ = self.h.post_json(seed)
        self.assertEqual(status, 200)

        original_replace = aa.os.replace
        broken = threading.Event()

        def fail_replace(src, dst):
            if broken.is_set():
                raise OSError("simulated disk failure")
            return original_replace(src, dst)

        aa.os.replace = fail_replace
        try:
            broken.set()
            changed = self.valid_payload(keep="reject", notes="试图覆盖")
            status, _, data = self.h.post_json(changed)
            self.assertEqual(status, 500)
            self.assertEqual(json.loads(data)["error"]["code"], "write_failed")
        finally:
            broken.clear()
            aa.os.replace = original_replace

        _, _, feedback = self.h.get_json("/api/feedback")
        rec = feedback["ratings"][self.track["id"]]
        self.assertEqual(rec["keep"], "keep")
        self.assertEqual(rec["notes"], "重要的既有记录")

        # server still writable after recovery
        status, _, _ = self.h.post_json(
            self.valid_payload(keep="revise", notes="故障后恢复保存")
        )
        self.assertEqual(status, 200)


class ManifestBoundsTestCase(unittest.TestCase):
    def _server_with_n(self, n):
        tmp = tempfile.TemporaryDirectory()
        base = Path(tmp.name)
        make_collection(base, n=n)
        return tmp, base

    def test_ten_tracks_normal(self):
        tmp, base = self._server_with_n(10)
        try:
            server = aa.create_server(base, host=HOST, port=0)
            self.assertEqual(len(server.manifest["tracks"]), 10)
            server.server_close()
        finally:
            tmp.cleanup()

    def test_one_track_allowed(self):
        tmp, base = self._server_with_n(1)
        try:
            server = aa.create_server(base, host=HOST, port=0)
            self.assertEqual(len(server.manifest["tracks"]), 1)
            server.server_close()
        finally:
            tmp.cleanup()

    def test_zero_and_too_many_tracks_rejected(self):
        tmp, base = self._server_with_n(2)
        try:
            manifest = json.loads((base / "collection.json").read_text())
            manifest["tracks"] = []
            (base / "collection.json").write_text(json.dumps(manifest))
            with self.assertRaises(aa.ManifestError):
                aa.create_server(base, host=HOST, port=0)
        finally:
            tmp.cleanup()

        tmp, base = self._server_with_n(2)
        try:
            manifest = json.loads((base / "collection.json").read_text())
            cloned = manifest["tracks"][0]
            manifest["tracks"] = [dict(cloned, id=f"x{i}") for i in range(21)]
            (base / "collection.json").write_text(json.dumps(manifest))
            with self.assertRaises(aa.ManifestError):
                aa.create_server(base, host=HOST, port=0)
        finally:
            tmp.cleanup()

    def test_bad_sha_rejected(self):
        tmp, base = self._server_with_n(2)
        try:
            manifest = json.loads((base / "collection.json").read_text())
            manifest["tracks"][0]["mix_sha256"] = "not-a-hash"
            (base / "collection.json").write_text(json.dumps(manifest))
            with self.assertRaises(aa.ManifestError):
                aa.create_server(base, host=HOST, port=0)
        finally:
            tmp.cleanup()

    def test_missing_collection_dir_rejected(self):
        with self.assertRaises(aa.ManifestError):
            aa.create_server("/nonexistent/path/xyz", host=HOST, port=0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
