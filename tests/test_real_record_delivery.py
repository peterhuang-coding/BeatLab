"""Synthetic tests for examples.real_record_delivery.

No media is committed or downloaded: the "full recordings" and "processed cuts"
are deterministic sinusoid/noise WAVs written into temporary directories. The
tests exercise the non-template helper APIs; one end-to-end deliver() test runs
only when the local Ableton factory templates are present, and even then it only
inspects preset references statically - Live itself is never claimed to run.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import shutil
import tempfile
import unittest

import numpy as np
import soundfile as sf
from mido import MidiFile, bpm2tempo

from examples import real_record_delivery as d
from examples.real_record_delivery import (
    validate_flip, load_flip, build_listening, build_solo, collect_samples,
    write_phrase_midi, level_for_match, read_stereo, deliver, sha256,
    factory_templates_available)

BPM = 92
BARS = 20
BEAT = 60 / BPM


def _tone(frequency, seconds, amplitude=0.5, rate=44100, channels=2, phase=0.0):
    t = np.arange(round(seconds * rate), dtype=np.float32) / rate
    wave = amplitude * np.sin(2 * np.pi * frequency * t + phase).astype(np.float32)
    env = np.minimum(1.0, t / 0.01) * np.minimum(1.0, (seconds - t) / 0.02)
    wave = wave * np.clip(env, 0, 1)
    if channels == 1:
        return wave
    return np.repeat(wave[:, None], 2, axis=1)


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class SongFixture:
    """Builds the prepared inputs and renders a full synthetic 20-bar song."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.recordings_dir = root / 'recordings'
        self.sample_root = root / 'sample_root'
        self.song_dir = root / 'song'
        self.recordings_dir.mkdir()
        self.sample_root.mkdir()

        # Three "full historical recordings": deliberately mono at 48 kHz so
        # rate conversion and stereo promotion are exercised by the delivery.
        self.recordings = []
        for index, (name, freq) in enumerate([('rec_one', 180), ('rec_two', 220),
                                              ('rec_three', 140)]):
            t = np.arange(8 * 48000, dtype=np.float32) / 48000
            audio = (0.6 * np.sin(2 * np.pi * freq * t)
                     + 0.15 * np.sin(2 * np.pi * (freq * 1.5) * t)).astype(np.float32)
            audio *= np.clip(np.minimum(1.0, t / 0.05)
                             * np.minimum(1.0, (8 - t) / 0.05), 0, 1)
            path = self.recordings_dir / f'{name}.wav'
            sf.write(path, audio, 48000, subtype='PCM_24')
            self.recordings.append({'id': name, 'file': str(path), 'sha256': _digest(path)})

        self._build_slices()
        self._build_instruments()
        self._build_score()
        from pipeline.song import render_score
        render_score(self.score, self.song_dir)
        self.manifest = json.loads((self.song_dir / 'run_manifest.json').read_text())

    def _build_slices(self):
        spec = ([('lead', f'lead{i + 1}', 2, i) for i in range(4)]
                + [('answer', f'answer{i + 1}', 2, 4 + i) for i in range(4)]
                + [('turnaround', f'turn{i + 1}', 4, 8 + i) for i in range(2)])
        beats = [0, 8, 32, 40, 4, 12, 36, 44, 64, 72]
        freqs = [330, 392, 440, 523, 294, 349, 494, 587, 262, 392]
        self.slices, self.placements = [], []
        for (role, slice_id, target_b, slot_index), beat, freq in zip(spec, beats, freqs):
            recording = self.recordings[slot_index % len(self.recordings)]
            seconds = target_b * BEAT
            start = 0.4 + (slot_index % 5) * 0.18
            cut = _tone(freq, seconds, amplitude=0.35 + 0.03 * slot_index)
            cut_path = self.sample_root / f'{slice_id}.wav'
            sf.write(cut_path, cut, 44100, subtype='PCM_24')
            self.slices.append({
                'id': slice_id, 'file': str(cut_path.resolve()),
                'role': role, 'target_beats': float(target_b), 'root_midi': 60,
                'bass_midi': 36 + slot_index, 'gain_db': -7.0 - 0.2 * (slot_index % 3),
                'midi_note': 36 + slot_index, 'source_id': recording['id'],
                'source_file': str(recording['file']),
                'source_sha256': recording['sha256'],
                'start_seconds': start, 'end_seconds': start + seconds,
                'semitones': (slot_index % 7) - 3, 'reverse': False,
                'sha256': _digest(cut_path), 'track': slice_id})
            self.placements.append({'slice': slice_id, 'beat': float(beat),
                                    'duration_beats': float(target_b),
                                    'velocity': 0.62 + 0.03 * (slot_index % 4)})

    def _build_instruments(self):
        kick = _tone(60, 0.35, amplitude=0.8)
        self.kick_path = self.sample_root / 'kick.wav'
        sf.write(self.kick_path, kick, 44100, subtype='PCM_24')
        snare = _tone(200, 0.25, amplitude=0.5)
        self.snare_path = self.sample_root / 'snare.wav'
        sf.write(self.snare_path, snare, 44100, subtype='PCM_24')
        bass = _tone(98, 0.9, amplitude=0.55)
        self.bass_path = self.sample_root / 'bass.wav'
        sf.write(self.bass_path, bass, 44100, subtype='PCM_24')

    def _build_score(self):
        tracks = []
        for sliced in self.slices:
            placement = next(p for p in self.placements if p['slice'] == sliced['id'])
            tracks.append({
                'id': sliced['id'], 'sample': f'{sliced["id"]}.wav',
                'name': f'{sliced["role"]} {sliced["id"]}', 'root_midi': 60,
                'gain_db': sliced['gain_db'],
                'events': [{'beat': placement['beat'], 'note': 60,
                            'duration_beats': placement['duration_beats'],
                            'velocity': placement['velocity']}]})
        drum_events = []
        for bar in range(BARS - 4):
            drum_events += [{'beat': bar * 4 + 0.0, 'duration_beats': 0.4, 'velocity': 0.8},
                            {'beat': bar * 4 + 2.0, 'duration_beats': 0.4, 'velocity': 0.7}]
        tracks.append({'id': 'kick', 'sample': 'kick.wav', 'name': 'Kick', 'root_midi': 60,
                       'gain_db': -10.0, 'drum': True, 'events': drum_events})
        snare_events = [{'beat': bar * 4 + off, 'duration_beats': 0.3, 'velocity': 0.55}
                        for bar in range(BARS - 4) for off in (1.0, 3.0)]
        tracks.append({'id': 'snare', 'sample': 'snare.wav', 'name': 'Snare', 'root_midi': 60,
                       'gain_db': -11.0, 'drum': True, 'events': snare_events})
        bass_events = [{'beat': bar * 4 + 0.0, 'duration_beats': 1.6, 'velocity': 0.5,
                        'note': 40} for bar in range(BARS - 4)]
        tracks.append({'id': 'bass', 'sample': 'bass.wav', 'name': 'Bass', 'root_midi': 60,
                       'gain_db': -12.0, 'events': bass_events})
        self.score = {
            'title': 'Gold From Dust', 'bpm': BPM, 'bars': BARS,
            'sample_root': str(self.sample_root.resolve()), 'tail_seconds': 1.0,
            'fade_seconds': 1.0,
            'license': {'scope': 'synthetic fixture; no third-party media'},
            'sections': [{'name': 'Hook', 'start_bar': 0},
                         {'name': 'Midsection', 'start_bar': 8},
                         {'name': 'Turnaround', 'start_bar': 16}],
            'tracks': tracks,
            'sample_flip': {'slices': self.slices, 'placements': self.placements,
                            'source_records': self.recordings}}

    def snapshot(self):
        files = {}
        for base in (self.recordings_dir, self.sample_root, self.song_dir):
            for path in base.rglob('*'):
                if path.is_file():
                    files[str(path)] = _digest(path)
        return files


class RealRecordDeliveryTest(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.temp = Path(self._temp.name)
        self.fixture = SongFixture(self.temp / 'fixture')
        self.song = self.fixture.song_dir
        self.score = json.loads((self.song / 'score.json').read_text())
        self.manifest = json.loads((self.song / 'run_manifest.json').read_text())

    def tearDown(self):
        self._temp.cleanup()

    def plan(self):
        return validate_flip(self.score, self.manifest)

    # -- Listening comparison ------------------------------------------------

    def test_comparison_sinusoid_lengths_labels_rms(self):
        plan = self.plan()
        listening_dir = self.temp / 'listening'
        meta = build_listening(self.song, plan, listening_dir)
        wav_path = listening_dir / 'original-cut-mix.wav'
        info = sf.info(wav_path)
        self.assertEqual(info.samplerate, 44100)
        self.assertEqual(info.channels, 2)

        segments = meta['segments']
        self.assertEqual([s['label'] for s in segments], ['raw', 'processed', 'mixed'])
        first = min(plan['placements'], key=lambda p: p['beat'])
        sliced = next(s for s in plan['slices'] if s['id'] == first['slice'])
        raw_frames = round(sliced['end_seconds'] * 44100) - round(sliced['start_seconds'] * 44100)
        cut_info = sf.info(sliced['file'])
        proc_frames = round(cut_info.frames * 44100 / cut_info.samplerate)
        mix_frames = round(16 * BEAT * 44100)  # first placement at bar 0, no edge clipping
        self.assertEqual(segments[0]['frames'], raw_frames)
        self.assertEqual(segments[1]['frames'], proc_frames)
        self.assertEqual(segments[2]['frames'], mix_frames)
        expected = raw_frames + 44100 + proc_frames + 44100 + mix_frames
        self.assertEqual(info.frames, expected)

        audio, _ = sf.read(wav_path, always_2d=True)
        offsets = []
        cursor = 0
        for index, frames in enumerate([raw_frames, proc_frames, mix_frames]):
            offsets.append((cursor, cursor + frames))
            cursor = cursor + frames + 44100
        levels = []
        for start, end in offsets:
            block = audio[start:end]
            levels.append(math.sqrt(float(np.mean(block ** 2))))
            self.assertLessEqual(float(np.max(np.abs(block))), 0.99 + 1e-6)
        # RMS matched to the mixed excerpt: raw/processed land within 5%.
        self.assertTrue(abs(levels[0] - levels[2]) / levels[2] < 0.05,
                        f'raw {levels[0]} vs mixed {levels[2]}')
        self.assertTrue(abs(levels[1] - levels[2]) / levels[2] < 0.05,
                        f'processed {levels[1]} vs mixed {levels[2]}')
        for segment, level in zip(segments, levels):
            self.assertAlmostEqual(segment['rms_after'], level, delta=1e-5)

        self.assertIn('not LUFS and not a blind test', meta['level_matching'])
        self.assertEqual(meta['gap_seconds'], 1.0)
        self.assertEqual(segments[0]['offset_kind'], 'recording offset seconds')
        self.assertEqual(segments[2]['offset_kind'], 'local excerpt offset seconds')
        self.assertEqual(segments[0]['start_seconds'], sliced['start_seconds'])
        self.assertEqual(segments[2]['start_seconds'], 0.0)

    def test_solo_default_and_peak_guard(self):
        # Quiet synthetic stems: no guard, unity gain.
        quiet = self.temp / 'quiet'
        quiet.mkdir()
        stems = []
        for index in range(2):
            path = quiet / f'{index}.wav'
            sf.write(path, np.ones((4410, 2), dtype=np.float32) * 0.2, 44100)
            stems.append({'id': f'q{index}', 'file': str(path.name)})
        plan = {'slices': [{'id': 'q0', 'track': 'q0'}, {'id': 'q1', 'track': 'q1'}]}
        (quiet / 'run_manifest.json').write_text(json.dumps({'tracks': stems}))
        meta = build_solo(quiet, plan, quiet / 'solo.wav')
        self.assertFalse(meta['peak_guard_applied'])
        self.assertEqual(meta['gain_db'], 0.0)

        # Loud aligned stems force the explicit linear peak guard.
        loud = self.temp / 'loud'
        loud.mkdir()
        stems = []
        for index in range(2):
            path = loud / f'{index}.wav'
            sf.write(path, np.ones((4410, 2), dtype=np.float32) * 0.6, 44100)
            stems.append({'id': f'l{index}', 'file': str(path.name)})
        plan = {'slices': [{'id': 'l0', 'track': 'l0'}, {'id': 'l1', 'track': 'l1'}]}
        (loud / 'run_manifest.json').write_text(json.dumps({'tracks': stems}))
        meta = build_solo(loud, plan, loud / 'solo.wav')
        self.assertTrue(meta['peak_guard_applied'])
        self.assertAlmostEqual(meta['gain_linear'], 0.99 / 1.2, delta=1e-4)
        audio, _ = sf.read(loud / 'solo.wav', always_2d=True)
        self.assertLessEqual(float(np.max(np.abs(audio))), 0.99 + 1e-6)

    def test_level_for_match_peak_guard_boundary(self):
        gain, guarded = level_for_match(0.1, 0.99, 0.5)
        self.assertTrue(guarded)
        self.assertAlmostEqual(gain, 1.0)
        gain, guarded = level_for_match(0.4, 0.5, 0.2)
        self.assertFalse(guarded)
        self.assertAlmostEqual(gain, 0.5)
        gain, guarded = level_for_match(0.0, 0.0, 0.3)
        self.assertFalse(guarded)
        self.assertEqual(gain, 1.0)

    # -- MIDI pad mapping ----------------------------------------------------

    def test_phrase_midi_tempo_ticks_notes_and_end(self):
        plan = self.plan()
        midi_path = self.temp / 'phrase-chops.mid'
        info = write_phrase_midi(plan, midi_path)
        self.assertEqual(info['ticks_per_beat'], 960)
        self.assertEqual(info['tempo_us_per_beat'], bpm2tempo(BPM))
        mid = MidiFile(midi_path)
        self.assertEqual(mid.ticks_per_beat, 960)
        tick = 0
        on_events, last_abs = {}, 0
        end_tick = None
        for message in mid.tracks[0]:
            tick += message.time
            if message.type == 'set_tempo':
                self.assertEqual(message.tempo, bpm2tempo(BPM))
            if message.type == 'note_on' and message.velocity > 0:
                self.assertEqual(message.channel, 9)
                self.assertTrue(36 <= message.note <= 51)
                on_events[message.note] = tick
            elif message.type == 'note_off':
                start = on_events.pop(message.note)
                sliced = next(s for s in plan['slices'] if s['midi_note'] == message.note)
                placement = next(p for p in plan['placements'] if p['slice'] == sliced['id'])
                self.assertEqual(start, round(placement['beat'] * 960))
                self.assertEqual(tick - start, round(placement['duration_beats'] * 960))
                last_abs = max(last_abs, tick)
            elif message.type == 'end_of_track':
                end_tick = tick
        self.assertEqual(len(on_events), 0)
        self.assertEqual(info['notes'], len(plan['placements']))
        self.assertEqual(end_tick, BARS * 4 * 960)
        self.assertGreaterEqual(end_tick, last_abs)

    def test_collect_samples_pad_shape_and_hashes(self):
        rack_dir = self.temp / 'rack'
        pads = collect_samples(self.plan(), rack_dir)
        self.assertEqual(len(pads), len(self.fixture.slices))
        for pad in pads:
            self.assertEqual(set(pad), {'midi', 'name', 'choke', 'file', 'slice'})
            self.assertEqual(pad['choke'], 1)
            path = rack_dir / pad['file']
            sliced = next(s for s in self.fixture.slices if s['id'] == pad['slice'])
            self.assertEqual(sha256(path), sliced['sha256'])
        self.assertEqual(sorted(p['midi'] for p in pads), list(range(36, 36 + len(pads))))

    # -- Sample rate conversion ---------------------------------------------

    def test_read_stereo_mono_48k_to_stereo_44100(self):
        path = self.fixture.recordings[0]['file']
        audio = read_stereo(path)
        self.assertEqual(audio.shape[1], 2)
        expected = round(sf.info(path).frames * 44100 / 48000)
        self.assertAlmostEqual(len(audio), expected, delta=2)
        self.assertTrue(np.isfinite(audio).all())

    # -- Source immutability -------------------------------------------------

    def test_inputs_unchanged_by_helper_apis(self):
        before = self.fixture.snapshot()
        build_dir = self.temp / 'build'
        plan = load_flip(self.song)[3]
        build_listening(self.song, plan, build_dir / 'Listening')
        rack_dir = build_dir / 'ChopRack'
        pads = collect_samples(plan, rack_dir)
        write_phrase_midi(plan, rack_dir / 'phrase-chops.mid')
        after = self.fixture.snapshot()
        self.assertEqual(before, after)
        self.assertEqual(len(pads), len(self.fixture.slices))

    # -- Validation boundary tests ------------------------------------------

    def _expect_invalid(self, mutator, fragment=''):
        import copy
        score = copy.deepcopy(self.score)
        manifest = copy.deepcopy(self.manifest)
        mutator(score, manifest)
        with self.assertRaises(ValueError) as context:
            validate_flip(score, manifest)
        if fragment:
            self.assertIn(fragment, str(context.exception))

    def test_invalid_source_hash(self):
        def mutate(score, manifest):
            score['sample_flip']['slices'][0]['source_sha256'] = '0' * 64
        self._expect_invalid(mutate, 'hash mismatch')

    def test_invalid_source_window(self):
        def mutate(score, manifest):
            sliced = score['sample_flip']['slices'][0]
            sliced['end_seconds'] = 99.0
        self._expect_invalid(mutate, 'window outside')

    def test_missing_source_file(self):
        def mutate(score, manifest):
            score['sample_flip']['slices'][0]['source_file'] = str(self.temp / 'nope.wav')
        self._expect_invalid(mutate, 'existing absolute path')

    def test_invalid_processed_hash(self):
        def mutate(score, manifest):
            score['sample_flip']['slices'][0]['sha256'] = 'f' * 64
        self._expect_invalid(mutate, 'processed cut hash mismatch')

    def test_missing_processed_file(self):
        def mutate(score, manifest):
            score['sample_flip']['slices'][0]['file'] = str(self.temp / 'gone.wav')
        self._expect_invalid(mutate)

    def test_duplicate_slice_id(self):
        def mutate(score, manifest):
            score['sample_flip']['slices'][1]['id'] = 'lead1'
        self._expect_invalid(mutate, 'Duplicate slice id')

    def test_duplicate_pad_mapping(self):
        def mutate(score, manifest):
            score['sample_flip']['slices'][1]['midi_note'] = 36
        self._expect_invalid(mutate, 'duplicate pad mapping')

    def test_pad_note_boundaries(self):
        def low(score, manifest):
            score['sample_flip']['slices'][0]['midi_note'] = 35

        def high(score, manifest):
            score['sample_flip']['slices'][0]['midi_note'] = 52
        self._expect_invalid(low, '36 to 51')
        self._expect_invalid(high, '36 to 51')

    def test_role_and_target_beats(self):
        def bad_role(score, manifest):
            score['sample_flip']['slices'][0]['role'] = 'bridge'

        def bad_beats(score, manifest):
            score['sample_flip']['slices'][0]['target_beats'] = 3.0
        self._expect_invalid(bad_role, 'lead, answer or turnaround')
        self._expect_invalid(bad_beats, 'must target')

    def test_role_counts(self):
        def mutate(score, manifest):
            flip = score['sample_flip']
            flip['slices'] = [s for s in flip['slices'] if s['id'] != 'lead4']
            flip['placements'] = [p for p in flip['placements'] if p['slice'] != 'lead4']
        self._expect_invalid(mutate, 'at least 4 lead')

    def test_unknown_placement_slice(self):
        def mutate(score, manifest):
            score['sample_flip']['placements'][0]['slice'] = 'ghost'
        self._expect_invalid(mutate, 'Unknown placement slice')

    def test_placement_outside_score(self):
        def mutate(score, manifest):
            score['sample_flip']['placements'][0]['beat'] = 79.5
        self._expect_invalid(mutate, 'outside the score')

    def test_placement_without_note60_event(self):
        def mutate(score, manifest):
            score['tracks'][0]['events'] = []
        self._expect_invalid(mutate, 'no note-60 stem event')

    def test_stem_sample_must_be_processed_cut(self):
        def mutate(score, manifest):
            score['tracks'][0]['sample'] = 'kick.wav'
        self._expect_invalid(mutate, 'not the processed cut')

    def test_source_id_missing_from_records(self):
        def mutate(score, manifest):
            referenced = score['sample_flip']['slices'][0]['source_id']
            score['sample_flip']['source_records'] = [
                r for r in score['sample_flip']['source_records']
                if r['id'] != referenced]
        self._expect_invalid(mutate, 'missing from source_records')

    def test_real_digit_leading_ids_copy(self):
        plan = self.plan()
        for row in plan['slices']:
            row['source_id'] = '9' + row['source_id']
        copied = d.copy_sources(plan, self.temp / 'sources')
        self.assertEqual(len(copied), 3)

    def test_stale_rendered_cut_rejected(self):
        self.manifest['tracks'][0]['source_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'rendered source'):
            self.plan()

    def test_placement_multiset_matches_all_stem_events(self):
        import copy
        original = copy.deepcopy(self.score)
        for mutation in ('omit', 'duplicate', 'velocity'):
            with self.subTest(mutation=mutation):
                self.score = copy.deepcopy(original)
                placements = self.score['sample_flip']['placements']
                if mutation == 'omit': placements.pop()
                elif mutation == 'duplicate': placements.append(dict(placements[0]))
                else: placements[0]['velocity'] = 0.1
                with self.assertRaisesRegex(ValueError, 'placement/event'):
                    self.plan()

    def test_source_record_identity_conflict_rejected(self):
        import copy
        original = copy.deepcopy(self.score)
        for duplicate in (False, True):
            self.score = copy.deepcopy(original)
            records = self.score['sample_flip']['source_records']
            conflicting = dict(records[1], id=records[0]['id'])
            if duplicate: records.append(conflicting)
            else: records[0] = conflicting
            with self.assertRaisesRegex(ValueError, 'source identity'):
                self.plan()

    def test_retriggered_pad_preserves_two_events(self):
        from collections import Counter
        p = dict(self.score['sample_flip']['placements'][0], beat=20.0)
        self.score['sample_flip']['placements'].append(p)
        self.score['tracks'][0]['events'].append(dict(self.score['tracks'][0]['events'][0], beat=20.0))
        plan = self.plan()
        path = self.temp / 'retrigger.mid'
        write_phrase_midi(plan, path)
        tick = 0; found = []
        for msg in MidiFile(path).tracks[0]:
            tick += msg.time
            if msg.type == 'note_on' and msg.velocity:
                found.append((tick, msg.note, msg.velocity))
        by_id = {s['id']:s['midi_note'] for s in plan['slices']}
        expected = [(round(p['beat']*960), by_id[p['slice']], round(p['velocity']*127)) for p in plan['placements']]
        self.assertEqual(Counter(found), Counter(expected))

    # -- deliver() orchestration --------------------------------------------

    def test_deliver_refuses_nonempty_out(self):
        out = self.temp / 'delivery'
        out.mkdir()
        (out / 'occupied.txt').write_text('x')
        with self.assertRaises(FileExistsError):
            deliver(self.song, out)

    def test_load_flip_reads_rendered_song(self):
        song, score, manifest, plan = load_flip(self.song)
        self.assertEqual(song, self.song.resolve())
        self.assertEqual(len(plan['slices']), len(self.fixture.slices))

    @unittest.skipUnless(factory_templates_available(),
                         'Local Ableton factory templates not available; static-only test skipped')
    def test_deliver_end_to_end_static(self):
        before = self.fixture.snapshot()
        out = self.temp / 'delivery'
        result = deliver(self.song, out)
        self.assertTrue(out.is_dir())
        self.assertEqual(result['set_name'], 'Gold From Dust')

        als = out / 'AbletonProject' / 'Gold From Dust.als'
        self.assertTrue(als.is_file())
        self.assertEqual(result['ableton_project']['als'],
                         'AbletonProject/Gold From Dust.als')

        export = json.loads((out / 'AbletonProject/export_manifest.json').read_text())
        self.assertEqual(export['project'], str((out / 'AbletonProject').resolve()))
        self.assertEqual(export['als'], str(als.resolve()))

        # Static preset inspection: every FileRef is a collected chop sample.
        import gzip
        import xml.etree.ElementTree as ET
        preset = out / 'ChopRack' / 'Gold From Dust Record Chops.adg'
        self.assertTrue(preset.is_file())
        root = ET.fromstring(gzip.decompress(preset.read_bytes()))
        refs = root.findall('.//SampleRef/FileRef')
        self.assertEqual(len(refs), len(self.fixture.slices))
        for ref in refs:
            relative = ref.find('RelativePath').get('Value')
            self.assertTrue(relative.startswith('Samples/'))
            self.assertTrue((out / 'ChopRack' / relative).is_file())

        phrase = out / 'ChopRack' / 'phrase-chops.mid'
        self.assertTrue(phrase.is_file())

        # Every file listed in the delivery manifest exists with that hash.
        delivery = json.loads((out / 'delivery_manifest.json').read_text())
        for record in delivery['files']:
            path = out / record['path']
            self.assertTrue(path.is_file(), record['path'])
            self.assertEqual(sha256(path), record['sha256'])
            self.assertEqual(path.stat().st_size, record['bytes'])
        # The manifest itself is written last and is the one unhashed file.
        self.assertTrue((out / 'delivery_manifest.json').is_file())

        # Sources are hash-checked local copies of actual used recordings.
        copied_ids = {s['source_id'] for s in delivery['sources']}
        used_ids = {s['source_id'] for s in self.fixture.slices}
        self.assertEqual(copied_ids, used_ids)
        for record in delivery['sources']:
            self.assertEqual(sha256(out / record['file']), record['sha256'])

        self.assertTrue((out / 'source_provenance.json').is_file())
        readme = (out / 'README.md').read_text()
        self.assertIn('PENDING', readme)
        self.assertIn('not LUFS and not a blind test', readme)
        for item in delivery['qa']['not_verified']:
            self.assertIn(item, ('Live open', 'Live save', 'Live rerender versus reference',
                                 'preset import in Live', 'phrase-chops.mid playback in Live'))
        # Inputs, including the real rendered song, are untouched afterwards.
        self.assertEqual(before, self.fixture.snapshot())


if __name__ == '__main__':
    unittest.main()
