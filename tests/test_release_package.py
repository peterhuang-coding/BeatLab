"""A draft delivery must preserve the music and carry its publication limits."""
import importlib
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'pipeline'))


class DraftPackageTests(unittest.TestCase):
    def setUp(self):
        try:
            self.package = importlib.import_module('package')
        except ModuleNotFoundError:
            self.package = None
        self.assertIsNotNone(self.package, 'draft package builder not implemented')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.song = self.root/'song'
        (self.song/'stems').mkdir(parents=True)
        (self.song/'midi').mkdir()
        t = np.arange(97020)/44100
        audio = .1*np.sin(2*np.pi*440*t)
        sf.write(self.song/'stems/piano.wav', np.stack([audio,audio],axis=1), 44100, subtype='PCM_24')
        (self.song/'full_mix.wav').write_bytes((self.song/'stems/piano.wav').read_bytes())
        from mido import MidiFile, MidiTrack, MetaMessage, Message
        midi = MidiFile()
        midi.tracks.append(MidiTrack([Message('note_on',note=69,velocity=70),
                                     Message('note_off',note=69,time=480),MetaMessage('end_of_track')]))
        midi.save(self.song/'midi/piano.mid')
        (self.song/'score.json').write_text('{}')
        self.manifest = dict(title='Fixture', bpm=120, bars=1, mix='full_mix.wav',
                             duration_seconds=2.2, sample_rate=44100,
                             license={'commercial_status':'needs_review'},
                             tracks=[dict(id='piano',name='Piano',file='stems/piano.wav',midi='midi/piano.mid',
                                          sha256=hashlib.sha256((self.song/'stems/piano.wav').read_bytes()).hexdigest())])
        self.save()

    def save(self):
        (self.song/'run_manifest.json').write_text(json.dumps(self.manifest))

    def test_draft_contains_unmodified_master_individual_tracks_and_rights(self):
        out = self.root/'delivery'
        self.package.build_package(self.song, out)
        self.assertEqual((out/'master.wav').read_bytes(), (self.song/'full_mix.wav').read_bytes())
        metadata = json.loads((out/'metadata.json').read_text())
        self.assertEqual(metadata['publication_status'], 'draft')
        self.assertFalse(metadata['ready_to_publish'])
        self.assertEqual(metadata['license']['commercial_status'], 'needs_review')
        self.assertTrue((out/'preview.mp3').is_file())
        with zipfile.ZipFile(out/'trackouts.zip') as z:
            self.assertEqual(z.namelist(), ['stems/piano.wav'])
            self.assertEqual(z.read('stems/piano.wav'), (self.song/'stems/piano.wav').read_bytes())
        self.assertIn(str((out/'master.wav').resolve()), (out/'DJ.m3u8').read_text())

    def test_missing_stem_does_not_leave_a_partial_package(self):
        (self.song/'stems/piano.wav').unlink()
        out = self.root/'delivery'
        with self.assertRaises(FileNotFoundError):
            self.package.build_package(self.song, out)
        self.assertFalse(out.exists())

    def test_changed_stem_is_rejected(self):
        self.manifest['tracks'][0]['sha256'] = '0'*64
        self.save()
        with self.assertRaises(ValueError):
            self.package.build_package(self.song, self.root/'delivery')

    def test_previous_delivery_is_preserved(self):
        out = self.root/'delivery'
        out.mkdir()
        (out/'user-file.txt').write_text('keep')
        with self.assertRaises(FileExistsError):
            self.package.build_package(self.song, out)
        self.assertEqual((out/'user-file.txt').read_text(), 'keep')

    def test_source_change_during_packaging_cannot_receive_a_passed_label(self):
        original_run = self.package.subprocess.run
        def change_after_preview(*args, **kwargs):
            result = original_run(*args, **kwargs)
            stem = self.song/'stems/piano.wav'
            y, sr = sf.read(stem)
            sf.write(stem, y*.5, sr, subtype='PCM_24')
            return result
        out = self.root/'delivery'
        with patch.object(self.package.subprocess, 'run', side_effect=change_after_preview):
            with self.assertRaisesRegex(ValueError, 'changed|mismatch'):
                self.package.build_package(self.song, out)
        self.assertFalse(out.exists())
