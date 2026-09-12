"""Media and Live XML contracts; these checks are not a Live render test."""
import gzip
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

import numpy as np
import soundfile as sf
from mido import Message, MidiFile, MidiTrack

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
CORE = Path('/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library')
TEMPLATE = CORE / 'Defaults/Creating Tracks/Audio Track/Default Audio Track.als'
CLIP_TEMPLATE = CORE / 'Lessons/Sets/Driver Error Compensation.als'
ARRANGEMENT = 'DeviceChain/MainSequencer/Sample/ArrangerAutomation/Events'


class ExportIntegrityTests(unittest.TestCase):
    def setUp(self):
        try:
            self.exporter = importlib.import_module('ableton_export')
        except ModuleNotFoundError:
            self.exporter = None
        self.assertIsNotNone(self.exporter, 'Ableton song exporter is not implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.song = self.root / 'song'
        (self.song / 'stems').mkdir(parents=True)
        (self.song / 'midi').mkdir()
        sr, frames = 8000, 18000
        t = np.arange(frames) / sr
        self.layers = [np.column_stack([.12*np.sin(2*np.pi*f*t),
                                       .09*np.cos(2*np.pi*f*t)]) for f in (220, 110)]
        records = []
        for i, audio in enumerate(self.layers):
            tid = f'track{i}'
            path = self.song / f'stems/{tid}.wav'
            sf.write(path, audio, sr, subtype='PCM_24')
            midi = MidiFile()
            midi.tracks.append(MidiTrack([Message('note_on', note=60, velocity=80),
                                         Message('note_off', note=60, time=480)]))
            midi.save(self.song / f'midi/{tid}.mid')
            records.append({'id': tid, 'name': f'Part {i}', 'file': f'stems/{tid}.wav',
                            'midi': f'midi/{tid}.mid', 'sha256': self.sha(path)})
        sf.write(self.song / 'full_mix.wav', sum(self.layers), sr, subtype='PCM_24')
        self.manifest = {'title': 'Windowlight', 'bpm': 88, 'bars': 1,
                         'sample_rate': sr, 'duration_seconds': frames/sr,
                         'tracks': records, 'mix': 'full_mix.wav',
                         'sections': [{'name': 'Intro', 'start_bar': 0, 'bars': 1}]}
        (self.song / 'score.json').write_text(json.dumps({'title': 'Fixture', 'tracks': records}))
        self.save_manifest()

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def save_manifest(self):
        (self.song / 'run_manifest.json').write_text(json.dumps(self.manifest))

    def rewrite_stem(self, audio, sr=8000):
        path = self.song / self.manifest['tracks'][0]['file']
        sf.write(path, audio, sr, subtype='PCM_24')
        self.manifest['tracks'][0]['sha256'] = self.sha(path)
        self.save_manifest()

    def test_validates_actual_audio_sum_and_reports_no_live_verification(self):
        result = self.exporter.validate_song(self.song)
        self.assertEqual(result['sample_rate'], 8000)
        self.assertEqual(result['frames'], 18000)
        self.assertEqual(result['channels'], 2)
        self.assertEqual(len(result['tracks']), 2)
        self.assertLess(result['stem_sum']['peak_error'], 4e-7)
        self.assertEqual(result['live_verification']['open'], 'unverified')
        self.assertEqual(result['live_verification']['rerender'], 'unverified')

    def test_missing_required_media_is_rejected_before_output_exists(self):
        for relative in ['stems/track0.wav', 'midi/track0.mid', 'full_mix.wav', 'score.json']:
            with self.subTest(relative=relative):
                path = self.song / relative
                original = path.read_bytes()
                path.unlink()
                with self.assertRaises(FileNotFoundError):
                    self.exporter.export_song(self.song, self.root / 'output')
                self.assertFalse((self.root / 'output').exists())
                path.write_bytes(original)

    def test_bad_hash_is_rejected(self):
        self.manifest['tracks'][0]['sha256'] = '0' * 64
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, '[Hh]ash|SHA'):
            self.exporter.validate_song(self.song)

    def test_missing_hash_is_rejected(self):
        del self.manifest['tracks'][0]['sha256']
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, '[Hh]ash|SHA'):
            self.exporter.validate_song(self.song)

    def test_silent_required_track_is_rejected(self):
        self.rewrite_stem(np.zeros_like(self.layers[0]))
        with self.assertRaisesRegex(ValueError, '[Ss]ilent|[Ee]mpty'):
            self.exporter.validate_song(self.song)

    def test_different_lengths_or_sample_rates_are_rejected(self):
        for audio, sr in [(self.layers[0][:-1], 8000), (self.layers[0], 16000)]:
            with self.subTest(frames=len(audio), sample_rate=sr):
                self.rewrite_stem(audio, sr)
                with self.assertRaisesRegex(ValueError, 'length|rate|align'):
                    self.exporter.validate_song(self.song)

    def test_manifest_timing_and_nonzero_start_are_rejected(self):
        self.manifest['duration_seconds'] += 1
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'duration|length'):
            self.exporter.validate_song(self.song)
        self.manifest['duration_seconds'] -= 1
        self.manifest['tracks'][0]['start_seconds'] = .2
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'start|origin'):
            self.exporter.validate_song(self.song)

    def test_empty_track_list_and_duplicate_ids_are_rejected(self):
        tracks = self.manifest['tracks']
        for invalid in [[], [tracks[0], tracks[0]]]:
            self.manifest['tracks'] = invalid
            self.save_manifest()
            with self.assertRaisesRegex(ValueError, 'tracks|duplicate|Duplicate'):
                self.exporter.validate_song(self.song)

    def test_stems_with_wrong_mix_gain_are_rejected(self):
        sf.write(self.song / 'full_mix.wav', sum(self.layers) * .5, 8000, subtype='PCM_24')
        with self.assertRaisesRegex(ValueError, 'reconstruct|residual|sum'):
            self.exporter.validate_song(self.song)

    def test_external_media_paths_are_rejected(self):
        source = self.song / 'stems/track0.wav'
        self.manifest['tracks'][0]['file'] = str(source)
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'relative|outside'):
            self.exporter.validate_song(self.song)
        self.manifest['tracks'][0]['file'] = '../outside.wav'
        shutil.copy2(source, self.root / 'outside.wav')
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'relative|outside'):
            self.exporter.validate_song(self.song)

    def test_existing_nonempty_project_is_never_overwritten(self):
        out = self.root / 'output'
        out.mkdir()
        (out / 'keep.txt').write_text('user content')
        with self.assertRaises(FileExistsError):
            self.exporter.export_song(self.song, out)
        self.assertEqual((out / 'keep.txt').read_text(), 'user content')

    def test_reassigns_global_targets_and_references_without_touching_local_ids(self):
        tree = ET.fromstring('<Track><Pointee Id="9"/><AutomationTarget Id="12"/>'
                             '<VolumeModulationTarget Id="14"/><PointeeId Value="9"/>'
                             '<EnvelopeTarget><PointeeId Value="12"/></EnvelopeTarget>'
                             '<ModulationTargetRef Id="14"/><ClipSlot Id="9"/></Track>')
        next_id = self.exporter._reassign_ids(tree, 100)
        self.assertEqual(next_id, 103)
        self.assertEqual(tree.find('Pointee').get('Id'), '100')
        self.assertEqual(tree.find('PointeeId').get('Value'), '100')
        self.assertEqual(tree.find('EnvelopeTarget/PointeeId').get('Value'), '101')
        self.assertEqual(tree.find('ModulationTargetRef').get('Id'), '102')
        self.assertEqual(tree.find('ClipSlot').get('Id'), '9')

    @unittest.skipUnless(TEMPLATE.exists() and CLIP_TEMPLATE.exists(), 'Live 12 templates required')
    def test_collected_project_has_real_clips_unity_gains_and_relocatable_media(self):
        out = self.root / 'output'
        result = self.exporter.export_song(self.song, out)
        als = Path(result['als'])
        self.assertEqual(als.name, 'Windowlight.als')
        root = ET.fromstring(gzip.decompress(als.read_bytes()))
        tracks = root.findall('LiveSet/Tracks/AudioTrack')
        self.assertEqual(len(tracks), 2)
        self.assertEqual(len(root.findall('.//AudioClip')), 2)
        self.assertEqual(len(root.findall('.//MidiClip')), 0)
        self.assertEqual(root.find('LiveSet/MainTrack/DeviceChain/Mixer/Volume/Manual').get('Value'), '1')
        self.assertEqual(float(root.find('LiveSet/MainTrack/DeviceChain/Mixer/Tempo/Manual').get('Value')), 88)
        for track in tracks:
            self.assertEqual(track.find('DeviceChain/Mixer/Volume/Manual').get('Value'), '1')
            self.assertEqual(track.find('DeviceChain/Mixer/Pan/Manual').get('Value'), '0')
            self.assertEqual(track.find('DeviceChain/AudioOutputRouting/Target').get('Value'), 'AudioOut/Main')
            clip = track.find(ARRANGEMENT + '/AudioClip')
            self.assertIsNotNone(clip)
            self.assertEqual(float(clip.get('Time')), 0)
            self.assertEqual(float(clip.find('CurrentStart').get('Value')), 0)
            self.assertAlmostEqual(float(clip.find('CurrentEnd').get('Value')), 2.25*88/60)
            self.assertEqual(float(clip.find('Loop/LoopEnd').get('Value')), 2.25)
            self.assertEqual(clip.find('IsWarped').get('Value'), 'false')
            self.assertEqual(clip.find('Loop/LoopOn').get('Value'), 'false')
            self.assertEqual(clip.find('SampleVolume').get('Value'), '1')
            self.assertEqual(clip.find('Fade').get('Value'), 'false')
        self.assertTrue(all(len(e) == 0 for e in root.findall('.//Devices')))
        ids = [int(e.get('Id')) for e in root.iter() if e.tag in ('Pointee', 'AutomationTarget')
               or e.tag.endswith('ModulationTarget')]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreater(int(root.find('LiveSet/NextPointeeId').get('Value')), max(ids))
        self.assertTrue(all(int(e.get('Value')) in ids for e in root.findall('.//PointeeId')))
        self.assertEqual(root.find('LiveSet/Locators/Locators/Locator/Name').get('Value'), 'Intro')
        self.assertTrue((out / 'Ableton Project Info').is_dir())
        self.assertEqual((out/'Source/score.json').read_bytes(), (self.song/'score.json').read_bytes())
        self.assertEqual((out/'Source/midi/track0.mid').read_bytes(), (self.song/'midi/track0.mid').read_bytes())
        self.assertEqual((out/'Reference/full_mix.wav').read_bytes(), (self.song/'full_mix.wav').read_bytes())
        self.assertEqual(json.loads((out/'export_manifest.json').read_text())['live_verification']['rerender'], 'unverified')
        moved = self.root / 'relocated' / 'different project name'
        shutil.copytree(out, moved)
        shutil.rmtree(out)
        shutil.rmtree(self.song)
        for ref in root.findall('.//FileRef'):
            relative = ref.find('RelativePath').get('Value')
            self.assertTrue(relative.startswith('Samples/Imported/'))
            self.assertEqual(ref.find('Path').get('Value'), '')
            self.assertEqual(ref.find('LivePackName').get('Value'), '')
            self.assertEqual(ref.find('LivePackId').get('Value'), '')
            copied = moved / relative
            self.assertTrue(copied.is_file())
            expected = next(t['sha256'] for t in self.manifest['tracks'] if Path(t['file']).name == copied.name)
            self.assertEqual(self.sha(copied), expected)
        self.assertNotIn('tom 11', ET.tostring(root, encoding='unicode'))


if __name__ == '__main__':
    unittest.main()
