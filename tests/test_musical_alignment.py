"""Musical alignment regressions using metadata and disposable files only."""
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import common
import compose
import recipes


class MusicalAlignmentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (("ROOT", Path(tmp.name)),
                            ("DB_PATH", Path(tmp.name) / "db.sqlite")):
            patched = patch.object(common, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        database = patch.object(common, "get_db", side_effect=AssertionError("No database needed"))
        database.start()
        self.addCleanup(database.stop)
        self.hero = {"id": "phrase", "asset_id": "fixture", "stem": "source",
                     "start_sec": 4.0, "end_sec": 20.0}
        self.asset = {"id": "fixture", "library_path": "library/loops/fixture/source.wav",
                      "bpm": 120, "key_note": "A1"}
        self.assets = {"fixture": self.asset}

    def placements(self, kind, *, hook=None, target_bpm=92):
        builder = recipes.build_loop_recipe if kind == "loop" else recipes.build_stem_recipe
        manifest = builder("alignment", self.hero, [], self.assets, target_bpm, 1,
                           {"hook" if hook else "verse": 8})
        placements, vocals = compose.place_chops(
            random.Random(1), manifest, self.hero, [], self.assets,
            self.asset["library_path"], hook_hero=hook)
        return placements + vocals

    def assert_eight_source_bars(self, placements, start):
        self.assertEqual([p["start_sec"] for p in placements], [start + 2 * i for i in range(8)])
        self.assertEqual([p["end_sec"] for p in placements], [start + 2 * (i + 1) for i in range(8)])
        self.assertTrue(all(p["stretch_to"] == round(240 / 92, 4) for p in placements))

    def test_loop_preserves_eight_source_bars_at_slower_target_tempo(self):
        self.assert_eight_source_bars(self.placements("loop"), 4)

    def test_stem_preserves_eight_source_bars_at_slower_target_tempo(self):
        self.assert_eight_source_bars(self.placements("stem"), 4)

    def test_hook_uses_its_own_source_window(self):
        hook = dict(self.hero, id="hook", start_sec=24.0, end_sec=40.0)
        for kind in ("loop", "stem"):
            with self.subTest(kind=kind):
                self.assert_eight_source_bars(self.placements(kind, hook=hook), 24)

    def test_explicit_moment_bar_count_works_without_source_bpm(self):
        self.asset.pop("bpm")
        self.hero["bars"] = 8
        for kind in ("loop", "stem"):
            with self.subTest(kind=kind):
                placements = self.placements(kind)
                self.assert_eight_source_bars(placements, 4)
                self.assertEqual(placements[0].get("timing_basis"), "moment_bars")

    def test_unknown_tempo_keeps_legacy_duration_fallback_and_labels_it(self):
        self.asset.pop("bpm")
        for kind in ("loop", "stem"):
            with self.subTest(kind=kind):
                placements = self.placements(kind)
                self.assertAlmostEqual(placements[1]["start_sec"], 4 + 16 / 6, places=6)
                self.assertEqual(placements[0].get("timing_basis"), "duration_fallback")

    def test_estimated_source_bpm_is_used(self):
        self.asset["bpm_est"] = self.asset.pop("bpm")
        placements = self.placements("loop")
        self.assert_eight_source_bars(placements, 4)
        self.assertEqual(placements[0].get("timing_basis"), "source_bpm")

    def test_key_names_without_octaves_resolve_pitch_class(self):
        for text, pitch_class in (("C", 0), ("C minor", 0), ("Bb major", 10),
                                  ("F# minor", 6), ("eb", 3), ("Gm", 7)):
            with self.subTest(text=text):
                note = recipes.parse_key_note(text)
                self.assertIsNotNone(note)
                self.assertEqual(note % 12, pitch_class)

    def test_explicit_octaves_and_numeric_midi_remain_compatible(self):
        for text, expected in (("A1", 33), ("C#2", 37), ("Bb1", 34),
                               ("C4", 60), ("0", 0), (69, 69), ("127", 127)):
            with self.subTest(text=text):
                self.assertEqual(recipes.parse_key_note(text), expected)

    def test_invalid_keys_are_not_guessed(self):
        for text in (None, "", "unknown", "H minor", "C nonsense", "128"):
            with self.subTest(text=text):
                self.assertIsNone(recipes.parse_key_note(text))

    def test_manifest_folds_explicit_key_into_bass_register(self):
        for key in ("C4", "Bb5", "A1", "0", "127"):
            with self.subTest(key=key):
                self.asset["key_note"] = key
                manifest = recipes._base_manifest("alignment", "loop", self.hero, [], self.assets, 92, 1)
                root = manifest["bass_root"]
                self.assertGreaterEqual(root, 28)
                self.assertLessEqual(root, 47)
                self.assertEqual(root % 12, recipes.parse_key_note(key) % 12)

    def test_estimated_melody_pitch_is_folded_into_bass_register(self):
        self.asset.pop("key_note")
        with patch.object(recipes, "_estimate_root_note", return_value=81):
            manifest = recipes._base_manifest("alignment", "loop", self.hero, [], self.assets, 92, 1)
        self.assertEqual(manifest["bass_root"] % 12, 9)
        self.assertGreaterEqual(manifest["bass_root"], 28)
        self.assertLessEqual(manifest["bass_root"], 47)

    def build_stem_spec(self):
        manifest = recipes.build_stem_recipe("alignment", self.hero, [], self.assets, 92, 1,
                                             compose.section_bars(8))
        # Audio analysis is outside this metadata/path regression; no model or render runs.
        with patch.object(recipes, "hero_onset_steps", return_value=set()):
            return compose.build_spec_for_recipe(manifest, "stem", "alignment", self.hero,
                                                  [], self.assets, 92, None)

    def test_missing_required_stem_fails_instead_of_using_source(self):
        source = common.ROOT / self.asset["library_path"]
        source.parent.mkdir(parents=True)
        source.write_bytes(b"fixture")
        self.hero["stem"] = "other"
        with self.assertRaisesRegex(FileNotFoundError, r"stems/other\.wav"):
            self.build_stem_spec()

    def test_source_moment_remains_a_valid_stem_recipe(self):
        source = common.ROOT / self.asset["library_path"]
        source.parent.mkdir(parents=True)
        source.write_bytes(b"fixture")
        spec = self.build_stem_spec()
        self.assertTrue(spec.chop_placements)
        self.assertTrue(all(p["file"] == self.asset["library_path"] for p in spec.chop_placements))

    def test_available_stem_is_used_when_asset_uses_path_alias(self):
        self.asset["path"] = self.asset.pop("library_path")
        self.hero["stem"] = "other"
        stem = common.ROOT / "library/loops/fixture/stems/other.wav"
        stem.parent.mkdir(parents=True)
        stem.write_bytes(b"fixture")
        spec = self.build_stem_spec()
        self.assertTrue(all(p["file"] == str(stem.relative_to(common.ROOT)) for p in spec.chop_placements))


if __name__ == "__main__":
    unittest.main()
