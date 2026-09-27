"""Audio invariants for the verified stem-splice locked vocal edit.

All checks use real decoded audio rendered by song.render_score against
synthetic oscillator WAVs - no mocks for audio invariants.
"""
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from song import render_score
from ableton_export import validate_song


class LockedEditTests(unittest.TestCase):
    def setUp(self):
        try:
            self.le = importlib.import_module("locked_edit")
        except ModuleNotFoundError:
            self.le = None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sr = 22050

        def wav(name, freq, dur, amp=0.3):
            t = np.arange(round(dur * self.sr)) / self.sr
            sf.write(self.root / name, amp * np.sin(2 * np.pi * freq * t),
                     self.sr, subtype="FLOAT")

        wav("tone440.wav", 440, 0.5)
        wav("tone330.wav", 330, 0.5)
        wav("phrase523.wav", 523, 0.5)
        wav("phrase330long.wav", 330, 2.0)
        self.parent = self.root / "parent"
        render_score(self.score(), self.parent, sr=self.sr)
        # Deliberately copied parent provenance file (lives with the delivery).
        (self.parent / "source_catalog.json").write_text(
            json.dumps({"families": ["fam-old"]}) + "\n")

    def score(self):
        sample = str(self.root / "tone440.wav")
        return {"title": "fixture", "bpm": 120, "bars": 2, "tail_seconds": 0.2,
                "tracks": [
                    {"id": "voice", "sample": sample, "root_midi": 69, "gain_db": -12,
                     "role": "answer", "events": [
                         {"beat": 0, "note": 69, "duration_beats": 1, "velocity": 0.8},
                         {"beat": 4, "note": 69, "duration_beats": 1, "velocity": 0.8}]},
                    {"id": "drums", "sample": sample, "drum": True, "gain_db": -12,
                     "events": [
                         {"beat": 0, "note": 69, "duration_beats": 1, "velocity": 0.8},
                         {"beat": 4, "note": 69, "duration_beats": 1, "velocity": 0.8}]},
                    {"id": "bass", "sample": sample, "root_midi": 69, "role": "bass",
                     "gain_db": -12, "events": [
                         {"beat": 0, "note": 57, "duration_beats": 1, "velocity": 0.8},
                         {"beat": 4, "note": 57, "duration_beats": 1, "velocity": 0.8}]}]}

    def _sha(self, path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def edit(self, **over):
        d = {"track_id": "voice", "start_beat": 4, "end_beat": 8,
             "expected_parent_mix_sha256": self._sha(self.parent / "full_mix.wav"),
             "current_family_id": "fam-old", "identity_verified": True,
             "purpose": "vocal-replace"}
        d.update(over)
        return d

    def phrase(self, **over):
        path = self.root / "phrase523.wav"
        d = {"id": "answer-b", "path": str(path),
             "sha256": self._sha(path), "family_id": "fam-new",
             "identity_verified": True, "allowed_uses": ["vocal-replace"],
             "root_midi": 60, "role": "answer"}
        d.update(over)
        return d

    def render(self, edit=None, phrase=None, out=None):
        self.assertIsNotNone(self.le, "locked_edit module is not implemented")
        return self.le.render_locked_edit(
            self.parent, out or self.root / "child",
            edit or self.edit(), phrase or self.phrase())

    def assertRejected(self, edit=None, phrase=None, out=None, exc=ValueError):
        target = out or self.root / "rejected"
        with self.assertRaises(exc):
            self.le.render_locked_edit(self.parent, target,
                                       edit or self.edit(), phrase or self.phrase())
        self.assertFalse(target.exists())

    def test_preflight_reports_real_slots(self):
        info = self.le.validate_edit(self.parent, self.edit(), self.phrase())
        self.assertEqual(info["track_id"], "voice")
        self.assertEqual(info["replacement_event_count"], 1)
        self.assertEqual(info["frames"], [44100, 88200])
        self.assertGreater(info["phrase"]["peak"], 0.0)

    def test_lowpass_parent_and_outside_midi_are_preserved(self):
        from mido import MidiFile
        score = self.score(); score['tracks'][0]['lowpass_hz'] = 3000
        self.parent = self.root / 'filtered'
        render_score(score, self.parent, sr=self.sr)
        child = Path(self.render()['song'])
        tick = 0; notes = []
        for message in MidiFile(child/'midi/voice.mid').tracks[0]:
            tick += message.time
            if message.type == 'note_on' and message.velocity:
                notes.append((tick, message.note))
        self.assertEqual(notes, [(0, 69), (4*480, 60)])

    def test_source_tampering_rejected_before_render(self):
        sf.write(self.root/'tone440.wav', np.ones(11025)*.2, self.sr, subtype='FLOAT')
        with self.assertRaisesRegex(ValueError, 'source.*changed|source.*hash'):
            self.render()
        self.assertFalse((self.root/'child').exists())

    def test_score_explicitly_marks_splice_and_refuses_vanilla_replay(self):
        child = Path(self.render()['song'])
        score = json.loads((child/'score.json').read_text())
        self.assertEqual(score['locked_edit_recipe']['engine'], 'stem_splice')
        with self.assertRaisesRegex(ValueError, 'stem.splic|locked.edit'):
            render_score(score, self.root/'wrong-replay', sr=self.sr)
        self.assertFalse((self.root/'wrong-replay').exists())

    def test_output_inside_parent_is_refused_without_new_directories(self):
        with self.assertRaisesRegex(ValueError, 'inside|parent'):
            self.render(out=self.parent/'new'/'child')
        self.assertFalse((self.parent/'new').exists())

    def test_existing_pending_directory_is_not_deleted(self):
        edit, phrase = self.edit(), self.phrase()
        material = json.dumps({'parent_mix': edit['expected_parent_mix_sha256'],
            'edit': edit, 'phrase_sha': phrase['sha256'], 'f0': 44100, 'f1': 88200},
            sort_keys=True, default=str).encode()
        token = hashlib.sha256(material).hexdigest()[:12]
        pending = self.root/f'.child.pending-{token}'
        pending.mkdir(); (pending/'keep').write_text('another worker or interrupted attempt')
        with self.assertRaises(FileExistsError): self.render()
        self.assertEqual((pending/'keep').read_text(), 'another worker or interrupted attempt')

    def test_candidate_cache_cannot_render_previously_cached_bytes(self):
        first = Path(self.render(out=self.root/'child-one')['song'])
        path = self.root/'phrase523.wav'; t = np.arange(11025)/self.sr
        sf.write(path, .3*np.sin(2*np.pi*660*t), self.sr, subtype='FLOAT')
        second = Path(self.render(phrase=self.phrase(sha256=self._sha(path)), out=self.root/'child-two')['song'])
        self.assertNotEqual(self._sha(first/'full_mix.wav'), self._sha(second/'full_mix.wav'))

    def test_true_edit_changes_mix_and_publishes_valid_child(self):
        before = (self.parent / "full_mix.wav").read_bytes()
        result = self.render()
        child = Path(result["song"])
        self.assertEqual(result["mix_sha256"], self._sha(child / "full_mix.wav"))
        self.assertNotEqual((child / "full_mix.wav").read_bytes(), before)
        old_mix = sf.read(self.parent / "full_mix.wav", always_2d=True)[0]
        new_mix = sf.read(child / "full_mix.wav", always_2d=True)[0]
        self.assertTrue(np.any(np.abs(new_mix - old_mix) > 1e-4))
        validate_song(child)

    def test_non_target_stems_and_midi_are_byte_identical(self):
        child = Path(self.render()["song"])
        for tid in ("drums", "bass"):
            self.assertEqual((child / f"stems/{tid}.wav").read_bytes(),
                             (self.parent / f"stems/{tid}.wav").read_bytes())
            self.assertEqual((child / f"midi/{tid}.mid").read_bytes(),
                             (self.parent / f"midi/{tid}.mid").read_bytes())

    def test_target_outside_window_is_decoded_sample_identical(self):
        child = Path(self.render()["song"])
        old = sf.read(self.parent / "stems/voice.wav", always_2d=True)[0]
        new = sf.read(child / "stems/voice.wav", always_2d=True)[0]
        self.assertEqual(old.shape, new.shape)
        self.assertTrue(np.array_equal(new[:44100], old[:44100]))
        self.assertTrue(np.array_equal(new[88200:], old[88200:]))
        # The interior actually changed.
        self.assertFalse(np.array_equal(new[44100:88200], old[44100:88200]))

    def test_parent_files_keep_bytes_and_mtime(self):
        snap = {}
        for p in self.parent.rglob("*"):
            if p.is_file():
                snap[str(p.relative_to(self.parent))] = (
                    hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        self.render()
        for p in self.parent.rglob("*"):
            if p.is_file():
                self.assertEqual(
                    snap[str(p.relative_to(self.parent))],
                    (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns))

    def test_sources_provenance_recipe_and_protection(self):
        child = Path(self.render()["song"])
        copied = child / "sources" / "answer-b.wav"
        self.assertTrue(copied.is_file())
        self.assertEqual(self._sha(copied), self._sha(self.root / "phrase523.wav"))
        self.assertEqual((child / "source_catalog.json").read_bytes(),
                         (self.parent / "source_catalog.json").read_bytes())
        recipe = json.loads((child / "locked_edit_recipe.json").read_text())
        self.assertEqual(recipe["engine"], "stem_splice")
        self.assertEqual(recipe["phrase"]["family_id"], "fam-new")
        self.assertEqual(recipe["phrase"]["sha256"], self._sha(copied))
        self.assertEqual(recipe["region"]["start_beat"], 4)
        self.assertEqual(recipe["parent"]["mix_sha256"],
                         self._sha(self.parent / "full_mix.wav"))
        self.assertIn("identity", recipe["identity_trust_boundary"].lower())
        protection = json.loads((child / "protection.json").read_text())
        checks = protection["checks"]
        self.assertTrue(checks["non_target_stems_byte_identical"])
        self.assertTrue(checks["non_target_midi_byte_identical"])
        self.assertTrue(checks["target_outside_window_sample_identical"])
        self.assertEqual(checks["target_outside_window_max_abs_diff"], 0.0)
        self.assertTrue(checks["audible_change"])
        self.assertTrue(checks["parent_files_byte_and_mtime_unchanged"])
        self.assertTrue(json.loads((child / "locked-edit.json").read_text()))

    def test_replay_against_immutable_parent_is_faithful(self):
        child = Path(self.render()["song"])
        replay_out = self.root / "replay"
        result = self.le.replay_locked_edit(child, replay_out)
        replayed = Path(result["song"])
        validate_song(replayed)
        self.assertEqual(result["mix_sha256"],
                         self._sha(child / "full_mix.wav"))
        self.assertEqual(self._sha(replayed / "stems/voice.wav"),
                         self._sha(child / "stems/voice.wav"))

    def test_replay_rejects_mutated_or_missing_parent(self):
        child = Path(self.render()["song"])
        stranger = self.root / "stranger-parent"
        stranger_score = self.score()
        stranger_score["title"] = "different content"  # changes every recorded hash
        render_score(stranger_score, stranger, sr=self.sr)

        def tamper(parent_path):
            recipe_path = child / "locked_edit_recipe.json"
            recipe = json.loads(recipe_path.read_text())
            recipe["parent"]["path"] = parent_path
            recipe_path.write_text(json.dumps(recipe, indent=2))

        # A different parent directory whose mix hash differs is refused.
        tamper(str(stranger))
        with self.assertRaises(ValueError):
            self.le.replay_locked_edit(child, self.root / "replay2")
        self.assertFalse((self.root / "replay2").exists())
        # A parent path that is gone entirely is refused.
        tamper(str(self.root / "no-such-parent"))
        with self.assertRaises(ValueError):
            self.le.replay_locked_edit(child, self.root / "replay3")
        self.assertFalse((self.root / "replay3").exists())

    def test_stale_parent_hash_rejected(self):
        self.assertRejected(self.edit(expected_parent_mix_sha256="0" * 64))

    def test_same_family_rejected(self):
        self.assertRejected(phrase=self.phrase(family_id="fam-old"))

    def test_identity_must_be_explicitly_verified(self):
        self.assertRejected(edit=self.edit(identity_verified=False))
        self.assertRejected(phrase=self.phrase(identity_verified=False))

    def test_forbidden_use_and_empty_purpose_rejected(self):
        self.assertRejected(phrase=self.phrase(allowed_uses=["something-else"]))
        self.assertRejected(edit=self.edit(purpose=""))

    def test_nonfinite_boolean_or_out_of_bounds_beats_rejected(self):
        bad_edits = [
            self.edit(start_beat=float("nan")),
            self.edit(end_beat=float("inf")),
            self.edit(start_beat=True),
            self.edit(end_beat=8),  # tail boundary equality is fine for end
        ]
        # End exactly on the last musical beat is valid; verify it renders.
        good = self.render(edit=self.edit(end_beat=8), out=self.root / "edge-end")
        validate_song(Path(good["song"]))
        for bad in bad_edits[:3]:
            with self.subTest(bad=bad):
                self.assertRejected(bad)
        for kwargs in ({"start_beat": -1}, {"end_beat": 9},
                       {"start_beat": 5, "end_beat": 3}):
            with self.subTest(kwargs=kwargs):
                self.assertRejected(self.edit(**kwargs))

    def test_crossing_events_rejected(self):
        sample = str(self.root / "tone440.wav")
        score = self.score()
        score["tracks"][0]["events"].append(
            {"beat": 3.5, "note": 69, "duration_beats": 1, "velocity": 0.8})
        parent = self.root / "crossing-parent"
        render_score(score, parent, sr=self.sr)
        with self.assertRaises(ValueError):
            self.le.render_locked_edit(parent, self.root / "crossing-child",
                                       self.edit(), self.phrase())
        self.assertFalse((self.root / "crossing-child").exists())

    def test_no_target_events_in_interval_rejected(self):
        self.assertRejected(self.edit(start_beat=2, end_beat=3))

    def test_candidate_too_long_rejected(self):
        self.assertRejected(phrase=self.phrase(
            path=str(self.root / "phrase330long.wav"),
            sha256=self._sha(self.root / "phrase330long.wav")))

    def test_same_audio_even_with_gain_change_rejected(self):
        # Byte-identical copy: source hash catches it.
        identical = self.root / "copy440.wav"
        identical.write_bytes((self.root / "tone440.wav").read_bytes())
        self.assertRejected(phrase=self.phrase(
            path=str(identical), sha256=self._sha(identical)))
        # Simple gain change: different file hash, same decoded material.
        quiet, sr = sf.read(self.root / "tone440.wav")
        quiet_path = self.root / "quiet440.wav"
        sf.write(quiet_path, quiet * 0.5, sr, subtype="FLOAT")
        self.assertRejected(phrase=self.phrase(
            path=str(quiet_path), sha256=self._sha(quiet_path)))

    def test_silent_candidate_rejected(self):
        silent_path = self.root / "silent.wav"
        sf.write(silent_path, np.zeros(11025), self.sr, subtype="FLOAT")
        self.assertRejected(phrase=self.phrase(
            path=str(silent_path), sha256=self._sha(silent_path)))

    def test_drum_or_bass_target_rejected(self):
        self.assertRejected(self.edit(track_id="drums"))
        self.assertRejected(self.edit(track_id="bass"))

    def test_target_track_without_events_rejected(self):
        score = self.score()
        score["tracks"][0]["events"] = []
        parent = self.root / "empty-voice-parent"
        render_score(score, parent, sr=self.sr)
        with self.assertRaises(ValueError):
            self.le.render_locked_edit(parent, self.root / "empty-voice-child",
                                       self.edit(), self.phrase())

    def test_missing_or_corrupt_phrase_inputs_rejected(self):
        missing = self.root / "missing.wav"
        self.assertRejected(phrase=self.phrase(path=str(missing)))
        bad = self.root / "bad.wav"
        bad.write_bytes(b"RIFFxxxxWAVEfmt broken-not-audio")
        self.assertRejected(phrase=self.phrase(
            path=str(bad), sha256=self._sha(bad)))
        self.assertRejected(phrase=self.phrase(sha256="0" * 64))

    def test_existing_out_is_not_overwritten(self):
        out = self.root / "taken"
        out.mkdir()
        (out / "marker").write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            self.le.render_locked_edit(self.parent, out,
                                       self.edit(), self.phrase())
        self.assertEqual((out / "marker").read_bytes(), b"keep")

    def test_clipping_result_rejected(self):
        # Parent global peak comes from the lone voice note in bar 1 (raw .5),
        # so the frozen master gain is large. In bar 2 the 2-second replacement
        # phrase is still sounding at beat 6 and adds in phase with keys.
        score = {"title": "clip", "bpm": 120, "bars": 2, "tail_seconds": 0.2,
                 "fade_seconds": 0, "tracks": [
                     {"id": "voice", "sample": str(self.root / "tone440.wav"),
                      "root_midi": 69, "gain_db": -6, "role": "answer", "events": [
                          {"beat": 0, "note": 69, "duration_beats": 1, "velocity": 1.0},
                          {"beat": 4, "note": 69, "duration_beats": 4, "velocity": 1.0}]},
                     {"id": "keys", "sample": str(self.root / "tone330.wav"),
                      "root_midi": 69, "gain_db": -12, "events": [
                          {"beat": 6, "note": 69, "duration_beats": 1, "velocity": 1.0}]}]}
        parent = self.root / "clip-parent"
        render_score(score, parent, sr=self.sr)
        phrase_path = self.root / "phrase330long.wav"
        edit = {"track_id": "voice", "start_beat": 4, "end_beat": 8,
                "expected_parent_mix_sha256": self._sha(parent / "full_mix.wav"),
                "current_family_id": "fam-old", "identity_verified": True,
                "purpose": "vocal-replace"}
        phrase = {"id": "answer-long", "path": str(phrase_path),
                  "sha256": self._sha(phrase_path), "family_id": "fam-new",
                  "identity_verified": True, "allowed_uses": ["vocal-replace"],
                  "root_midi": 60, "role": "answer"}
        with self.assertRaises(ValueError):
            self.le.render_locked_edit(parent, self.root / "clip-child",
                                       edit, phrase)
        self.assertFalse((self.root / "clip-child").exists())


if __name__ == "__main__":
    unittest.main()
