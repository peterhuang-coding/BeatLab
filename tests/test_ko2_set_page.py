"""Synthetic tests for the offline KO II static set page generator.

No real audio, network, subprocess or hardware is involved: every test
builds an independent synthetic set.json inside a temp directory.
"""

import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / "examples" / "ko2_set_page.py"
_SPEC = importlib.util.spec_from_file_location("ko2_set_page", MODULE_PATH)
ko2 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ko2)


def base_manifest():
    return {
        "schema": "beatlab.ko2-set.v1",
        "set_id": "demo-v1",
        "title": "Demo",
        "bpm": 88,
        "bars": 16,
        "parent_version": "x",
        "hardware_verified": False,
        "listening_review": "pending",
        "pads": [
            {
                "id": "kick",
                "group": "A",
                "slot": 0,
                "pad_label": ".",
                "midi_note": 36,
                "label": "Kick",
                "file": "Samples/A/00-kick.wav",
                "sha256": "abc",
                "source_sha256": "def",
                "duration_seconds": 0.5,
                "gain_db": -3,
                "provenance": {
                    "kind": "recorded_sample",
                    "source_url": "https://example.org/source",
                    "note": "test",
                },
            }
        ],
        "demo": {
            "file": "Demo/performance.wav",
            "sha256": "hash",
            "gain_db": 0,
            "duration_seconds": 45,
        },
        "midi": {"file": "Demo/performance.mid", "sha256": "hash"},
        "events": [],
        "scenes": [{"name": "Intro", "start_bar": 0, "bars": 4, "instruction": "B first"}],
        "sample_bytes": 100,
    }


CELL_RE = re.compile(
    r'<div class="pad [^"]*" '
    r'data-slot="(\d+)" data-key="([^"]*)" '
    r'data-midi="(\d+)" data-assigned="([01])">'
)


class Ko2SetPageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.set_dir = Path(self._tmp.name) / "demo-v1"
        self.set_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def write_manifest(self, data):
        (self.set_dir / "set.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def generate_html(self, data=None):
        if data is None:
            data = base_manifest()
        self.write_manifest(data)
        ko2.generate(self.set_dir)
        return (self.set_dir / "index.html").read_text(encoding="utf-8")

    def panels(self, html):
        marks = [(m.group(1), m.start()) for m in re.finditer(r'data-group="([ABCD])"', html)]
        out = {}
        for i, (group, start) in enumerate(marks):
            end = marks[i + 1][1] if i + 1 < len(marks) else html.find("</section>", start)
            out[group] = html[start:end]
        return out

    # --- contract content -------------------------------------------------

    def test_safe_escaped_metadata(self):
        xss_title = '"><script>alert("x")</script>'
        xss_label = "<img src=x onerror=alert(1)>"
        xss_note = 'q & "quoted" <script>bad</script>'
        data = base_manifest()
        data["title"] = xss_title
        data["pads"][0]["label"] = xss_label
        data["pads"][0]["provenance"]["note"] = xss_note
        data["scenes"][0]["instruction"] = "<b>x</b>"
        html = self.generate_html(data)

        self.assertNotIn(xss_title, html)
        self.assertNotIn(xss_label, html)
        self.assertNotIn(xss_note, html)
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img src=x onerror", html)
        self.assertIn("&lt;script&gt;alert", html)
        self.assertIn("&lt;img src=x onerror=", html)
        self.assertNotIn("<b>x</b>", html)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)

    def test_complete_group_grid(self):
        html = self.generate_html()
        panels = self.panels(html)
        self.assertEqual(set(panels), {"A", "B", "C", "D"})

        expected_keys = [ko2.SLOT_LABELS[s] for s in ko2.PHYSICAL_SLOTS]
        # Physical 4x3 layout: top row 7,8,9 then 4,5,6 then 1,2,3 then .,0,ENTER
        self.assertEqual(
            expected_keys,
            ["7", "8", "9", "4", "5", "6", "1", "2", "3", ".", "0", "ENTER"],
        )
        total_cells = 0
        for group, base in (("A", 36), ("B", 48), ("C", 60), ("D", 72)):
            self.assertEqual(ko2.GROUP_BASES[group], base)
            cells = CELL_RE.findall(panels[group])
            self.assertEqual(len(cells), 12, group)
            keys = [c[1] for c in cells]
            self.assertEqual(keys, expected_keys, group)
            slots = [int(c[0]) for c in cells]
            self.assertEqual(slots, ko2.PHYSICAL_SLOTS, group)
            midis = [int(c[2]) for c in cells]
            self.assertEqual(midis, [base + s for s in ko2.PHYSICAL_SLOTS], group)
            total_cells += len(cells)
        self.assertEqual(total_cells, 48)

    def test_sparse_pad_mapping(self):
        data = base_manifest()  # only A slot 0 (".") is assigned
        html = self.generate_html(data)
        assigned = CELL_RE.findall(html)
        self.assertEqual(sum(int(c[3]) for c in assigned), 1)
        self.assertEqual(sum(1 for c in assigned if c[3] == "0"), 47)

        panel_a = self.panels(html)["A"]
        cells = list(CELL_RE.finditer(panel_a))
        # Physical order ends with row [".", "0", "ENTER"]; slot 0 is index 9.
        slot0 = cells[9]
        self.assertEqual(slot0.groups(), ("0", ".", "36", "1"))
        body = panel_a[slot0.start() : panel_a.find("</div>", slot0.start())]
        self.assertIn("Kick", body)
        self.assertIn("Samples/A/00-kick.wav", body)
        for idx in (0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11):
            self.assertEqual(cells[idx].group(4), "0")
        self.assertIn("空", panel_a)

    def test_proper_media_paths(self):
        html = self.generate_html()
        # Demo audio is a plain relative file, works on file:// and HTTP.
        self.assertIn('<audio controls preload="none" src="Demo/performance.wav">', html)
        self.assertIn('href="Demo/performance.wav"', html)
        self.assertIn('href="Demo/performance.mid"', html)
        # Assigned pad both plays and offers its exact relative sample path.
        self.assertIn('data-src="Samples/A/00-kick.wav"', html)
        self.assertIn('href="Samples/A/00-kick.wav"', html)
        # Group zips and the parent-level set zip (built later, never auto-install).
        for z in ("A.zip", "B.zip", "C.zip", "D.zip"):
            self.assertIn(f'href="{z}"', html)
        self.assertIn('href="../demo-v1.zip"', html)
        # No audio/sample URL may be absolute or external.
        self.assertNotRegex(html, r'(?:src|href)="https?://[^"]*\.wav"')
        self.assertNotRegex(html, r'data-src="(?:/|[a-zA-Z]+:|\.\.)')
        # Self contained: no CDN/network assets.
        self.assertNotIn("cdn.", html.lower())
        self.assertNotRegex(html, r'<(?:script|link)[^>]+(?:src|href)="https?://')

    def test_reject_traversal_and_external_audio(self):
        bad_paths = [
            "../escape.wav",
            "a/../../b.wav",
            "/abs/x.wav",
            "https://example.com/a.wav",
            "http://evil.test/a.wav",
            "file:///tmp/a.wav",
            "Samples\\A\\x.wav",
            "Samples/%2e%2e/x.wav",
            "a#frag.wav",
            "a?query.wav",
        ]
        for bad in bad_paths:
            data = base_manifest()
            data["pads"][0]["file"] = bad
            with self.subTest(bad=bad, where="pad"):
                self.write_manifest(data)
                with self.assertRaises(ValueError):
                    ko2.generate(self.set_dir)
            data = base_manifest()
            data["demo"]["file"] = bad
            with self.subTest(bad=bad, where="demo"):
                self.write_manifest(data)
                with self.assertRaises(ValueError):
                    ko2.generate(self.set_dir)

        bad_set_ids = ["../x", "a/b", "..", "x:y", "a\\b", ""]
        for sid in bad_set_ids:
            data = base_manifest()
            data["set_id"] = sid
            with self.subTest(sid=sid):
                self.write_manifest(data)
                with self.assertRaises(ValueError):
                    ko2.generate(self.set_dir)

    def test_bad_sheet_mapping_rejected(self):
        data = base_manifest()
        data["pads"][0]["midi_note"] = 99
        self.write_manifest(data)
        with self.assertRaises(ValueError):
            ko2.generate(self.set_dir)

        data = base_manifest()
        data["pads"][0]["pad_label"] = "9"
        self.write_manifest(data)
        with self.assertRaises(ValueError):
            ko2.generate(self.set_dir)

        data = base_manifest()
        data["schema"] = "other"
        self.write_manifest(data)
        with self.assertRaises(ValueError):
            ko2.generate(self.set_dir)

    def test_no_overwrite(self):
        self.write_manifest(base_manifest())
        self.assertEqual(ko2.main([str(self.set_dir)]), 0)
        self.assertTrue((self.set_dir / "index.html").exists())
        # Second run must refuse and exit non-zero.
        self.assertEqual(ko2.main([str(self.set_dir)]), 1)

        idx = self.set_dir / "index.html"
        before = idx.read_text(encoding="utf-8")
        self.assertEqual(ko2.main([str(self.set_dir)]), 1)
        self.assertEqual(idx.read_text(encoding="utf-8"), before)

    def test_existing_index_never_touched(self):
        self.write_manifest(base_manifest())
        idx = self.set_dir / "index.html"
        idx.write_text("KEEP-ME", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            ko2.generate(self.set_dir)
        self.assertEqual(idx.read_text(encoding="utf-8"), "KEEP-ME")

    def test_cli_missing_dir(self):
        self.assertEqual(ko2.main([str(self.set_dir / "does-not-exist")]), 1)

    def test_required_chinese_guidance_and_feedback(self):
        html = self.generate_html()
        # Import steps required by the contract.
        for phrase in (
            "Chrome",
            "Edge",
            "USB",
            "备份",
            "红色虚线",
            "顺序不保证",
            "稀疏",
            "空闲",
            "文件名不会预留",
            "BPM",
            "one-shot",
            ".pak",
            "未验证",
        ):
            self.assertIn(phrase, html)
        # No destructive guidance.
        self.assertNotIn("恢复出厂设置", html.replace("不要执行“删除全部/恢复出厂设置”", ""))
        self.assertNotIn("删除全部</", html)
        # Official references (plain links only; nothing auto-loads).
        self.assertIn(
            "https://support.teenage.engineering/hc/en-us/articles/"
            "11639171890204-how-to-use-the-EP-sample-tool",
            html,
        )
        self.assertIn("https://teenage.engineering/guides/ep-133/system", html)
        # Feedback instructions + self-contained downloadable template.
        self.assertIn("30", html)
        self.assertIn("60", html)
        self.assertIn("set_id", html)
        self.assertIn('download="feedback-template.json"', html)
        self.assertIn('href="data:application/json;charset=utf-8,', html)
        # Scenes are practice notes, never claimed to render on the device.
        self.assertIn("Intro", html)
        self.assertIn("B first", html)

    def test_utf8_chinese_and_basic_structure(self):
        data = base_manifest()
        data["title"] = "节拍实验室"
        html = self.generate_html(data)
        self.assertIn("节拍实验室", html)
        self.assertIn('charset="utf-8"', html)
        self.assertIn('lang="zh-CN"', html)
        self.assertIn("88", html)  # BPM shown
        self.assertIn("hardware_verified", html)


if __name__ == "__main__":
    unittest.main()
