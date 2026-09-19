import fcntl
import hashlib
import io
import json
import os
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import common
import crate
import ingest
import library
from connectors import citizen_dj
from connectors.citizen_dj import COLLECTIONS, CitizenDJConnector

ROOT = Path(__file__).resolve().parents[1]


def wav_bytes(seed, seconds=0.2, rate=48000):
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * rate)) / rate
    audio = 0.08 * np.sin(2 * np.pi * (220 + 7 * seed) * t)
    audio += 0.02 * rng.normal(size=audio.shape)
    bio = io.BytesIO()
    sf.write(bio, audio.astype("float32"), rate, format="WAV", subtype="PCM_16")
    return bio.getvalue()


def catalog_html(coll, n=3):
    name = COLLECTIONS[coll]
    prefix = citizen_dj.ASSETS + name + "/"
    parts = [citizen_dj.RIGHTS_REQUIRED[0], citizen_dj.RIGHTS_REQUIRED[1]]
    for i in range(n):
        item = f"https://www.loc.gov/item/{coll}{i}/"
        href = f"{prefix}{i}.wav"
        start = 8 + i * 3
        parts.append(
            f"<h4>{coll.title()} {i}</h4>"
            f'<a href="{item}">item</a>'
            f"<span>Excerpt starting at 0:{start:02d}</span>"
            f'<a href="{href}" download>wav</a>'
            f'<a href="https://citizen-dj.labs.loc.gov/{name}/remix/?itemStart={start*1000}">x</a>'
        )
    return "".join(parts).encode()


class CrateIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "root"
        self.lib = self.root / "library"
        self.sources = self.lib / "sources"
        self.stage = self.lib / "staging"
        self.db = self.lib / "beatlab.db"
        self.root.mkdir(parents=True)
        self.wavs = {c: [wav_bytes(10 * i + {"blues": 1, "jazz": 50}[c] + j)
                         for j in range(3)] for i, c in enumerate(COLLECTIONS)}
        self.pages = {c: catalog_html(c) for c in COLLECTIONS}
        self.gets = []
        for mod, key, val in [
            (common, "ROOT", self.root), (common, "LIBRARY", self.lib),
            (common, "DB_PATH", self.db), (crate, "ROOT", self.root),
            (crate, "LIBRARY", self.lib), (library, "LIBRARY", self.lib),
            (library, "STAGING", self.stage),
        ]:
            patcher = patch.object(mod, key, val)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.gets = []
        patcher = patch.object(CitizenDJConnector, "_get", side_effect=self.mock_get)
        patcher.start()
        self.addCleanup(patcher.stop)
        orig_init = CitizenDJConnector.__init__
        def init(conn, config=None):
            orig_init(conn, config)
            conn.config["pause_s"] = 0
        patcher = patch.object(CitizenDJConnector, "__init__", init)
        patcher.start()
        self.addCleanup(patcher.stop)

    def mock_get(self, url, max_bytes):
        self.gets.append(url)
        if max_bytes <= 0:
            raise ValueError("bad limit")
        for coll in COLLECTIONS:
            page = f"{citizen_dj.SITE}/{COLLECTIONS[coll]}/use/"
            if url == page:
                return self.pages[coll]
            for i, blob in enumerate(self.wavs[coll]):
                if url == f"{citizen_dj.ASSETS}{COLLECTIONS[coll]}/{i}.wav":
                    return blob
        raise AssertionError(f"unmocked URL {url}")

    def manifest(self, bid="b1"):
        return json.loads((self.lib / "crates" / bid / "manifest.json").read_text())

    def asset_rows(self):
        db = sqlite3.connect(self.db)
        try:
            return db.execute("select id, md5, library_path, source_url, title from assets order by id").fetchall()
        finally:
            db.close()

    def test_full_batch_diversity_duplicate_resume_and_options(self):
        man = crate.run_batch("b1", list(COLLECTIONS), limit=4, timeout_s=30)
        self.assertEqual(man["state"], "complete")
        self.assertEqual([i["state"] for i in man["items"]], ["ok"] * 4)
        colls = [i["collection"] for i in man["items"]]
        self.assertEqual(colls.count("blues"), 2)
        self.assertEqual(colls.count("jazz"), 2)
        urls = [i["record"]["source_url"] for i in man["items"]]
        self.assertEqual(len(set(urls)), 4)
        rows = self.asset_rows()
        self.assertEqual(len(rows), 4)
        for item in man["items"]:
            orig = Path(item["entry"]["orig_path"])
            normalized = Path(item["library_path"])
            self.assertNotEqual(item["downloaded_sha256"], item["library_sha256"])
            self.assertEqual(hashlib.sha256(orig.read_bytes()).hexdigest(),
                             item["downloaded_sha256"])
            self.assertEqual(hashlib.sha256(normalized.read_bytes()).hexdigest(),
                             item["library_sha256"])
            info = sf.info(normalized)
            self.assertEqual(info.samplerate, 44100)
            self.assertIn(info.channels, (1, 2))
        gets_after_first = len(self.gets)
        before = {r[1]: r[2] for r in rows}
        man2 = crate.run_batch("b1", resume=True)
        self.assertEqual(man2["state"], "complete")
        self.assertEqual(len(self.gets), gets_after_first)
        rows2 = self.asset_rows()
        self.assertEqual(len(rows2), 4)
        self.assertEqual(before, {r[1]: r[2] for r in rows2})
        with self.assertRaises(ValueError):
            crate.run_batch("../bad")
        with self.assertRaises(ValueError):
            crate.run_batch("b2", limit=0)
        with self.assertRaises(ValueError):
            crate.run_batch("b2", timeout_s=0)
        with self.assertRaises(ValueError):
            crate.run_batch("b2", ["blues", "blues"])
        with self.assertRaises(ValueError):
            crate.run_batch("b1", resume=True, limit=4)
        with self.assertRaises(FileNotFoundError):
            crate.run_batch("missing", resume=True)

    def test_discovery_only_then_resume(self):
        man = crate.run_batch("d1", ["blues"], limit=2, discover_only=True)
        self.assertEqual(man["state"], "ready")
        self.assertEqual(len(man["items"]), 2)
        self.assertEqual(self.asset_rows(), [])
        man = crate.run_batch("d1", resume=True)
        self.assertEqual(man["state"], "complete")
        self.assertEqual(len(self.asset_rows()), 2)

    def test_failure_resume_without_rediscovery_then_corruption(self):

        def failing(url, max_bytes):
            if url.endswith("/1.wav") and not getattr(self, "allow_second", False):
                raise ConnectionError("boom")
            return self.mock_get(url, max_bytes)

        with patch.object(CitizenDJConnector, "_get", side_effect=failing):
            man = crate.run_batch("f1", ["blues"], limit=2, timeout_s=30)
        self.assertEqual(man["state"], "partial")
        self.assertEqual([i["state"] for i in man["items"]], ["ok", "error"])
        gets_discovery = sum("/use/" in u for u in self.gets)
        first_audio_gets = len(self.gets)
        self.allow_second = True
        selected = [i["record"] for i in man["items"]]
        self.pages["blues"] = b"changed catalog must not be fetched during resume"
        man = crate.run_batch("f1", resume=True)
        self.assertEqual(man["state"], "complete")
        self.assertEqual(sum("/use/" in u for u in self.gets), gets_discovery)
        self.assertEqual(len(self.gets) - first_audio_gets, 1)
        self.assertEqual(len(self.asset_rows()), 2)
        self.assertEqual([i["record"] for i in man["items"]], selected)
        item = man["items"][0]
        normalized = Path(item["library_path"])
        corrupted = normalized.read_bytes()[:-8] + b"BADCORRUP"
        normalized.write_bytes(corrupted)
        mtime = normalized.stat().st_mtime_ns
        man = crate.run_batch("f1", resume=True)
        self.assertEqual(man["items"][0]["state"], "error")
        self.assertTrue(man["items"][0]["corrupt_completed"])
        self.assertEqual(normalized.read_bytes(), corrupted)
        self.assertEqual(normalized.stat().st_mtime_ns, mtime)
        man = crate.run_batch("f1", resume=True)
        self.assertEqual(man["items"][0]["state"], "error")
        self.assertEqual(normalized.read_bytes(), corrupted)
        self.assertEqual(normalized.stat().st_mtime_ns, mtime)

    def test_duplicate_content_creates_one_asset(self):
        crate.run_batch("x1", ["blues"], limit=2, timeout_s=30)
        self.wavs["jazz"][0] = self.wavs["blues"][0]
        man = crate.run_batch("x2", ["jazz"], limit=2, timeout_s=30)
        md5s = [i["md5"] for i in man["items"]]
        self.assertEqual(len(md5s), 2)
        self.assertEqual(len(set(md5s)), 2)
        rows = self.asset_rows()
        unique = {r[1] for r in rows}
        self.assertEqual(len(rows), len(unique))
        self.assertEqual(len(rows), 3)

    def test_timeout_resume_and_exact_flock(self):
        real_monotonic = __import__("time").monotonic
        state = {"calls": 0}

        def fast_clock():
            val = real_monotonic() + state["calls"] * 1000
            state["calls"] += 1
            return val

        with patch("crate.time.monotonic", fast_clock):
            man = crate.run_batch("t1", ["blues"], limit=2, timeout_s=1)
        self.assertEqual(man["state"], "discovery_error")
        self.assertIn("timeout", man["error"])
        man = crate.run_batch("t1", resume=True)
        self.assertEqual(man["state"], "complete")
        lock = self.sources / ".crate.lock"
        self.assertTrue(lock.exists())
        fd = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(RuntimeError):
                crate.run_batch("locked")
        finally:
            os.close(fd)
        # The lock is released after run_batch; the exact path above is asserted.
        fd = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_pending_timeout_resumes_fixed_batch_and_clears_error(self):
        clock = [0.0]
        process = crate._process_item
        def advance(*args):
            result = process(*args)
            clock[0] = 2.0
            return result
        with patch("crate.time.monotonic", side_effect=lambda: clock[0]), patch.object(crate, "_process_item", side_effect=advance):
            man = crate.run_batch("deadline", ["blues"], limit=2, timeout_s=1)
        self.assertEqual(man["state"], "partial")
        self.assertEqual([i["state"] for i in man["items"]], ["ok", "pending"])
        man = crate.run_batch("deadline", resume=True)
        self.assertEqual(man["state"], "complete")
        self.assertFalse(man["has_errors"])

    def test_ingest_failure_resumes_cached_download_without_network(self):
        with patch.object(ingest, "ingest_entry", side_effect=RuntimeError("interrupted after download")):
            man = crate.run_batch("ingest-fail", ["blues"], limit=1)
        self.assertEqual(man["state"], "failed")
        before = len(self.gets)
        man = crate.run_batch("ingest-fail", resume=True)
        self.assertEqual(man["state"], "complete")
        self.assertEqual(len(self.gets), before)

    def test_new_batch_skips_verified_cached_assets(self):
        first = crate.run_batch("first", ["blues"], limit=2)
        second = crate.run_batch("second", ["blues"], limit=2)
        self.assertEqual(len(second["items"]), 1)
        self.assertNotIn(second["items"][0]["asset_id"], {i["asset_id"] for i in first["items"]})



if __name__ == "__main__":
    unittest.main()
