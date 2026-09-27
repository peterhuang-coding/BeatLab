"""Build a soulful 20-bar / 92 BPM vocal-chop score from prepared real slices.

The score follows the standard ``pipeline.song.render_score`` contract: every
track is a sample instrument with explicit, editable MIDI events.  No audio is
rendered here and no source is downloaded; all cuts, gains and provenance come
from the supplied ``prepared.json``.  ``build_score`` is pure and deterministic.

Structure (20 bars = 80 beats):
  intro 4 bars  : a longer record fragment breathes, drums enter one layer at
                  a time.
  hook  8 bars  : two four-bar chopped lead -> answer cycles.
  half  4 bars  : halftime switch: backbeat on the third quarter, sparse
                  hats, wide space.
  return 4 bars : the opening motif returns with an altered turnaround ending;
                  the final bar stops to leave breath.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from numbers import Real
from pathlib import Path
import re

TITLE = '尘里有金 · Gold From Dust'
RUN_ID = 'gold-from-dust-20260927-v1'
BPM = 92
BARS = 20
BEATS = BARS * 4            # 80
TAIL_SECONDS = 2
FADE_SECONDS = 2

SWUNG = 0.56                # fixed swing position for off-beat eighths (0.55-0.58)
LATE = 0.035                # fixed late backbeat push

SLUG_RE = re.compile(r'[a-z][a-z0-9_-]*$')
ROLES = ('lead', 'answer', 'turnaround')
ROLE_BEATS = {'lead': 2.0, 'answer': 2.0, 'turnaround': 4.0}
PERCUSSION = ('kick', 'snare', 'hat', 'open_hat', 'shaker', 'rim')
DRUM_ORDER = ('kick', 'snare', 'hat', 'open_hat', 'shaker', 'rim')

DEFAULT_GAIN_DB = {
    'kick': -10.0, 'snare': -11.0, 'hat': -16.0, 'open_hat': -14.0,
    'shaker': -16.0, 'rim': -13.0, 'bass': -12.0,
}
DEFAULT_NAME = {
    'kick': 'Kick', 'snare': 'Snare', 'hat': 'Swing Hat',
    'open_hat': 'Open Hat', 'shaker': 'Shaker', 'rim': 'Rim', 'bass': 'Bass',
}

LICENSE_STATEMENT = (
    'Every melodic/vocal chop is cut from the historical recordings listed in '
    'source_records; drums and bass use the supplied one-shot instruments. '
    'This edit preserves source provenance and does not claim '
    'that any source recording is cleared for release; rights must be '
    'verified before distribution.'
)


class PreparedError(ValueError):
    """Raised when the prepared input is malformed or insufficient."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PreparedError(message)


def _is_finite_number(value) -> bool:
    return (isinstance(value, Real) and not isinstance(value, bool)
            and float(value) == float(value)
            and float(value) not in (float('inf'), float('-inf')))


def _validate_slice(raw) -> dict:
    _require(isinstance(raw, dict), 'each slice must be an object')
    missing = [k for k in ('id', 'file', 'role', 'target_beats', 'root_midi',
                           'bass_midi', 'gain_db', 'midi_note', 'source_id',
                           'source_file', 'source_sha256', 'start_seconds',
                           'end_seconds', 'semitones', 'reverse', 'sha256')
               if k not in raw]
    _require(not missing, f'slice {raw.get("id", "<unknown>")} missing keys: {missing}')
    sid = raw['id']
    _require(isinstance(sid, str) and bool(SLUG_RE.match(sid)),
             f'slice id must match [a-z][a-z0-9_-]*: {sid!r}')
    _require(isinstance(raw['file'], str) and raw['file'].startswith('/'),
             f'slice {sid}: file must be an absolute path string')
    _require(raw['role'] in ROLES, f'slice {sid}: role must be one of {ROLES}')
    _require(_is_finite_number(raw['target_beats']) and float(raw['target_beats']) > 0,
             f'slice {sid}: target_beats must be a positive number')
    _require(raw['root_midi'] == 60,
             f'slice {sid}: root_midi must be 60 (pitch is already baked in)')
    _require(isinstance(raw['bass_midi'], int) and not isinstance(raw['bass_midi'], bool)
             and 0 <= raw['bass_midi'] <= 120,
             f'slice {sid}: bass_midi must be an integer 0..120 (fifth must stay <=127)')
    _require(_is_finite_number(raw['gain_db']), f'slice {sid}: gain_db must be a number')
    _require(isinstance(raw['midi_note'], int) and not isinstance(raw['midi_note'], bool)
             and 36 <= raw['midi_note'] <= 51,
             f'slice {sid}: midi_note must be an integer 36..51')
    for key in ('source_id', 'source_file', 'source_sha256', 'sha256'):
        _require(isinstance(raw[key], str) and raw[key],
                 f'slice {sid}: {key} must be a non-empty string')
    _require(_is_finite_number(raw['start_seconds']) and float(raw['start_seconds']) >= 0,
             f'slice {sid}: start_seconds must be >= 0')
    _require(_is_finite_number(raw['end_seconds'])
             and float(raw['end_seconds']) > float(raw['start_seconds']),
             f'slice {sid}: end_seconds must be greater than start_seconds')
    _require(_is_finite_number(raw['semitones']),
             f'slice {sid}: semitones must be a number')
    _require(isinstance(raw['reverse'], bool), f'slice {sid}: reverse must be boolean')
    return dict(raw)


def _instrument_role(track: dict):
    text = (str(track.get('id', '')).lower().replace('-', '_') + ' '
            + str(track.get('name', '')).lower().replace('-', '_'))
    if 'open_hat' in text or 'openhat' in text:
        return 'open_hat'
    for role in ('kick', 'snare', 'rim', 'shaker', 'bass', 'hat'):
        if role in text:
            return role
    return None


def _validate_instrument(raw) -> dict:
    _require(isinstance(raw, dict), 'each instrument must be an object')
    _require('id' in raw and isinstance(raw['id'], str)
             and bool(SLUG_RE.match(str(raw['id']))),
             f'instrument id must match [a-z][a-z0-9_-]*: {raw.get("id")!r}')
    _require(isinstance(raw.get('sample'), str) and raw['sample'],
             f'instrument {raw["id"]}: sample path string required')
    return dict(raw)


def build_score(prepared: dict) -> dict:
    """Build the Gold From Dust song score from a prepared-slices document."""
    _require(isinstance(prepared, dict), 'prepared must be a JSON object')
    bpm = prepared.get('bpm')
    _require(_is_finite_number(bpm) and float(bpm) == float(BPM),
             f'prepared.bpm must be {BPM}')
    sample_root = prepared.get('sample_root')
    _require(isinstance(sample_root, str) and sample_root.startswith('/'),
             'prepared.sample_root must be an absolute path string')

    raw_slices = prepared.get('slices')
    _require(isinstance(raw_slices, list) and raw_slices,
             'prepared.slices must be a non-empty list')
    seen_ids: set[str] = set()
    slices = []
    for raw in raw_slices:
        sl = _validate_slice(raw)
        _require(sl['id'] not in seen_ids, f'duplicate slice id: {sl["id"]}')
        seen_ids.add(sl['id'])
        slices.append(sl)

    by_role: dict[str, list[dict]] = {r: [] for r in ROLES}
    for sl in sorted(slices, key=lambda s: s['id']):
        if float(sl['target_beats']) == ROLE_BEATS[sl['role']]:
            by_role[sl['role']].append(sl)
    _require(len(by_role['lead']) >= 4,
             'need at least four 2-beat lead slices to build the motif')
    _require(len(by_role['answer']) >= 4,
             'need at least four 2-beat answer slices to build responses')
    _require(len(by_role['turnaround']) >= 2,
             'need at least two 4-beat turnaround slices')
    lead = by_role['lead'][:4]
    answer = by_role['answer'][:4]
    turn = by_role['turnaround'][:2]
    l0, l1, l2, l3 = lead
    a0, a1, a2, a3 = answer
    t0, t1 = turn

    raw_instruments = prepared.get('instruments')
    _require(isinstance(raw_instruments, list) and raw_instruments,
             'prepared.instruments must be a non-empty list')
    instruments: dict[str, dict] = {}
    for raw in raw_instruments:
        inst = _validate_instrument(raw)
        role = _instrument_role(inst)
        _require(role is not None,
                 f'instrument {inst["id"]}: cannot identify drum/bass role')
        _require(role not in instruments,
                 f'more than one instrument resolves to role {role!r}')
        instruments[role] = inst
    missing_roles = [r for r in (*DRUM_ORDER, 'bass') if r not in instruments]
    _require(not missing_roles,
             f'instruments missing required roles: {missing_roles}')

    source_records = prepared.get('source_records')
    _require(isinstance(source_records, list) and source_records
             and all(isinstance(r, dict) for r in source_records),
             'prepared.source_records must be a non-empty list of provenance records')

    # ---- vocal chop placements ------------------------------------------
    placements: list[dict] = []

    def place(sl: dict, beat: float, duration: float, velocity: float) -> None:
        _require(0 <= beat < BEATS and duration > 0
                 and beat + duration <= BEATS,
                 f'placement for {sl["id"]} escapes the 80-beat score')
        _require(duration <= float(sl['target_beats']),
                 f'placement for {sl["id"]} exceeds its {sl["target_beats"]}-beat cut')
        placements.append({'slice_id': sl['id'], 'beat': round(float(beat), 4),
                           'duration_beats': float(duration),
                           'velocity': float(velocity), 'note': 60})

    # Intro: the 4-beat record fragment breathes alone, then one answer.
    place(t0, 0.0, 4.0, 0.90)
    place(a0, 5.0, 2.0, 0.85)

    # Two four-bar hook cells. The motif: lead call, eighth retrigger pickup,
    # answer response, second lead, second answer with retrigger, lead echo.
    def hook_cell(start: float, la, lb, aa, ab) -> None:
        place(la, start + 0.0, 2.0, 0.92)
        place(la, start + 2.5, 0.5, 0.78)
        place(aa, start + 3.0, 2.0, 0.88)
        place(lb, start + 6.0, 2.0, 0.90)
        place(ab, start + 9.0, 2.0, 0.88)
        place(ab, start + 11.5, 0.5, 0.80)
        place(la, start + 14.5, 0.5, 0.76)

    hook_cell(16.0, l0, l1, a0, a1)
    hook_cell(32.0, l2, l3, a2, a3)

    # Halftime: delayed, sparse entries over the switched drum feel.
    place(l1, 49.0, 2.0, 0.86)
    place(a1, 54.0, 2.0, 0.84)
    place(l1, 60.5, 0.5, 0.74)
    place(l0, 62.5, 0.5, 0.78)       # pickup hinting the return

    # Return: recognizable opening motif, then an altered turnaround ending.
    place(l0, 64.0, 2.0, 0.92)
    place(l0, 66.5, 0.5, 0.78)
    place(a0, 67.0, 2.0, 0.88)
    place(l1, 70.0, 2.0, 0.90)
    place(t1, 72.0, 4.0, 0.92)       # replaces the expected second answer
    # beat 76..80 deliberately empty: the final-bar stop / breath.

    placements.sort(key=lambda p: (p['beat'], p['slice_id']))

    # Enforce one recording cut at a time across every sample track.
    for earlier, later in zip(placements, placements[1:]):
        _require(earlier['beat'] + earlier['duration_beats'] <= later['beat'],
                 f'vocal cuts overlap: {earlier["slice_id"]} / {later["slice_id"]}')

    # ---- drums ------------------------------------------------------------
    drum_events: dict[str, list[dict]] = {r: [] for r in (*DRUM_ORDER, 'bass')}

    def hit(role: str, beat: float, velocity: float, duration: float) -> None:
        _require(0 <= beat < BEATS and 0 < beat + duration <= BEATS,
                 f'drum event {role} escapes the score')
        drum_events[role].append({'beat': round(float(beat), 4),
                                  'duration_beats': float(duration),
                                  'velocity': float(velocity)})

    # Intro bars 1-3 (bar 0 stays drum-free): progressive layer entry.
    for off, vel in ((0.0, 0.90), (2.0, 0.70)):
        hit('kick', 4 + off, vel, 0.5)
    for off, vel in ((0.0, 0.85), (3.0, 0.70)):
        hit('kick', 8 + off, vel, 0.5)
    for off, vel in ((0.0, 0.85), (2.5, 0.70)):
        hit('kick', 12 + off, vel, 0.5)
    for k in range(4):
        hit('hat', 8 + k, 0.35, 0.3)
        hit('hat', 8 + k + SWUNG, 0.42, 0.3)
        hit('hat', 12 + k, 0.35, 0.3)
        hit('hat', 12 + k + SWUNG, 0.42, 0.3)
    hit('snare', 15 + LATE, 0.85, 0.4)                       # single teased backbeat

    # Hook cell drum recipe.
    KICK_CELL = (
        ((0.0, 1.00), (2.0, 0.72)),
        ((1.0, 0.78), (2.0, 0.95)),
        ((0.0, 0.70), (1.0, 0.85), (3.0, 0.70)),
        ((0.0, 1.00), (2.0, 0.72)),
    )

    def hook_drums(start: float, *, extras: bool) -> None:
        for j, kicks in enumerate(KICK_CELL):
            bar = start + 4 * j
            for off, vel in kicks:
                hit('kick', bar + off, vel, 0.5)
            hit('snare', bar + 1 + LATE, 0.85, 0.4)          # late beat 2
            hit('snare', bar + 3 + LATE, 0.95, 0.4)          # late beat 4
            for k in range(4):
                hit('hat', bar + k, 0.55 if k == 0 else 0.40, 0.3)
                hit('hat', bar + k + SWUNG, 0.50, 0.3)
            if extras:
                hit('open_hat', bar + 1 + SWUNG, 0.50, 0.45)
                for k in range(4):
                    hit('shaker', bar + k + SWUNG, 0.40, 0.25)
        # small bounded ghost-note colours (fixed positions, <=0.20 velocity)
        hit('snare', start + 3 + SWUNG, 0.16, 0.3)
        hit('snare', start + 11 + SWUNG, 0.20, 0.3)
        hit('rim', start + 15.5, 0.55, 0.3)                  # pickup out

    hook_drums(16.0, extras=False)
    hook_drums(32.0, extras=True)

    # Halftime: the backbeat moves from quarters 2/4 to quarter 3 (offset 2).
    half_kicks = ((0.0, 0.95), (2.5, 0.60))
    for j in range(4):
        bar = 48 + 4 * j
        hit('snare', bar + 2 + LATE, 0.80, 0.4)
        hit('hat', bar, 0.32, 0.3)
        hit('hat', bar + 2 + SWUNG, 0.35, 0.3)
        if j == 0:
            for off, vel in half_kicks:
                hit('kick', bar + off, vel, 0.5)
        elif j == 1:
            hit('kick', bar, 0.85, 0.5)
        elif j == 3:
            hit('kick', bar + 2.5, 0.70, 0.4)                # pickup to return

    # Return: hook recipe on the first three bars, full stop on the last bar
    # apart from one downbeat kick.
    for j, kicks in enumerate(KICK_CELL[:3]):
        bar = 64 + 4 * j
        for off, vel in kicks:
            hit('kick', bar + off, vel, 0.5)
        hit('snare', bar + 1 + LATE, 0.85, 0.4)
        hit('snare', bar + 3 + LATE, 0.95, 0.4)
        for k in range(4):
            hit('hat', bar + k, 0.55 if k == 0 else 0.40, 0.3)
            hit('hat', bar + k + SWUNG, 0.50, 0.3)
    hit('snare', 64 + 3 + SWUNG, 0.16, 0.3)
    hit('snare', 64 + 11 + SWUNG, 0.20, 0.3)
    hit('kick', 76.0, 1.00, 0.6)

    # ---- bass: roots/fifths following the placed chops, with space --------
    def bass(sl: dict, beat: float, duration: float, fifth: bool = False) -> None:
        note = int(sl['bass_midi']) + (7 if fifth else 0)
        _require(0 <= beat < BEATS and 0 < beat + duration <= BEATS
                 and 0 <= note <= 127, f'bass note escapes score/range at beat {beat}')
        drum_events['bass'].append({'beat': float(beat),
                                    'duration_beats': float(duration),
                                    'velocity': 0.80, 'note': note})

    bass(t0, 4.0, 1.5)
    bass(a0, 8.0, 2.0)
    bass(l0, 16.0, 2.0)
    bass(a0, 19.0, 1.5)
    bass(l1, 22.0, 2.0)
    bass(a1, 25.0, 1.5, fifth=True)
    bass(l2, 32.0, 2.0)
    bass(a2, 35.0, 1.5)
    bass(l3, 38.0, 2.0)
    bass(a3, 41.0, 1.5, fifth=True)
    bass(l1, 48.0, 2.0)
    bass(a1, 54.0, 1.5, fifth=True)
    bass(l1, 60.0, 1.0)
    bass(l0, 64.0, 2.0)
    bass(a0, 67.0, 1.5)
    bass(l1, 70.0, 2.0)
    bass(t1, 72.0, 2.0)
    bass_events = sorted(drum_events['bass'], key=lambda e: e['beat'])
    for earlier, later in zip(bass_events, bass_events[1:]):
        _require(earlier['beat'] + earlier['duration_beats'] <= later['beat'],
                 'bass notes overlap')
    drum_events['bass'] = bass_events

    # ---- assemble tracks --------------------------------------------------
    used = {p['slice_id'] for p in placements}
    slice_by_id = {s['id']: s for s in (*lead, *answer, *turn)}
    answer_ordinal = {s['id']: i for i, s in enumerate(answer)}
    tracks: list[dict] = []
    for i, sid in enumerate(sorted(
            used,
            key=lambda x: min(p['beat'] for p in placements if p['slice_id'] == x))):
        sl = slice_by_id[sid]
        events = [dict(p) for p in placements if p['slice_id'] == sid]
        for e in events:                      # sample events are note 60 only
            e['note'] = 60
        if sl['role'] == 'lead':
            room, pan = 0.04, 0.0
        elif sl['role'] == 'turnaround':
            room, pan = 0.06, 0.0
        else:
            room = 0.05
            pan = -0.16 if answer_ordinal[sid] % 2 == 0 else 0.16
        pretty = sid.replace('_', ' ').replace('-', ' ').title()
        tracks.append({
            'id': sid, 'name': pretty, 'sample': sl['file'],
            'root_midi': 60, 'midi_note': int(sl['midi_note']),
            'gain_db': float(sl['gain_db']),
            'highpass_hz': 150 + (i * 13) % 41,
            'lowpass_hz': 6000 + (i * 311) % 2501,
            'room': room, 'pan': pan, 'events': events,
        })

    for role in (*DRUM_ORDER, 'bass'):
        definition = deepcopy(instruments[role])
        definition.pop('events', None)          # supplied definitions carry no events
        definition.setdefault('name', DEFAULT_NAME[role])
        definition.setdefault('gain_db', DEFAULT_GAIN_DB[role])
        if role in PERCUSSION:
            definition.setdefault('drum', True)
        definition['events'] = drum_events[role]
        tracks.append(definition)

    score = {
        'title': TITLE,
        'run_id': RUN_ID,
        'bpm': BPM,
        'bars': BARS,
        'tail_seconds': TAIL_SECONDS,
        'fade_seconds': FADE_SECONDS,
        'sample_root': sample_root,
        'sections': [
            {'name': 'intro', 'start_bar': 0, 'bars': 4},
            {'name': 'hook', 'start_bar': 4, 'bars': 8},
            {'name': 'halftime', 'start_bar': 12, 'bars': 4},
            {'name': 'return', 'start_bar': 16, 'bars': 4},
        ],
        'license': {
            'usage_statement': LICENSE_STATEMENT,
            'release_cleared': False,
            'source_records': deepcopy(source_records),
        },
        'tracks': tracks,
        'instrument_sources': deepcopy(prepared.get('instrument_sources', [])),
        'sample_flip': {
            'slices': deepcopy(slices),
            'placements': placements,
            'source_records': deepcopy(source_records),
        },
    }
    return score


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared', type=Path, required=True,
                        help='prepared.json describing real recording slices')
    parser.add_argument('--score', type=Path, required=True,
                        help='output score JSON path (must not already exist)')
    args = parser.parse_args(argv)
    if args.score.exists():
        parser.error(f'refusing to overwrite existing score: {args.score}')
    prepared = json.loads(args.prepared.read_text())
    score = build_score(prepared)
    args.score.parent.mkdir(parents=True, exist_ok=True)
    args.score.write_text(json.dumps(score, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'score': str(args.score.resolve()),
                      'placements': len(score['sample_flip']['placements'])},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
