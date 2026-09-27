"""Package a rendered real-record sample-flip song for delivery.

Inputs are never modified: a song directory produced by pipeline.song.render_score
whose score carries ``sample_flip = {slices, placements, source_records}``. Every
slice is a cut of a real recording (pitch already baked in, played at note 60) and
also one song stem. The package contains:

* ``AbletonProject/Gold From Dust.als`` - audio-stem Set via the existing exporter;
* ``ChopRack/Gold From Dust Record Chops.adg`` + ``phrase-chops.mid`` - a playable
  pad rack (factory templates read locally, never vendored) and its performance;
* ``Listening/original-cut-mix.wav`` - raw recording window / processed cut / mixed
  excerpt with an honest linear RMS match (not LUFS, not a blind test);
* ``Listening/sample-flip-solo.wav`` - only the slice stems summed;
* ``Sources/`` - local copies of the collected source recording excerpts used;
* ``source_provenance.json`` and ``delivery_manifest.json``.

An actual Ableton Live open/save/rerender and preset import remain manual,
explicitly unverified steps. No listening result, Keep decision, or taste rating
is created: the user has not kept this draft.
"""
from __future__ import annotations

import argparse
from collections import Counter
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET

from mido import MidiFile, MidiTrack, Message, MetaMessage, bpm2tempo
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

try:  # Repository layout.
    from pipeline.ableton_export import validate_song, export_song
except ModuleNotFoundError as exc:  # Flat module directory layout used by other CLIs.
    if exc.name != 'pipeline.ableton_export':
        raise
    from ableton_export import validate_song, export_song

CORE = Path('/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library')
SLICE_TEMPLATE = CORE / 'Defaults/Slicing/Default.adg'
PART_TEMPLATE = CORE / 'Racks/Drum Racks/Drum Machines/808 Core Kit.adg'
ALS_TEMPLATE = CORE / 'Defaults/Creating Tracks/Audio Track/Default Audio Track.als'
ALS_CLIP_TEMPLATE = CORE / 'Lessons/Sets/Driver Error Compensation.als'

SET_NAME = 'Gold From Dust'
RACK_NAME = 'Gold From Dust Record Chops'
BPM = 92
TARGET_RATE = 44100
GAP_SECONDS = 1.0
PEAK_LIMIT = 0.99
SLUG = re.compile(r'[a-z][a-z0-9_-]*')
SOURCE_ID = re.compile(r'[a-z0-9][a-z0-9_-]*')
ROLE_BEATS = {'lead': 2.0, 'answer': 2.0, 'turnaround': 4.0}
ROLE_COUNTS = {'lead': (4, None), 'answer': (4, None), 'turnaround': (2, 2)}


def sha256(path: Path) -> str:
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


# --------------------------------------------------------------------------
# Audio helpers
# --------------------------------------------------------------------------

def read_stereo(path: Path, rate: int = TARGET_RATE) -> np.ndarray:
    """Read any local WAV as stereo float32 at the requested rate.

    Mono is duplicated to stereo; other sample rates are converted with
    scipy.signal.resample_poly (integer ratio reduction before up/downsampling).
    """
    audio, source_rate = sf.read(str(path), dtype='float32', always_2d=True)
    if not len(audio) or not np.isfinite(audio).all():
        raise ValueError(f'Empty or invalid audio: {path}')
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    else:
        audio = audio[:, :2]
    source_rate = int(source_rate)
    if source_rate != rate:
        divisor = math.gcd(source_rate, rate)
        audio = resample_poly(audio, rate // divisor, source_rate // divisor, axis=0)
    return np.asarray(audio, dtype='float32')


def rms(audio: np.ndarray) -> float:
    if not audio.size:
        return 0.0
    return float(math.sqrt(float(np.mean(np.square(audio, dtype=np.float64)))))


def level_for_match(level_rms: float, peak: float, target_rms: float) -> tuple[float, bool]:
    """Linear gain matching RMS to target, with an explicit peak guard."""
    if level_rms <= 1e-12 or target_rms <= 1e-12:
        return 1.0, False
    gain = target_rms / level_rms
    guarded = False
    if peak * gain > PEAK_LIMIT:
        gain = PEAK_LIMIT / peak
        guarded = True
    return float(gain), guarded


# --------------------------------------------------------------------------
# sample_flip validation (pure, no Live templates)
# --------------------------------------------------------------------------

def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _number(value, name: str) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool)
             and math.isfinite(float(value)), f'{name} must be a finite number')
    return float(value)


def validate_flip(score: dict, manifest: dict) -> dict:
    """Validate score.sample_flip against the rendered song; return a plan.

    Checked before any export: slice hash, source hash/window, duplicate slice
    ids / pad mappings, role/duration counts, track correspondence with stems,
    and that every placement is known and backed by a note-60 stem event.
    """
    _require(isinstance(score, dict), 'score must be an object')
    _require(isinstance(manifest, dict), 'manifest must be an object')
    flip = score.get('sample_flip')
    _require(isinstance(flip, dict), 'score.sample_flip must be an object')
    slice_records = flip.get('slices')
    placements = flip.get('placements')
    source_records = flip.get('source_records')
    _require(isinstance(slice_records, list) and slice_records,
             'sample_flip.slices must be a non-empty list')
    _require(isinstance(placements, list), 'sample_flip.placements must be a list')
    _require(isinstance(source_records, list) and source_records,
             'sample_flip.source_records must be a non-empty list')

    score_tracks = {t['id']: t for t in score.get('tracks', [])}
    manifest_tracks = {t['id']: t for t in manifest.get('tracks', [])}
    _require(len(score_tracks) == len(score.get('tracks', []))
             and len(manifest_tracks) == len(manifest.get('tracks', [])),
             'Duplicate track id in score or manifest')
    sample_root = Path(score.get('sample_root', '')).expanduser()
    _require(sample_root.is_absolute() and sample_root.is_dir(),
             'score.sample_root must be an existing absolute directory')

    slices, ids, pad_notes, role_counts = [], set(), set(), {}
    known_sources = {}
    for source in source_records:
        _require(isinstance(source, dict), 'Invalid source identity record')
        source_id = source.get('id')
        _require(isinstance(source_id, str) and SOURCE_ID.fullmatch(source_id),
                 'Invalid source identity id')
        identity = (source.get('source_file', source.get('file')), source.get('source_sha256', source.get('sha256')))
        _require(all(isinstance(v, str) and v for v in identity), 'Missing source identity path/hash')
        identity = (str(Path(identity[0]).resolve()), identity[1].lower())
        _require(source_id not in known_sources or known_sources[source_id] == identity,
                 f'Conflicting source identity: {source_id}')
        known_sources[source_id] = identity
    for record in slice_records:
        _require(isinstance(record, dict), 'Each slice must be an object')
        slice_id = record.get('id')
        _require(isinstance(slice_id, str) and SLUG.fullmatch(slice_id),
                 f'Invalid slice id: {slice_id!r}')
        _require(slice_id not in ids, f'Duplicate slice id: {slice_id}')
        ids.add(slice_id)

        role = record.get('role')
        _require(role in ROLE_BEATS, f'{slice_id}: role must be lead, answer or turnaround')
        role_counts[role] = role_counts.get(role, 0) + 1
        target_beats = _number(record.get('target_beats'), f'{slice_id}.target_beats')
        _require(target_beats > 0, f'{slice_id}: target_beats must be positive')
        _require(abs(target_beats - ROLE_BEATS[role]) < 1e-6,
                 f'{slice_id}: {role} slices must target {ROLE_BEATS[role]:g} beats')

        _require(int(record.get('root_midi', -1)) == 60,
                 f'{slice_id}: root_midi must be 60 (pitch is already baked in)')
        _number(record.get('gain_db'), f'{slice_id}.gain_db')
        if 'bass_midi' in record:
            _require(isinstance(record['bass_midi'], int), f'{slice_id}: bass_midi must be integer')
        pad = record.get('midi_note')
        _require(isinstance(pad, int) and 36 <= pad <= 51,
                 f'{slice_id}: midi_note must be an integer from 36 to 51')
        _require(pad not in pad_notes, f'{slice_id}: duplicate pad mapping note {pad}')
        pad_notes.add(pad)
        _number(record.get('semitones'), f'{slice_id}.semitones')
        _require(isinstance(record.get('reverse'), bool), f'{slice_id}: reverse must be boolean')

        source_id = record.get('source_id')
        _require(isinstance(source_id, str) and source_id, f'{slice_id}: source_id required')
        _require(source_id in known_sources,
                 f'{slice_id}: source_id {source_id} missing from source_records')
        source_file = Path(record.get('source_file', ''))
        _require(source_file.is_absolute() and source_file.is_file(),
                 f'{slice_id}: source_file must be an existing absolute path: {source_file}')
        source_sha = record.get('source_sha256')
        _require(isinstance(source_sha, str) and re.fullmatch(r'[a-fA-F0-9]{64}', source_sha),
                 f'{slice_id}: invalid source_sha256')
        _require(sha256(source_file) == source_sha.lower(),
                 f'{slice_id}: source recording hash mismatch: {source_file.name}')
        _require(known_sources[source_id] == (str(source_file.resolve()), source_sha.lower()),
                 f'{slice_id}: source identity does not match source_records')
        source_info = sf.info(source_file)
        start = _number(record.get('start_seconds'), f'{slice_id}.start_seconds')
        end = _number(record.get('end_seconds'), f'{slice_id}.end_seconds')
        start_frame, end_frame = round(start * source_info.samplerate), round(end * source_info.samplerate)
        _require(0 <= start_frame < end_frame <= source_info.frames,
                 f'{slice_id}: source window outside the recording '
                 f'({start:g}..{end:g}s of {source_info.frames / source_info.samplerate:g}s)')

        cut_file = Path(record.get('file', ''))
        _require(cut_file.is_absolute() and cut_file.is_file(),
                 f'{slice_id}: processed file must be an existing absolute path: {cut_file}')
        cut_sha = record.get('sha256')
        _require(isinstance(cut_sha, str) and re.fullmatch(r'[a-fA-F0-9]{64}', cut_sha),
                 f'{slice_id}: invalid processed sha256')
        _require(sha256(cut_file) == cut_sha.lower(),
                 f'{slice_id}: processed cut hash mismatch: {cut_file.name}')

        track_id = record.get('track', slice_id)
        _require(track_id in score_tracks and track_id in manifest_tracks,
                 f'{slice_id}: no song stem track for {track_id}')
        _require(manifest_tracks[track_id].get('source_sha256') == cut_sha.lower(),
                 f'{slice_id}: rendered source hash differs from processed cut')
        track_def = score_tracks[track_id]
        _require(int(track_def.get('root_midi', -1)) == 60,
                 f'{slice_id}: stem track {track_id} root_midi must be 60')
        track_sample = (sample_root / track_def['sample']).resolve()
        _require(track_sample == cut_file.resolve(),
                 f'{slice_id}: stem track sample is not the processed cut')
        if 'gain_db' in track_def:
            _require(abs(float(track_def['gain_db']) - float(record['gain_db'])) < 1e-6,
                     f'{slice_id}: stem track gain_db differs from the slice record')

        slices.append({**record, 'source_file': str(source_file), 'file': str(cut_file),
                       'track': track_id, 'source_frames': source_info.frames,
                       'source_rate': source_info.samplerate})

    for role, (minimum, exact) in ROLE_COUNTS.items():
        count = role_counts.get(role, 0)
        if exact is not None:
            _require(count == exact, f'Expected exactly {exact} {role} slices, found {count}')
        else:
            _require(count >= minimum, f'Expected at least {minimum} {role} slices, found {count}')

    bars = int(manifest['bars'])
    total_beats = bars * 4
    checked_placements = []
    for placement in placements:
        _require(isinstance(placement, dict), 'Each placement must be an object')
        slice_id = placement.get('slice', placement.get('slice_id'))
        _require(slice_id in ids, f'Unknown placement slice: {slice_id!r}')
        beat = _number(placement.get('beat'), 'placement.beat')
        duration = _number(placement.get('duration_beats'), 'placement.duration_beats')
        velocity = placement.get('velocity', 0.7)
        velocity = _number(velocity, 'placement.velocity')
        _require(0 <= beat < total_beats and 0 < duration and beat + duration <= total_beats,
                 f'Placement for {slice_id} lies outside the score')
        _require(0 < velocity <= 1, f'Placement for {slice_id} has invalid velocity')
        track_id = next(s['track'] for s in slices if s['id'] == slice_id)
        events = score_tracks[track_id].get('events', [])
        _require(any(abs(float(e['beat']) - beat) < 1e-6
                     and abs(float(e['duration_beats']) - duration) < 1e-6
                     and int(e.get('note', 60)) == 60 for e in events),
                 f'Placement for {slice_id} at beat {beat:g} has no note-60 stem event')
        checked_placements.append({'slice': slice_id, 'beat': beat,
                                   'duration_beats': duration, 'velocity': velocity})

    # A note must neither disappear, duplicate nor change velocity in the pad MIDI.
    def event_key(track_id, event):
        return (track_id, round(float(event['beat']), 6),
                round(float(event['duration_beats']), 6), int(event.get('note', 60)),
                round(float(event.get('velocity', .7)), 6))
    track_by_slice = {s['id']: s['track'] for s in slices}
    _require(len(set(track_by_slice.values())) == len(slices), 'Slice tracks must be unique')
    intended = Counter(event_key(track_by_slice[p['slice']], p) for p in checked_placements)
    rendered = Counter(event_key(t, e) for t in track_by_slice.values()
                       for e in score_tracks[t].get('events', []))
    _require(intended == rendered, 'Slice placement/event multiset mismatch')

    return {'slices': slices, 'placements': checked_placements,
            'source_records': source_records, 'bars': bars,
            'bpm': float(manifest['bpm']), 'sample_rate': int(manifest['sample_rate'])}


def load_flip(song: Path) -> tuple[Path, dict, dict, dict]:
    """Read-only load of a rendered song, returning (song, score, manifest, plan)."""
    song = Path(song).expanduser().resolve()
    if not song.is_dir():
        raise FileNotFoundError(song)
    score = json.loads((song / 'score.json').read_text())
    manifest = json.loads((song / 'run_manifest.json').read_text())
    plan = validate_flip(score, manifest)
    return song, score, manifest, plan


# --------------------------------------------------------------------------
# Listening delivery: raw / processed / mixed comparison and slice solo
# --------------------------------------------------------------------------

def build_listening(song: Path, plan: dict, out_dir: Path) -> dict:
    """Write original-cut-mix.wav plus its JSON map and sample-flip-solo.wav."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    first = min(plan['placements'], key=lambda p: (p['beat'], p['slice']))
    sliced = next(s for s in plan['slices'] if s['id'] == first['slice'])

    # "raw": the exact source-recording window the cut was taken from. The
    # seconds labels are offsets inside the original recording, nothing more.
    source_path = Path(sliced['source_file'])
    source_audio = read_stereo(source_path)
    raw = source_audio[round(sliced['start_seconds'] * TARGET_RATE):
                       round(sliced['end_seconds'] * TARGET_RATE)]
    if not len(raw):
        raise ValueError(f'Raw window is empty after conversion: {sliced["id"]}')

    # "processed": the actual processed cut used as a song instrument.
    processed = read_stereo(sliced['file'])

    # "mixed": four bars of the actual full mix beginning at the bar of the
    # first placement. Labels are local offsets inside the rendered mix file.
    mix_path = song / 'full_mix.wav'
    mixed_all = read_stereo(mix_path)
    beat_s = 60 / plan['bpm']
    first_bar = math.floor(first['beat'] / 4)
    mix_start = round(first_bar * 4 * beat_s * TARGET_RATE)
    mix_end = min(len(mixed_all), mix_start + round(16 * beat_s * TARGET_RATE))
    mixed = mixed_all[mix_start:mix_end]
    if not len(mixed):
        raise ValueError('Mixed excerpt is empty')

    sources = [
        ('raw', raw, {'source_kind': 'full recording window',
                      'source_file': str(source_path),
                      'offset_kind': 'recording offset seconds',
                      'start_seconds': sliced['start_seconds'],
                      'end_seconds': sliced['end_seconds']}),
        ('processed', processed, {'source_kind': 'processed cut',
                                  'source_file': sliced['file'],
                                  'offset_kind': 'local file, whole cut',
                                  'start_seconds': 0.0,
                                  'end_seconds': len(processed) / TARGET_RATE}),
        ('mixed', mixed, {'source_kind': 'full song mix excerpt',
                          'source_file': str(mix_path),
                          'offset_kind': 'local excerpt offset seconds',
                          'start_seconds': mix_start / TARGET_RATE,
                          'end_seconds': mix_end / TARGET_RATE}),
    ]
    target = rms(mixed)
    gap = np.zeros((int(GAP_SECONDS * TARGET_RATE), 2), dtype='float32')
    blocks, segments = [], []
    for index, (label, audio, provenance) in enumerate(sources):
        before_rms, peak = rms(audio), float(np.max(np.abs(audio)))
        gain, guarded = level_for_match(before_rms, peak, target)
        scaled = (audio * gain).astype('float32')
        after_rms = rms(scaled)
        segments.append({
            'label': label, 'frames': len(scaled),
            'rms_before': before_rms, 'rms_after': after_rms,
            'gain_linear': gain, 'gain_db': 20 * math.log10(gain) if gain > 0 else None,
            'peak_before': peak, 'peak_guard_applied': guarded, **provenance})
        if index:
            blocks.append(gap)
        blocks.append(scaled)
    comparison = np.concatenate(blocks, axis=0)
    comparison_peak = float(np.max(np.abs(comparison)))
    if comparison_peak >= 1.0:
        raise ValueError(f'Comparison would clip: peak={comparison_peak}')
    wav_name = 'original-cut-mix.wav'
    sf.write(out_dir / wav_name, comparison, TARGET_RATE, subtype='PCM_24')

    solo_meta = build_solo(song, plan, out_dir / 'sample-flip-solo.wav')

    map_data = {
        'file': wav_name, 'sample_rate': TARGET_RATE,
        'gap_seconds': GAP_SECONDS,
        'order': ['raw', 'processed', 'mixed'],
        'level_matching': 'linear RMS energy matched to the mixed excerpt with a per-segment '
                          'peak guard at 0.99; this is not LUFS and not a blind test',
        'segments': segments,
        'sample_flip_solo': solo_meta,
        'first_placement': {'slice': first['slice'], 'beat': first['beat'],
                            'duration_beats': first['duration_beats']}}
    (out_dir / 'original-cut-mix.json').write_text(
        json.dumps(map_data, ensure_ascii=False, indent=2) + '\n')
    return map_data


def build_solo(song: Path, plan: dict, out_path: Path) -> dict:
    """Sum only the slice stems; apply an explicit linear peak guard if needed."""
    total = None
    used = []
    for sliced in plan['slices']:
        record = next(t for t in json.loads((song / 'run_manifest.json').read_text())['tracks']
                      if t['id'] == sliced['track'])
        audio = read_stereo(song / record['file'])
        total = audio.copy() if total is None else total + audio
        used.append({'slice': sliced['id'], 'track': sliced['track'], 'stem': record['file']})
    peak_before = float(np.max(np.abs(total)))
    gain, guarded = 1.0, False
    if peak_before > PEAK_LIMIT:
        gain = PEAK_LIMIT / peak_before
        guarded = True
    total = (total * gain).astype('float32')
    sf.write(out_path, total, TARGET_RATE, subtype='PCM_24')
    return {'file': out_path.name, 'stems': used, 'peak_before': peak_before,
            'gain_linear': gain, 'gain_db': 20 * math.log10(gain),
            'peak_guard_applied': guarded, 'rms_after': rms(total),
            'method': 'linear sum of rendered slice stems only; common song gain already baked in'}


# --------------------------------------------------------------------------
# ChopRack: collected samples, pad MIDI and native Drum Rack preset
# --------------------------------------------------------------------------

def collect_samples(plan: dict, rack_dir: Path) -> list[dict]:
    """Copy processed cuts under ChopRack/Samples with unambiguous pad filenames."""
    samples_dir = Path(rack_dir) / 'Samples'
    samples_dir.mkdir(parents=True, exist_ok=True)
    pads = []
    for sliced in sorted(plan['slices'], key=lambda s: s['midi_note']):
        destination = samples_dir / f'{sliced["midi_note"]:02d}-{sliced["id"]}.wav'
        shutil.copy2(sliced['file'], destination)
        if sha256(destination) != sliced['sha256']:
            raise ValueError(f'Collected sample hash mismatch: {destination.name}')
        relative = f'Samples/{destination.name}'
        pads.append({'midi': sliced['midi_note'],
                     'name': f'{sliced["role"].title()} {sliced["midi_note"]}',
                     'choke': 1, 'file': relative, 'slice': sliced['id']})
    return pads


def write_phrase_midi(plan: dict, out_path: Path) -> dict:
    """phrase-chops.mid: 92 BPM, 960 ticks/beat, placements mapped to pad notes.

    Channel 9 (drum convention). End of track lands exactly at the full song
    length (bars * 4 beats), even when the last note ends earlier.
    """
    by_id = {s['id']: s for s in plan['slices']}
    total_ticks = plan['bars'] * 4 * 960
    queue = []
    for placement in plan['placements']:
        note = by_id[placement['slice']]['midi_note']
        start = round(placement['beat'] * 960)
        end = start + round(placement['duration_beats'] * 960)
        velocity = max(1, round(placement['velocity'] * 127))
        if not (36 <= note <= 51 and 0 <= start < end <= total_ticks):
            raise ValueError(f'Invalid pad event for {placement["slice"]}: '
                             f'note {note}, ticks {start}..{end}')
        queue.extend([(start, 1, note, velocity), (end, 0, note, 0)])
    mid = MidiFile(type=0, ticks_per_beat=960)
    track = MidiTrack()
    mid.tracks.append(track)
    track.extend([MetaMessage('track_name', name=out_path.stem),
                  MetaMessage('set_tempo', tempo=bpm2tempo(plan['bpm'])),
                  MetaMessage('time_signature', numerator=4, denominator=4)])
    last_tick = 0
    for tick, is_on, note, velocity in sorted(queue):
        track.append(Message('note_on' if is_on else 'note_off', note=note,
                             velocity=velocity, channel=9, time=tick - last_tick))
        last_tick = tick
    track.append(MetaMessage('end_of_track', time=total_ticks - last_tick))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mid.save(out_path)
    return {'file': out_path.name, 'ticks_per_beat': 960,
            'tempo_us_per_beat': bpm2tempo(plan['bpm']), 'bpm': plan['bpm'],
            'total_ticks': total_ticks, 'notes': len(plan['placements'])}


def _xml(path: Path) -> ET.Element:
    return ET.fromstring(gzip.decompress(Path(path).read_bytes()))


def _set_value(element: ET.Element, path: str, value) -> None:
    child = element.find(path)
    if child is None:
        raise ValueError(f'Factory preset schema is missing {path}')
    child.set('Value', str(value))


def factory_templates_available() -> bool:
    return (SLICE_TEMPLATE.is_file() and PART_TEMPLATE.is_file()
            and ALS_TEMPLATE.is_file() and ALS_CLIP_TEMPLATE.is_file())


def build_rack(pads: list[dict], stage_rack_dir: Path, final_rack_dir: Path,
               preset_name: str = RACK_NAME) -> Path:
    """Build the native Drum Rack preset from local factory templates.

    Reuses the existing windowlight helper: pad dicts carry midi, name, choke
    and a Samples-relative file. Factory XML is read locally and never vendored;
    raises a clear error when the local templates are missing instead of faking.
    """
    if not SLICE_TEMPLATE.is_file() or not PART_TEMPLATE.is_file():
        raise FileNotFoundError(
            'Native preset needs local Ableton factory templates, which were not found: '
            f'{SLICE_TEMPLATE} and {PART_TEMPLATE}. Install/locate Ableton Live 12 Suite; '
            'no preset file is fabricated.')
    from examples.windowlight_drum_practice import build_rack as build_existing_rack
    build_existing_rack(pads, stage_rack_dir, final_rack_dir)
    original = stage_rack_dir / 'Windowlight 16 Pads.adg'
    root = _xml(original)
    _set_value(root.find('.//DrumGroupDevice'), 'UserName', preset_name)
    references = root.findall('.//SampleRef/FileRef/RelativePath')
    if sorted(r.get('Value') for r in references) != sorted(p['file'] for p in pads):
        raise ValueError('Preset references must be exactly the collected samples')
    out_path = stage_rack_dir / f'{preset_name}.adg'
    out_path.write_bytes(gzip.compress(
        ET.tostring(root, encoding='utf-8', xml_declaration=True), mtime=0))
    original.unlink()  # Only the temporary preset just generated above.
    return out_path


# --------------------------------------------------------------------------
# Sources, provenance and the public deliver() orchestration
# --------------------------------------------------------------------------

def copy_sources(plan: dict, sources_dir: Path) -> list[dict]:
    """Copy only the collected source recording excerpts used, under unambiguous local IDs."""
    sources_dir = Path(sources_dir)
    sources_dir.mkdir(parents=True, exist_ok=True)
    records = []
    seen = set()
    for sliced in plan['slices']:
        source_id = sliced['source_id']
        if source_id in seen:
            continue
        seen.add(source_id)
        _require(SOURCE_ID.fullmatch(source_id), f'Source id is not a safe filename: {source_id}')
        destination = sources_dir / f'{source_id}.wav'
        shutil.copy2(sliced['source_file'], destination)
        digest = sha256(destination)
        if digest != sliced['source_sha256']:
            raise ValueError(f'Copied source hash mismatch: {destination.name}')
        records.append({'source_id': source_id, 'file': f'Sources/{destination.name}',
                        'sha256': digest})
    return records


def _write_provenance(song: Path, plan: dict, stage: Path, copied: list[dict]) -> Path:
    for candidate in ('source_provenance.json', 'provenance.json', 'source_records.json'):
        original = song / candidate
        if original.is_file():
            destination = stage / 'source_provenance.json'
            shutil.copy2(original, destination)
            return destination
    data = {'title': SET_NAME, 'note': 'Reviewed source recording provenance copied from the '
                                       'rendered song score.sample_flip.source_records',
            'source_records': plan['source_records'], 'local_copies': copied}
    destination = stage / 'source_provenance.json'
    destination.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    return destination


_README = """# Gold From Dust - real record flip delivery

This package plays an existing rendered song. The song mix, stems, MIDI and source
recordings were not modified; every media file here is a verified copy or an export.

- AbletonProject/Gold From Dust.als - Ableton Live 12 Set made from the rendered
  audio stems (warp off, unity gains, no added FX). Built by pipeline.ableton_export.
- ChopRack/Gold From Dust Record Chops.adg - playable Drum Rack preset whose pads
  reference only ChopRack/Samples (the collected processed cuts).
- ChopRack/phrase-chops.mid - the full 20-bar performance; pad notes 36..51 map
  placements to the cuts (92 BPM, 960 ticks per beat, channel 9).
- Listening/original-cut-mix.wav - raw recording window, processed cut and mixed
  excerpt in that order, linear RMS matched with a peak guard. RMS only: this is
  not LUFS and not a blind test. Map: original-cut-mix.json.
- Listening/sample-flip-solo.wav - only the slice stems summed; gain recorded in
  original-cut-mix.json, with an explicit peak guard when required.
- Sources/ - local copies of the collected source recording excerpts used; source_provenance.json
  carries the reviewed provenance. Recording excerpts may contain accompaniment.

Still PENDING (manual verification, not claimed here): opening, saving and
rerendering Gold From Dust.als in Ableton Live; importing the preset and playing
phrase-chops.mid in Live. The numerical stem-sum check is not a Live render.
No listening result, Keep decision or taste rating exists; this draft is unkept.
"""


def deliver(song: Path, out: Path) -> dict:
    """Validate and package a rendered song into a new, empty delivery directory."""
    song = Path(song).expanduser().resolve()
    out = Path(out).expanduser().resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError(f'Choose a new empty delivery directory: {out}')

    # All read-only validation happens before any export.
    loaded_song, score, manifest, plan = load_flip(song)
    validation = validate_song(song)

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f'.{out.name}-', dir=out.parent) as temporary:
        stage = Path(temporary) / 'package'
        stage.mkdir()
        export = export_song(song, stage / 'AbletonProject', set_name=SET_NAME)
        # The nested exporter cannot know this package will be relocated.
        export['project'] = str(out / 'AbletonProject')
        export['als'] = str(out / 'AbletonProject' / f'{SET_NAME}.als')
        (stage / 'AbletonProject/export_manifest.json').write_text(
            json.dumps(export, ensure_ascii=False, indent=2) + '\n')

        listening = build_listening(song, plan, stage / 'Listening')
        rack_dir = stage / 'ChopRack'
        pads = collect_samples(plan, rack_dir)
        midi_info = write_phrase_midi(plan, rack_dir / 'phrase-chops.mid')
        preset = build_rack(pads, rack_dir, out / 'ChopRack')
        sources = copy_sources(plan, stage / 'Sources')
        provenance = _write_provenance(song, plan, stage, sources)
        (stage / 'README.md').write_text(_README)

        files = []
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                files.append({'path': str(path.relative_to(stage)),
                              'sha256': sha256(path), 'bytes': path.stat().st_size})
        delivery = {
            'package': RACK_NAME, 'set_name': SET_NAME, 'song': str(song),
            'bpm': plan['bpm'], 'bars': plan['bars'],
            'sample_rate': plan['sample_rate'],
            'ableton_project': {
                'path': 'AbletonProject', 'als': 'AbletonProject/' + export['als'].split('/')[-1],
                'als_sha256': export['als_sha256'],
                'export_manifest': 'AbletonProject/export_manifest.json',
                'stem_sum': validation['stem_sum'],
                'live_verification': validation['live_verification']},
            'chop_rack': {'path': 'ChopRack',
                          'preset': f'ChopRack/{preset.name}',
                          'midi': f'ChopRack/{midi_info["file"]}',
                          'total_ticks': midi_info['total_ticks'],
                          'pads': pads},
            'listening': {'path': 'Listening',
                          'comparison': 'Listening/original-cut-mix.wav',
                          'map': 'Listening/original-cut-mix.json',
                          'solo': 'Listening/sample-flip-solo.wav',
                          'solo_gain_db': listening['sample_flip_solo']['gain_db'],
                          'segments': listening['order']},
            'sources': sources, 'provenance': provenance.name,
            'files': files,
            'qa': {
                'passed': [
                    'validate_song: nonempty stems/mix, matching rates and lengths, stem-sum residual',
                    'source recording sha256 and window bounds inside each recording',
                    'processed cut sha256 and stem-track sample correspondence',
                    'unique slice ids and unique pad notes 36..51; role/duration counts',
                    'every placement is known and backed by a note-60 stem event',
                    'collected ChopRack/Samples and Sources copies re-hashed after copy',
                    'preset FileRef list contains only the collected samples'],
                'scope': 'packaged copies/exports only; song score, mix, stems, MIDI and '
                         'source recordings are never modified',
                'not_verified': ['Live open', 'Live save', 'Live rerender versus reference',
                                 'preset import in Live', 'phrase-chops.mid playback in Live'],
                'manual_verification_remaining': [
                    'Open AbletonProject/Gold From Dust.als in Ableton Live 12 and save it',
                    'Rerender in Live and compare against AbletonProject/Reference/full_mix.wav',
                    'Load ChopRack/Gold From Dust Record Chops.adg on a MIDI track',
                    'Play ChopRack/phrase-chops.mid through the rack and confirm the pad map'],
                'decisions': 'No fabricated listening/taste result, Keep decision or rating; '
                             'the user has not kept this draft'}}
        manifest_path = stage / 'delivery_manifest.json'
        manifest_path.write_text(json.dumps(delivery, ensure_ascii=False, indent=2) + '\n')
        if out.exists():
            out.rmdir()  # Refuses a nonempty directory, e.g. a concurrent user edit.
        stage.rename(out)
    return delivery


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--song', type=Path, required=True,
                        help='Rendered song directory (score, manifest, mix, stems, midi)')
    parser.add_argument('--out', type=Path, required=True,
                        help='New empty delivery directory')
    args = parser.parse_args()
    result = deliver(args.song, args.out)
    print(json.dumps({'out': str(args.out.resolve()), 'bars': result['bars'],
                      'pads': len(result['chop_rack']['pads']),
                      'files': len(result['files']) + 1}, ensure_ascii=False))


if __name__ == '__main__':
    main()
