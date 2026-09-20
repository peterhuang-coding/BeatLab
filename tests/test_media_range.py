"""真实 HTTP 测试：/media 单字节区间 GET + beats 目录包含校验。

用 TemporaryDirectory 作为 BEATLAB_ROOT，启动真实 FeedbackServer（127.0.0.1:0），
全部请求走 urllib / 原生 socket（仅本机回环），不触碰真实歌曲文件。
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import socket
import struct
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

# The full suite may already have imported common. Patch its live root during
# this module only; changing the process environment at import is not isolation.
_TMP = None
_ROOT = None

_PIPELINE = Path(__file__).resolve().parent.parent / "pipeline"
sys.path.insert(0, str(_PIPELINE))

import common  # noqa: E402
import feedback  # noqa: E402

HOST = "127.0.0.1"
TARGET = "tgt"


def wav_bytes(payload: bytes, sample_rate: int = 44100) -> bytes:
    """合成最小合法 PCM mono 16bit WAV（44 字节头 + payload）。"""
    assert len(payload) % 2 == 0
    return (
        b"RIFF" + struct.pack("<I", 36 + len(payload)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                                sample_rate * 2, 2, 16)
        + b"data" + struct.pack("<I", len(payload))
        + payload
    )


WAV_DATA = wav_bytes(bytes(i % 256 for i in range(1024)))  # 1068 字节
WAV_SIZE = len(WAV_DATA)
ALS_DATA = b"AbletonLiveSet\x00\x01\x02" * 8
BIN_DATA = bytes((i * 7) % 256 for i in range(333))
BIG_SIZE = 4 * 1024 * 1024
OUTSIDE_DATA = b"OUTSIDE-SECRET-" * 10
OUTSIDE_DIR_DATA = b"OUTSIDE-DIR-SECRET"

SERVER = None
PORT = 0


def setUpModule() -> None:
    global SERVER, PORT, _TMP, _ROOT, _HAS_SYMLINKS
    _TMP = tempfile.TemporaryDirectory()
    _ROOT = Path(_TMP.name)
    unittest.addModuleCleanup(_TMP.cleanup)
    for name, value in (("ROOT", _ROOT), ("DB_PATH", _ROOT / 'db.sqlite')):
        root_patch = patch.object(common, name, value)
        root_patch.start()
        unittest.addModuleCleanup(root_patch.stop)
    beats = _ROOT / "beats" / TARGET
    (beats / "sub").mkdir(parents=True, exist_ok=True)
    (beats / "audio.wav").write_bytes(WAV_DATA)
    (beats / "empty.wav").write_bytes(b"")
    (beats / "song.als").write_bytes(ALS_DATA)
    (beats / "sub" / "extra.bin").write_bytes(BIN_DATA)
    # 大文件用于断连流式测试（分块填充，不整体驻留测试内存）
    with (beats / "big.bin").open("wb") as fh:
        chunk = bytes((i % 256 for i in range(64 * 1024)))
        written = 0
        while written < BIG_SIZE:
            fh.write(chunk)
            written += len(chunk)
    # beats 之外的合成秘密文件（穿越/逃逸目标）
    (_ROOT / "outside_secret.wav").write_bytes(OUTSIDE_DATA)
    outside_dir = _ROOT / "outside_dir"
    outside_dir.mkdir(exist_ok=True)
    (outside_dir / "secret.wav").write_bytes(OUTSIDE_DIR_DATA)
    # 符号链接逃逸：文件与目录两种
    try:
        os.symlink(_ROOT / "outside_secret.wav", beats / "escape.wav")
        os.symlink(outside_dir, beats / "linkdir")
        _HAS_SYMLINKS = True
    except (OSError, NotImplementedError):
        _HAS_SYMLINKS = False
    SERVER = feedback.FeedbackServer(port=0)
    PORT = SERVER.port
    import threading
    threading.Thread(target=SERVER.httpd.serve_forever, daemon=True).start()


def tearDownModule() -> None:
    if SERVER is not None:
        SERVER.shutdown()


# ---------- HTTP 工具 ----------
def _base() -> str:
    return f"http://{HOST}:{PORT}"


def http_get(path: str, range_header: str | None = None):
    headers = {}
    if range_header is not None:
        headers["Range"] = range_header
    req = urllib.request.Request(_base() + path, headers=headers, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status, {k: v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k: v for k, v in exc.headers.items()}, exc.read()


def hget(headers: dict, name: str):
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v
    return None


def raw_get(raw_path: str, range_header: str | None = None,
            connection: str = "close", read_all: bool = True) -> bytes:
    sock = socket.create_connection((HOST, PORT), timeout=10)
    req = f"GET {raw_path} HTTP/1.1\r\nHost: x\r\nConnection: {connection}\r\n"
    if range_header is not None:
        req += f"Range: {range_header}\r\n"
    req += "\r\n"
    sock.sendall(req.encode("ascii"))
    chunks: list[bytes] = []
    try:
        if read_all:
            while True:
                b = sock.recv(65536)
                if not b:
                    break
                chunks.append(b)
        else:
            time.sleep(0.1)
            try:
                chunks.append(sock.recv(65536))
            except socket.timeout:
                pass
    finally:
        sock.close()
    return b"".join(chunks)


def parse_raw(raw: bytes):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    parts = lines[0].split(b" ", 2)
    status = int(parts[1])
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(b":")
        headers[k.decode().strip()] = v.decode().strip()
    return status, headers, body


class MediaRangeTests(unittest.TestCase):
    MEDIA = f"/media/{TARGET}/audio.wav"

    # ---- 完整 GET ----
    def test_01_full_get_200_exact_bytes_and_headers(self):
        status, headers, body = http_get(self.MEDIA)
        self.assertEqual(status, 200)
        self.assertEqual(body, WAV_DATA)
        self.assertEqual(hget(headers, "Content-Length"), str(WAV_SIZE))
        self.assertEqual(hget(headers, "Accept-Ranges"), "bytes")
        self.assertEqual(hget(headers, "Cache-Control"), "no-store")
        self.assertEqual(
            hget(headers, "Content-Type"),
            mimetypes.guess_type("audio.wav")[0] or "application/octet-stream")
        self.assertIsNone(hget(headers, "Content-Range"))

    # ---- bytes=start-end ----
    def test_02_range_start_end_206(self):
        status, headers, body = http_get(self.MEDIA, "bytes=44-99")
        self.assertEqual(status, 206)
        self.assertEqual(body, WAV_DATA[44:100])
        self.assertEqual(len(body), 56)
        self.assertEqual(hget(headers, "Content-Length"), "56")
        self.assertEqual(hget(headers, "Content-Range"), f"bytes 44-99/{WAV_SIZE}")
        self.assertEqual(hget(headers, "Accept-Ranges"), "bytes")
        self.assertEqual(hget(headers, "Cache-Control"), "no-store")

    def test_03_range_open_ended(self):
        status, headers, body = http_get(self.MEDIA, f"bytes=44-")
        self.assertEqual(status, 206)
        self.assertEqual(body, WAV_DATA[44:])
        self.assertEqual(hget(headers, "Content-Length"), str(WAV_SIZE - 44))
        self.assertEqual(hget(headers, "Content-Range"),
                         f"bytes 44-{WAV_SIZE - 1}/{WAV_SIZE}")

    def test_04_range_suffix(self):
        status, headers, body = http_get(self.MEDIA, "bytes=-256")
        self.assertEqual(status, 206)
        self.assertEqual(body, WAV_DATA[-256:])
        self.assertEqual(hget(headers, "Content-Length"), "256")
        self.assertEqual(hget(headers, "Content-Range"),
                         f"bytes {WAV_SIZE - 256}-{WAV_SIZE - 1}/{WAV_SIZE}")

    def test_05_range_suffix_larger_than_file(self):
        status, headers, body = http_get(self.MEDIA, "bytes=-999999")
        self.assertEqual(status, 206)
        self.assertEqual(body, WAV_DATA)
        self.assertEqual(hget(headers, "Content-Range"),
                         f"bytes 0-{WAV_SIZE - 1}/{WAV_SIZE}")

    def test_06_range_end_clamped_to_eof(self):
        status, headers, body = http_get(self.MEDIA, "bytes=44-999999")
        self.assertEqual(status, 206)
        self.assertEqual(body, WAV_DATA[44:])
        self.assertEqual(hget(headers, "Content-Range"),
                         f"bytes 44-{WAV_SIZE - 1}/{WAV_SIZE}")
        self.assertEqual(hget(headers, "Content-Length"), str(WAV_SIZE - 44))

    def test_07_range_first_byte_only(self):
        status, headers, body = http_get(self.MEDIA, "bytes=0-0")
        self.assertEqual(status, 206)
        self.assertEqual(body, WAV_DATA[:1])
        self.assertEqual(hget(headers, "Content-Range"), f"bytes 0-0/{WAV_SIZE}")

    # ---- 416 ----
    def test_08_range_beyond_eof_416(self):
        for rng in (f"bytes={WAV_SIZE}-", "bytes=999999-", "bytes=999999-1000000"):
            with self.subTest(rng=rng):
                status, headers, body = http_get(self.MEDIA, rng)
                self.assertEqual(status, 416)
                self.assertEqual(body, b"")
                self.assertEqual(hget(headers, "Content-Range"),
                                 f"bytes */{WAV_SIZE}")
                self.assertEqual(hget(headers, "Content-Length"), "0")
                self.assertEqual(hget(headers, "Accept-Ranges"), "bytes")

    def test_09_range_zero_suffix_ignored_returns_full(self):
        status, headers, body = http_get(self.MEDIA, "bytes=-0")
        self.assertEqual(status, 200)
        self.assertEqual(body, WAV_DATA)
        self.assertEqual(hget(headers, "Content-Length"), str(WAV_SIZE))

    # ---- 空文件 ----
    def test_10_empty_file_full_get_remains_200(self):
        status, headers, body = http_get(f"/media/{TARGET}/empty.wav")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(hget(headers, "Content-Length"), "0")
        self.assertEqual(hget(headers, "Accept-Ranges"), "bytes")

    def test_11_range_on_empty_file_416(self):
        for rng in ("bytes=0-", "bytes=0-99", "bytes=-1"):
            with self.subTest(rng=rng):
                status, headers, body = http_get(
                    f"/media/{TARGET}/empty.wav", rng)
                self.assertEqual(status, 416)
                self.assertEqual(body, b"")
                self.assertEqual(hget(headers, "Content-Range"), "bytes */0")

    # ---- 畸形 / 不支持的 Range：统一回退 200 完整内容，不切错片 ----
    def test_12_malformed_ranges_ignored(self):
        for rng in ("bytes=abc", "bytes=1-2-3", "bytes=5-1",
                    "items=0-9", "bytes==", "bytes=", "bytes=xy-",
                    "bytes=-xy", "bytes=,"):
            with self.subTest(rng=rng):
                status, headers, body = http_get(self.MEDIA, rng)
                self.assertEqual(status, 200, rng)
                self.assertEqual(body, WAV_DATA, rng)
                self.assertIsNone(hget(headers, "Content-Range"))

    def test_13_multi_range_unsupported_returns_full(self):
        status, headers, body = http_get(self.MEDIA, "bytes=0-9,100-109")
        self.assertEqual(status, 200)
        self.assertEqual(body, WAV_DATA)
        self.assertIsNone(hget(headers, "Content-Range"))
        self.assertEqual(hget(headers, "Accept-Ranges"), "bytes")

    # ---- 原生 socket：确认媒体响应后不追加第二个 JSON 响应 ----
    def test_14_raw_response_has_no_trailing_json(self):
        raw = raw_get(self.MEDIA, "bytes=44-99")
        status, headers, body = parse_raw(raw)
        self.assertEqual(status, 206)
        self.assertEqual(int(headers["Content-Length"]), 56)
        self.assertEqual(len(body), 56)
        self.assertEqual(body, WAV_DATA[44:100])
        self.assertNotIn(b"{", body)

    # ---- 路径包含 / 穿越 ----
    def test_15_encoded_target_traversal_rejected(self):
        path = "/media/" + urllib.parse.quote("..", safe="") + "/outside_secret.wav"
        status, _, body = http_get(path)
        self.assertEqual(status, 404)
        self.assertNotIn(OUTSIDE_DATA, body)

    def test_16_encoded_target_with_slash_rejected(self):
        path = "/media/" + urllib.parse.quote("../tgt", safe="") + "/audio.wav"
        status, _, body = http_get(path)
        self.assertEqual(status, 404)
        self.assertNotIn(WAV_DATA, body)

    def test_17_encoded_rel_traversal_rejected(self):
        rel = urllib.parse.quote("../../outside_secret.wav", safe="")
        status, _, body = http_get(f"/media/{TARGET}/{rel}")
        self.assertEqual(status, 404)
        self.assertNotIn(OUTSIDE_DATA, body)

    def test_18_literal_rel_traversal_rejected_raw(self):
        raw = raw_get(f"/media/{TARGET}/../../outside_secret.wav")
        status, _, body = parse_raw(raw)
        self.assertEqual(status, 404)
        self.assertNotIn(OUTSIDE_DATA, body)

    def test_19_encoded_dotdot_toward_root_rejected(self):
        rel = urllib.parse.quote("sub/../../../../outside_secret.wav", safe="")
        status, _, body = http_get(f"/media/{TARGET}/{rel}")
        self.assertEqual(status, 404)
        self.assertNotIn(OUTSIDE_DATA, body)

    def test_20_internal_dotdot_still_served(self):
        rel = urllib.parse.quote("sub/../audio.wav", safe="")
        status, headers, body = http_get(f"/media/{TARGET}/{rel}")
        self.assertEqual(status, 200)
        self.assertEqual(body, WAV_DATA)
        self.assertEqual(hget(headers, "Accept-Ranges"), "bytes")

    def test_21_missing_target_and_file_404(self):
        status, _, _ = http_get("/media/no-such-target/audio.wav")
        self.assertEqual(status, 404)
        status, _, _ = http_get(f"/media/{TARGET}/no-such-file.wav")
        self.assertEqual(status, 404)

    def test_22_symlink_file_escape_rejected(self):
        link = _ROOT / "beats" / TARGET / "escape.wav"
        if not link.is_symlink():
            self.skipTest("symlinks unavailable")
        status, _, body = http_get(f"/media/{TARGET}/escape.wav")
        self.assertEqual(status, 404)
        self.assertNotIn(OUTSIDE_DATA, body)

    def test_23_symlink_dir_escape_rejected(self):
        link = _ROOT / "beats" / TARGET / "linkdir"
        if not link.is_symlink():
            self.skipTest("symlinks unavailable")
        status, _, body = http_get(f"/media/{TARGET}/linkdir/secret.wav")
        self.assertEqual(status, 404)
        self.assertNotIn(OUTSIDE_DIR_DATA, body)

    # ---- 其他内容类型 / 端点保留 ----
    def test_24_als_content_type_preserved(self):
        status, headers, body = http_get(f"/media/{TARGET}/song.als", "bytes=0-15")
        self.assertEqual(status, 206)
        self.assertEqual(body, ALS_DATA[:16])
        self.assertEqual(hget(headers, "Content-Type"),
                         "application/x-ableton-live-set")
        self.assertEqual(hget(headers, "Content-Range"),
                         f"bytes 0-15/{len(ALS_DATA)}")

    def test_25_octet_stream_default_and_subdir(self):
        status, headers, body = http_get(f"/media/{TARGET}/sub/extra.bin")
        self.assertEqual(status, 200)
        self.assertEqual(body, BIN_DATA)
        self.assertEqual(hget(headers, "Content-Type"),
                         "application/octet-stream")

    def test_26_ping_endpoint_retained(self):
        status, headers, body = http_get("/api/ping")
        self.assertEqual(status, 200)
        payload = __import__("json").loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["service"], "beatlab-feedback")

    # ---- 不得整体 read_bytes 进内存 ----
    def test_27_media_does_not_use_read_bytes(self):
        def boom(*a, **k):
            raise AssertionError("media transport must stream, not read_bytes")
        with patch.object(Path, "read_bytes", boom):
            status, _, body = http_get(self.MEDIA, "bytes=44-99")
            self.assertEqual(status, 206)
            self.assertEqual(body, WAV_DATA[44:100])
            status, _, body = http_get(f"/media/{TARGET}/sub/extra.bin")
            self.assertEqual(status, 200)
            self.assertEqual(body, BIN_DATA)

    # ---- 文件哈希不变 ----
    def test_28_file_hashes_unchanged(self):
        files = [
            _ROOT / "beats" / TARGET / "audio.wav",
            _ROOT / "beats" / TARGET / "empty.wav",
            _ROOT / "beats" / TARGET / "song.als",
            _ROOT / "beats" / TARGET / "sub" / "extra.bin",
            _ROOT / "outside_secret.wav",
        ]
        before = {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
        http_get(self.MEDIA)
        http_get(self.MEDIA, "bytes=0-43")
        http_get(self.MEDIA, f"bytes={WAV_SIZE}-")
        http_get(self.MEDIA, "bytes=0-9,99-109")
        http_get("/media/" + urllib.parse.quote("..", safe="") +
                 "/outside_secret.wav")
        after = {f: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
        self.assertEqual(before, after)

    # ---- 流式传输中断连：服务存活，不追加 JSON ----
    def test_29_client_disconnect_during_stream(self):
        sock = socket.create_connection((HOST, PORT), timeout=10)
        sock.sendall(
            f"GET /media/{TARGET}/big.bin HTTP/1.1\r\nHost: x\r\n"
            f"Range: bytes=0-\r\n\r\n".encode("ascii"))
        try:
            sock.recv(4096)
        finally:
            sock.close()
        time.sleep(0.3)
        # 服务仍可正常响应完整区间请求
        status, headers, body = http_get(self.MEDIA, "bytes=10-19")
        self.assertEqual(status, 206)
        self.assertEqual(body, WAV_DATA[10:20])
        self.assertEqual(hget(headers, "Content-Range"), f"bytes 10-19/{WAV_SIZE}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
