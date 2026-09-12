"""Audio invariants for the editable, sample-instrument song renderer."""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))


class SongRenderTests(unittest.TestCase):
    def setUp(self):
        try:
            self.song = importlib.import_module("song")
        except ModuleNotFoundError:
            self.song = None
        self.assertIsNotNone(self.song, "sample-instrument song renderer is not implemented")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sr = 22050
        t = np.arange(self.sr) / self.sr
        self.source = self.root / "tone.wav"
        sf.write(self.source, .3 * np.sin(2 * np.pi * 440 * t), self.sr, subtype="FLOAT")

    def score(self):
        return {"title": "fixture", "bpm": 120, "bars": 1, "tail_seconds": .2,
                "tracks": [{"id": "keys", "sample": str(self.source), "root_midi": 69,
                            "gain_db": -12, "events": [{"beat": 0, "note": 69,
                            "duration_beats": 1, "velocity": .5}]},
                           {"id": "bass", "sample": str(self.source), "root_midi": 69,
                            "gain_db": -18, "events": [{"beat": 1, "note": 57,
                            "duration_beats": 1, "velocity": .7}]}]}

    def test_pitch_changes_frequency_without_changing_requested_gate(self):
        y = self.song.sample_voice(self.source, 69, 81, .25, self.sr)
        self.assertEqual(y.shape, (round(.25*self.sr), 2))
        spectrum = abs(np.fft.rfft(y[100:-100, 0]))
        freq = np.fft.rfftfreq(len(y)-200, 1/self.sr)[np.argmax(spectrum)]
        self.assertAlmostEqual(freq, 880, delta=6)

    def test_exported_stems_sum_to_actual_listening_mix(self):
        out = self.root / "result"
        self.song.render_score(self.score(), out, sr=self.sr)
        mix, sr = sf.read(out / "full_mix.wav", always_2d=True)
        stems = [sf.read(p, always_2d=True)[0] for p in sorted((out/'stems').glob('*.wav'))]
        self.assertEqual(sr, self.sr)
        self.assertEqual(len(mix), round(2.2 * self.sr))
        self.assertLess(np.max(np.abs(sum(stems)-mix)), 5e-7)
        self.assertLessEqual(np.max(abs(mix)), 10**(-1/20)+1e-6)
        manifest = json.loads((out/'run_manifest.json').read_text())
        self.assertEqual(len(manifest['tracks']), 2)
        self.assertTrue(all((out/t['midi']).is_file() for t in manifest['tracks']))

    def test_missing_source_fails_before_creating_a_song(self):
        score = self.score()
        score['tracks'][0]['sample'] = str(self.root/'missing.wav')
        out = self.root/'missing-result'
        with self.assertRaises(FileNotFoundError):
            self.song.render_score(score, out, sr=self.sr)
        self.assertFalse((out/'full_mix.wav').exists())

    def test_existing_delivery_is_not_overwritten(self):
        out = self.root/'existing'
        out.mkdir()
        (out/'full_mix.wav').write_bytes(b'old music')
        with self.assertRaises(FileExistsError):
            self.song.render_score(self.score(), out, sr=self.sr)
        self.assertEqual((out/'full_mix.wav').read_bytes(), b'old music')

    def test_omitted_note_uses_sample_root_in_audio_and_midi(self):
        from mido import MidiFile
        score = self.score()
        del score['tracks'][0]['events'][0]['note']
        out = self.root/'default-note'
        self.song.render_score(score, out, sr=self.sr)
        notes = [m.note for t in MidiFile(out/'midi/keys.mid').tracks
                 for m in t if m.type == 'note_on']
        self.assertEqual(notes, [69])
