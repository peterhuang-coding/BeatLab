"""Structural tests for examples/real_record_flip.py.

These tests check the score contract, arrangement shape, provenance and
determinism.  One test renders the score with the public song renderer using
locally synthesised stand-in WAVs, proving the emitted contract is accepted.
No musical/audio-quality claims are made.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_flip():
    spec = importlib.util.spec_from_file_location(
        'real_record_flip', ROOT / 'examples' / 'real_record_flip.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules['real_record_flip'] = module
    spec.loader.exec_module(module)
    return module


flip = _load_flip()
PreparedError = flip.PreparedError


def make_prepared() -> dict:
    """A valid prepared document; audio files need not exist for build_score."""
    def sl(sid, role, target, bass, note, gain, reverse=False, start=1.0):
        return {
            'id': sid, 'file': f'/prepared/audio/{sid}.wav', 'role': role,
            'target_beats': target, 'root_midi': 60, 'bass_midi': bass,
            'gain_db': gain, 'midi_note': note,
            'source_id': f'src-{sid.split("-")[0]}',
            'source_file': f'/recordings/{sid.split("-")[0]}.wav',
            'source_sha256': f'sha-src-{sid}',
            'start_seconds': start, 'end_seconds': start + target * 0.652,
            'semitones': 2 if sid.endswith('a') else -1,
            'reverse': reverse, 'sha256': f'sha-cut-{sid}',
        }
    slices = [sl(f'lead-{c}', 'lead', 2, bass, note, gain)
              for c, bass, note, gain in
              (('a', 45, 36, -3.0), ('b', 43, 38, -4.5),
               ('c', 45, 40, -2.5), ('d', 41, 42, -5.0))]
    slices += [sl(f'answer-{c}', 'answer', 2, bass, note, gain)
               for c, bass, note, gain in
               (('a', 45, 44, -5.0), ('b', 43, 46, -6.0),
                ('c', 45, 48, -4.0), ('d', 40, 51, -6.5))]
    slices += [sl('turn-a', 'turnaround', 4, 45, 37, -2.0, reverse=True, start=40.0),
               sl('turn-b', 'turnaround', 4, 43, 39, -3.5, start=70.0)]
    instruments = [
        {'id': 'kick', 'sample': 'drums/kick.wav', 'name': 'Kick'},
        {'id': 'snare', 'sample': 'drums/snare.wav', 'name': 'Snare'},
        {'id': 'hat', 'sample': 'drums/hat.wav', 'name': 'Hat'},
        {'id': 'open_hat', 'sample': 'drums/open_hat.wav', 'name': 'Open Hat'},
        {'id': 'shaker', 'sample': 'drums/shaker.wav', 'name': 'Shaker'},
        {'id': 'rim', 'sample': 'drums/rim.wav', 'name': 'Rim'},
        {'id': 'bass', 'sample': 'melody/bass.wav', 'name': 'Bass',
         'root_midi': 45},
    ]
    source_records = [
        {'source_id': 'src-lead', 'title': 'Historical recording one',
         'year': 1968, 'statement': 'public archive review copy, rights unasserted'},
        {'source_id': 'src-answer', 'title': 'Historical recording two',
         'year': 1972, 'statement': 'public archive review copy, rights unasserted'},
        {'source_id': 'src-turn', 'title': 'Historical recording three',
         'year': 1971, 'statement': 'public archive review copy, rights unasserted'},
    ]
    return {'bpm': 92, 'sample_root': '/prepared', 'slices': slices,
            'instruments': instruments, 'source_records': source_records}




class StructuralBoundsTests(unittest.TestCase):
    def setUp(self):
        self.prepared = make_prepared()
        self.score = flip.build_score(self.prepared)



    def test_all_events_in_bounds(self):
        for track in self.score['tracks']:
            for e in track['events']:
                self.assertGreaterEqual(e['beat'], 0)
                self.assertLess(e['beat'], 80, f'{track["id"]} starts at/after 80')
                self.assertGreater(e['duration_beats'], 0)
                self.assertLessEqual(e['beat'] + e['duration_beats'], 80,
                                     f'{track["id"]} ends after 80')
                self.assertGreaterEqual(e.get('note', 60), 0)
                self.assertLessEqual(e.get('note', 60), 127)

    def test_slice_event_duration_within_cut(self):
        targets = {s['id']: s['target_beats']
                   for s in self.score['sample_flip']['slices']}
        for p in self.score['sample_flip']['placements']:
            self.assertLessEqual(p['duration_beats'], targets[p['slice_id']])




class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.score = flip.build_score(make_prepared())

    def _sample_tracks(self):
        ids = {p['slice_id'] for p in self.score['sample_flip']['placements']}
        return [t for t in self.score['tracks'] if t['id'] in ids]

    def test_placements_map_exactly_to_sample_events(self):
        placements = self.score['sample_flip']['placements']
        tracks = {t['id']: t for t in self._sample_tracks()}
        self.assertEqual(set(tracks), {p['slice_id'] for p in placements})
        total_events = sum(len(t['events']) for t in tracks.values())
        self.assertEqual(total_events, len(placements))
        index: dict[tuple, int] = {}
        for p in placements:
            key = (p['slice_id'], p['beat'])
            self.assertNotIn(key, index)
            index[key] = 0
            event = next(e for e in tracks[p['slice_id']]['events']
                         if e['beat'] == p['beat'])
            self.assertEqual(event['duration_beats'], p['duration_beats'])
            self.assertEqual(event['velocity'], p['velocity'])
            self.assertEqual(event['note'], 60)
            self.assertEqual(p['note'], 60)

    def test_no_cut_overlap(self):
        intervals = sorted((p['beat'], p['beat'] + p['duration_beats'])
                           for p in self.score['sample_flip']['placements'])
        for (s0, e0), (s1, e1) in zip(intervals, intervals[1:]):
            self.assertLessEqual(e0, s1, f'cuts overlap: [{s0},{e0}) vs [{s1},{e1})')






class ArrangementTests(unittest.TestCase):
    def setUp(self):
        self.score = flip.build_score(make_prepared())
        self.placements = self.score['sample_flip']['placements']

    def test_opening_motif_recurs_in_return(self):
        first = {round(p['beat'] - 16, 4): (p['slice_id'], p['duration_beats'],
                                            p['velocity'])
                 for p in self.placements if 16 <= p['beat'] < 21}
        recalled = {round(p['beat'] - 64, 4): (p['slice_id'], p['duration_beats'],
                                               p['velocity'])
                    for p in self.placements if 64 <= p['beat'] < 69}
        self.assertEqual(first, recalled)
        self.assertGreaterEqual(len(first), 3)






class DrumPatternTests(unittest.TestCase):
    def setUp(self):
        self.score = flip.build_score(make_prepared())
        self.tracks = {t['id']: t for t in self.score['tracks']}

    def test_snare_patterns_differ_between_hook_and_halftime(self):
        snare = self.tracks['snare']['events']
        # backbeats = loud hits; ghost-note colours are excluded here
        hook = [e for e in snare if 16 <= e['beat'] < 48 and e['velocity'] >= 0.5]
        half = [e for e in snare if 48 <= e['beat'] < 64 and e['velocity'] >= 0.5]
        hook_offsets = sorted({round(e['beat'] % 4, 4) for e in hook})
        half_offsets = sorted({round(e['beat'] % 4, 4) for e in half})
        self.assertEqual(hook_offsets, [1.035, 3.035])
        self.assertEqual(half_offsets, [2.035])
        # 2 per hook bar, 1 per halftime bar
        self.assertEqual(len(hook), 16)
        self.assertEqual(len(half), 4)









class BassTests(unittest.TestCase):
    def setUp(self):
        self.prepared = make_prepared()
        self.score = flip.build_score(self.prepared)
        bass_track = next(t for t in self.score['tracks'] if t['id'] == 'bass')
        self.events = bass_track['events']
        self.used_slices = {s['id']: s
                            for s in self.score['sample_flip']['slices']}

    def test_single_bass_voice_and_no_overlap(self):
        ordered = sorted(self.events, key=lambda e: e['beat'])
        for a, b in zip(ordered, ordered[1:]):
            self.assertLessEqual(a['beat'] + a['duration_beats'], b['beat'])

    def test_bass_notes_derive_from_slice_roots_or_fifths(self):
        roots = {s['bass_midi'] for s in self.used_slices.values()}
        allowed = roots | {r + 7 for r in roots}
        for e in self.events:
            self.assertIn(e['note'], allowed)




class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.prepared = make_prepared()
        self.score = flip.build_score(self.prepared)

    def test_slices_copied_verbatim(self):
        self.assertEqual(self.score['sample_flip']['slices'], self.prepared['slices'])





class DeterminismTests(unittest.TestCase):
    def test_repeated_builds_identical(self):
        a = flip.build_score(make_prepared())
        b = flip.build_score(make_prepared())
        self.assertEqual(a, b)
        self.assertEqual(json.dumps(a, ensure_ascii=False, sort_keys=True),
                         json.dumps(b, ensure_ascii=False, sort_keys=True))


class ValidationTests(unittest.TestCase):
    def assertRejected(self, prepared):
        with self.assertRaises(PreparedError):
            flip.build_score(prepared)




    def test_too_few_leads(self):
        p = make_prepared()
        p['slices'] = [s for s in p['slices'] if s['id'] != 'lead-a']
        self.assertRejected(p)





    def test_duplicate_slice_id(self):
        p = make_prepared()
        p['slices'][1]['id'] = p['slices'][0]['id']
        self.assertRejected(p)

    def test_missing_instrument_role(self):
        p = make_prepared()
        p['instruments'] = [i for i in p['instruments'] if i['id'] != 'rim']
        self.assertRejected(p)




    def test_bad_times(self):
        p = make_prepared()
        p['slices'][0]['end_seconds'] = 0.5
        self.assertRejected(p)



class CliTests(unittest.TestCase):
    def test_cli_writes_score_and_refuses_overwrite(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            td = Path(temp)
            prepared_path = td / 'prepared.json'
            score_path = td / 'nested' / 'score.json'
            prepared_path.write_text(json.dumps(make_prepared()))
            flip.main(['--prepared', str(prepared_path), '--score', str(score_path)])
            self.assertTrue(score_path.is_file())
            emitted = json.loads(score_path.read_text())
            self.assertEqual(emitted, flip.build_score(make_prepared()))
            with self.assertRaises(SystemExit):
                flip.main(['--prepared', str(prepared_path),
                           '--score', str(score_path)])


class RendererContractTests(unittest.TestCase):
    """The existing song renderer must accept the emitted score end to end."""

    def test_render_score_with_local_stand_in_wavs(self):
        import numpy as np
        import soundfile as sf
        from pipeline.song import render_score

        rng = np.random.RandomState(7)
        sr = 22050

        def write_tone(path: Path, seconds: float, freq: float, stereo_jitter=True):
            path.parent.mkdir(parents=True, exist_ok=True)
            n = max(1, int(seconds * sr))
            t = np.arange(n) / sr
            x = 0.35 * np.sin(2 * np.pi * freq * t)
            y = 0.30 * np.sin(2 * np.pi * (freq * 1.01) * t + .3)
            if not stereo_jitter:
                y = x * 0.9
            sf.write(path, np.stack([x, y], axis=1).astype('float32'), sr)

        def write_noise(path: Path, seconds: float):
            path.parent.mkdir(parents=True, exist_ok=True)
            n = max(1, int(seconds * sr))
            x = rng.standard_normal(n).astype('float32') * 0.4
            sf.write(path, np.stack([x, x * 0.85], axis=1), sr)

        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            td = Path(temp)
            prepared = make_prepared()
            prepared['sample_root'] = str(td)
            # stand-in WAVs for each instrument (relative sample paths)
            for inst in prepared['instruments']:
                target = td / inst['sample']
                if inst['id'] == 'bass':
                    write_tone(target, 0.8, 110.0, stereo_jitter=False)
                else:
                    write_noise(target, 0.18)
            # stand-in WAVs for every slice, at absolute paths
            freqs = {'lead': [196, 220, 175, 233],
                     'answer': [196, 220, 165, 247],
                     'turnaround': [147, 131]}
            for sl in prepared['slices']:
                seconds = float(sl['target_beats']) * 60 / 92 + 0.2
                group = sl['role']
                index = ord(sl['id'].rsplit('-', 1)[-1]) - ord('a')
                write_tone(td / f'{sl["id"]}.wav', seconds,
                           freqs[group][index])
                sl['file'] = str((td / f'{sl["id"]}.wav').resolve())
            score = flip.build_score(prepared)
            out = td / 'render'
            manifest = render_score(score, out, sr=sr)
            self.assertTrue((out / 'full_mix.wav').is_file())
            self.assertTrue((out / 'score.json').is_file())
            self.assertTrue((out / 'run_manifest.json').is_file())
            self.assertEqual(len(manifest['tracks']), len(score['tracks']))
            for record in manifest['tracks']:
                self.assertTrue((out / record['file']).is_file())
                self.assertTrue((out / record['midi']).is_file())
            self.assertEqual(manifest['bars'], 20)
            self.assertEqual(manifest['bpm'], 92)


if __name__ == '__main__':
    unittest.main()
