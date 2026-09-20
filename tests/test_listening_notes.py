"""时间戳试听笔记：单元测试 + 真实 HTTP 集成测试（合成 WAV / 临时目录隔离）。

覆盖:
- SHA256 流式哈希与 wave 真实时长；
- 两条不同时间点笔记的持久化与重载；
- request_id 幂等重放 / 同 id 改内容 409；
- 音频重新渲染后旧版本行保留、旧 SHA 提交 409；
- bool/NaN/Infinity/越界/字符串时间、缺字段/非法参数；
- 未知 run / 目录穿越 / 符号链接逃逸；
- score review 页面新绑定且保留 /api/revise 分层音量控件。
"""
import hashlib
import json
import math
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import common  # noqa: E402
import feedback  # noqa: E402
import listening_notes as ln  # noqa: E402

RUN = "note-demo"
MANIFEST = {
    "title": "试听曲", "bpm": 120, "bars": 4, "duration_seconds": 1.0,
    "tracks": [{"id": "voice", "name": "人声"}],
}


def write_synth_wav(path: Path, seconds: float = 1.0, sr: int = 8000, freq: float = 440.0):
    """用标准库 wave/struct 合成单声道 16bit WAV（无第三方依赖）。"""
    n = int(round(sr * seconds))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sr)
        chunks = bytearray()
        for i in range(n):
            sample = int(0.25 * 32767 * math.sin(2 * math.pi * freq * i / sr))
            chunks += struct.pack("<h", sample)
        wav.writeframes(bytes(chunks))


def make_run(root: Path, run_id: str = RUN, seconds: float = 1.0, with_files=True):
    song = root / "beats" / run_id
    song.mkdir(parents=True, exist_ok=True)
    (song / "run_manifest.json").write_text(
        json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8")
    (song / "score.json").write_text(json.dumps({"title": MANIFEST["title"]}),
                                     encoding="utf-8")
    if with_files:
        write_synth_wav(song / "full_mix.wav", seconds=seconds)
    return song


class ListeningNotesTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in (("ROOT", self.root), ("DB_PATH", self.root / "db.sqlite")):
            patcher = patch.object(common, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.song = make_run(self.root)
        self.mix = self.song / "full_mix.wav"
        self.expected_sha = ln.sha256_file(self.mix)
        self.expected_duration = ln.read_wav_duration(self.mix)
        self.server = feedback.FeedbackServer(port=0)
        self.thread = threading.Thread(target=self.server.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None):
        data = json.dumps(body, allow_nan=True).encode("utf-8") if body is not None else None
        req = Request(f"http://127.0.0.1:{self.server.port}{path}", data=data,
                      headers={"Content-Type": "application/json"}, method=method)
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read()
            if "json" in response.headers.get("Content-Type", ""):
                return response.status, json.loads(raw)
            return response.status, raw

    def get_notes(self, run_id=RUN):
        return self.call("GET", "/api/listening-notes?" + urlencode({"run_id": run_id}))

    def post_note(self, payload, run_id=RUN, **overrides):
        body = {
            "run_id": run_id,
            "mix_sha256": self.expected_sha,
            "request_id": "req-base",
            "time_seconds": 0.25,
            "category": "noisy",
            "text": "此处有杂音",
        }
        body.update(payload)
        body.update(overrides)
        return self.call("POST", "/api/listening-notes", body)

    def raw_rows(self):
        db_file = self.root / "listening-notes.sqlite"
        if not db_file.exists():
            return []
        conn = sqlite3.connect(str(db_file))
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='listening_notes'"
            ).fetchone()
            if not exists:
                return []
            return conn.execute(
                "SELECT run_id, mix_sha256, request_id, time_seconds, category, text "
                "FROM listening_notes ORDER BY time_seconds").fetchall()
        finally:
            conn.close()


class UnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.song = make_run(self.root)
        self.mix = self.song / "full_mix.wav"

    def test_sha256_matches_file_bytes_and_duration_is_real(self):
        self.assertEqual(
            ln.sha256_file(self.mix),
            hashlib.sha256(self.mix.read_bytes()).hexdigest())
        self.assertEqual(len(ln.sha256_file(self.mix)), 64)
        self.assertAlmostEqual(ln.read_wav_duration(self.mix), 1.0, places=6)

    def test_validate_rejects_bool_nan_infinity_out_of_range(self):
        for bad in (True, False, "0.5", None, float("nan"), float("inf"),
                    float("-inf"), 10**400, -0.01, 1.0001):
            with self.assertRaises(ln.InvalidNoteError, msg=f"time={bad!r}"):
                ln.validate_note(
                    {"request_id": "r1", "mix_sha256": "a" * 64,
                     "time_seconds": bad, "category": "other", "text": "x"}, 1.0)

    def test_validate_accepts_boundary_times(self):
        for t in (0, 0.5, 1, 1.0):
            cleaned = ln.validate_note(
                {"request_id": "r1", "mix_sha256": "a" * 64,
                 "time_seconds": t, "category": "like", "text": "  ok  "}, 1.0)
            self.assertEqual(cleaned["text"], "ok")

    def test_resolve_rejects_traversal_and_missing(self):
        for bad in ("", " ", ".", "..", "../outside", "a/b", "a\\b", "x\x00y"):
            with self.assertRaises(ln.InvalidNoteError, msg=f"run_id={bad!r}"):
                ln.resolve_run(self.root, bad)
        with self.assertRaises(FileNotFoundError):
            ln.resolve_run(self.root, "does-not-exist")

    def test_resolve_rejects_symlink_escape(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        write_synth_wav(outside / "full_mix.wav")
        (outside / "run_manifest.json").write_text("{}")
        link = self.root / "beats" / "evil-link"
        os.symlink(outside, link, target_is_directory=True)
        with self.assertRaises(FileNotFoundError):
            ln.resolve_run(self.root, "evil-link")

    def test_requires_manifest_and_wav_within_run_dir(self):
        incomplete = make_run(self.root, "incomplete", with_files=False)
        with self.assertRaises(FileNotFoundError):
            ln.current_version(self.root, "incomplete")
        # 指向库外的 full_mix.wav 符号链接也必须被拒绝
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        foreign = outside / "foreign.wav"
        write_synth_wav(foreign)
        (incomplete / "full_mix.wav").symlink_to(foreign)
        with self.assertRaises(FileNotFoundError):
            ln.current_version(self.root, "incomplete")

    def test_add_note_direct_call_is_idempotent(self):
        body = {"run_id": RUN, "mix_sha256": ln.sha256_file(self.mix),
                "request_id": "req-1", "time_seconds": 0.2, "category": "like",
                "text": "不错"}
        first = ln.add_note(self.root, body)
        second = ln.add_note(self.root, body)
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        with self.assertRaises(ln.ConflictError):
            ln.add_note(self.root, dict(body, text="被改过的内容"))


class HttpPersistenceTests(ListeningNotesTestBase):
    def test_persist_reload_uses_real_sha_and_duration_without_touching_song(self):
        before = self.mix.read_bytes()
        status, body = self.post_note({"request_id": "req-1"})
        self.assertEqual(status, 200, body)
        note = body["note"]
        self.assertEqual(note["run_id"], RUN)
        self.assertEqual(note["mix_sha256"], self.expected_sha)
        self.assertEqual(note["request_id"], "req-1")
        self.assertAlmostEqual(note["time_seconds"], 0.25, places=6)
        self.assertEqual(note["category"], "noisy")
        self.assertEqual(note["text"], "此处有杂音")
        self.assertTrue(note["id"])
        self.assertTrue(note["created_at"].endswith("+00:00"))
        # 歌曲文件原样保留，笔记落在独立 DB
        self.assertEqual(self.mix.read_bytes(), before)
        self.assertTrue((self.root / "listening-notes.sqlite").is_file())
        self.assertFalse((self.root / "db.sqlite").exists())

        status, body = self.get_notes()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["mix_sha256"], self.expected_sha)
        self.assertAlmostEqual(body["duration_seconds"], self.expected_duration, places=6)
        self.assertEqual(len(body["notes"]), 1)
        self.assertEqual(body["notes"][0]["text"], "此处有杂音")

    def test_two_notes_at_different_times_are_both_preserved(self):
        status, _ = self.post_note({"request_id": "req-late", "time_seconds": 0.9})
        self.assertEqual(status, 200)
        status, _ = self.post_note({"request_id": "req-early", "time_seconds": 0.2})
        self.assertEqual(status, 200)
        status, body = self.get_notes()
        self.assertEqual(status, 200, body)
        times = [n["time_seconds"] for n in body["notes"]]
        self.assertEqual(times, [0.2, 0.9])
        self.assertEqual(len({n["id"] for n in body["notes"]}), 2)

    def test_retry_same_request_is_idempotent(self):
        status, first = self.post_note({"request_id": "req-dup", "text": "重复提交"})
        self.assertEqual(status, 200, first)
        status, second = self.post_note({"request_id": "req-dup", "text": "重复提交"})
        self.assertEqual(status, 200, second)
        self.assertEqual(first["note"]["id"], second["note"]["id"])
        self.assertTrue(second["note"]["deduplicated"])
        self.assertEqual(len(self.raw_rows()), 1)

    def test_conflicting_retry_same_id_changed_payload_rejected(self):
        status, _ = self.post_note({"request_id": "req-c", "category": "like",
                                    "text": "原版内容"})
        self.assertEqual(status, 200)
        status, body = self.post_note({"request_id": "req-c", "category": "drums",
                                       "text": "改成鼓组问题"})
        self.assertEqual(status, 409, body)
        self.assertIn("request_id", body["error"])
        status, body = self.get_notes()
        self.assertEqual(len(body["notes"]), 1)
        self.assertEqual(body["notes"][0]["category"], "like")
        self.assertEqual(body["notes"][0]["text"], "原版内容")

    def test_stale_audio_version_fails_and_old_rows_are_preserved(self):
        status, _ = self.post_note({"request_id": "req-old", "text": "旧版本笔记"})
        self.assertEqual(status, 200)
        old_sha = self.expected_sha

        # 重新渲染：同路径写更长的新 WAV
        write_synth_wav(self.mix, seconds=2.0, freq=660)
        new_sha = ln.sha256_file(self.mix)
        self.assertNotEqual(old_sha, new_sha)

        status, body = self.get_notes()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["mix_sha256"], new_sha)
        self.assertAlmostEqual(body["duration_seconds"], 2.0, places=6)
        self.assertEqual(body["notes"], [])  # 当前版本暂无笔记

        # 旧版本行仍保留在独立 DB 中
        rows = self.raw_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], old_sha)

        # 用旧 SHA 提交必须明确失败
        status, body = self.post_note({"request_id": "req-old2",
                                       "mix_sha256": old_sha,
                                       "time_seconds": 0.3})
        self.assertEqual(status, 409, body)
        self.assertIn("音频版本", body["error"])
        self.assertEqual(len(self.raw_rows()), 1)

        # 用新 SHA 可继续记录，新旧两行共存
        self.expected_sha = new_sha
        status, body = self.post_note({"request_id": "req-new",
                                       "mix_sha256": new_sha,
                                       "time_seconds": 1.5, "text": "新版本笔记"})
        self.assertEqual(status, 200, body)
        shas = {row[1] for row in self.raw_rows()}
        self.assertEqual(shas, {old_sha, new_sha})
        self.assertEqual(len(self.raw_rows()), 2)


class HttpValidationTests(ListeningNotesTestBase):
    def test_boundary_times_accepted(self):
        status, _ = self.post_note({"request_id": "req-zero", "time_seconds": 0})
        self.assertEqual(status, 200)
        status, _ = self.post_note({"request_id": "req-end",
                                    "time_seconds": self.expected_duration})
        self.assertEqual(status, 200)

    def test_bad_times_rejected(self):
        for bad in (True, False, float("nan"), float("inf"), float("-inf"),
                    10**400, -0.01, 1.001, "0.5", None, []):
            status, body = self.post_note({"request_id": f"req-{type(bad).__name__}",
                                           "time_seconds": bad})
            self.assertEqual(status, 400, f"time={bad!r} -> {body}")
        self.assertEqual(self.raw_rows(), [])

    def test_missing_and_invalid_fields_rejected(self):
        good = {"mix_sha256": self.expected_sha, "request_id": "req-ok",
                "time_seconds": 0.25, "category": "other", "text": "内容"}
        for key in list(good) + ["run_id"]:
            body = dict(good, run_id=RUN)
            body.pop(key)
            status, response = self.call("POST", "/api/listening-notes", body)
            self.assertEqual(status, 400, f"missing {key} -> {response}")

        bad_cases = [
            {"request_id": ""}, {"request_id": "x" * 129}, {"request_id": 7},
            {"mix_sha256": "z" * 64}, {"mix_sha256": "A" * 64},
            {"mix_sha256": "a" * 63}, {"mix_sha256": 123},
            {"category": "loud"}, {"category": "LIKE"}, {"category": None},
            {"text": "   "}, {"text": "x" * 1001}, {"text": 9},
        ]
        for case in bad_cases:
            status, response = self.post_note(case)
            self.assertEqual(status, 400, f"{case} -> {response}")
        self.assertEqual(self.raw_rows(), [])


class HttpPathSafetyTests(ListeningNotesTestBase):
    def test_unknown_run_returns_404(self):
        status, body = self.get_notes("missing-song")
        self.assertEqual(status, 404, body)
        status, body = self.post_note({}, run_id="missing-song")
        self.assertEqual(status, 404, body)

    def test_path_traversal_rejected(self):
        query = urlencode({"run_id": "../../etc/passwd"})
        status, body = self.call("GET", f"/api/listening-notes?{query}")
        self.assertEqual(status, 400, body)
        status, body = self.post_note({}, run_id="../note-demo")
        self.assertEqual(status, 400, body)

    def test_symlink_escape_rejected_over_http(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        write_synth_wav(outside / "full_mix.wav")
        (outside / "run_manifest.json").write_text("{}")
        os.symlink(outside, self.root / "beats" / "evil-link",
                   target_is_directory=True)
        status, body = self.get_notes("evil-link")
        self.assertIn(status, (400, 404), body)
        self.assertNotEqual(status, 500)


class ReviewPageTests(ListeningNotesTestBase):
    def test_page_has_notes_bindings_and_preserves_revise_controls(self):
        status, page = self.call("GET", f"/review/{RUN}")
        self.assertEqual(status, 200)
        self.assertIsInstance(page, bytes)
        html_text = page.decode("utf-8")
        # 新增：主音频 id / 笔记控件 / 中文类别 / API 绑定
        self.assertIn('id="main-audio"', html_text)
        self.assertIn("/api/listening-notes", html_text)
        self.assertIn('id="capture-time"', html_text)
        self.assertIn('id="note-time"', html_text)
        self.assertIn('id="note-category"', html_text)
        self.assertIn('id="note-text"', html_text)
        self.assertIn('id="save-note"', html_text)
        self.assertIn('id="notes-history"', html_text)
        for label in ("太嘈杂", "重复", "鼓组", "过渡", "喜欢", "其他"):
            self.assertIn(label, html_text)
        self.assertIn('aria-live="polite"', html_text)
        self.assertIn('for="note-text"', html_text)
        # 既有：分层音量反馈版控件与 /api/revise 原行为保留
        self.assertIn("/api/revise", html_text)
        self.assertIn('data-track="voice"', html_text)
        self.assertIn('id="make"', html_text)
        self.assertIn("生成反馈版", html_text)
        self.assertIn(RUN, html_text)


if __name__ == "__main__":
    unittest.main()
