"""Tests for the feedback-driven v2 revision delivery.

Everything here runs against synthetic fixtures only: the render/export
pipeline, soundfile, ffmpeg and both score builders are replaced with
sys.modules stubs before ``examples.render_city_feedback`` is imported.  The
real ``examples.album_audition`` manifest validator is used so output is
checked against the actual server contract.  No network, media, installs or
server processes are involved.
"""
from copy import deepcopy
from pathlib import Path
import hashlib
import importlib
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock

import numpy as np

OLD_COLLECTION = 'city-afterimages-20260927'
NEW_COLLECTION = 'city-afterimages-20261013-v2'
RAW_LUFS = [-17.0, -19.0, -21.0, -18.5, -20.5, -16.0, -22.0, -18.0, -19.5, -23.2]
CHINESE_DIRECTIONS = [f'v1 方向 {n}' for n in range(1, 11)]
# Revised fake renders genuinely differ from v1 (0.2 s of additional audio);
# the offset never collides with an adjacent number's v1 frame count.
V2_FRAME_OFFSET = 8820


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def frames_for(n):
    return int((46.0 + 0.37 * n) * 44100)


class FakeWorld:
    """Shared mutable state for the sys.modules stubs."""

    def __init__(self):
        self.raw_lufs = {}          # full_mix path -> lufs
        self.frames_lufs = {}       # frame count -> raw lufs
        self.render_calls = 0
        self.export_calls = 0
        self.builder_calls = 0

    def reset_counters(self):
        self.render_calls = 0
        self.export_calls = 0
        self.builder_calls = 0


WORLD = FakeWorld()


# ---------------------------------------------------------------------------
# soundfile stub: deterministic tiny JSON "wav" headers, no native libs
# ---------------------------------------------------------------------------

def sf_write(target, y, sr, subtype=None, format=None, **kwargs):  # noqa: A002
    arr = np.asarray(y, dtype='float32')
    header = json.dumps({
        'fake_wav': True,
        'frames': int(arr.shape[0]),
        'sr': int(sr),
        'channels': int(arr.shape[1]) if arr.ndim == 2 else 1,
        'amp': float(arr.flat[0]) if arr.size else 0.0,
        'subtype': str(subtype),
    }, sort_keys=True).encode('utf-8')
    if hasattr(target, 'write'):
        target.write(header)
    else:
        Path(target).write_bytes(header)


def sf_read(path, dtype='float32', always_2d=False, **kwargs):
    if hasattr(path, 'read'):
        raw = path.read()
        label = '<wav-buffer>'
    else:
        raw = Path(path).read_bytes()
        label = path
    header = json.loads(raw)
    if not header.get('fake_wav'):
        raise ValueError(f'not a fake wav: {label}')
    channels = int(header.get('channels', 2))
    y = np.full((header['frames'], channels), float(header['amp']), dtype='float32')
    return y, int(header['sr'])


sf_stub = types.ModuleType('soundfile')
sf_stub.write = sf_write
sf_stub.read = sf_read


def sf_info(path):
    header = json.loads(Path(path).read_bytes())
    info = types.SimpleNamespace(samplerate=header['sr'], frames=header['frames'])
    return info


sf_stub.info = sf_info


# ---------------------------------------------------------------------------
# pipeline / examples stubs
# ---------------------------------------------------------------------------

def _make_score(number, revision=False):
    bpm = 80 + number
    base = {
        'bpm': bpm,
        'bars': 16,
        'sample_root': '/tmp/synthetic-sample-root',
        'sample_flip': {
            'slices': [{'id': 'chop1', 'target_beats': 2.0,
                        'source_file': '/tmp/src.wav', 'file': '/tmp/cut.wav',
                        'start_seconds': 0.0, 'end_seconds': 1.0,
                        'sha256': 'a' * 64, 'source_sha256': 'b' * 64}],
            'placements': [{'slice_id': 'chop1', 'beat': 0.0,
                            'duration_beats': 2.0, 'note': 60, 'velocity': 0.7}],
            'source_records': [{'id': f'src-{number}', 'title': f'老唱片 {number}',
                                'source_url': f'local://sources/{number}.wav'}],
        },
        'tracks': [
            {'id': 'chops', 'sample': 'cuts/chop1.wav',
             'events': [{'beat': 0.0, 'duration_beats': 2.0, 'note': 60,
                         'velocity': 0.7}]},
            {'id': 'keys' if not revision else 'bass', 'sample': None,
             'events': [{'beat': 4.0 if not revision else 6.0,
                         'duration_beats': 1.0, 'note': 60, 'velocity': 0.6}]},
        ],
        'metadata': {'middle_switch': {'beat': 32.0}},
    }
    if revision:
        base['metadata']['revision_summary'] = (
            f'第{number}首：根据反馈收紧鼓组律动并突出采样记忆点')
    return base


def fake_build_score(prepared):
    WORLD.builder_calls += 1
    number = int(prepared['number'])
    if not prepared['run_id'].endswith('-v2-20261013'):
        raise AssertionError('builder received the prepared clone with the new run id')
    return _make_score(number, revision=True)


def fake_render_score(score, song):
    WORLD.render_calls += 1
    song = Path(song)
    song.mkdir(parents=True, exist_ok=True)
    number = int(score['bpm']) - 80
    revision = bool((score.get('metadata') or {}).get('revision_summary'))
    frames = frames_for(number) + (V2_FRAME_OFFSET if revision else 0)
    y = np.full((frames, 2), 0.1, dtype='float32')
    sf_write(song / 'full_mix.wav', y, 44100)
    (song / 'score.json').write_text(
        json.dumps(score, ensure_ascii=False, sort_keys=True), encoding='utf-8')
    manifest = {'sample_rate': 44100,
                'tracks': [{'id': t['id'], 'source_sha256': 'a' * 64}
                           for t in score['tracks']]}
    (song / 'run_manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    WORLD.raw_lufs[str(song / 'full_mix.wav')] = RAW_LUFS[number - 1]
    WORLD.frames_lufs[frames] = RAW_LUFS[number - 1]


def fake_validate_song(song):
    song = Path(song)
    for rel in ('run_manifest.json', 'score.json', 'full_mix.wav'):
        if not (song / rel).is_file():
            raise RuntimeError(f'song 不完整: {song / rel}')
    manifest = json.loads((song / 'run_manifest.json').read_text())
    return {'ok': True, 'sample_rate': manifest['sample_rate'],
            'stems': len(manifest['tracks'])}


def fake_export_song(song, target, set_name):
    WORLD.export_calls += 1
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    (target / 'project.als').write_bytes(b'ALS::' + str(set_name).encode('utf-8'))
    manifest = json.loads((Path(song) / 'run_manifest.json').read_text())
    stems = target / 'Stems'
    stems.mkdir(exist_ok=True)
    for track in manifest['tracks']:
        (stems / f"{track['id']}.wav").write_bytes(b'STEM::' + track['id'].encode())
    return {'als': 'AbletonProject/project.als', 'set_name': str(set_name),
            'stems': len(manifest['tracks'])}


def fake_plan_for(score, manifest):
    return {
        'bars': score['bars'], 'bpm': score['bpm'],
        'sample_rate': manifest['sample_rate'],
        'slices': deepcopy(score['sample_flip']['slices']),
        'placements': deepcopy(score['sample_flip']['placements']),
        'source_records': deepcopy(score['sample_flip']['source_records']),
    }


def fake_loudness(path):
    header = json.loads(Path(path).read_bytes())
    key = str(Path(path))
    if key in WORLD.raw_lufs:
        return {'lufs': WORLD.raw_lufs[key], 'true_peak_db': -2.5}
    # gained preview: amp == 0.1 * 10**(gain_db/20)
    raw = WORLD.frames_lufs[header['frames']]
    gain_db = 20.0 * math.log10(header['amp'] / 0.1)
    return {'lufs': raw + gain_db, 'true_peak_db': -1.5}


def fake_collect_samples(plan, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pads = []
    for sl in plan['slices']:
        path = out_dir / f"{sl['id']}.wav"
        path.write_bytes(b'PAD::' + sl['id'].encode())
        pads.append({'id': sl['id'], 'file': str(path)})
    return pads


def fake_write_phrase_midi(plan, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'MIDI::phrase-chops')


def fake_build_rack(pads, in_dir, out_dir, preset_name='rack'):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'drum-rack.adg').write_bytes(b'RACK::' + str(preset_name).encode())


def fake_build_listening(song, plan, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'LISTENING.md').write_text('对照试听占位文件\n', encoding='utf-8')
    return []


def fake_copy_sources(plan, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    records = plan['source_records']
    (out_dir / 'sources.json').write_text(
        json.dumps(records, ensure_ascii=False), encoding='utf-8')
    return [{'id': r['id'], 'title': r['title']} for r in records]


def install_stubs():
    """Register synthetic dependency modules; returns list of prior entries."""
    prior = {}
    modules = {
        'soundfile': sf_stub,
        'pipeline.song': types.ModuleType('pipeline.song'),
        'pipeline.ableton_export': types.ModuleType('pipeline.ableton_export'),
        'examples.render_city_afterimages': types.ModuleType(
            'examples.render_city_afterimages'),
        'examples.real_record_delivery': types.ModuleType(
            'examples.real_record_delivery'),
        'examples.city_feedback_scores': types.ModuleType(
            'examples.city_feedback_scores'),
    }
    modules['pipeline.song'].render_score = fake_render_score
    modules['pipeline.ableton_export'].validate_song = fake_validate_song
    modules['pipeline.ableton_export'].export_song = fake_export_song
    after = modules['examples.render_city_afterimages']
    after.plan_for = fake_plan_for
    after.loudness = fake_loudness
    after.COLLECTION = OLD_COLLECTION
    after.CHINESE_DIRECTIONS = CHINESE_DIRECTIONS
    delivery = modules['examples.real_record_delivery']
    delivery.sha256 = sha256
    delivery.collect_samples = fake_collect_samples
    delivery.write_phrase_midi = fake_write_phrase_midi
    delivery.build_rack = fake_build_rack
    delivery.build_listening = fake_build_listening
    delivery.copy_sources = fake_copy_sources
    modules['examples.city_feedback_scores'].build_score = fake_build_score
    for name, module in modules.items():
        prior[name] = sys.modules.get(name)
        sys.modules[name] = module
    return prior


def restore_stubs(prior):
    for name, old in prior.items():
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old


# ---------------------------------------------------------------------------
# Synthetic v1 world (already delivered + reviewed)
# ---------------------------------------------------------------------------

def prepared_payload(root, number):
    return {
        'number': number,
        'run_id': f'city-afterimages-{number:02d}-v1',
        'title': f'城市测试第{number}首',
        'bpm': 80 + number,
        'bars': 16,
        'instrument_sources': {'keys': {'plugin': 'SyntheticKeys'}},
        'source_records': [{'id': f'src-{number}', 'title': f'老唱片 {number}',
                            'source_url': f'local://sources/{number}.wav'}],
    }


def build_world(root):
    """Create a frozen v1 delivery matching the v1 renderer on-disk layout."""
    root = Path(root)
    library = root / 'library' / 'instruments' / OLD_COLLECTION
    library.mkdir(parents=True)
    index_paths = []
    old_tracks = []
    old_evidence = []
    old_dir = root / 'exports' / OLD_COLLECTION
    (old_dir / 'audio').mkdir(parents=True)

    for number in range(1, 11):
        run_id = f'city-afterimages-{number:02d}-v1'
        prepared_path = library / f'prepared-{number:02d}.json'
        prepared = prepared_payload(root, number)
        prepared_path.write_text(
            json.dumps(prepared, ensure_ascii=False, indent=2), encoding='utf-8')
        index_paths.append(str(prepared_path))

        song = root / 'beats' / run_id
        score = _make_score(number)
        fake_render_score(score, song)
        manifest = json.loads((song / 'run_manifest.json').read_text())
        project = old_dir / 'Projects' / run_id
        plan = fake_plan_for(score, manifest)
        export_meta = fake_export_song(song, project / 'AbletonProject',
                                       set_name=prepared['title'])
        pads = fake_collect_samples(plan, project / 'ChopRack')
        fake_write_phrase_midi(plan, project / 'ChopRack/phrase-chops.mid')
        fake_build_rack(pads, project / 'ChopRack', project / 'ChopRack',
                        preset_name=prepared['title'] + ' Chops')
        fake_build_listening(song, plan, project / 'Listening')
        sources = fake_copy_sources(plan, project / 'Sources')
        (project / 'source_map.json').write_text(json.dumps(plan), encoding='utf-8')
        (project / 'prepared.json').write_text(
            json.dumps(prepared, ensure_ascii=False), encoding='utf-8')
        qa = fake_validate_song(song)
        files = [
            dict(path=str(p.relative_to(project)), sha256=sha256(p),
                 bytes=p.stat().st_size)
            for p in sorted(project.rglob('*')) if p.is_file()
        ]
        (project / 'delivery_manifest.json').write_text(json.dumps(
            dict(song=str(song), qa=qa, files=files, sources=sources,
                 export=export_meta, keep=None, publication='not published'),
            ensure_ascii=False, indent=2), encoding='utf-8')

        levels = fake_loudness(song / 'full_mix.wav')
        wav_rel = f'audio/{run_id}.wav'
        mp3_rel = f'audio/{run_id}.mp3'
        (old_dir / wav_rel).write_bytes(b'OLD-PREVIEW-WAV::' + run_id.encode())
        (old_dir / mp3_rel).write_bytes(b'OLD-PREVIEW-MP3::' + run_id.encode())
        duration = frames_for(number) / 44100
        old_tracks.append(dict(
            id=run_id, number=number, title=prepared['title'], bpm=prepared['bpm'],
            duration_seconds=duration, direction=CHINESE_DIRECTIONS[number - 1],
            audio=mp3_rel, original_audio=wav_rel,
            source_summary=[{'title': '老唱片 %d' % number,
                             'source_url': f'local://sources/{number}.wav'}],
            editable_path=str(project), version='v1',
            mix_sha256=sha256(song / 'full_mix.wav'),
            change_point_seconds=32.0 * 60.0 / prepared['bpm'],
            listening_gain_db=-2.0, listening_lufs=-20.0,
            matched_audio_sha256=sha256(old_dir / wav_rel), keep=None))
        # Exact real v1 render-evidence shape: list of objects with only
        # song/project/qa/levels/mix_sha256/score_sha256 (no id/number field).
        old_evidence.append(dict(
            song=str(song), project=str(project), qa=qa, levels=levels,
            mix_sha256=sha256(song / 'full_mix.wav'),
            score_sha256=sha256(song / 'score.json')))

    (library / 'prepared-index.json').write_text(
        json.dumps(index_paths, ensure_ascii=False), encoding='utf-8')
    (old_dir / 'collection.json').write_text(json.dumps(dict(
        id=OLD_COLLECTION, title='城市余像 · City Afterimages',
        subtitle='v1 frozen', tracks=old_tracks,
        purpose='human style audition before targeted revisions',
        loudness_target_lufs=-20.0, keep=None), ensure_ascii=False, indent=2),
        encoding='utf-8')
    (old_dir / 'render-evidence.json').write_text(
        json.dumps(old_evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    # Private human feedback from the v1 round; must stay frozen and unread.
    (old_dir / 'feedback.json').write_text(json.dumps({
        'collection_id': OLD_COLLECTION,
        'ratings': {old_tracks[0]['id']: {'keep': 'revise', 'notes': '鼓太满'}}},
        ensure_ascii=False), encoding='utf-8')
    return root


def snapshot(root, protected):
    out = {}
    base = Path(root)
    for rel in protected:
        folder = base / rel
        if not folder.exists():
            continue
        for path in folder.rglob('*'):
            if path.is_file():
                out[str(path.relative_to(base))] = (sha256(path), path.stat().st_size)
    return out


def protected_rels():
    return [Path('library') / 'instruments' / OLD_COLLECTION,
            Path('exports') / OLD_COLLECTION,
            Path('beats')]


def _synthetic_build_media_allowlist(manifest):
    """Faithful fallback for staged album_audition copies without the server helper."""
    allow = {}
    for track in manifest['tracks']:
        for key in ('audio', 'original_audio'):
            rel = track[key]
            if rel in allow:
                raise AssertionError(f'媒体路径被两个曲目同时声明: {rel}')
            allow[rel] = track['id']
    return allow


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class FeedbackDeliveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._prior = install_stubs()
        sys.modules.pop('examples.render_city_feedback', None)
        cls.rcf = importlib.import_module('examples.render_city_feedback')
        # The real, stdlib-only manifest validator must accept the output.
        from examples import album_audition
        cls.album_audition = album_audition

    @classmethod
    def tearDownClass(cls):
        restore_stubs(cls._prior)
        sys.modules.pop('examples.render_city_feedback', None)

    def setUp(self):
        # Loudness maps must be empty *before* building the frozen v1 world
        # (build_world renders ten v1 songs and registers their levels)...
        WORLD.raw_lufs.clear()
        WORLD.frames_lufs.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = build_world(self._tmp.name)
        # ...while call counters start at zero *after* that fixture build, so
        # counts reflect only the run under test and never leak across cases.
        WORLD.reset_counters()
        self.new_dir = self.root / 'exports' / NEW_COLLECTION

    def _sidecar(self):
        path = self.new_dir / 'audio' / self.rcf.PREVIEW_EVIDENCE_FILENAME
        return path, json.loads(path.read_text(encoding='utf-8'))

    def _run(self):
        return self.rcf.run(self.root)

    def _patch_ffmpeg(self):
        def fake_ffmpeg(argv, **kwargs):
            if not kwargs.get('check', False):
                raise AssertionError('ffmpeg 调用必须 check=True')
            inp = argv[argv.index('-i') + 1]
            if not Path(inp).is_file():
                raise FileNotFoundError(inp)
            blob = Path(inp).read_bytes()
            # Decode-to-stdout mode used by the resume verification: only the
            # fake MP3 container produced below decodes; garbage fails loudly.
            if argv[-1] == '-' and kwargs.get('stdout') == subprocess.PIPE:
                if not blob.startswith(b'MP3::'):
                    raise subprocess.CalledProcessError(1, argv, stderr=b'fake decode failure')
                return types.SimpleNamespace(returncode=0, stdout=blob[len(b'MP3::'):])
            outp = argv[-1]
            Path(outp).write_bytes(b'MP3::' + blob)
            return types.SimpleNamespace(returncode=0)
        return unittest.mock.patch.object(self.rcf.subprocess, 'run', fake_ffmpeg)


class DeliveryEndToEnd(FeedbackDeliveryTest):

    def test_mapping_version_identity_and_manifest_contract(self):
        before = snapshot(self.root, protected_rels())
        original = types.ModuleType('examples.ten_beat_scores')
        original.build_score = unittest.mock.Mock(
            side_effect=AssertionError('unexpected v1 builder fallback'))
        with self._patch_ffmpeg(), unittest.mock.patch.dict(
                sys.modules, {'examples.ten_beat_scores': original}):
            result = self._run()
        original.build_score.assert_not_called()
        self.assertEqual(Path(result).resolve(), self.new_dir.resolve())

        manifest = self.album_audition.load_manifest(self.new_dir)
        tracks = manifest['tracks']
        self.assertEqual(len(tracks), 20)
        self.assertEqual(manifest['purpose'], 'original-vs-revision audition')
        self.assertIsNone(manifest['keep'])
        self.assertEqual(manifest['id'], NEW_COLLECTION)

        ids = [t['id'] for t in tracks]
        self.assertEqual(len(set(ids)), 20)  # unique ids
        old_manifest = json.loads(
            (self.root / 'exports' / OLD_COLLECTION / 'collection.json').read_text())
        old_by_id = {t['id']: t for t in old_manifest['tracks']}

        for i in range(10):
            v1, v2 = tracks[2 * i], tracks[2 * i + 1]
            number = i + 1
            old_id = f'city-afterimages-{number:02d}-v1'
            new_id = f'city-afterimages-{number:02d}-v2-20261013'
            self.assertEqual(v1['id'], old_id)
            self.assertEqual(v2['id'], new_id)
            self.assertEqual(v1['number'], number)
            self.assertEqual(v2['number'], number)  # same number, adjacent pair
            self.assertEqual(v1['version'], 'v1')
            self.assertEqual(v2['version'], 'v2')
            self.assertIn('v1', v1['title'])
            self.assertIn('v2', v2['title'])
            self.assertIn('v1', v1['direction'])
            self.assertIn('修订', v2['direction'])
            # exact parent identity binding
            self.assertEqual(v1['mix_sha256'], old_by_id[old_id]['mix_sha256'])
            self.assertNotEqual(v2['mix_sha256'], v1['mix_sha256'])
            # preview is a distinct gained artifact, never the master
            self.assertNotEqual(v1['mix_sha256'], v1['matched_audio_sha256'])
            self.assertNotEqual(v2['mix_sha256'], v2['matched_audio_sha256'])
            for key in ('audio', 'original_audio'):
                rel = v1[key]
                self.assertFalse(rel.startswith('/'))
                self.assertNotIn('..', Path(rel).parts)
                self.assertTrue((self.new_dir / rel).is_file())
                self.assertTrue((self.new_dir / v2[key]).is_file())
            # editable packages: old one untouched, new one real
            self.assertEqual(Path(v1['editable_path']).resolve(),
                             (self.root / 'exports' / OLD_COLLECTION / 'Projects'
                              / old_id).resolve())
            self.assertEqual(Path(v2['editable_path']).resolve(),
                             (self.new_dir / 'Projects' / new_id).resolve())
            self.assertTrue(Path(v2['editable_path']).is_dir())
            for t in (v1, v2):
                self.assertIsNone(t['keep'])
                self.assertNotIn('ratings', t)

        build_allowlist = getattr(
            self.album_audition, 'build_media_allowlist',
            _synthetic_build_media_allowlist)
        allow = build_allowlist(manifest)
        self.assertEqual(len(allow), 40)
        for rel, owner in allow.items():
            self.assertTrue((self.new_dir / rel).is_file())
            self.assertIn(owner, ids)

        # loudness: single linear gain per track, shared target across all 20
        target = min(-20.0, min(RAW_LUFS))
        self.assertAlmostEqual(manifest['loudness_target_lufs'], target)
        for t in tracks:
            self.assertLessEqual(t['listening_gain_db'], 0.0)
            self.assertLessEqual(abs(t['listening_lufs'] - target), 0.15)

        # revision builder was used exactly ten times, v1 builder never
        self.assertEqual(WORLD.builder_calls, 10)
        self.assertEqual(WORLD.render_calls, 10)
        self.assertEqual(WORLD.export_calls, 10)

        # durable preview receipts exist for all 20 previews
        _, sidecar = self._sidecar()
        self.assertEqual(set(sidecar['previews']), set(ids))
        for rid in ids:
            receipt = sidecar['previews'][rid]
            self.assertEqual(receipt['sample_rate'], 44100)
            self.assertEqual(receipt['channels'], 2)
            self.assertEqual(receipt['wav_sha256'],
                             sha256(self.new_dir / 'audio' / (rid + '.wav')))
            self.assertEqual(receipt['mp3_sha256'],
                             sha256(self.new_dir / 'audio' / (rid + '.mp3')))

        # no feedback machinery in the new collection
        self.assertFalse((self.new_dir / 'feedback.json').exists())

        after = snapshot(self.root, protected_rels())
        # v2 songs live under beats/ too, so exclude all new v2 run directories

        def frozen_only(item):
            return not any(
                part.startswith('city-afterimages-') and part.endswith('-v2-20261013')
                for part in Path(item).parts)

        before_v1 = {k: v for k, v in before.items() if frozen_only(k)}
        after_v1 = {k: v for k, v in after.items() if frozen_only(k)}
        self.assertEqual(before_v1, after_v1)

    def test_render_evidence_binds_parent_and_artifacts(self):
        with self._patch_ffmpeg():
            self._run()
        # The frozen v1 evidence must keep the real production shape.
        old_evidence = json.loads(
            (self.root / 'exports' / OLD_COLLECTION / 'render-evidence.json')
            .read_text())
        self.assertEqual(len(old_evidence), 10)
        for entry in old_evidence:
            self.assertEqual(
                set(entry),
                {'song', 'project', 'qa', 'levels', 'mix_sha256', 'score_sha256'})
            self.assertTrue(Path(entry['song']).is_absolute())
            self.assertTrue(Path(entry['project']).is_absolute())

        evidence = json.loads((self.new_dir / 'render-evidence.json').read_text())
        self.assertEqual(len(evidence), 20)
        v1_by_id = {e['id']: e for e in evidence if e['version'] == 'v1'}
        v2_by_id = {e['id']: e for e in evidence if e['version'] == 'v2'}
        self.assertEqual(len(v1_by_id), 10)
        self.assertEqual(len(v2_by_id), 10)
        for number in range(1, 11):
            old_id = f'city-afterimages-{number:02d}-v1'
            new_id = f'city-afterimages-{number:02d}-v2-20261013'
            v1, v2 = v1_by_id[old_id], v2_by_id[new_id]
            self.assertIn('lufs', v1['levels'])
            self.assertIn('true_peak_db', v1['levels'])
            self.assertTrue(v1['artifacts'])
            self.assertIsNone(v1['parent'])
            self.assertEqual(v2['parent'], {
                'collection': OLD_COLLECTION, 'run_id': old_id,
                'mix_sha256': v1['mix_sha256'],
                'score_sha256': v1['score_sha256']})
            self.assertNotEqual(v2['score_sha256'], v1['score_sha256'])
            self.assertTrue(v2['artifacts'])
            self.assertTrue(v2['qa'])
            # The exact concise Chinese change summary the synthetic builder
            # produced must be carried through verbatim (no fabricated text).
            self.assertEqual(
                v2['revision_summary'],
                f'第{number}首：根据反馈收紧鼓组律动并突出采样记忆点')
            self.assertEqual(v2['preview']['matched_audio_sha256'],
                             sha256(self.new_dir / 'audio' / (new_id + '.wav')))
            self.assertEqual(v2['preview']['mp3_sha256'],
                             sha256(self.new_dir / 'audio' / (new_id + '.mp3')))

    def test_v2_package_manifest_binds_parent_and_keeps_null(self):
        with self._patch_ffmpeg():
            self._run()
        new_id = 'city-afterimages-01-v2-20261013'
        out = self.new_dir / 'Projects' / new_id
        delivery = json.loads((out / 'delivery_manifest.json').read_text())
        self.assertIsNone(delivery['keep'])
        self.assertEqual(delivery['publication'], 'not published')
        self.assertEqual(delivery['revision'], 'v2')
        parent_song = self.root / 'beats' / 'city-afterimages-01-v1'
        self.assertEqual(delivery['parent'], {
            'collection': OLD_COLLECTION, 'run_id': 'city-afterimages-01-v1',
            'mix_sha256': sha256(parent_song / 'full_mix.wav'),
            'score_sha256': sha256(parent_song / 'score.json')})
        # same artifact kinds as v1 package
        self.assertTrue((out / 'AbletonProject' / 'project.als').is_file())
        self.assertTrue((out / 'ChopRack' / 'phrase-chops.mid').is_file())
        self.assertTrue((out / 'ChopRack' / 'drum-rack.adg').is_file())
        self.assertTrue((out / 'Listening').is_dir())
        self.assertTrue((out / 'Sources').is_dir())
        self.assertTrue((out / 'source_map.json').is_file())
        self.assertTrue((out / 'prepared.json').is_file())
        # package prepared payload carries the new run id only
        packaged = json.loads((out / 'prepared.json').read_text())
        self.assertEqual(packaged['run_id'], new_id)

    def test_zero_gain_real_pcm_preview_may_match_master(self):
        # Real PCM regression (only runs when the real soundfile dependency is
        # provided): the quietest preview receives a 0 dB gain, and a PCM_24
        # master re-encoded as PCM_24 must be byte-identical. The delivery
        # must accept that equal SHA instead of declaring a false failure.
        saved = sys.modules.pop('soundfile', None)
        try:
            try:
                import soundfile as real_sf  # real, dependency-provided codec
            except Exception as exc:  # pragma: no cover - environment guard
                self.skipTest(f'real soundfile dependency not provided: {exc}')
        finally:
            if saved is not None:
                sys.modules['soundfile'] = saved

        sr = 44100
        frames = sr // 2  # short fixture; the helper has no duration rule
        t = np.arange(frames, dtype='float64') / sr
        wave = 0.2 * np.sin(2 * np.pi * 220.0 * t)
        master = np.stack([wave, wave * 0.9], axis=1).astype('float32')
        with tempfile.TemporaryDirectory() as td:
            master_path = Path(td) / 'master.wav'
            real_sf.write(master_path, master, sr,
                          format='WAV', subtype='PCM_24')
            master_sha = sha256(master_path)
            original, _ = real_sf.read(
                master_path, dtype='float32', always_2d=True)

            data, gained, out_sr = self.rcf.build_preview_wav_bytes(
                real_sf, np, master_path, 0.0)
            self.assertEqual(out_sr, sr)
            # zero gain reproduces the master byte-for-byte and is accepted
            self.assertEqual(self.rcf.sha256_bytes(data), master_sha)
            decoded, decoded_sr = real_sf.read(
                io.BytesIO(data), dtype='float32', always_2d=True)
            self.assertEqual(decoded_sr, sr)
            np.testing.assert_array_equal(decoded, original)
            np.testing.assert_allclose(gained, original, rtol=0, atol=2 ** -24)

            # a real attenuation must produce a distinct artifact
            quieter, _, _ = self.rcf.build_preview_wav_bytes(
                real_sf, np, master_path, -6.0)
            self.assertNotEqual(self.rcf.sha256_bytes(quieter), master_sha)


class ResumeAndSafety(FeedbackDeliveryTest):

    def test_completed_resume_verifies_every_artifact_hash(self):
        with self._patch_ffmpeg():
            self._run()
            first = (self.new_dir / 'collection.json').read_bytes()
            WORLD.reset_counters()
            self._run()  # resume
        self.assertEqual((self.new_dir / 'collection.json').read_bytes(), first)
        self.assertEqual(WORLD.render_calls, 0)
        self.assertEqual(WORLD.export_calls, 0)

        # tampered artifact after manifest completion must block resume
        target = self.new_dir / 'Projects' / 'city-afterimages-03-v2-20261013' \
            / 'AbletonProject' / 'project.als'
        target.write_bytes(target.read_bytes() + b'corrupt')
        with self._patch_ffmpeg(), self.assertRaises(ValueError):
            self._run()

    def test_missing_artifact_blocks_resume(self):
        with self._patch_ffmpeg():
            self._run()
        missing = self.new_dir / 'Projects' / 'city-afterimages-02-v2-20261013' \
            / 'Sources' / 'sources.json'
        missing.unlink()
        with self._patch_ffmpeg(), self.assertRaises(FileNotFoundError):
            self._run()

    def test_incomplete_package_dir_blocks_without_overwrite(self):
        out = self.new_dir / 'Projects' / 'city-afterimages-01-v2-20261013'
        out.mkdir(parents=True)
        marker = out / 'incomplete.txt'
        marker.write_text('partial export')
        with self._patch_ffmpeg(), self.assertRaises(FileExistsError):
            self._run()
        self.assertEqual(marker.read_text(), 'partial export')

    def test_resume_rejects_different_score(self):
        song = self.root / 'beats' / 'city-afterimages-01-v2-20261013'
        song.mkdir(parents=True)
        (song / 'run_manifest.json').write_text(json.dumps({'sample_rate': 44100}))
        (song / 'score.json').write_text(json.dumps({'different': True}))
        with self._patch_ffmpeg(), self.assertRaises(ValueError):
            self._run()

    def test_missing_full_mix_is_failure_not_false_success(self):
        with self._patch_ffmpeg():
            self._run()
        (self.root / 'beats' / 'city-afterimages-04-v2-20261013'
         / 'full_mix.wav').unlink()
        with self._patch_ffmpeg(), self.assertRaises(FileNotFoundError):
            self._run()

    def test_corrupt_preview_wav_blocks(self):
        with self._patch_ffmpeg():
            self._run()
        wav = self.new_dir / 'audio' / 'city-afterimages-05-v2-20261013.wav'
        wav.write_bytes(b'not a wav at all')
        with self._patch_ffmpeg(), self.assertRaises(ValueError):
            self._run()

    def test_tampered_resumed_mp3_blocks(self):
        with self._patch_ffmpeg():
            self._run()
        mp3 = self.new_dir / 'audio' / 'city-afterimages-03-v1.mp3'
        mp3.write_bytes(mp3.read_bytes() + b'tampered')
        with self._patch_ffmpeg(), self.assertRaises(ValueError):
            self._run()

    def test_undecodable_resumed_mp3_blocks_even_with_matching_receipt(self):
        # An MP3 whose SHA matches its receipt must STILL decode to finite,
        # nonempty audio of the recorded duration/sample rate/channels.
        rid = 'city-afterimages-04-v2-20261013'
        with self._patch_ffmpeg():
            self._run()
        mp3 = self.new_dir / 'audio' / (rid + '.mp3')
        mp3.write_bytes(b'completely undecodable garbage bytes')
        sidecar_path, sidecar = self._sidecar()
        sidecar['previews'][rid]['mp3_sha256'] = sha256(mp3)
        sidecar_path.write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2, sort_keys=True),
            encoding='utf-8')
        with self._patch_ffmpeg(), self.assertRaises(ValueError):
            self._run()

    def test_missing_preview_receipt_sidecar_blocks_resume(self):
        with self._patch_ffmpeg():
            self._run()
        sidecar_path, _ = self._sidecar()
        sidecar_path.unlink()
        with self._patch_ffmpeg(), self.assertRaises(ValueError):
            self._run()

    def test_missing_mp3_with_receipt_blocks_instead_of_reencoding(self):
        rid = 'city-afterimages-06-v1'
        with self._patch_ffmpeg():
            self._run()
        mp3 = self.new_dir / 'audio' / (rid + '.mp3')
        mp3.unlink()
        with self._patch_ffmpeg(), self.assertRaises(FileNotFoundError):
            self._run()

    def test_resume_decodes_and_reverifies_every_existing_mp3(self):
        # A completed run resumes with zero re-encodes and identical outputs;
        # the receipts plus an actual decode verify each MP3 on the way.
        with self._patch_ffmpeg():
            self._run()
            first_collection = (self.new_dir / 'collection.json').read_bytes()
            first_sidecar, _ = self._sidecar()
            first_sidecar_bytes = first_sidecar.read_bytes()
            WORLD.reset_counters()
            self._run()
        self.assertEqual((self.new_dir / 'collection.json').read_bytes(),
                         first_collection)
        self.assertEqual(first_sidecar.read_bytes(), first_sidecar_bytes)
        self.assertEqual(WORLD.render_calls, 0)
        self.assertEqual(WORLD.export_calls, 0)


class ParentAndBuilderGuards(FeedbackDeliveryTest):

    def test_parent_mix_hash_mismatch_rejected_before_render(self):
        parent_mix = self.root / 'beats' / 'city-afterimages-01-v1' / 'full_mix.wav'
        parent_mix.write_bytes(parent_mix.read_bytes() + b'X')
        with self._patch_ffmpeg(), self.assertRaises(ValueError):
            self._run()
        # no revision work landed on disk after the guard
        self.assertFalse((self.root / 'beats'
                          / 'city-afterimages-01-v2-20261013').exists())
        self.assertEqual(WORLD.render_calls, 0)

    def test_late_parent_corruption_fails_with_zero_renders(self):
        # Corrupting the LAST parent must fail just as hard as the first:
        # every parent is validated before any revision is rendered.
        late = self.root / 'beats' / 'city-afterimages-10-v1' / 'full_mix.wav'
        late.write_bytes(late.read_bytes() + b'late-corruption')
        with self._patch_ffmpeg(), self.assertRaises(ValueError):
            self._run()
        self.assertEqual(WORLD.render_calls, 0)
        self.assertEqual(WORLD.export_calls, 0)
        self.assertEqual(WORLD.builder_calls, 0)
        v2_songs = [p for p in (self.root / 'beats').iterdir()
                    if p.name.endswith('-v2-20261013')]
        self.assertEqual(v2_songs, [])
        self.assertFalse(self.new_dir.exists())

    def test_missing_builder_is_import_error_with_no_v1_fallback(self):
        # Behavior, not a source-text scan: when the dedicated revision
        # builder cannot be imported, run() must surface ImportError with no
        # fallback to the v1 builder (see also the end-to-end assertion that
        # the original builder is never called).
        class Blocker:
            def find_spec(self, name, path, target=None):
                if name == 'examples.city_feedback_scores':
                    raise ImportError('synthetic builder unavailable')
                return None

        blocker = Blocker()
        sys.meta_path.insert(0, blocker)
        sys.modules.pop('examples.city_feedback_scores', None)
        try:
            with self._patch_ffmpeg(), self.assertRaises(ImportError):
                self._run()
        finally:
            sys.meta_path.remove(blocker)
            install_stubs()  # restore the builder stub


class RunIdMapping(FeedbackDeliveryTest):
    def test_suffix_mapping(self):
        f = self.rcf.revise_run_id
        self.assertEqual(f('city-afterimages-07-v1'),
                         'city-afterimages-07-v2-20261013')

    def test_bad_suffix_rejected(self):
        f = self.rcf.revise_run_id
        for bad in ('city-afterimages-07-v2', 'city-afterimages-07', '-v1', '', None):
            with self.assertRaises(ValueError):
                f(bad)


if __name__ == '__main__':
    unittest.main()
