"""Feedback-driven revision scores (v2) for the city-afterimages collection.

Pure deterministic entry point :func:`build_score` takes the same *prepared*
payload accepted by :mod:`examples.ten_beat_scores`, builds the frozen v1 score
with the original builder, deep-copies it and then applies one of ten explicit
feedback directions.  The original module and its globals are never mutated and
the prepared payload is never touched (the original builder already deep-copies
its input; this module only reads the returned score).

Every revision is hand-authored: new drum patterns, explicit call/answer phrase
grids, deliberate rests and per-track gain moves - there is no generic
every-nth-event thinning or uniform volume pass.
"""
from __future__ import annotations

import copy

from examples import ten_beat_scores as orig

COLLECTION_ID = "city-afterimages-20261013-v2"
VERSION = "v2"

DRUM_IDS = ("kick", "snare", "hat", "open_hat", "shaker", "rim", "clap")
_KEY_IDS = ("piano", "vibes")

# ---------------------------------------------------------------------------
# Concise Chinese revision summaries (one per contracted direction)
# ---------------------------------------------------------------------------
SUMMARIES = {
    "01": "保留主旋律动机，鼓组重构为半拍 drill 式切分：底鼓强弱反差、后置军鼓、"
          "偶发踩镲滚奏与段落变化，层次克制不堆叠。",
    "02": "保留钢琴爵士切片音色，中段改为重复可辨识的应答句并刻意留白，减弱鼓组与"
          "附加键盘遮掩，突出采样本身。",
    "03": "保留主要律动与全部乐句位置，附加鼓组整体降4dB、贝斯降2dB，仅在转折与"
          "再现段做小幅鼓组变化。",
    "04": "保留背景素材动机，移除顿足/拍手墙与密集踩镲，改为稀疏克制的支撑，附加"
          "鼓组整体降6dB，不替换素材。",
    "05": "附加鼓组重设为清晰半拍锚点与两小节交替口袋，稀疏踩镲与过门，减弱附加"
          "键盘与贝斯密度，安排静默转折后再现。",
    "06": "附加鼓组降6dB并降低密度，采样切换简化为可重复的应答动机与停顿，不再十"
          "切片轮替，移除干扰性附加键盘。",
    "07": "保留爵士切片身份，重组为两小节重复问答，减少随意切换与踩镲/拍手叠加，"
          "无需用尽全部切片。",
    "08": "冻结全部乐句位置、文件、增益、处理及贝斯/键盘轨，仅修改鼓组形态与增益，"
          "呈现克制弹跳、偶发过门与停顿。",
    "09": "除附加鼓组降2dB、贝斯降1dB外，全部事件、采样设置、乐句与编排冻结。",
    "10": "采用长乐句与可辨识主题的多次再现，减少素材切换，减弱附加键盘与鼓组密度。",
}


# ---------------------------------------------------------------------------
# Shared revision helpers
# ---------------------------------------------------------------------------
def _bounds(score):
    return {s["name"]: (s["start_bar"] * 4,
                        (s["start_bar"] + s["bars"]) * 4)
            for s in score["sections"]}


def _render_drums(score, specs, gain_db_delta=0.0):
    """Replace drum events with hand-authored per-section patterns.

    Spec per section: {"layers": {layer: [(local, vel, dur), ...]},
                       "vel": section velocity scale (optional),
                       "fills": {bar_index: {layer: replacement events}},
                       "only_bars": {layer: [bar indices allowed]},
                       "silent_bars": [bar indices],
                       "bars": {bar_index: {layer: events}}}.
    A layer pattern may be ``{"a": [...], "b": [...]}`` for a two-bar pocket.
    Hat/shaker timing reuses the original deterministic humaniser.
    Layers that end up empty remove their track, matching v1 format.
    """
    number = int(score["metadata"]["track_number"])
    made = {}
    for sec in score["sections"]:
        spec = specs[sec["name"]]
        for bi in range(sec["bars"]):
            bar = sec["start_bar"] + bi
            if "bars" in spec and bi in spec["bars"]:
                active = spec["bars"][bi]
            elif bi in spec.get("silent_bars", ()):
                active = {}
            else:
                active = {}
                for layer, pat in spec["layers"].items():
                    allowed = spec.get("only_bars", {}).get(layer)
                    if allowed is not None and bi not in allowed:
                        continue
                    active[layer] = (pat["b"] if bi & 1 else pat["a"]) \
                        if isinstance(pat, dict) else pat
            fills = spec.get("fills", {}).get(bi, {})
            for layer, evs in active.items():
                if layer in fills:
                    evs = fills[layer]
                for slot, (local, vel, dur) in enumerate(evs):
                    beat = bar * 4 + local
                    if layer in ("hat", "shaker") and local % 4 != 0:
                        beat += orig._jitter(number, layer, bar, slot)
                    made.setdefault(layer, []).append(
                        orig._ev(beat, min(1.0, vel * spec.get("vel", 1.0)),
                                 dur))
    by_id = {t["id"]: t for t in score["tracks"]}
    for layer in DRUM_IDS:
        evs = made.get(layer, [])
        evs.sort(key=lambda e: e["beat"])
        track = by_id.get(layer)
        if evs:
            if track is None:
                raise ValueError(f"Revised drum layer {layer} has no instrument")
            track["events"] = evs
            if gain_db_delta:
                track["gain_db"] = round(float(track["gain_db"])
                                         + gain_db_delta, 6)
        elif track is not None:
            score["tracks"].remove(track)
            by_id.pop(layer)


def _rephrase(score, per_section):
    """Replace phrase events/placements from per-section local-beat grids.

    Grids are validated (section bounds, target_beats, velocity, no overlap,
    inside song), phrase tracks are rebuilt from v1 templates and
    ``sample_flip.placements`` is regenerated to mirror the slice events.
    """
    slices_by_id = {s["id"]: s for s in score["sample_flip"]["slices"]}
    bounds = _bounds(score)
    replaced = set(per_section)
    # Keep v1 placements in every section that this revision does not name.
    raw = [(p["beat"], p["slice_id"], p["duration_beats"], p["velocity"])
           for p in score["sample_flip"]["placements"]
           if not any(a - 1e-9 <= p["beat"] < b
                      for n, (a, b) in bounds.items() if n in replaced)]
    for name, phrases in per_section.items():
        start, end = bounds[name]
        for local, sid, dur, vel in phrases:
            if sid not in slices_by_id:
                raise ValueError(f"Revision references unknown slice {sid}")
            if not 0 < vel <= 1 or not 0 <= local or local + dur > end - start:
                raise ValueError(f"Phrase outside section {name}: {sid}")
            if not 0 < dur <= float(slices_by_id[sid]["target_beats"]):
                raise ValueError(f"Phrase duration exceeds target for {sid}")
            raw.append((start + local, sid, float(dur), float(vel)))
    raw.sort(key=lambda r: (r[0], r[1]))
    prev_end = 0.0
    for beat, _sid, dur, _vel in raw:
        if beat < prev_end - 1e-9:
            raise ValueError("Revised phrases overlap")
        prev_end = beat + dur
    if prev_end > score["bars"] * 4 + 1e-9:
        raise ValueError("Revised phrases extend past the score")

    new_beats = {start + local for name, phrases in per_section.items()
                 for local, _sid, _dur, _vel in phrases
                 for start in [bounds[name][0]]}
    kept = [p for p in score["sample_flip"]["placements"]
            if p["beat"] not in new_beats
            and not any(a - 1e-9 <= p["beat"] < b
                        for n, (a, b) in bounds.items() if n in replaced)]
    placements = kept + [
        {"slice_id": sid, "beat": round(beat, 6),
         "duration_beats": round(dur, 6), "velocity": round(vel, 6),
         "note": 60}
        for beat, sid, dur, vel in raw
        if any(n in replaced and bounds[n][0] - 1e-9 <= beat < bounds[n][1]
               for n in per_section)]
    placements.sort(key=lambda p: (p["beat"], p["slice_id"]))
    score["sample_flip"]["placements"] = placements

    events = {sid: [] for sid in orig.SLICE_ORDER}
    for pl in placements:
        events[pl["slice_id"]].append(
            orig._ev(pl["beat"], pl["velocity"], pl["duration_beats"], 60))

    templates = {t["id"]: t for t in score["tracks"]
                 if t.get("drum") is False}
    inst_tracks = [t for t in score["tracks"] if t.get("drum") is not False]
    new_phrase = []
    for sid in orig.SLICE_ORDER:
        if not events[sid]:
            continue
        tr = templates.get(sid)
        if tr is None:  # fallback: synthesise the same v1 track shape
            sl = slices_by_id[sid]
            tr = {"id": sid, "name": sid.replace("_", " "),
                  "sample": sl["file"], "root_midi": 60,
                  "gain_db": float(sl["gain_db"]), "drum": False,
                  "highpass_hz": orig.PHRASE_HIGHPASS_HZ,
                  "lowpass_hz": orig.PHRASE_LOWPASS_HZ,
                  "room": orig.PHRASE_ROOM, "pan": 0.0}
        tr = copy.deepcopy(tr)
        tr["events"] = events[sid]
        new_phrase.append(tr)
    score["tracks"] = new_phrase + inst_tracks

    # Retune existing bass/key notes to the active *revised* slice (beats,
    # durations and velocities are kept; only the inferred pitch changes).
    for tr in score["tracks"]:
        if tr["id"] == "bass":
            for e in tr["events"]:
                e["note"] = orig._active_bass_midi(slices_by_id, placements,
                                                   e["beat"])
        elif tr["id"] in _KEY_IDS:
            for e in tr["events"]:
                anchor = orig._active_bass_midi(slices_by_id, placements,
                                                e["beat"])
                # v1 vocabulary: root (+12) on local beat 1, fifth (+19) on 3
                e["note"] = anchor + (19 if abs((e["beat"] - 3) % 4) < 1e-6
                                      else 12)


def _drop_keys_over_phrases(score):
    """Never keep an added pitched key event on top of a phrase window."""
    windows = [(p["beat"], p["beat"] + p["duration_beats"])
               for p in score["sample_flip"]["placements"]]
    for tr in score["tracks"]:
        if tr["id"] not in _KEY_IDS:
            continue
        tr["events"] = [e for e in tr["events"]
                        if not any(a - 1e-9 <= e["beat"] < b - 1e-9
                                   for a, b in windows)]


def _remove_keys(score):
    score["tracks"] = [t for t in score["tracks"] if t["id"] not in _KEY_IDS]


def _roots_only_keys(score):
    """Keep only the back-of-one root; drop the beat-3 fifth."""
    for tr in score["tracks"]:
        if tr["id"] in _KEY_IDS:
            tr["events"] = [e for e in tr["events"]
                            if abs((e["beat"] - 1) % 4) < 1e-6]


def _downbeat_bass(score):
    """Explicit 'roots on the downbeat' reduction; still monophonic."""
    for tr in score["tracks"]:
        if tr["id"] == "bass":
            tr["events"] = [e for e in tr["events"]
                            if abs(e["beat"] % 4) < 1e-6]


def _drum_tracks(score):
    return [t for t in score["tracks"] if t["id"] in DRUM_IDS]


# ---------------------------------------------------------------------------
# 01 - drill-influenced half-time rebuild, motif retained
# ---------------------------------------------------------------------------
_DRILL_HATS = [(.5, .5, .12), (1.5, .48, .12), (2.5, .46, .12), (3.5, .5, .12)]
_ROLL = [(3, .5, .1), (3.25, .46, .1), (3.5, .42, .1), (3.75, .4, .1)]


def _r01(score):
    specs = {
        "intro": {"layers": {
            "kick": [(0, .9, .3)], "snare": [(2, .72, .2)],
            "hat": [(1.5, .4, .12), (3.5, .4, .12)]}},
        "verse": {"layers": {
            "kick": [(0, 1.0, .3), (2.75, .92, .24), (3.5, .5, .18)],
            "snare": [(2, .92, .24)], "hat": _DRILL_HATS},
            "fills": {3: {"hat": _ROLL}}},
        "drop": {"layers": {
            "kick": [(0, .95, .3), (3.5, .5, .18)],
            "snare": [(2, .85, .22)], "hat": [(2.5, .4, .12)]}},
        "return": {"layers": {
            "kick": [(0, 1.0, .3), (2.75, .9, .24), (3.5, .52, .18)],
            "snare": [(2, .95, .24)], "hat": _DRILL_HATS},
            "fills": {5: {"hat": _ROLL}}},
        "outro": {"layers": {
            "kick": [(0, .8, .3)], "snare": [(2, .6, .2)]}},
    }
    _render_drums(score, specs)


# ---------------------------------------------------------------------------
# 02 - repeated call/answer middle with gaps, sampling focus
# ---------------------------------------------------------------------------
def _r02(score):
    _rephrase(score, {"middle": [
        (0, "lead_b", 2, 1.0), (2, "answer_b", 1, .9),     # call + short answer
        (4, "lead_b", 2, .95), (6, "answer_b", 1, .85),   # recognisable repeat
        # bar 2 is a deliberate full-bar rest
        (12, "lead_b", 2, 1.0), (14, "answer_b", 2, .95),
    ]})
    _remove_keys(score)
    specs = {
        "head": {"layers": {
            "kick": [(0, .8, .25), (2.5, .55, .2)], "rim": [(2, .6, .12)],
            "hat": [(1.5, .4, .12), (3.5, .4, .12)]}},
        "verse": {"layers": {
            "kick": [(0, .85, .25)], "rim": [(2, .62, .12)],
            "hat": [(1.5, .4, .12), (3.5, .4, .12)]}},
        "middle": {"layers": {"kick": [(0, .6, .22)]},
                   "only_bars": {"kick": [0, 1, 3]}},
        "return": {"layers": {
            "kick": [(0, .82, .25), (2.5, .55, .2)], "rim": [(2, .62, .12)],
            "hat": [(1.5, .4, .12), (3.5, .4, .12)]}},
        "outro": {"layers": {
            "kick": [(0, .6, .22)], "rim": [(2, .5, .12)]}},
    }
    _render_drums(score, specs)


# ---------------------------------------------------------------------------
# 03 - frozen phrases / primary kit timing; gains -4/-2, small variations
# ---------------------------------------------------------------------------
def _r03(score):
    for tr in _drum_tracks(score):
        tr["gain_db"] = round(float(tr["gain_db"]) - 4, 6)
    for tr in score["tracks"]:
        if tr["id"] == "bass":
            tr["gain_db"] = round(float(tr["gain_db"]) - 2, 6)
    start, bars = next((s["start_bar"], s["bars"])
                       for s in score["sections"] if s["name"] == "turn")
    last_bar = (start + bars - 1) * 4
    # Variation 1: drop the rim colour in the final bar of the turn (gap).
    for tr in score["tracks"]:
        if tr["id"] == "rim":
            tr["events"] = [e for e in tr["events"]
                            if not (last_bar <= e["beat"] < last_bar + 4)]
    # Variation 2: soft hat double in the last return bar, before beat 3.5.
    ret = next(s for s in score["sections"] if s["name"] == "return")
    anchor = (ret["start_bar"] + ret["bars"] - 1) * 4
    for tr in score["tracks"]:
        if tr["id"] == "hat":
            tr["events"].append(orig._ev(anchor + 3.0, .36, .1))
            tr["events"].append(orig._ev(anchor + 3.25, .32, .1))
            tr["events"].sort(key=lambda e: e["beat"])


# ---------------------------------------------------------------------------
# 04 - sparse support, no stomp/clap wall, drums >= 6 dB lower
# ---------------------------------------------------------------------------
def _r04(score):
    specs = {
        "intro": {"layers": {
            "kick": [(0, .7, .25)], "hat": [(2.5, .35, .12)]}},
        "drive": {"layers": {
            "kick": [(0, .8, .25), (3.5, .5, .2)], "rim": [(2, .55, .12)],
            "hat": [(1.5, .35, .12), (3.5, .35, .12)]}},
        "switch": {"layers": {
            "kick": [(0, .7, .25)], "rim": [(2, .5, .12)],
            "hat": [(3.5, .35, .12)]}},
        "return": {"layers": {
            "kick": [(0, .8, .25), (3.5, .5, .2)], "rim": [(2, .6, .12)],
            "hat": [(1.5, .35, .12)]}},
        "outro": {"layers": {"kick": [(0, .6, .25)]}},
    }
    _render_drums(score, specs, gain_db_delta=-6.0)


# ---------------------------------------------------------------------------
# 05 - half-time anchor, two-bar pocket, quiet turn, less key/bass density
# ---------------------------------------------------------------------------
def _r05(score):
    hats = {"a": [(.5, .4, .12), (1.5, .4, .12), (3.5, .42, .12)],
            "b": [(.5, .4, .12), (2.5, .4, .12), (3.5, .42, .12)]}
    fills = {3: {"hat": _ROLL}, 7: {"hat": _ROLL}}
    specs = {
        "intro": {"layers": {
            "kick": [(0, .9, .3)], "snare": [(2, .72, .22)],
            "hat": [(1.5, .38, .12), (3.5, .4, .12)]}},
        "verse": {"layers": {
            "kick": {"a": [(0, 1.0, .3), (3.5, .6, .22)],
                     "b": [(0, .9, .3), (1.75, .55, .2), (3.5, .6, .22)]},
            "snare": [(2, .9, .24)], "hat": hats}, "fills": fills},
        # Intentional quiet turn: kick-only bars, two silent bars, then a
        # restrained rim pickup hinting the return.
        "middle": {"layers": {
            "kick": [(0, .5, .25)], "rim": [(2, .4, .12)]},
            "only_bars": {"kick": [0, 1, 4, 5, 6, 7],
                          "rim": [4, 5, 6, 7]}},
        "return": {"layers": {
            "kick": {"a": [(0, 1.0, .3), (3.5, .6, .22)],
                     "b": [(0, .9, .3), (1.75, .55, .2), (3.5, .6, .22)]},
            "snare": [(2, .9, .24)], "hat": hats},
            "fills": {5: {"hat": _ROLL}}},
        "outro": {"layers": {
            "kick": [(0, .8, .3)], "snare": [(2, .6, .2)]}},
    }
    _render_drums(score, specs)
    _downbeat_bass(score)
    _roots_only_keys(score)


# ---------------------------------------------------------------------------
# 06 - repeatable motif with pauses, drums -6 dB and thinner, no key bed
# ---------------------------------------------------------------------------
def _r06(score):
    _rephrase(score, {
        # intro kept: already a repeated lead_b/answer_b motif
        "verse": [
            (0, "lead_b", 2, .95), (2, "answer_b", 2, .9),
            # bar 1 rest
            (8, "lead_b", 2, .95), (10, "answer_b", 1, .9),
            (12, "lead_b", 1, .9), (14, "answer_b", 1, .85),
        ],
        "drop": [
            (0, "turn_b", 4, 1.0),          # single long colour
            # bar 1 rest
            (8, "lead_b", 2, .95), (10, "answer_b", 2, .9),
            # bar 3 rest
        ],
        "return": [
            (0, "lead_b", 2, 1.0), (2, "answer_b", 2, .9),
            (8, "lead_b", 2, .95), (10, "answer_b", 2, .9),
            (16, "lead_b", 2, 1.0), (18, "answer_b", 2, .95),
        ],
        # outro kept: lead_b statement
    })
    _remove_keys(score)
    specs = {
        "intro": {"layers": {
            "kick": [(0, .7, .25)], "rim": [(2, .5, .12)],
            "hat": [(3, .35, .12)]}},
        "verse": {"layers": {
            "kick": [(0, .75, .25)], "rim": [(2, .55, .12)]}},
        "drop": {"layers": {"kick": [(0, .7, .25)]}},
        "return": {"layers": {
            "kick": [(0, .8, .25), (2.5, .55, .2)], "snare": [(2, .7, .2)],
            "hat": [(3.5, .4, .12)]}},
        "outro": {"layers": {"kick": [(0, .65, .25)]}},
    }
    _render_drums(score, specs, gain_db_delta=-6.0)


# ---------------------------------------------------------------------------
# 07 - repeated two-bar question/answer, no clap doubling, thinner hats
# ---------------------------------------------------------------------------
def _r07(score):
    _rephrase(score, {
        # intro kept
        "funk": [
            (0, "lead_c", 2, .95), (4, "answer_c", 2, .9),
            (8, "lead_c", 2, .95), (12, "answer_c", 2, .9),
            (16, "lead_c", 2, .95), (20, "answer_c", 2, .9),
            (24, "lead_c", 2, .95), (28, "lead_a", 2, .9),
        ],
        "switch": [
            (0, "turn_a", 4, 1.0),
            # bar 1 rest
            (8, "lead_c", 2, .95), (12, "answer_c", 2, .9),
            (16, "lead_c", 2, .9), (20, "answer_c", 2, .9),
        ],
        "return": [
            (0, "lead_c", 2, 1.0), (4, "answer_c", 2, .9),
            (8, "lead_c", 2, .95), (12, "answer_c", 2, .9),
            (16, "lead_a", 2, .95), (20, "answer_a", 2, .9),
        ],
        # outro kept
    })
    specs = {
        "intro": {"layers": {
            "kick": [(0, .85, .25), (2.5, .6, .2)], "snare": [(2, .75, .2)],
            "hat": [(1.5, .4, .12), (3.5, .4, .12)]}},
        "funk": {"layers": {
            "kick": {"a": [(0, .95, .3)],
                     "b": [(0, .9, .3), (2.5, .6, .22)]},
            "snare": [(2, .85, .22)],
            "hat": [(1.5, .4, .12), (3.5, .4, .12)]},
            "fills": {7: {"hat": _ROLL}}},
        "switch": {"layers": {
            "kick": [(0, .7, .25)], "rim": [(2, .5, .12)],
            "hat": [(3.5, .35, .12)]}},
        "return": {"layers": {
            "kick": {"a": [(0, .95, .3)],
                     "b": [(0, .9, .3), (2.5, .6, .22)]},
            "snare": [(2, .85, .22)],
            "hat": [(1.5, .4, .12), (3.5, .4, .12)]},
            "fills": {5: {"hat": _ROLL}}},
        "outro": {"layers": {"kick": [(0, .7, .25)]}},
    }
    _render_drums(score, specs)


# ---------------------------------------------------------------------------
# 08 - everything frozen except drum patterns/gains: bounce, fills, stops
# ---------------------------------------------------------------------------
def _r08(score):
    bounce = {
        "kick": [(0, .95, .3), (2.5, .8, .22)],
        "snare": [(1, .85, .2), (3, .9, .2)],
        "hat": [(1.5, .4, .12), (3.5, .4, .12)]}
    specs = {
        "intro": {"layers": {
            "kick": [(0, .9, .3), (2.5, .7, .22)],
            "snare": [(1, .8, .2), (3, .85, .2)], "hat": [(2.5, .4, .12)]}},
        "bounce": {"layers": bounce,
                   "fills": {7: {"hat": _ROLL}}},
        # Surprise stops: groove bar, kick-only bar, full stop, rim pickup.
        "middle": {"bars": {
            0: {"kick": [(0, .8, .25)], "rim": [(2, .5, .12)]},
            1: {"kick": [(0, .7, .25)]},
            2: {},
            3: {"rim": [(2, .55, .12)]}}},
        "return": {"layers": bounce, "fills": {3: {"hat": _ROLL}}},
        "outro": {"layers": {
            "kick": [(0, .75, .25)], "snare": [(3, .7, .2)]}},
    }
    _render_drums(score, specs, gain_db_delta=-2.0)


# ---------------------------------------------------------------------------
# 09 - gains only: drums -2 dB, bass -1 dB; every event frozen
# ---------------------------------------------------------------------------
def _r09(score):
    for tr in _drum_tracks(score):
        tr["gain_db"] = round(float(tr["gain_db"]) - 2, 6)
    for tr in score["tracks"]:
        if tr["id"] == "bass":
            tr["gain_db"] = round(float(tr["gain_db"]) - 1, 6)


# ---------------------------------------------------------------------------
# 10 - long phrases, recurring theme, fewer switches, thinner support
# ---------------------------------------------------------------------------
def _r10(score):
    _rephrase(score, {
        # intro kept: long turn_a
        "verse": [
            (0, "lead_a", 2, .95), (2, "answer_a", 2, .9),
            (4, "lead_a", 2, .95), (6, "answer_a", 2, .9),
            (8, "turn_a", 4, 1.0),
            (12, "lead_a", 2, .9), (14, "answer_a", 2, .9),
            (16, "lead_a", 2, .9), (18, "answer_a", 2, .9),
            (20, "lead_a", 2, .95),
        ],
        "middle": [
            (0, "turn_b", 4, .95),
            # bar 1 rest
            (8, "turn_a", 4, .95),
            # bar 3 rest
        ],
        "reprise": [
            (0, "lead_a", 2, 1.0), (2, "answer_a", 2, .9),
            (4, "lead_a", 2, .95), (6, "answer_a", 2, .9),
            (8, "turn_a", 4, 1.0),
            (12, "lead_a", 2, .9), (14, "answer_a", 2, .9),
            (16, "lead_a", 2, .95), (18, "answer_a", 2, .9),
            (20, "lead_a", 2, 1.0),
        ],
        # outro kept
    })
    _roots_only_keys(score)
    verse = {
        "kick": [(0, .92, .3), (2.75, .62, .22)],
        "snare": [(1, .82, .2), (3, .88, .2)],
        "hat": [(1.5, .4, .12), (3.5, .4, .12)]}
    specs = {
        "intro": {"layers": {
            "kick": [(0, .8, .3)], "hat": [(2.5, .38, .12)]}},
        "verse": {"layers": verse},
        "middle": {"layers": {
            "kick": [(0, .7, .25)], "rim": [(2, .5, .12)]},
            "only_bars": {"kick": [0, 2], "rim": [0, 2]}},
        "reprise": {"layers": verse},
        "outro": {"layers": {"kick": [(0, .7, .3)]}},
    }
    _render_drums(score, specs)


REVISIONS = {"01": _r01, "02": _r02, "03": _r03, "04": _r04, "05": _r05,
             "06": _r06, "07": _r07, "08": _r08, "09": _r09, "10": _r10}


# ---------------------------------------------------------------------------
# Final validation (structural mirrors of the contracted constraints)
# ---------------------------------------------------------------------------
def _validate(score):
    placements = score["sample_flip"]["placements"]
    slices_by_id = {s["id"]: s for s in score["sample_flip"]["slices"]}
    total = score["bars"] * 4
    bounds = _bounds(score)

    last_end = 0.0
    prev_beat = -1.0
    for pl in placements:
        assert pl["beat"] >= prev_beat - 1e-9, "placements not sorted"
        prev_beat = pl["beat"]
        sec = next((name for name, (a, b) in bounds.items()
                    if a - 1e-9 <= pl["beat"] < b), None)
        assert sec is not None, "placement outside any section"
        a, b = bounds[sec]
        assert a - 1e-9 <= pl["beat"] and pl["beat"] + pl["duration_beats"] \
            <= b + 1e-9, "placement crosses section boundary"
        assert 0 < pl["duration_beats"] <= float(
            slices_by_id[pl["slice_id"]]["target_beats"]) + 1e-9
        assert 0 < pl["velocity"] <= 1
        assert pl["beat"] >= last_end - 1e-9, "overlapping placements"
        last_end = pl["beat"] + pl["duration_beats"]
    assert last_end <= total + 1e-9

    # placements mirror the phrase track events exactly
    mirrored = sorted(
        ((round(e["beat"], 6), round(e["duration_beats"], 6),
          round(e["velocity"], 6), t["id"])
         for t in score["tracks"] if t.get("drum") is False
         for e in t["events"]))
    # v1 placements carry unrounded recipe arithmetic (e.g. .95-.05); the
    # wire format rounds events to 6 dp, so mirror at that precision.
    expect = sorted((round(p["beat"], 6), round(p["duration_beats"], 6),
                     round(p["velocity"], 6), p["slice_id"])
                    for p in placements)
    assert mirrored == expect, "phrase events do not mirror placements"

    windows = [(p["beat"], p["beat"] + p["duration_beats"])
               for p in placements]
    bass_midis = {int(s["bass_midi"]) for s in score["sample_flip"]["slices"]}
    prev = None
    for tr in score["tracks"]:
        assert isinstance(tr["gain_db"], (int, float))
        assert abs(float(tr["gain_db"])) != float("inf")
        beats = [e["beat"] for e in tr["events"]]
        assert beats == sorted(beats)
        for e in tr["events"]:
            assert 0 < e["velocity"] <= 1 and e["duration_beats"] > 0
            assert 0 <= e["beat"] <= total + 1e-9
        if tr["id"] in _KEY_IDS:
            for e in tr["events"]:
                assert not any(a - 1e-9 <= e["beat"] < b - 1e-9
                               for a, b in windows), "key over phrase"
        if tr["id"] == "bass":
            for e in tr["events"]:
                assert e["note"] in bass_midis
                if prev is not None:
                    assert e["beat"] >= prev[0] + prev[1] - 1e-9, \
                        "bass overlap"
                prev = (e["beat"], e["duration_beats"])


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def build_score(prepared: dict) -> dict:
    """Build the v2 feedback revision for one prepared payload (pure)."""
    base = orig.build_score(prepared)
    score = copy.deepcopy(base)
    number = str(int(score["metadata"]["track_number"])).zfill(2)
    if number not in REVISIONS:
        raise ValueError(f"No feedback revision for track number {number}")
    REVISIONS[number](score)
    _drop_keys_over_phrases(score)

    md = score["metadata"]
    md["collection_id"] = COLLECTION_ID
    md["version"] = VERSION
    md["keep"] = None
    md["revision_summary"] = SUMMARIES[number]
    _validate(score)
    return score
