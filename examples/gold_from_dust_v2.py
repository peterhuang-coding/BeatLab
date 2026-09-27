"""尘里有金 · Prism Cut v2 — a cleaner, more composed revision of the real flip.

Same prepared input contract as ``real_record_flip`` v1 plus
``extra_instruments`` (piano, vibes, clap).  v1 stays immutable; this module
only reuses its validators and constants.  ``build_score`` is pure and
deterministic and emits the ``pipeline.song.render_score`` contract: sample
tracks with explicit MIDI events, modern clean processing, and every vocal
event cut from a real prepared slice — never a synthetic stand-in.

20 bars / 92 BPM / 2 s tail:
  opening 2 bars : one recognizable record fragment + clean piano, no drums
  A       6 bars : lead / answer call & response, backbeat on 2 and 4
  break   2 bars : the kit drops out; a C-minor piano line carries the tune
  B       6 bars : evolved chops, half-time snare on 3, double-time hat cells
  finale  4 bars : the motif returns with a new ending; half-bar gap, last drop
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path

from examples import real_record_flip as v1

PreparedError = v1.PreparedError
_require = v1._require

TITLE = '尘里有金 · Prism Cut v2'
RUN_ID = 'gold-from-dust-20260927-v2'
BPM = v1.BPM
BARS = v1.BARS
BEATS = v1.BEATS
TAIL_SECONDS = v1.TAIL_SECONDS
FADE_SECONDS = v1.FADE_SECONDS

EXTRA_IDS = ('piano', 'vibes', 'clap')
_MELODIC_EXTRAS = ('piano', 'vibes')
_SCRUB = ('events', 'crackle', 'wow', 'fuzz', 'taper', 'global_lowpass')

# role -> default gain, lowpass, highpass, room   (old dark filters replaced)
KIT_SETTINGS = {
    'kick': (-9.0, 9000, 0, 0.0),
    'snare': (-13.0, 12000, 0, 0.0),
    'hat': (-23.0, 16000, 0, 0.0),
    'open_hat': (-24.0, 16000, 0, 0.0),
    'shaker': (-25.0, 16000, 0, 0.0),
    'rim': (-20.0, 12000, 0, 0.0),
    'bass': (-14.0, 1800, 28, 0.0),
}
EXTRA_SETTINGS = {
    'piano': {'highpass_hz': 180, 'lowpass_hz': 12000, 'room': 0.10, 'pan': -0.18},
    'vibes': {'highpass_hz': 400, 'lowpass_hz': 14000, 'room': 0.12, 'pan': 0.22},
    'clap': {'lowpass_hz': 12000},
}

_VOICINGS = {
    8: [60, 63, 67, 68],   # Ab root (bass 32): Abmaj7/C
    3: [62, 65, 67, 70],   # Eb root (bass 39): Ebmaj9 without root
    0: [60, 63, 67, 70],   # Cm root (bass 36): C minor
}
_PENT = (72, 75, 79, 82)     # vibes answers: Ab/C pentatonic
_KNOWN_CLASSES = (8, 3, 0)


def _voicing(bass_midi) -> list[int]:
    cls = int(bass_midi) % 12
    if cls not in _KNOWN_CLASSES:                       # deterministic fallback:
        cls = min(_KNOWN_CLASSES,                       # nearest known root class
                  key=lambda c: min((cls - c) % 12, (c - cls) % 12))
    return list(_VOICINGS[cls])


def _validate_extra(raw, expected: str) -> dict:
    _require(isinstance(raw, dict), f'{expected} instrument must be an object')
    _require(raw.get('id') == expected,
             f'extra_instruments slot must contain id {expected!r}, got {raw.get("id")!r}')
    _require(isinstance(raw.get('sample'), str) and raw['sample'],
             f'{expected}: sample path string required')
    root = raw.get('root_midi')
    _require(isinstance(root, int) and not isinstance(root, bool)
             and 0 <= root <= 127,
             f'{expected}: root_midi must be an integer 0..127')
    if expected in _MELODIC_EXTRAS:
        _require(root == 60, f'{expected}: root_midi must be 60')
    _require(v1._is_finite_number(raw.get('gain_db')),
             f'{expected}: gain_db must be a finite number')
    return dict(raw)


def build_score(prepared: dict) -> dict:
    """Build the Prism Cut v2 score from a v1-schema prepared document."""
    _require(isinstance(prepared, dict), 'prepared must be a JSON object')
    _require(v1._is_finite_number(prepared.get('bpm'))
             and float(prepared['bpm']) == float(BPM),
             f'prepared.bpm must be {BPM}')
    sample_root = prepared.get('sample_root')
    _require(isinstance(sample_root, str) and sample_root.startswith('/'),
             'prepared.sample_root must be an absolute path string')

    raw_slices = prepared.get('slices')
    _require(isinstance(raw_slices, list) and raw_slices,
             'prepared.slices must be a non-empty list')
    seen: set[str] = set()
    slices = []
    for raw in raw_slices:
        sl = v1._validate_slice(raw)
        _require(sl['id'] not in seen, f'duplicate slice id: {sl["id"]}')
        seen.add(sl['id'])
        slices.append(sl)
    by_role = {r: [] for r in v1.ROLES}
    for sl in sorted(slices, key=lambda s: s['id']):
        if float(sl['target_beats']) == v1.ROLE_BEATS[sl['role']]:
            by_role[sl['role']].append(sl)
    _require(len(by_role['lead']) >= 4 and len(by_role['answer']) >= 4
             and len(by_role['turnaround']) >= 2,
             'need at least 4 lead, 4 answer and 2 turnaround slices')
    l0, l1, l2, l3 = by_role['lead'][:4]
    a0, a1, a2, a3 = by_role['answer'][:4]
    t0, t1 = by_role['turnaround'][:2]
    cuts = {'l0': l0, 'l1': l1, 'l2': l2, 'l3': l3,
            'a0': a0, 'a1': a1, 'a2': a2, 'a3': a3, 't0': t0, 't1': t1}

    raw_instruments = prepared.get('instruments')
    _require(isinstance(raw_instruments, list) and raw_instruments,
             'prepared.instruments must be a non-empty list')
    kit: dict[str, dict] = {}
    for raw in raw_instruments:
        inst = v1._validate_instrument(raw)
        role = v1._instrument_role(inst)
        _require(role is not None,
                 f'instrument {inst["id"]}: cannot identify drum/bass role')
        _require(role not in kit, f'more than one instrument resolves to {role!r}')
        kit[role] = inst
    _require(all(r in kit for r in (*v1.DRUM_ORDER, 'bass')),
             'instruments must resolve to kick, snare, hat, open_hat, shaker, rim, bass')

    raw_extra = prepared.get('extra_instruments')
    _require(isinstance(raw_extra, list) and len(raw_extra) == len(EXTRA_IDS),
             f'extra_instruments must list exactly {len(EXTRA_IDS)} entries: piano, vibes, clap')
    extras: dict[str, dict] = {}
    for expected, raw in zip(EXTRA_IDS, raw_extra):
        ext = _validate_extra(raw, expected)
        _require(ext['id'] not in kit and ext['id'] not in extras,
                 f'duplicate instrument id: {ext["id"]}')
        extras[expected] = ext

    source_records = prepared.get('source_records')
    _require(isinstance(source_records, list) and source_records
             and all(isinstance(r, dict) for r in source_records),
             'prepared.source_records must be a non-empty list of provenance records')
    inst_sources = prepared.get('instrument_sources')
    _require(isinstance(inst_sources, list)
             and len(inst_sources) >= len(kit) + len(extras)
             and all(isinstance(r, dict) for r in inst_sources),
             'instrument_sources must contain provenance rows for all instruments')
    provided = {r.get('id') for r in inst_sources}
    _require({t['id'] for t in kit.values()} | set(extras) <= provided,
             'instrument_sources must cover every kit and extra instrument id')

    # ---- vocal chop placements (one old recording at a time) --------------
    placements: list[dict] = []

    def place(key: str, beat: float, dur: float, vel: float) -> None:
        sl = cuts[key]
        _require(0 <= beat < BEATS and dur > 0 and beat + dur <= BEATS
                 and dur <= float(sl['target_beats']),
                 f'placement {key}@{beat} invalid or beyond its cut')
        placements.append({'slice_id': sl['id'], 'beat': round(float(beat), 4),
                           'duration_beats': float(dur),
                           'velocity': float(vel), 'note': 60})

    # (key, beat, duration, velocity) — 39 placements, all ten slices used.
    CHOP_TABLE = [
        # opening: record fragment + short stab + pickup
        ('t0', 0, 4, .90), ('l0', 5, 1, .84), ('l0', 6.5, .5, .78),
        # A/1 (Ab): call, eighth pickup, answer, echo
        ('l0', 8, 2, .92), ('l0', 10.5, .5, .78), ('a0', 11, 2, .86), ('a0', 14, 1, .80),
        # A/2 (Eb): tight call/response, then a half-bar gap
        ('l1', 16, 1.5, .90), ('a1', 18, 1.5, .85), ('l1', 20, 1, .82), ('a1', 21.5, .5, .78),
        # A/3 (Ab), Eb turnaround fragment leading into the break
        ('l2', 24, 2, .92), ('l2', 26.5, .5, .80), ('a2', 27, 2, .86), ('t1', 30, 2, .84),
        ('l3', 36, 1.5, .62),                              # break: one distant Cm fragment
        # B/1 (Cm): evolved syncopation
        ('l3', 40, 1.5, .90), ('a3', 42, 1, .84), ('l3', 43.5, .5, .78),
        ('a3', 45, 2, .86), ('l3', 47.5, .5, .80),
        # B/2: earlier cuts recalled, off-beat entries, Eb move
        ('l0', 48, 2, .88), ('a0', 50.5, 1, .82), ('l1', 52, 1.5, .86), ('a1', 54, 1.5, .82),
        # B/3: sequenced rise
        ('l3', 56, 1, .88), ('a3', 57.5, .5, .80), ('l3', 58, 1, .86),
        ('a1', 59.5, .5, .78), ('l2', 60, 2, .90), ('a2', 62.5, .5, .80),
        # finale: motif, half-bar gap 74-76, then a new ending (no final-bar stop)
        ('l0', 64, 2, .94), ('a0', 66.5, 1.5, .86), ('l0', 68, 1, .88),
        ('a0', 69.5, .5, .80), ('t0', 70, 4, .92),
        ('l2', 76, 1.5, .92), ('a2', 77.5, .5, .84), ('l0', 78.5, 1.5, .90),
    ]
    for row in CHOP_TABLE:
        if not 32 <= row[1] < 40:  # Let the clean piano carry the whole break.
            place(*row)
    placements.sort(key=lambda p: (p['beat'], p['slice_id']))
    for earlier, later in zip(placements, placements[1:]):
        _require(earlier['beat'] + earlier['duration_beats'] <= later['beat'],
                 'vocal cuts overlap')
    used_ids = {p['slice_id'] for p in placements}
    _require(used_ids == {s['id'] for s in slices},
             'every slice must appear at least once (delivery needs all 10)')
    _require(32 <= len(placements) <= 45, 'expected 32..45 chop placements')

    by_id = {sl['id']: sl for sl in slices}

    def root_at(beat):
        if 32 <= beat < 40:
            return 36  # Deliberate C-minor piano interlude, no record playing.
        recent = [p for p in placements if p['beat'] <= beat]
        return by_id[recent[-1]['slice_id']]['bass_midi']

    # ---- drums (events carry no note: root-pitch drums keep their root) ----
    parts = {r: [] for r in (*v1.DRUM_ORDER, 'clap')}

    def hit(role: str, beat: float, vel: float, dur: float) -> None:
        _require(0 <= beat < BEATS and 0 < beat + dur <= BEATS,
                 f'drum {role}@{beat} escapes the score')
        parts[role].append({'beat': round(float(beat), 4),
                            'duration_beats': float(dur),
                            'velocity': float(vel)})

    HAT8 = ((0, .50), (.5, .30), (1, .40), (1.5, .28),
            (2, .44), (2.5, .30), (3, .40), (3.5, .30))

    def hats8(bar: float, skip=()) -> None:
        for off, vel in HAT8:
            if off not in skip:
                hit('hat', bar + off, vel, 0.3)

    def shaker8(bar: float) -> None:
        for k in range(8):
            hit('shaker', bar + k * 0.5, 0.22, 0.2)

    # A: backbeat 2/4, syncopated kick, dynamic eighths.
    KICK_A = (((0, .95), (2.5, .70)), ((.75, .75), (2, .90), (3.5, .70)),
              ((0, .90), (3, .75)), ((.5, .70), (2, .85)),
              ((0, .95), (2.5, .70), (3.5, .65)), ((0, .95),))
    for j, kicks in enumerate(KICK_A):
        bar = 8 + 4 * j
        for off, vel in kicks:
            hit('kick', bar + off, vel, .5)
        hit('snare', bar + 1, .85, .4)
        hit('snare', bar + 3, .92, .4)
        hats8(bar, skip=(2.5,) if j in (1, 4) else ())
        if j in (1, 4):
            hit('open_hat', bar + 2.5, .40, .45)
        if j in (2, 3):
            shaker8(bar)
    hit('rim', 31.5, .50, .25)                # click into the drumless break

    # B: half-time snare on 3; sparse-hat bars alternate with 16th-note cells.
    KICK_B = (((0, .95), (2.5, .70), (3.5, .60)), ((.5, .70), (2, .85)),
              ((0, .90), (1.5, .70), (3, .70)), ((0, .95), (2.5, .70)),
              ((.5, .70), (2, .85), (3.5, .60)),
              ((0, .95), (3, .70), (3.5, .75)))          # pickup to finale
    HAT16 = tuple((k * .25, vel) for k, vel in enumerate((.34, .20, .26, .16) * 4))
    for j, kicks in enumerate(KICK_B):
        bar = 40 + 4 * j
        for off, vel in kicks:
            hit('kick', bar + off, vel, .5)
        hit('snare', bar + 2, .90, .4)
        if j in (0, 4):
            for off, vel in HAT16:
                hit('hat', bar + off, vel, .18)
        elif j in (1, 3):
            hit('hat', bar, .30, .3)
            hit('hat', bar + 2, .32, .3)
        else:
            hats8(bar)
        if j in (2, 4):
            hit('open_hat', bar + .5, .40, .45)
        if j in (1, 3, 5):
            shaker8(bar)
    for beat in (47.5, 55.5, 63.5):           # 16th fills only at bar-end joins
        hit('hat', beat, .30, .18)
        hit('hat', beat + .25, .26, .18)
    hit('snare', 46.5, .15, .25)              # ghost notes on transition bars
    hit('snare', 54.5, .15, .25)
    hit('rim', 63.5, .45, .25)

    # finale: 2/4 + layered claps; bar 2 sparse; gap 74-76 then the last drop.
    KICK_F = (((0, 1.00), (2.5, .75)), ((.75, .75), (2, .90), (3.5, .70)),
              ((0, .85),), ((0, 1.00), (2.5, .80), (3.5, .85)))
    CLAP_F = (((1, .60), (3, .62)), ((1, .62), (3, .72)),
              ((1, .62), (3, .78)), ((1, .80), (3, .88)))
    for j in range(4):
        bar = 64 + 4 * j
        for off, vel in KICK_F[j]:
            hit('kick', bar + off, vel, .5)
        hit('snare', bar + 1, .85, .4)
        hit('snare', bar + 3, .92, .4)
        for off, vel in CLAP_F[j]:
            hit('clap', bar + off, vel, .4)
        if j in (0, 1, 3):
            hats8(bar, skip=(2.5,) if j == 0 else ())
    hit('hat', 71.5, .30, .18)                # one small fill into bar 3
    hit('hat', 71.75, .26, .18)
    hit('snare', 70.5, .15, .25)
    hit('open_hat', 66.5, .42, .45)
    hit('hat', 72, .40, .3)                   # bar 3 stays sparse...
    hit('hat', 73, .28, .3)
    for off in (0, .5, 1, 1.5):
        hit('shaker', 72 + off, .22, .2)      # ...everything stops at 74

    # ---- bass: one voice, roots from the active/recent slice ---------------
    bass_events: list[dict] = []

    def bass(beat: float, note: int, dur: float, vel: float) -> None:
        _require(0 <= beat < BEATS and 0 < beat + dur <= BEATS
                 and 0 <= note <= 127, f'bass note@{beat} escapes score/range')
        bass_events.append({'beat': float(beat), 'duration_beats': float(dur),
                            'velocity': float(vel), 'note': int(note)})

    # (beat, note, duration, velocity); 31/35/38 are gap-only chromatic approaches
    BASS_TABLE = [
        (4, 32, 1, .72), (6, 32, 1, .68),
        (8, 32, 2, .80), (10, 32, .5, .62), (11, 32, 2, .75), (14, 32, 1, .68),
        (15.5, 38, .5, .60),
        (16, 39, 2, .80), (18, 39, 1.5, .75), (20, 39, 1, .68), (21.5, 39, .5, .62),
        (23.5, 31, .5, .60),
        (24, 32, 2, .80), (26, 32, .5, .62), (27, 32, 2, .75), (30, 39, 2, .75),
        (32, 36, 2, .70), (34, 43, 1, .60), (35, 34, .5, .58),
        (36, 36, 1.5, .70), (38, 39, 1, .58), (39, 35, 1, .58),
        (40, 36, 1.5, .80), (42, 36, 1, .70), (43.5, 36, .5, .62), (45, 36, 2, .75),
        (47, 31, .5, .60),
        (48, 32, 2, .80), (50.5, 32, 1, .68), (52, 39, 1.5, .80), (54, 39, 1.5, .70),
        (55.5, 35, .5, .60),
        (56, 36, 1, .78), (57, 36, .5, .62), (58, 36, 1, .70),
        (60, 32, 2, .80), (62, 32, 1, .68),
        (64, 32, 2, .82), (66, 32, .5, .62), (66.5, 32, 1.5, .75),
        (68, 32, 1, .72), (69, 32, 1, .68),
        (70, 32, 2, .75), (72, 32, 2, .70),
        (76, 32, 1.5, .82), (78, 32, .5, .68), (78.5, 32, 1.5, .80),
    ]
    for beat, note, duration, velocity in BASS_TABLE:
        if beat in (15.5, 23.5, 47, 55.5):
            next_call = next(p for p in placements if p['beat'] > beat)
            note = max(0, by_id[next_call['slice_id']]['bass_midi'] - 1)
        elif beat == 39:
            note = max(0, root_at(40) - 1)
        elif not 32 <= beat < 40:
            note = root_at(beat)
        bass(beat, note, duration, velocity)
    bass_events.sort(key=lambda e: e['beat'])
    for earlier, later in zip(bass_events, bass_events[1:]):
        _require(earlier['beat'] + earlier['duration_beats'] <= later['beat'],
                 'bass notes overlap')

    # ---- piano: slice-derived voicings; 3 notes under vocal, 4 at ends ------
    piano_events: list[dict] = []

    def pnote(beat: float, note: int, dur: float, vel: float) -> None:
        _require(0 <= beat and beat + dur <= BEATS and 0 <= note <= 127
                 and .4 <= dur <= 1.5,
                 f'piano event invalid: {beat},{note},{dur}')
        piano_events.append({'beat': round(float(beat), 4),
                             'duration_beats': float(dur),
                             'velocity': float(vel), 'note': int(note)})

    _rot: dict[int, int] = {}

    def pchord(beat: float, bass_midi, dur: float, vel: float, count: int) -> None:
        voices = _voicing(bass_midi)
        if count == 4:
            chosen = voices
        else:
            cls = int(bass_midi) % 12
            k = _rot.get(cls, 0)
            _rot[cls] = k + 1
            chosen = voices[:3] if k % 2 == 0 else voices[1:]    # voice-leading
        for note in chosen:
            pnote(beat, note, dur, vel)

    CHORDS = [
        # opening, then full voicing into A
        (0, 32, 1.5, .52, 3), (4.5, 32, 1.0, .52, 3),
        (6.5, 32, .5, .50, 3), (7, 32, 1.0, .50, 4),
        # A cell 1 (Ab)
        (8, 32, 1.5, .52, 3), (11, 32, 1.5, .48, 3), (14, 32, 1.0, .48, 3),
        # A cell 2 (Eb)
        (16, 39, 1.5, .52, 3), (18, 39, 1.5, .48, 3), (20, 39, 1.0, .48, 3),
        # A cell 3 (Ab), full Eb voicing at the phrase end
        (24, 32, 1.5, .52, 3), (27, 32, 1.5, .48, 3), (30, 39, 2.0, .55, 4),
        # B cell 1 (Cm)
        (40, 36, 1.5, .52, 3), (42, 36, 1.0, .48, 3), (45, 36, 1.5, .48, 3),
        # B cell 2 (Ab -> Eb)
        (48, 32, 1.5, .52, 3), (50.5, 32, 1.0, .48, 3),
        (52, 39, 1.5, .52, 3), (54, 39, 1.5, .48, 3),
        # B cell 3 (Cm -> Ab), full voicing in the one-beat gap
        (56, 36, 1.0, .52, 3), (57.5, 36, .5, .45, 3),
        (58, 36, 1.0, .48, 3), (60, 32, 2.0, .52, 3), (63, 32, 1.0, .50, 4),
        # finale
        (64, 32, 2.0, .55, 3), (66.5, 32, 1.5, .50, 3),
        (68, 32, 1.0, .52, 3), (70, 32, 2.0, .52, 3),
        (76, 32, 1.5, .55, 3), (78.5, 32, 1.5, .60, 4),
    ]
    for beat, _, duration, velocity, count in CHORDS:
        duration = min(duration, 1.5)
        if any(p['beat'] < beat + duration and p['beat'] + p['duration_beats'] > beat
               for p in placements):
            count = min(count, 3)
        pchord(beat, root_at(beat), duration, velocity, count)

    # break: the piano plays an actual C-minor tune (drums are gone)
    BREAK_TUNE = [
        (32.0, 60, 1.5, .52), (32.0, 63, 1.5, .52),
        (32.0, 67, 1.5, .55), (32.0, 70, 1.5, .58),
        (33.5, 70, .5, .50),
        (34.0, 67, 1.0, .50), (34.0, 72, 1.0, .55),
        (35.0, 70, 1.0, .50),
        (36.0, 60, 1.5, .52), (36.0, 63, 1.5, .52),
        (36.0, 67, 1.5, .55), (36.0, 70, 1.5, .58),
        (37.5, 72, 1.0, .50), (38.5, 70, 1.5, .52),
    ]
    for row in BREAK_TUNE:
        pnote(*row)

    # ---- vibes: sparse pentatonic answers in the rests --------------------
    vibe_events: list[dict] = []

    def vibe(beat: float, note: int) -> None:
        _require(note in _PENT and beat + 1.0 <= BEATS, f'vibe invalid: {beat},{note}')
        vibe_events.append({'beat': round(float(beat), 4), 'duration_beats': 1.0,
                            'velocity': .42, 'note': int(note)})

    for beat, note in ((7, 72), (15, 75), (22, 72), (23, 79), (29, 82),
                       (44, 75), (63, 79), (74, 72), (75, 82)):
        vibe(beat, note)

    # ---- assemble tracks ---------------------------------------------------
    slice_by_id = {s['id']: s for s in slices}
    answer_ordinal = {s['id']: i for i, s in enumerate(by_role['answer'][:4])}
    first_use = sorted(used_ids,
                       key=lambda x: min(p['beat'] for p in placements
                                         if p['slice_id'] == x))
    tracks: list[dict] = []
    for i, sid in enumerate(first_use):
        sl = slice_by_id[sid]
        if sl['role'] == 'answer':
            pan = -0.16 if answer_ordinal[sid] % 2 == 0 else 0.16
        else:
            pan = 0.0
        tracks.append({
            'id': sid,
            'name': sid.replace('_', ' ').replace('-', ' ').title(),
            'sample': sl['file'], 'root_midi': 60,
            'midi_note': int(sl['midi_note']),
            'gain_db': float(sl['gain_db']),               # supplied gain exact
            'highpass_hz': 180,
            'lowpass_hz': 6500 + (i * 37) % 1501,          # retain old-record character
            'room': 0.02 + (i % 3) * 0.01, 'pan': pan,
            'events': [dict(p) for p in placements if p['slice_id'] == sid],
        })

    for role in (*v1.DRUM_ORDER, 'bass'):
        definition = deepcopy(kit[role])
        for key in _SCRUB:
            definition.pop(key, None)
        gain, lp, hp, room = KIT_SETTINGS[role]
        definition.setdefault('name', v1.DEFAULT_NAME[role])
        definition['gain_db'] = gain
        definition['lowpass_hz'] = lp                     # forced replacement
        if hp:
            definition['highpass_hz'] = hp
        else:
            definition.pop('highpass_hz', None)
        definition['room'] = room
        definition.pop('pan', None)
        if role in v1.PERCUSSION:
            definition.setdefault('drum', True)
        definition['events'] = (bass_events if role == 'bass'
                                else parts[role])
        tracks.append(definition)

    for eid in EXTRA_IDS:
        definition = deepcopy(extras[eid])
        for key in _SCRUB:
            definition.pop(key, None)
        definition.setdefault('name', eid.title())
        definition.update(EXTRA_SETTINGS[eid])
        if eid == 'clap':
            definition.pop('highpass_hz', None)
            definition.setdefault('drum', True)
        definition['events'] = {'piano': piano_events,
                                'vibes': vibe_events,
                                'clap': parts['clap']}[eid]
        tracks.append(definition)

    # Clear the half-bar drop gap across every part, including claps and vibes.
    for track in tracks:
        events = []
        for event in track['events']:
            beat = event['beat']
            if 74 <= beat < 76:
                continue
            event = dict(event)
            if beat < 74 < beat + event['duration_beats']:
                event['duration_beats'] = 74 - beat
            events.append(event)
        track['events'] = events

    return {
        'title': TITLE,
        'run_id': RUN_ID,
        'bpm': BPM,
        'bars': BARS,
        'tail_seconds': TAIL_SECONDS,
        'fade_seconds': FADE_SECONDS,
        'sample_root': sample_root,
        'sections': [
            {'name': 'opening', 'start_bar': 0, 'bars': 2},
            {'name': 'A', 'start_bar': 2, 'bars': 6},
            {'name': 'break', 'start_bar': 8, 'bars': 2},
            {'name': 'B', 'start_bar': 10, 'bars': 6},
            {'name': 'finale', 'start_bar': 16, 'bars': 4},
        ],
        'license': {
            'usage_statement': v1.LICENSE_STATEMENT,
            'release_cleared': False,
            'source_records': deepcopy(source_records),
        },
        'tracks': tracks,
        'instrument_sources': deepcopy(inst_sources),
        'sample_flip': {
            'slices': deepcopy(slices),
            'placements': placements,
            'source_records': deepcopy(source_records),
        },
    }


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
