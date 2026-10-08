"""Ten distinct sample-flip audition scores from prepared real recording cuts.

One pure deterministic entry point :func:`build_score` turns a *prepared*
payload (absolute cut WAVs + ready-to-use pipeline.song instrument dicts) into
a standard render-ready score.  No audio is opened, no randomness is drawn from
global state, and the prepared payload is never mutated.

Each of the ten pieces has an explicit arrangement recipe: hand-authored phrase
grids (question / answer pairs, long turns, selective retriggers), a named drum
feel and a middle switch into a contrasting section, with a deliberate hook
return.  A small common engine only realises those recipes - it never invents
the arrangement.
"""
from __future__ import annotations

import copy

COLLECTION_ID = "city-afterimages-20260927"
TAIL_SECONDS = 2.0
FADE_SECONDS = 1.5

# Fixed vocabulary of the ten already-cut phrase entries (contract order).
LEAD_IDS = ["lead_a", "lead_b", "lead_c", "lead_d"]
ANSWER_IDS = ["answer_a", "answer_b", "answer_c", "answer_d"]
TURN_IDS = ["turn_a", "turn_b"]
SLICE_ORDER = LEAD_IDS + ANSWER_IDS + TURN_IDS

# Phrase track processing: deliberately clean, modest room, mono-compatible.
PHRASE_HIGHPASS_HZ = 120      # inside contracted 100..200
PHRASE_LOWPASS_HZ = 12000     # inside contracted 10000..14000
PHRASE_ROOM = 0.10

# Quantisation grid and timing-humanisation ceiling (contract: <= .035 beats).
GRID = 0.25
JITTER_MAX = 0.03


# ---------------------------------------------------------------------------
# Small deterministic helpers (pure - no random module / global state)
# ---------------------------------------------------------------------------
def _str_hash(text: str) -> int:
    h = 0
    for ch in text:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return h or 1


def _jitter(track_number: int, layer: str, bar: int, slot: int) -> float:
    """Return a deterministic timing offset in [-JITTER_MAX, JITTER_MAX]."""
    h = (track_number * 1_000_003 + _str_hash(layer) * 9176
         + bar * 37 + slot * 7 + 11) & 0xFFFFFFFF
    return ((h % 2001) - 1000) / 1000.0 * JITTER_MAX


def _ev(beat: float, vel: float, dur: float, note=None) -> dict:
    ev = {"beat": round(float(beat), 6), "velocity": round(float(vel), 6),
          "duration_beats": round(float(dur), 6)}
    if note is not None:
        ev["note"] = int(note)
    return ev


# ---------------------------------------------------------------------------
# Drum feels.  Layer -> list of (beat-in-bar, velocity, duration-beats).
# Hat / shaker layers are the only layers that receive timing humanisation.
# ---------------------------------------------------------------------------
_EIGHTHS = [i * 0.5 for i in range(8)]
_OFFBEATS = [0.5, 1.5, 2.5, 3.5]
_SIXTEENTHS = [i * 0.25 for i in range(16)]


def _vel_map(beats, vel, step=0.0):
    return [(b, max(0.2, vel - step * (i % 2)), 0.16) for i, b in enumerate(beats)]


FEELS = {
    # 01 / verse engine: classic boom bap, kick picks up the end of the bar.
    "boombap": {
        "kick": [(0, 1.0, 0.3), (2.5, 0.9, 0.25), (3.5, 0.7, 0.2)],
        "snare": [(1, 0.88, 0.2), (3, 0.95, 0.2)],
        "hat": _vel_map(_EIGHTHS, 0.62, 0.08),
    },
    # 02: staccato jazz pocket, rim instead of snare, sparse offbeat kick.
    "jazz": {
        "kick": [(0, 0.9, 0.25), (1.75, 0.7, 0.2)],
        "rim": [(1, 0.65, 0.12), (3, 0.75, 0.12)],
        "hat": [(b, 0.5, 0.14) for b in _OFFBEATS],
    },
    # 03: loose head-nod backbeat.
    "headnod": {
        "kick": [(0, 1.0, 0.3), (2.5, 0.85, 0.25)],
        "snare": [(1, 0.85, 0.2), (3, 0.92, 0.2)],
        "hat": _vel_map(_EIGHTHS, 0.56, 0.06),
    },
    # 04 main: urgent four-placement stomp with claps.
    "stomp": {
        "kick": [(0, 1.0, 0.3), (1.5, 0.85, 0.25), (2.5, 0.92, 0.25),
                 (3.5, 0.8, 0.2)],
        "clap": [(1, 0.88, 0.2), (3, 0.95, 0.25)],
        "hat": _vel_map(_EIGHTHS, 0.62, 0.07),
    },
    # 04 middle: bold switch into broken syncopation.
    "brokenswitch": {
        "kick": [(0, 1.0, 0.25), (1.75, 0.8, 0.2), (3.25, 0.75, 0.2)],
        "snare": [(3, 0.9, 0.2)],
        "rim": [(1, 0.7, 0.12)],
        "hat": [(b, 0.5, 0.14) for b in (1.5, 2.5, 3.5)],
    },
    # 05: modern half time, snare on beat 3 of the bar (zero-based offset 2),
    # airy open hat.
    "halftime": {
        "kick": [(0, 1.0, 0.3), (2.5, 0.72, 0.25)],
        "snare": [(2, 0.95, 0.25)],
        "hat": _vel_map(_EIGHTHS, 0.52, 0.06),
        "open_hat": [(1.5, 0.55, 0.35)],
    },
    # Sparse intimate layer (06 body / generic drop support).
    "intimate": {
        "kick": [(0, 0.85, 0.25)],
        "rim": [(1.5, 0.6, 0.12), (3, 0.7, 0.12)],
        "hat": [(2.5, 0.45, 0.14)],
    },
    # 06 return: warm normal backbeat, hats only on offbeats.
    "backbeat": {
        "kick": [(0, 1.0, 0.3), (2.5, 0.85, 0.25)],
        "snare": [(1, 0.9, 0.2), (3, 0.95, 0.2)],
        "hat": [(b, 0.55, 0.14) for b in _OFFBEATS],
    },
    # 07: bright broken funk with busy sixteenth hats and a clap on 3.
    "brokenfunk": {
        "kick": [(0, 1.0, 0.3), (1.75, 0.85, 0.22), (2.5, 0.8, 0.22),
                 (3.75, 0.82, 0.2)],
        "snare": [(1, 0.85, 0.2), (3, 0.9, 0.2)],
        "clap": [(3, 0.8, 0.2)],
        "hat": [(b, 0.44 + 0.16 * ((i + 1) % 2), 0.12)
                for i, b in enumerate(_SIXTEENTHS)],
    },
    # 08: clean playful bounce; rim pickup into beat-3 open hat.
    "bounce": {
        "kick": [(0, 1.0, 0.3), (1.5, 0.85, 0.25), (2.5, 0.9, 0.25),
                 (3.5, 0.8, 0.2)],
        "snare": [(1, 0.85, 0.2), (3, 0.9, 0.2)],
        "rim": [(2.75, 0.6, 0.12)],
        "hat": [(b, 0.55, 0.14) for b in (0.5, 1.5, 2.5)],
        "open_hat": [(3.5, 0.6, 0.35)],
    },
    # 09: UK-leaning two step with shaker bed and lifted open hat.
    "twostep": {
        "kick": [(0, 1.0, 0.3), (2.5, 0.9, 0.25)],
        "snare": [(1, 0.9, 0.2), (3, 0.95, 0.2)],
        "hat": [(b, 0.5, 0.14) for b in (0.5, 1.5)],
        "open_hat": [(3.5, 0.7, 0.4)],
        "shaker": _vel_map(_EIGHTHS, 0.42, 0.05),
    },
    # 10 / soft support: warm soul backbeat with a late kick pickup.
    "soul": {
        "kick": [(0, 1.0, 0.3), (2.75, 0.85, 0.25)],
        "snare": [(1, 0.85, 0.2), (3, 0.92, 0.2)],
        "hat": _vel_map(_EIGHTHS, 0.54, 0.06),
        "open_hat": [(1.5, 0.5, 0.3)],
    },
}

# Bass styles: layer -> per-bar (beat, duration) list. Monophonic by
# construction (next note starts at/after the previous one ends).
BASS_STYLES = {
    "none": [],
    "soft": [(0, 1.25)],
    "roots": [(0, 1.25), (2, 1.5)],
    "drive": [(0, 0.75), (1.25, 0.75), (2.5, 1.0)],
    "feature": [(0, 0.95), (1, 0.95), (2, 0.95), (3, 0.95)],
}


# ---------------------------------------------------------------------------
# Explicit arrangement recipes.
# A section tuple: (name, bars, phrases[(local-beat, slice-id, dur, vel)],
#                   feel, active-layers, velocity-scale, bass-style)
# Phrase durations never exceed that slice's target_beats; .5/.75/1 entries
# are deliberate retriggers of the same cut.
#
# Two explicit timeline conventions, never mixed inside one expression:
#   * bar *cells* - each cell lists phrases relative to its own start; joining
#     cells with :func:`_bars` (one-bar cells) or :func:`_seq` (custom span)
#     offsets cell i onto bar i instead of stacking every cell on bar 0;
#   * absolute phrase lists - literals already measured from the section start
#     (e.g. turns deliberately placed at local beats 0 and 8).  These are kept
#     verbatim.
# ---------------------------------------------------------------------------
def _pair(sid, aid, v=0.92):
    """Full question(downbeat)+answer(back half) bar."""
    return [(0, sid, 2, v), (2, aid, 2, v - 0.05)]


def _cells(*specs):
    """Sequence cells with explicit per-cell bar spans, back to back.

    Each spec is ``(span_bars, phrases)`` with phrases authored from that
    cell's own beat zero.  Nothing is dropped or quantised away: every phrase
    is retained and placed at its proper section time.
    """
    out = []
    cursor = 0.0
    for span_bars, cell in specs:
        for (local_beat, sid, dur, vel) in cell:
            out.append((round(cursor + local_beat, 6), sid, dur, vel))
        cursor += span_bars * 4
    return out


def _seq(step_beats: float, *cells):
    """Sequence equal-span cells: cell *i* is offset by ``i * step_beats``."""
    return _cells(*[(step_beats / 4.0, cell) for cell in cells])


def _bars(*cells):
    """Sequence one-bar phrase cells: cell *i* starts on bar *i* (4 beats)."""
    return _cells(*[(1, cell) for cell in cells])


RECIPES = {
    "01": {
        "keys": ("piano",),
        "sections": [
            ("intro", 2,
             [(0, "lead_a", 2, 1.0), (2, "answer_a", 2, 0.9)],
             "soul", ("kick", "hat"), 0.7, "soft"),
            ("verse", 6,
             _bars(
                 _pair("lead_a", "answer_a", 0.95),
                 _pair("lead_b", "answer_b", 0.9),
                 _pair("lead_c", "answer_c", 0.9),
                 _pair("lead_a", "answer_a", 0.95),
                 _pair("lead_d", "answer_d", 0.95),
                 _pair("lead_b", "answer_b", 0.9)),
             "boombap", ("kick", "snare", "hat"), 1.0, "roots"),
            ("drop", 4,
             [(0, "turn_a", 4, 1.0), (8, "turn_b", 4, 0.9)],
             "intimate", ("kick", "rim"), 0.85, "roots"),
            ("return", 6,
             _bars(
                 _pair("lead_a", "answer_a", 1.0),
                 _pair("lead_c", "answer_c", 0.9),
                 [(0, "lead_a", 0.75, 0.95), (0.75, "answer_a", 0.75, 0.9),
                  (1.5, "lead_b", 0.5, 0.85), (2, "answer_b", 2, 0.9)],
                 _pair("lead_d", "answer_d", 0.95),
                 _pair("lead_a", "answer_a", 1.0),
                 _pair("lead_c", "answer_c", 0.9)),
             "boombap", ("kick", "snare", "hat"), 1.0, "roots"),
            ("outro", 2,
             [(0, "lead_a", 2, 1.0)],
             "soul", ("kick",), 0.7, "soft"),
        ],
    },
    "02": {
        "keys": ("vibes",),
        "sections": [
            ("head", 4,
             _bars(
                 _pair("lead_b", "answer_b", 1.0),
                 _pair("lead_b", "answer_b", 0.9),
                 _pair("lead_c", "answer_c", 0.85)),
             "jazz", ("kick", "rim", "hat"), 0.75, "soft"),
            ("verse", 6,
             _bars(
                 [(0, "lead_a", 1, 0.9), (1.5, "answer_a", 1, 0.85)],
                 [(0, "lead_c", 1, 0.9), (2, "answer_c", 1, 0.85)],
                 [(0, "lead_b", 1, 0.9), (2, "answer_b", 0.75, 0.85),
                  (3, "lead_d", 1, 0.9)],
                 [(0, "lead_d", 1, 0.95), (1.5, "answer_d", 1, 0.9)],
                 [(0, "lead_a", 0.75, 0.9), (1, "lead_a", 0.75, 0.85),
                  (2.5, "answer_a", 0.75, 0.85)],
                 _pair("lead_c", "answer_c", 0.9)),
             "jazz", ("kick", "rim", "hat"), 1.0, "roots"),
            ("middle", 4,
             [(0, "turn_b", 4, 1.0), (8, "turn_a", 4, 0.9)],
             "twostep", ("kick", "shaker"), 0.75, "soft"),
            ("return", 4,
             _bars(
                 _pair("lead_b", "answer_b", 1.0),
                 [(0, "lead_a", 1, 0.9), (1.5, "answer_a", 1, 0.85)],
                 [(0, "lead_d", 1, 0.9), (2, "answer_d", 1, 0.85)],
                 [(0, "lead_c", 0.75, 0.9), (1, "answer_c", 0.75, 0.85),
                  (2, "lead_b", 1, 0.9)]),
             "jazz", ("kick", "rim", "hat"), 1.0, "roots"),
            ("outro", 2,
             [(0, "answer_b", 1, 0.9), (2, "answer_b", 0.75, 0.85)],
             "jazz", ("kick", "rim"), 0.7, "none"),
        ],
    },
    "03": {
        "keys": ("piano",),
        "sections": [
            ("intro", 2,
             _pair("lead_c", "answer_c", 1.0),
             "headnod", ("kick", "hat"), 0.7, "soft"),
            ("head", 6,
             _bars(
                 _pair("lead_a", "answer_a"),
                 _pair("lead_c", "answer_c"),
                 _pair("lead_b", "answer_b"),
                 _pair("lead_c", "answer_c"),
                 _pair("lead_d", "answer_d"),
                 _pair("lead_c", "answer_c")),
             "headnod", ("kick", "snare", "hat"), 1.0, "roots"),
            ("turn", 4,
             [(0, "turn_a", 4, 1.0), (8, "turn_b", 4, 0.9)],
             "intimate", ("kick", "rim"), 0.8, "roots"),
            ("return", 4,
             _bars(
                 _pair("lead_c", "answer_c", 0.95),
                 _pair("lead_a", "answer_a"),
                 _pair("lead_b", "answer_b"),
                 _pair("lead_c", "answer_c", 0.95)),
             "headnod", ("kick", "snare", "hat"), 1.0, "roots"),
            ("outro", 2,
             [(0, "lead_c", 2, 0.95)],
             "headnod", ("kick",), 0.6, "soft"),
        ],
    },
    "04": {
        "keys": ("vibes",),
        "sections": [
            ("intro", 2,
             _bars(
                 _pair("lead_d", "answer_d", 1.0),
                 [(0, "lead_d", 0.75, 1.0), (0.75, "lead_d", 0.75, 0.95),
                  (2, "answer_d", 0.75, 0.95), (2.75, "answer_d", 0.75, 0.9)]),
             "stomp", ("kick", "hat"), 0.85, "soft"),
            ("drive", 8,
             _bars(
                 _pair("lead_d", "answer_d"),
                 _pair("lead_a", "answer_a"),
                 _pair("lead_d", "answer_d"),
                 [(0, "lead_a", 1, 0.95), (1.5, "answer_a", 1, 0.9),
                  (3, "lead_d", 1, 0.95)],
                 _pair("lead_b", "answer_b"),
                 _pair("lead_d", "answer_d"),
                 _pair("lead_c", "answer_c"),
                 _pair("lead_d", "answer_d")),
             "stomp", ("kick", "clap", "hat"), 1.0, "drive"),
            ("switch", 6,
             [(0, "turn_b", 4, 1.0), (8, "lead_d", 2, 0.95),
              (10, "answer_d", 2, 0.9), (16, "turn_a", 4, 0.95)],
             "brokenswitch", ("kick", "snare", "rim", "hat"), 0.9, "roots"),
            ("return", 6,
             _bars(
                 _pair("lead_d", "answer_d", 1.0),
                 _pair("lead_a", "answer_a"),
                 [(0, "lead_d", 0.5, 1.0), (0.5, "lead_d", 0.5, 0.95),
                  (1, "answer_d", 1, 0.9), (2, "lead_d", 2, 0.95)],
                 _pair("lead_c", "answer_c"),
                 _pair("lead_d", "answer_d", 1.0),
                 _pair("lead_b", "answer_b")),
             "stomp", ("kick", "clap", "hat"), 1.0, "drive"),
            ("outro", 2,
             [(0, "lead_d", 1, 1.0)],
             "stomp", ("kick",), 0.7, "soft"),
        ],
    },
    "05": {
        "keys": ("piano", "vibes"),
        "sections": [
            ("intro", 4,
             [(0, "lead_a", 2, 1.0), (6, "answer_a", 2, 0.9),
              (8, "lead_b", 2, 0.9)],
             "halftime", ("kick", "hat"), 0.65, "soft"),
            ("verse", 8,
             # Two-bar cells (lead on the downbeat, answer late in bar 2): each
             # cell advances the timeline by 8 beats.
             _seq(8.0,
                  [(0, "lead_a", 2, 0.95), (6, "answer_a", 2, 0.9)],
                  [(0, "lead_c", 2, 0.9), (6, "answer_c", 2, 0.85)],
                  [(0, "lead_a", 2, 0.95), (6, "answer_a", 2, 0.9)],
                  [(0, "lead_d", 2, 0.9), (6, "answer_d", 2, 0.85)]),
             "halftime", ("kick", "snare", "hat", "open_hat"), 1.0, "roots"),
            ("middle", 8,
             [(8, "turn_a", 4, 1.0), (16, "lead_a", 2, 0.95),
              (18, "answer_a", 2, 0.9), (24, "turn_b", 4, 0.9)],
             "intimate", ("kick", "rim"), 0.75, "soft"),
            ("return", 6,
             _bars(
                 _pair("lead_a", "answer_a", 1.0),
                 _pair("lead_b", "answer_b"),
                 _pair("lead_a", "answer_a", 1.0),
                 _pair("lead_c", "answer_c"),
                 [(0, "lead_a", 1, 0.95), (2, "answer_a", 1, 0.9)],
                 _pair("lead_d", "answer_d")),
             "halftime", ("kick", "snare", "hat", "open_hat"), 1.0, "roots"),
            ("outro", 2,
             [(0, "lead_a", 2, 1.0)],
             "halftime", ("kick",), 0.6, "soft"),
        ],
    },
    "06": {
        "keys": ("piano",),
        "sections": [
            ("intro", 2,
             # Absolute 2-bar timeline.  The final answer sits on local beat 2
             # of bar 1 (section beat 6); an earlier draft spilled it into a
             # third bar (beat 10) that this section does not have.
             [(0, "lead_b", 1, 1.0), (2, "answer_b", 1, 0.9),
              (4, "lead_b", 0.75, 0.9), (5, "lead_b", 0.75, 0.85),
              (6, "answer_b", 0.75, 0.85)],
             "intimate", ("kick", "rim", "hat"), 0.8, "soft"),
            ("verse", 4,
             # Spans 1 + 2 + 1 tile the four declared bars.  The middle cell
             # is two bars: lead_b on its downbeat, answer_b on the second
             # bar's beat 1 (local beat 5) - the authored 5 is preserved.
             _cells(
                 (1, _pair("lead_c", "answer_c", 0.9)),
                 (2, [(0, "lead_b", 2, 0.95), (5, "answer_b", 1, 0.9)]),
                 (1, _pair("lead_a", "answer_a", 0.9))),
             "intimate", ("kick", "rim", "hat"), 0.9, "soft"),
            ("drop", 4,
             [(0, "turn_b", 4, 1.0), (8, "lead_b", 2, 0.95),
              (10, "answer_b", 2, 0.9)],
             "intimate", ("kick",), 0.85, "feature"),
            ("return", 6,
             _bars(
                 _pair("lead_b", "answer_b", 1.0),
                 _pair("lead_c", "answer_c"),
                 _pair("lead_a", "answer_a"),
                 _pair("lead_b", "answer_b", 1.0),
                 [(0, "lead_b", 0.75, 1.0), (0.75, "answer_b", 0.75, 0.9),
                  (2, "lead_c", 2, 0.9)],
                 _pair("lead_d", "answer_d")),
             "backbeat", ("kick", "snare", "hat"), 1.0, "roots"),
            ("outro", 2,
             [(0, "lead_b", 2, 1.0)],
             "backbeat", ("kick",), 0.7, "soft"),
        ],
    },
    "07": {
        "keys": ("vibes",),
        "sections": [
            ("intro", 2,
             _bars(
                 _pair("lead_c", "answer_c", 1.0),
                 [(0, "lead_c", 0.5, 1.0), (0.5, "lead_c", 0.5, 0.95),
                  (1, "answer_c", 1, 0.9), (2, "answer_c", 2, 0.9)]),
             "brokenfunk", ("kick", "hat"), 0.8, "soft"),
            ("funk", 8,
             _bars(
                 _pair("lead_c", "answer_c"),
                 _pair("lead_a", "answer_a"),
                 [(0, "lead_c", 0.75, 0.95), (0.75, "answer_c", 0.75, 0.9),
                  (2, "lead_d", 2, 0.9)],
                 _pair("lead_c", "answer_c"),
                 _pair("lead_b", "answer_b"),
                 [(0, "lead_d", 1, 0.95), (1.5, "answer_d", 1, 0.9),
                  (3, "lead_c", 1, 0.95)],
                 _pair("lead_c", "answer_c"),
                 _pair("lead_a", "answer_a")),
             "brokenfunk", ("kick", "snare", "clap", "hat"), 1.0, "drive"),
            ("switch", 6,
             [(0, "turn_a", 4, 1.0), (8, "lead_c", 2, 0.95),
              (10, "answer_c", 2, 0.9), (16, "turn_b", 4, 0.9)],
             "intimate", ("kick", "rim"), 0.75, "roots"),
            ("return", 6,
             # Six one-bar cells - _bars puts cell i on bar i; joining with +
             # stacked every cell on bar 0 (overlap at the section downbeat).
             _bars(
                 _pair("lead_c", "answer_c", 1.0),
                 [(0, "lead_c", 0.5, 1.0), (0.5, "answer_c", 0.5, 0.9),
                  (1, "lead_a", 1, 0.9), (2, "answer_a", 2, 0.9)],
                 _pair("lead_d", "answer_d"),
                 _pair("lead_c", "answer_c", 1.0),
                 _pair("lead_b", "answer_b"),
                 _pair("lead_c", "answer_c")),
             "brokenfunk", ("kick", "snare", "clap", "hat"), 1.0, "drive"),
            ("outro", 2,
             [(0, "lead_c", 1, 1.0)],
             "brokenfunk", ("kick",), 0.7, "soft"),
        ],
    },
    "08": {
        "keys": ("piano",),
        "sections": [
            ("intro", 2,
             # Two one-bar cells.  The final answer was left at the absolute
             # beat 10 of an older draft, outside this 2-bar section; as a
             # bar-local cell phrase it sits on local beat 2 (a .75 retrigger
             # of answer_d right after the beat-1 answer ends).
             _bars(
                 _pair("lead_d", "answer_d", 1.0),
                 [(0, "lead_d", 0.75, 0.95), (1, "answer_d", 1, 0.9),
                  (2, "answer_d", 0.75, 0.85)]),
             "bounce", ("kick", "rim", "hat"), 0.8, "soft"),
            ("bounce", 8,
             # Eight one-bar cells sequenced with _bars (cell i on bar i).
             _bars(
                 _pair("lead_d", "answer_d"),
                 _pair("lead_c", "answer_c"),
                 _pair("lead_a", "answer_a"),
                 _pair("lead_d", "answer_d"),
                 [(0, "lead_c", 1, 0.95), (1.5, "answer_c", 1, 0.9),
                  (3, "lead_d", 1, 0.95)],
                 _pair("lead_b", "answer_b"),
                 _pair("lead_d", "answer_d"),
                 _pair("lead_c", "answer_c")),
             "bounce", ("kick", "snare", "rim", "hat", "open_hat"), 1.0,
             "roots"),
            ("middle", 4,
             # Surprise stops: delayed turn, answer cut dead, late long phrase.
             [(1, "turn_b", 3, 1.0), (4, "answer_d", 2, 0.95),
              (10, "turn_a", 2, 0.95)],
             "brokenswitch", ("kick", "rim"), 0.8, "roots"),
            ("return", 4,
             # Four one-bar cells sequenced with _bars (cell i on bar i).
             _bars(
                 _pair("lead_d", "answer_d", 1.0),
                 _pair("lead_a", "answer_a"),
                 [(0, "lead_d", 0.5, 1.0), (0.5, "answer_d", 0.5, 0.9),
                  (1, "lead_c", 1, 0.9), (2, "answer_c", 2, 0.9)],
                 _pair("lead_d", "answer_d", 1.0)),
             "bounce", ("kick", "snare", "rim", "hat", "open_hat"), 1.0,
             "roots"),
            ("outro", 2,
             [(0, "answer_d", 1, 0.95)],
             "bounce", ("kick",), 0.7, "none"),
        ],
    },
    "09": {
        "keys": ("vibes",),
        "sections": [
            ("intro", 4,
             # turn_b is the prepared reverse-cut pickup in this collection.
             [(0, "turn_b", 2, 0.8), (2, "lead_a", 2, 1.0),
              (4, "turn_b", 2, 0.7), (6, "answer_a", 2, 0.9),
              (8, "lead_a", 2, 1.0)],
             "twostep", ("kick", "hat"), 0.7, "soft"),
            ("twostep", 8,
             # Eight one-bar cells sequenced with _bars (cell i on bar i).
             _bars(
                 _pair("lead_a", "answer_a"),
                 _pair("lead_b", "answer_b"),
                 [(0, "lead_d", 1, 0.95), (2, "answer_d", 1, 0.9)],
                 _pair("lead_a", "answer_a"),
                 _pair("lead_c", "answer_c"),
                 [(0, "lead_a", 0.75, 1.0), (0.75, "lead_a", 0.75, 0.9),
                  (2, "answer_a", 2, 0.9)],
                 _pair("lead_d", "answer_d"),
                 _pair("lead_a", "answer_a")),
             "twostep", ("kick", "snare", "hat", "open_hat", "shaker"), 1.0,
             "roots"),
            ("middle", 6,
             [(0, "turn_a", 4, 1.0), (8, "turn_b", 2, 0.75),
              (10, "lead_a", 2, 1.0), (16, "turn_b", 4, 0.9)],
             "intimate", ("kick", "rim"), 0.8, "roots"),
            ("return", 8,
             # Eight one-bar cells sequenced with _bars (cell i on bar i).
             # The first cell keeps the turn_b pickup into lead_a.
             _bars(
                 [(0, "turn_b", 2, 0.8), (2, "lead_a", 2, 1.0)],
                 _pair("lead_a", "answer_a", 1.0),
                 _pair("lead_d", "answer_d"),
                 _pair("lead_a", "answer_a", 1.0),
                 [(0, "lead_a", 0.5, 1.0), (0.5, "answer_a", 0.5, 0.9),
                  (1, "lead_b", 1, 0.9), (2, "answer_b", 2, 0.9)],
                 _pair("lead_c", "answer_c"),
                 _pair("lead_a", "answer_a", 1.0),
                 _pair("lead_b", "answer_b")),
             "twostep", ("kick", "snare", "hat", "open_hat", "shaker"), 1.0,
             "roots"),
            ("outro", 2,
             [(0, "lead_a", 2, 1.0)],
             "twostep", ("kick",), 0.7, "soft"),
        ],
    },
    "10": {
        "keys": ("piano",),
        "sections": [
            ("intro", 2,
             [(0, "turn_a", 4, 1.0)],
             "soul", ("kick", "hat"), 0.7, "soft"),
            ("verse", 6,
             # Six one-bar pair cells sequenced with _bars (cell i on bar i).
             _bars(
                 _pair("lead_a", "answer_a"),
                 _pair("lead_b", "answer_b"),
                 _pair("lead_a", "answer_a"),
                 _pair("lead_c", "answer_c"),
                 _pair("lead_d", "answer_d"),
                 _pair("lead_a", "answer_a")),
             "soul", ("kick", "snare", "hat", "open_hat"), 1.0, "roots"),
            ("middle", 4,
             [(0, "turn_b", 4, 1.0), (8, "turn_a", 4, 0.95)],
             "intimate", ("kick", "rim"), 0.8, "roots"),
            ("reprise", 6,
             # Six one-bar cells sequenced with _bars (cell i on bar i).
             _bars(
                 _pair("lead_a", "answer_a", 1.0),
                 [(0, "lead_a", 0.75, 1.0), (0.75, "answer_a", 0.75, 0.95),
                  (2, "lead_d", 2, 0.9)],
                 _pair("lead_a", "answer_a", 1.0),
                 [(0, "lead_b", 1, 0.95), (1.5, "answer_b", 1, 0.9),
                  (3, "lead_a", 1, 0.95)],
                 _pair("lead_a", "answer_a", 1.0),
                 _pair("lead_c", "answer_c")),
             "soul", ("kick", "snare", "hat", "open_hat"), 1.0, "roots"),
            ("outro", 2,
             [(0, "lead_a", 2, 1.0)],
             "soul", ("kick",), 0.7, "soft"),
        ],
    },
}


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
def _active_bass_midi(slices_by_id, placements, beat):
    """bass_midi of the most recently started (still/last active) cut."""
    current = None
    for pl in placements:
        if pl["beat"] <= beat:
            current = pl
        else:
            break
    if current is None:
        raise ValueError("Bass/keys scheduled before any phrase placement")
    return int(slices_by_id[current["slice_id"]]["bass_midi"])


def build_score(prepared: dict) -> dict:
    """Build one deterministic render-ready score from a prepared payload."""
    p = copy.deepcopy(prepared)
    number = str(p["number"])
    if number not in RECIPES:
        raise ValueError(f"No arrangement recipe for track number {number}")
    recipe = RECIPES[number]
    track_number = int(number)
    total_bars = int(p["bars"])

    slices_by_id = {s["id"]: s for s in p["slices"]}
    missing = [sid for sid in SLICE_ORDER if sid not in slices_by_id]
    if missing:
        raise ValueError(f"Prepared payload missing slices: {missing}")
    instruments_by_id = {i["id"]: i for i in p["instruments"]}

    # -- 1. phrase placements ------------------------------------------------
    raw_placements = []
    cursor = 0
    sections_out = []
    section_specs = []
    for (name, sec_bars, phrases, feel, layers, vscale, bass_style) \
            in recipe["sections"]:
        sections_out.append({"name": name, "start_bar": cursor,
                             "bars": sec_bars})
        section_specs.append((name, cursor, sec_bars, feel, layers, vscale,
                              bass_style))
        for (local_beat, sid, dur, vel) in phrases:
            if sid not in slices_by_id:
                raise ValueError(f"Recipe references unknown slice {sid}")
            target = float(slices_by_id[sid]["target_beats"])
            if not 0 < dur <= target:
                raise ValueError(
                    f"Phrase duration {dur} outside (0,{target}] for {sid}")
            if not 0 < vel <= 1:
                raise ValueError(f"Phrase velocity out of range: {vel}")
            if not 0 <= local_beat < sec_bars * 4:
                raise ValueError("Phrase placed outside its section")
            raw_placements.append(
                {"slice_id": sid, "beat": cursor * 4 + local_beat,
                 "duration_beats": float(dur), "velocity": float(vel)})
        cursor += sec_bars
    if cursor != total_bars:
        raise ValueError(
            f"Recipe spans {cursor} bars but prepared declares {total_bars}")

    placements = sorted(raw_placements, key=lambda x: (x["beat"], x["slice_id"]))
    # No simultaneous phrase windows: accompaniments baked into the cuts must
    # never collide.
    previous_end = 0.0
    for pl in placements:
        if pl["beat"] < previous_end - 1e-9:
            raise ValueError(
                f"Overlapping phrase windows near beat {pl['beat']}")
        previous_end = pl["beat"] + pl["duration_beats"]
    if placements and placements[-1]["beat"] + placements[-1]["duration_beats"] \
            > total_bars * 4 + 1e-9:
        raise ValueError("Phrase placement extends past the score")

    # Phrase events grouped per slice track.
    phrase_events = {sid: [] for sid in SLICE_ORDER}
    for pl in placements:
        phrase_events[pl["slice_id"]].append(
            _ev(pl["beat"], pl["velocity"], pl["duration_beats"], note=60))
    used_slices = [sid for sid in SLICE_ORDER if phrase_events[sid]]

    # Bars that contain a phrase: keys may only live in the other bars.
    phrase_bars = {int(pl["beat"] // 4) for pl in placements}

    # -- 2. kit events --------------------------------------------------------
    kit_events = {iid: [] for iid in instruments_by_id}
    for (name, start_bar, sec_bars, feel, layers, vscale, bass_style) \
            in section_specs:
        feel_map = FEELS[feel]
        for layer in layers:
            if layer not in feel_map:
                raise ValueError(f"Feel {feel} has no layer {layer}")
            if layer not in instruments_by_id:
                raise ValueError(f"Prepared payload missing instrument {layer}")
            for bar in range(start_bar, start_bar + sec_bars):
                for slot, (local, vel, dur) in enumerate(feel_map[layer]):
                    beat = bar * 4 + local
                    if layer in ("hat", "shaker") and local % 4 != 0:
                        beat += _jitter(track_number, layer, bar, slot)
                    kit_events[layer].append(
                        _ev(beat, min(1.0, vel * vscale), dur))
    for evs in kit_events.values():
        evs.sort(key=lambda e: e["beat"])

    # -- 3. bass (follows the most recently active cut) ----------------------
    bass_events = []
    for (name, start_bar, sec_bars, feel, layers, vscale, bass_style) \
            in section_specs:
        for bar in range(start_bar, start_bar + sec_bars):
            for (local, dur) in BASS_STYLES[bass_style]:
                beat = bar * 4 + local
                note = _active_bass_midi(slices_by_id, placements, beat)
                bass_events.append(_ev(beat, 0.85 * vscale, dur, note=note))
    bass_events.sort(key=lambda e: e["beat"])

    # -- 4. sparse root/fifth keys, strictly in phrase-free bars -------------
    keys_events = {"piano": [], "vibes": []}
    gap_index = 0
    for bar in range(total_bars):
        if bar in phrase_bars:
            continue
        chosen = recipe["keys"][gap_index % len(recipe["keys"])]
        anchor = _active_bass_midi(slices_by_id, placements, bar * 4)
        # root on the back of beat 1, fifth on beat 3; sparse, non-overlapping.
        keys_events[chosen].append(_ev(bar * 4 + 1, 0.55, 0.75,
                                       note=anchor + 12))
        keys_events[chosen].append(_ev(bar * 4 + 3, 0.5, 1.0,
                                       note=anchor + 19))
        gap_index += 1

    # -- 5. assemble tracks ---------------------------------------------------
    tracks = []
    for sid in used_slices:
        sl = slices_by_id[sid]
        tracks.append({
            "id": sid,
            "name": sid.replace("_", " "),
            "sample": sl["file"],
            "root_midi": 60,
            "gain_db": float(sl["gain_db"]),
            "drum": False,
            "highpass_hz": PHRASE_HIGHPASS_HZ,
            "lowpass_hz": PHRASE_LOWPASS_HZ,
            "room": PHRASE_ROOM,
            "pan": 0.0,
            "events": phrase_events[sid],
        })
    for inst in p["instruments"]:
        iid = inst["id"]
        if iid == "bass":
            evs = bass_events
        elif iid in keys_events:
            evs = keys_events[iid]
        else:
            evs = list(kit_events.get(iid, []))
        if not evs:
            continue
        track = copy.deepcopy(inst)
        track["events"] = evs
        tracks.append(track)

    # Middle switch metadata: the section explicitly named as the contrasting
    # middle (drop / middle / turn / switch).
    middle_names = {"drop", "middle", "turn", "switch"}
    middle = next((s for s in sections_out if s["name"] in middle_names),
                  sections_out[len(sections_out) // 2])

    score = {
        "title": p["title"],
        "bpm": float(p["bpm"]),
        "bars": total_bars,
        "sample_root": p["sample_root"],
        "tail_seconds": TAIL_SECONDS,
        "fade_seconds": FADE_SECONDS,
        "license": {
            "type": "provenance-only",
            "status": "release-pending",
            "note": "Audition draft assembled from prepared cuts; musical "
                    "clearance and any release remain pending human/source "
                    "review.",
        },
        "sections": sections_out,
        "metadata": {
            "collection_id": COLLECTION_ID,
            "track_number": track_number,
            "direction": p.get("direction", ""),
            "keep": None,
            "middle_switch": {
                "section": middle["name"],
                "bar": middle["start_bar"],
                "beat": middle["start_bar"] * 4,
            },
        },
        "sample_flip": {
            "slices": p["slices"],
            "placements": [
                {"slice_id": pl["slice_id"], "beat": pl["beat"],
                 "duration_beats": pl["duration_beats"],
                 "velocity": pl["velocity"], "note": 60}
                for pl in placements
            ],
            "source_records": p["source_records"],
        },
        "tracks": tracks,
    }
    return score
