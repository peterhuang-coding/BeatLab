"""Deterministic, verified stem-splice locked vocal edit.

Exact-safety design: only decoded target-stem samples inside the requested
zero-based half-open beat interval change. Every other stem/MIDI byte is copied
verbatim, and the target stem outside the interval is decoded-sample identical.
The full mix is reconstructed from the final stems with the parent master gain
frozen; no renormalization and no changes to any existing module.

Identity is explicit reviewed catalog metadata (identity_verified flags, family
ids, allowed uses). This module never infers identity from hashes or lyrics and
makes no taste or Live-playback claims. The recipe replays only against the
immutable parent recorded inside it; vanilla render_score cannot replay it.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

from song import _midi, _sample, _space, sample_voice
from ableton_export import validate_song

HASH_RE = re.compile(r"[a-f0-9]{64}")
ID_RE = re.compile(r"[a-z][a-z0-9_-]*")
IDENTITY_BOUNDARY = (
    "Identity rests solely on explicit reviewed catalog metadata: the "
    "identity_verified flags, family_id records and allowed_uses supplied by "
    "the caller. Hashes only certify that bytes are unchanged, never that a "
    "performance says particular words; alternate edit windows are not offered "
    "as evidence of different words. This module performs no lyric inference.")


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _num(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _mono(audio: np.ndarray) -> np.ndarray:
    return audio.mean(axis=1) if audio.ndim == 2 else audio


def _same_material(a: np.ndarray, b: np.ndarray) -> bool:
    """True for identical decoded audio or a simple constant-gain version.

    Length must match exactly; the gain test is run both directions and fits a
    single scalar over the whole take, so genuinely different performances do
    not pass.
    """
    a, b = _mono(a).astype("float64"), _mono(b).astype("float64")
    if a.shape != b.shape:
        return False
    if np.array_equal(a, b):
        return True
    for x, y in ((a, b), (b, a)):
        mask = np.abs(x) > 1e-5
        if int(mask.sum()) >= 16:
            ratios = y[mask] / x[mask]
            scale = float(np.median(ratios))
            if scale > 0 and float(np.max(np.abs(ratios - scale))) <= 1e-3:
                return True
    return False


def _check_contract(edit: dict, phrase: dict) -> None:
    if not isinstance(edit, dict):
        raise ValueError("edit must be an object")
    if not isinstance(phrase, dict):
        raise ValueError("phrase must be an object")
    if not _text(edit.get("track_id")) or not ID_RE.fullmatch(edit["track_id"]):
        raise ValueError("edit.track_id must match [a-z][a-z0-9_-]*")
    if not isinstance(edit.get("expected_parent_mix_sha256"), str) or not HASH_RE.fullmatch(
            edit["expected_parent_mix_sha256"].lower()):
        raise ValueError("edit.expected_parent_mix_sha256 must be 64 hex chars")
    if not _text(edit.get("current_family_id")):
        raise ValueError("edit.current_family_id must be a nonempty string")
    if edit.get("identity_verified") is not True:
        raise ValueError("edit.identity_verified must be explicitly true")
    if not _text(edit.get("purpose")):
        raise ValueError("edit.purpose must be a nonempty string")

    if not _text(phrase.get("id")) or not ID_RE.fullmatch(phrase["id"]):
        raise ValueError("phrase.id must match [a-z][a-z0-9_-]*")
    if not _text(phrase.get("path")):
        raise ValueError("phrase.path must be a nonempty string")
    if not isinstance(phrase.get("sha256"), str) or not HASH_RE.fullmatch(phrase["sha256"].lower()):
        raise ValueError("phrase.sha256 must be 64 hex chars")
    if not _text(phrase.get("family_id")):
        raise ValueError("phrase.family_id must be a nonempty string")
    if phrase.get("identity_verified") is not True:
        raise ValueError("phrase.identity_verified must be explicitly true")
    uses = phrase.get("allowed_uses")
    if not isinstance(uses, list) or not all(isinstance(u, str) for u in uses):
        raise ValueError("phrase.allowed_uses must be a list of use strings")
    if edit["purpose"].strip() not in uses:
        raise ValueError(
            f"phrase.allowed_uses {uses!r} does not grant purpose "
            f"{edit['purpose']!r}; request the correct permission")
    root = phrase.get("root_midi", 60)
    if isinstance(root, bool) or not isinstance(root, int) or not 0 <= root <= 127:
        raise ValueError("phrase.root_midi must be an integer in [0, 127]")
    if phrase.get("role") != "answer":
        raise ValueError("phrase.role must be 'answer'")


def _plan(parent: Path, edit: dict, phrase: dict) -> dict:
    parent = Path(parent).expanduser().resolve()
    _check_contract(edit, phrase)
    # A caller can replace a file at the same path between bounded jobs.
    # The sampler cache is path-based; never render bytes from an older hash.
    _sample.cache_clear()
    checked = validate_song(parent)
    if edit["expected_parent_mix_sha256"].lower() != checked["mix_sha256"]:
        raise ValueError(
            f"stale parent: expected mix sha {edit['expected_parent_mix_sha256']}, "
            f"actual {checked['mix_sha256']}; refresh against the current parent")
    manifest = checked["manifest"]
    sr = int(checked["sample_rate"])
    bpm = float(manifest["bpm"])
    beat_s = 60.0 / bpm
    bars4 = int(manifest["bars"]) * 4

    start, end = edit["start_beat"], edit["end_beat"]
    for name, value in (("start_beat", start), ("end_beat", end)):
        if not _num(value):
            raise ValueError(f"edit.{name} must be a finite non-boolean number")
    start, end = float(start), float(end)
    if not 0 <= start < end <= bars4:
        raise ValueError(
            f"edit beats must satisfy 0 <= start < end <= {bars4} "
            "(within musical bars, not the render tail)")
    f0 = round(start * beat_s * sr)
    f1 = round(end * beat_s * sr)
    if not 0 <= f0 < f1 <= checked["frames"]:
        raise ValueError("edit region does not map to a valid sample interval")

    score = json.loads((parent / "score.json").read_text())
    score_ids = {t["id"] for t in score["tracks"]}
    manifest_ids = {t["id"] for t in manifest["tracks"]}
    if score_ids != manifest_ids:
        raise ValueError(
            f"score and manifest track ids differ: {sorted(score_ids)} vs "
            f"{sorted(manifest_ids)}")

    tid = edit["track_id"]
    records = {t["id"]: t for t in checked["tracks"]}
    score_tracks = {t["id"]: t for t in score["tracks"]}
    if tid not in records:
        raise ValueError(f"unknown target track: {tid}")
    track = score_tracks[tid]
    if bool(track.get("drum", False)):
        raise ValueError(f"target {tid} is marked drum; vocals only")
    role = str(track.get("role", "")).lower()
    if role in ("bass", "drums", "drum"):
        raise ValueError(f"target {tid} role is {role!r}; vocals only")
    if tid == 'bass' or tid.startswith('bass_'):
        raise ValueError(f"target {tid} is a protected bass part")
    for name in ("gain_db", "pan", "room", "highpass_hz", "lowpass_hz"):
        if name in track and not _num(track[name]):
            raise ValueError(f"target {name} must be a finite number")
    pan = float(track.get("pan", 0))
    if not -1 <= pan <= 1:
        raise ValueError("target pan must be within [-1, 1]")

    events = track.get("events")
    if not isinstance(events, list) or not events:
        raise ValueError(f"target {tid} has no events")
    for e in events:
        if not isinstance(e, dict):
            raise ValueError("target events must be objects")
        if not _num(e.get("beat")) or not _num(e.get("duration_beats")):
            raise ValueError("event beat and duration_beats must be finite numbers")
        if float(e["duration_beats"]) <= 0:
            raise ValueError("event duration must be positive")
        if "velocity" in e and not _num(e["velocity"]):
            raise ValueError("event velocity must be a finite number")
        if "note" in e and (isinstance(e["note"], bool) or not isinstance(e["note"], int)):
            raise ValueError("event note must be an integer")

    if phrase["family_id"] == edit["current_family_id"]:
        raise ValueError(
            f"phrase family {phrase['family_id']!r} is the current family; a "
            "locked edit must come from a genuinely different answer family")

    phrase_path = Path(phrase["path"]).expanduser()
    if not phrase_path.is_file():
        raise ValueError(f"phrase source file is missing: {phrase_path}")
    actual_phrase_sha = _sha(phrase_path)
    if actual_phrase_sha != phrase["sha256"].lower():
        raise ValueError(
            f"phrase sha256 mismatch: declared {phrase['sha256']}, actual "
            f"{actual_phrase_sha}")
    try:
        phr_audio, phr_sr = sf.read(phrase_path, dtype="float32", always_2d=True)
    except Exception as exc:
        raise ValueError(f"phrase WAV is unreadable or corrupt: {exc}") from exc
    if not len(phr_audio) or not np.isfinite(phr_audio).all():
        raise ValueError("phrase WAV is empty or contains nonfinite samples")
    phr_peak = float(np.max(np.abs(phr_audio)))
    if phr_peak < 1e-9:
        raise ValueError("phrase candidate is silent")

    target_source = Path(records[tid].get("source", ""))
    if not target_source.is_absolute():
        target_source = parent / target_source
    if not target_source.is_file():
        raise ValueError(
            f"parent target source material is unavailable: {target_source}; "
            "cannot certify the candidate differs from the current audio")
    if _sha(target_source) != records[tid]['source_sha256']:
        raise ValueError('parent target source hash changed since rendering')
    if actual_phrase_sha == records[tid]["source_sha256"]:
        raise ValueError("phrase source hash equals the current target sample")
    try:
        old_source = sf.read(target_source, dtype="float32", always_2d=True)[0]
    except Exception as exc:
        raise ValueError(f"parent target source is corrupt: {exc}") from exc
    if _same_material(phr_audio, old_source):
        raise ValueError(
            "phrase decoded audio is identical to the current target sample "
            "(including a simple gain change); nothing genuinely different")

    track_root = int(track.get("root_midi", 60))
    phr_root = int(phrase.get("root_midi", 60))
    inside, replaced = [], []
    for e in events:
        b = float(e["beat"])
        d = float(e["duration_beats"])
        if b < start and b + d > start:
            raise ValueError(
                f"target event at beat {b} crosses the edit start {start}; "
                "choose boundaries that do not cut through notes")
        if start <= b < end:
            if b + d > end:
                raise ValueError(
                    f"target event at beat {b} crosses the edit end {end}; "
                    "choose boundaries that do not cut through notes")
            note = int(e.get("note", track_root))
            new_note = phr_root + (note - track_root)
            if not 0 <= new_note <= 127:
                raise ValueError(
                    f"replacement note {new_note} at beat {b} is outside MIDI "
                    "range after the phrase root offset")
            slot_frames = round(d * beat_s * sr)
            natural_frames = len(_sample(str(phrase_path), phr_root, new_note, sr))
            if natural_frames > slot_frames:
                raise ValueError(
                    f"phrase at beat {b} is {natural_frames} frames after pitch "
                    f"resampling but the slot is only {slot_frames}; the phrase "
                    "must naturally fit - it will never be truncated")
            inside.append(e)
            replaced.append({"beat": b, "duration_beats": d,
                             "velocity": float(e.get("velocity", 0.7)),
                             "note": new_note, "source_note": note,
                             "natural_frames": natural_frames,
                             "slot_frames": slot_frames})
    if not inside:
        raise ValueError(
            f"no target events have an onset inside [{start}, {end}); the edit "
            "would change nothing")

    crossfade = min(round(0.005 * sr), (f1 - f0) // 3)
    if crossfade < 4 or 2 * crossfade >= f1 - f0:
        raise ValueError("edit interval is too short for safe crossfades")

    return {"parent": parent, "checked": checked, "score": score, "manifest": manifest,
            "sr": sr, "bpm": bpm, "beat_s": beat_s, "start": start, "end": end,
            "f0": f0, "f1": f1, "tid": tid, "track": track, "records": records,
            "phrase_path": phrase_path, "phrase_sha": actual_phrase_sha,
            "phrase_peak": phr_peak, "phrase_sr": phr_sr,
            "phr_root": phr_root, "replaced": replaced, "crossfade": crossfade}


def validate_edit(parent: Path, edit: dict, phrase: dict) -> dict:
    """Read-only preflight; raises ValueError with an actionable reason."""
    plan = _plan(parent, edit, phrase)
    return {
        "song": str(plan["parent"]), "track_id": plan["tid"],
        "start_beat": plan["start"], "end_beat": plan["end"],
        "frames": [plan["f0"], plan["f1"]],
        "crossfade_frames": plan["crossfade"],
        "replacement_event_count": len(plan["replaced"]),
        "replacement_events": plan["replaced"],
        "phrase": {"id": phrase["id"],
                   "sha256": plan["phrase_sha"], "family_id": phrase["family_id"],
                   "root_midi": plan["phr_root"], "role": phrase["role"],
                   "source_sample_rate": plan["phrase_sr"], "peak": plan["phrase_peak"]},
        "master_gain": plan["manifest"]["master_gain"]}


def _snapshot(parent: Path) -> dict:
    snap = {}
    for path in parent.rglob("*"):
        if path.is_file():
            rel = str(path.relative_to(parent))
            snap[rel] = (hashlib.sha256(path.read_bytes()).hexdigest(),
                         path.stat().st_mtime_ns)
    return snap


def _render_replacement(plan: dict) -> np.ndarray:
    """Render the new interval with the target's gain/pan/filter/room chain."""
    track = plan["track"]
    sr = plan["sr"]
    length = plan["f1"] - plan["f0"]
    gain = 10 ** (float(track.get("gain_db", -12)) / 20)
    segment = np.zeros((length, 2), dtype="float32")
    for event in plan["replaced"]:
        position = round(event["beat"] * plan["beat_s"] * sr) - plan["f0"]
        voice = sample_voice(plan["phrase_path"], plan["phr_root"], event["note"],
                             event["duration_beats"] * plan["beat_s"], sr)
        level = gain * event["velocity"]
        end = position + len(voice)
        segment[position:end] += voice * level
    low = float(track.get("highpass_hz", 0))
    high = float(track.get("lowpass_hz", 0))
    if low:
        segment = sosfilt(butter(2, low, btype="highpass", fs=sr, output="sos"),
                          segment, axis=0)
    if high:
        segment = sosfilt(butter(2, min(high, sr * 0.45), fs=sr, output="sos"),
                          segment, axis=0)
    segment = _space(segment, sr, float(track.get("room", 0)))
    pan = float(track.get("pan", 0))
    segment[:, 0] *= min(1.0, 1 - pan)
    segment[:, 1] *= min(1.0, 1 + pan)
    fade_s = float(plan["score"].get("fade_seconds", 2))
    total = plan["checked"]["frames"]
    fade = min(round(fade_s * sr), total)
    if fade:
        first = max(0, total - fade - plan["f0"])
        last = min(length, total - plan["f0"])
        if last > first:
            start_value = (plan["f0"] + first) - (total - fade)
            ramp = np.linspace(1, 0, fade, dtype="float32")[start_value:
                                                            start_value + (last - first)]
            segment[first:last] *= ramp[:, None]
    if not np.isfinite(segment).all():
        raise ValueError("replacement rendering produced nonfinite samples")
    return segment.astype("float32")


def render_locked_edit(parent: Path, out: Path, edit: dict, phrase: dict) -> dict:
    out = Path(out).expanduser().resolve()
    if out.is_relative_to(Path(parent).expanduser().resolve()):
        raise ValueError('output must not be inside the protected parent')
    if out.exists():
        raise FileExistsError(f"choose a new output directory; not overwriting {out}")
    out.parent.mkdir(parents=True, exist_ok=True)

    plan = _plan(parent, edit, phrase)
    parent_dir = plan["parent"]
    before = _snapshot(parent_dir)

    token_material = json.dumps({
        "parent_mix": plan["checked"]["mix_sha256"], "edit": edit,
        "phrase_sha": plan["phrase_sha"], "f0": plan["f0"], "f1": plan["f1"]},
        sort_keys=True, default=str).encode()
    token = hashlib.sha256(token_material).hexdigest()[:12]
    staging = out.parent / f".{out.name}.pending-{token}"
    if staging.exists():
        raise FileExistsError(f'Pending edit exists; inspect original attempt: {staging}')
    staging.mkdir()
    child = staging / "song"
    try:
        (child / "stems").mkdir(parents=True)
        (child / "midi").mkdir(parents=True)
        (child / "sources").mkdir(parents=True)

        tid = plan["tid"]
        # Verbatim copies of every protected stem and MIDI.
        for track_id, record in plan["records"].items():
            if track_id == tid:
                continue
            shutil.copyfile(parent_dir / record["file"], child / record["file"])
            shutil.copyfile(parent_dir / record["midi"], child / record["midi"])

        old_target = sf.read(
            parent_dir / plan["records"][tid]["file"],
            dtype="float32", always_2d=True)[0]
        master_gain = float(plan["manifest"]["master_gain"])
        # Parent stems on disk include the master gain; the splice must join
        # audio in the same domain while that gain stays frozen.
        new_segment = (_render_replacement(plan) * master_gain).astype("float32")
        if not np.isfinite(new_segment).all():
            raise ValueError("scaled replacement contains nonfinite samples")
        cf = plan["crossfade"]
        win_in = np.linspace(0, 1, cf, dtype="float32")[:, None]
        win_out = np.linspace(1, 0, cf, dtype="float32")[:, None]
        old_seg = old_target[plan["f0"]:plan["f1"]]
        blend = old_seg.copy()
        blend[:cf] = old_seg[:cf] * (1 - win_in) + new_segment[:cf] * win_in
        blend[-cf:] = new_segment[-cf:] * win_out + old_seg[-cf:] * (1 - win_out)
        blend[cf:-cf] = new_segment[cf:-cf]
        new_target = old_target.copy()
        new_target[plan["f0"]:plan["f1"]] = blend
        if not np.isfinite(new_target).all():
            raise ValueError("edited target stem contains nonfinite samples")
        if float(np.max(np.abs(new_target))) >= (2**23-1)/2**23:
            raise ValueError('edited target stem would clip before PCM encoding')

        target_record = plan["records"][tid]
        sf.write(child / target_record["file"], new_target, plan["sr"],
                 subtype="PCM_24")
        # Edited target MIDI: same beats/durations/velocities, phrase-root notes.
        prior_midi = plan['score'].get('locked_edit_recipe', {}).get('midi_events_by_track', {})
        midi_events = deepcopy(prior_midi.get(tid, plan['track']['events']))
        replacements = iter(plan['replaced'])
        for event in midi_events:
            if plan['start'] <= float(event['beat']) < plan['end']:
                event['note'] = next(replacements)['note']
        _midi(midi_events, plan["bpm"], child / target_record["midi"],
              drum=False, root_midi=plan["phr_root"])

        # Verify the outside window is decoded-sample identical after encoding.
        encoded_target = sf.read(child / target_record["file"],
                                 dtype="float32", always_2d=True)[0]
        outside_diff = float(max(
            float(np.max(np.abs(encoded_target[:plan["f0"]] - old_target[:plan["f0"]])))
            if plan["f0"] else 0.0,
            float(np.max(np.abs(encoded_target[plan["f1"]:] - old_target[plan["f1"]:])))
            if plan["f1"] < len(old_target) else 0.0))
        if outside_diff != 0.0:
            raise ValueError(
                f"target stem outside the window changed by {outside_diff}")

        # Preserve the logical score, explicitly marking its splice-only replay
        # contract so a vanilla renderer cannot silently recreate the old song.
        child_score = deepcopy(plan['score'])
        child_score['locked_edit_recipe'] = {'engine': 'stem_splice',
            'path': 'locked_edit_recipe.json',
            'midi_events_by_track': {**deepcopy(prior_midi), tid: midi_events}}
        (child/'score.json').write_text(json.dumps(child_score, ensure_ascii=False, indent=2)+'\n')

        # Phrase source copied deliberately under sources/, hash verified.
        phrase_dest = child / "sources" / f"{phrase['id']}.wav"
        shutil.copyfile(plan["phrase_path"], phrase_dest)
        if _sha(phrase_dest) != plan["phrase_sha"]:
            raise ValueError("copied phrase source failed hash verification")

        # Deliberately copied parent provenance, when present.
        for name in ("source-provenance.json", "source_catalog.json", "slice-map.json"):
            if (parent_dir / name).is_file():
                shutil.copy2(parent_dir / name, child / name)

        # Reconstruct the full mix from final stems; frozen master gain.
        stems = []
        for record in plan["manifest"]["tracks"]:
            audio = sf.read(child / record["file"], dtype="float64", always_2d=True)[0]
            if not np.isfinite(audio).all():
                raise ValueError(f"nonfinite stem: {record['id']}")
            stems.append(audio)
        mix = np.sum(stems, axis=0).astype("float32")
        if not np.isfinite(mix).all():
            raise ValueError("reconstructed mix contains nonfinite samples")
        mix_peak = float(np.max(np.abs(mix)))
        full_scale = (2 ** 23 - 1) / 2 ** 23
        if mix_peak >= full_scale:
            raise ValueError(
                f"reconstructed mix peak {mix_peak:.6g} reaches digital full "
                "scale; rejecting a clipping edit")
        sf.write(child / "full_mix.wav", mix, plan["sr"], subtype="PCM_24")
        new_mix_sha = _sha(child / "full_mix.wav")

        old_mix = sf.read(parent_dir / "full_mix.wav", dtype="float64",
                          always_2d=True)[0]
        new_mix = sf.read(child / "full_mix.wav", dtype="float64",
                          always_2d=True)[0]
        delta = new_mix - old_mix
        old_energy = float(np.sum(old_mix ** 2))
        change_rel = math.sqrt(float(np.sum(delta ** 2)) / old_energy)
        if change_rel < 1e-9:
            raise ValueError("edit produced no audible change in the mix")

        # Updated manifest: only target identity/hashes change; gain frozen.
        new_manifest = deepcopy(plan["manifest"])
        new_manifest["mix_sha256"] = new_mix_sha
        new_manifest["locked_edit"] = "locked_edit_recipe.json"
        for record in new_manifest["tracks"]:
            if record["id"] == tid:
                record["sha256"] = _sha(child / record["file"])
                record["midi_sha256"] = _sha(child / record["midi"])
                record["source"] = f"sources/{phrase['id']}.wav"
                record["source_sha256"] = plan["phrase_sha"]
        (child / "run_manifest.json").write_text(
            json.dumps(new_manifest, ensure_ascii=False, indent=2) + "\n")

        recipe = {
            "engine": "stem_splice", "schema_version": 1,
            "parent": {"path": str(parent_dir),
                       "mix_sha256": plan["checked"]["mix_sha256"],
                       "manifest_sha256": plan["checked"]["manifest_sha256"],
                       "score_sha256": plan["checked"]["score_sha256"]},
            "edit": edit,
            "phrase": {**phrase, "copied_path": f"sources/{phrase['id']}.wav"},
            "region": {"track_id": tid, "start_beat": plan["start"],
                       "end_beat": plan["end"], "start_frame": plan["f0"],
                       "end_frame": plan["f1"],
                       "crossfade_seconds": cf / plan["sr"],
                       "crossfade_frames": cf},
            "replacement_events": plan["replaced"],
            "master_gain": plan["manifest"]["master_gain"],
            "identity_trust_boundary": IDENTITY_BOUNDARY,
            "replay": {
                "method": "replay_locked_edit",
                "requires": ("the exact immutable parent recorded above; the "
                             "stem splice is not replayable by vanilla "
                             "render_score")}}
        (child / "locked_edit_recipe.json").write_text(
            json.dumps(recipe, ensure_ascii=False, indent=2) + "\n")

        locked = {"status": "rendered", "engine": "stem_splice",
                  "parent": str(parent_dir), "edit": edit,
                  "phrase": {k: v for k, v in phrase.items()},
                  "mix_sha256": new_mix_sha,
                  "subjective_acceptance": "pending"}
        (child / "locked-edit.json").write_text(
            json.dumps(locked, ensure_ascii=False, indent=2) + "\n")

        # Parent protection evidence: bytes and mtimes must still match.
        after = _snapshot(parent_dir)
        parent_unchanged = after == before
        if not parent_unchanged:
            raise ValueError("parent files changed during locked edit")

        other_stems, other_midi = [], []
        for track_id, record in plan["records"].items():
            if track_id == tid:
                continue
            same_stem = _sha(child / record["file"]) == record["sha256"]
            same_midi = _sha(child / record["midi"]) == record.get("midi_sha256",
                                                                    _sha(parent_dir / record["midi"]))
            if not same_stem or not same_midi:
                raise ValueError(f"protected bytes changed: {track_id}")
            other_stems.append(track_id)
            other_midi.append(track_id)

        protection = {
            "engine": "stem_splice",
            "parent": {"path": str(parent_dir),
                       "mix_sha256": plan["checked"]["mix_sha256"],
                       "score_sha256": plan["checked"]["score_sha256"],
                       "manifest_sha256": plan["checked"]["manifest_sha256"]},
            "region": {"track_id": tid, "start_beat": plan["start"],
                       "end_beat": plan["end"], "start_frame": plan["f0"],
                       "end_frame": plan["f1"], "crossfade_frames": cf},
            "phrase": {"id": phrase["id"], "family_id": phrase["family_id"],
                       "sha256": plan["phrase_sha"],
                       "root_midi": plan["phr_root"], "role": phrase["role"]},
            "child": {"mix_sha256": new_mix_sha,
                      "target_stem_sha256": _sha(child / target_record["file"])},
            "checks": {
                "non_target_stems_byte_identical": True,
                "non_target_stems": other_stems,
                "non_target_midi_byte_identical": True,
                "non_target_midi": other_midi,
                "target_outside_window_sample_identical": True,
                "target_outside_window_max_abs_diff": outside_diff,
                "frames_outside_window": plan["checked"]["frames"] - (plan["f1"] - plan["f0"]),
                "mix_finite": True, "mix_peak": mix_peak,
                "mix_below_full_scale": mix_peak < full_scale,
                "audible_change": True,
                "mix_change_relative_rms": change_rel,
                "master_gain_frozen": plan["manifest"]["master_gain"],
                "parent_files_byte_and_mtime_unchanged": parent_unchanged,
                "parent_file_count": len(after)},
            "identity_trust_boundary": IDENTITY_BOUNDARY,
            "replay_note": recipe["replay"]["requires"]}
        (child / "protection.json").write_text(
            json.dumps(protection, ensure_ascii=False, indent=2) + "\n")

        validated = validate_song(child)
        if validated["mix_sha256"] != new_mix_sha:
            raise ValueError("post-edit validation saw an unexpected mix hash")

        if out.exists():  # Last-moment reservation check.
            raise FileExistsError(f"output appeared during edit: {out}")
        os.rename(child, out)
        shutil.rmtree(staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"song": str(out), "mix_sha256": new_mix_sha, "protection": protection}


def replay_locked_edit(child: Path, out: Path) -> dict:
    """Replay the recorded splice against its immutable parent.

    The parent is verified against the hashes captured in the recipe; the phrase
    is read from the child's sources/ copy, so replay needs no external paths.
    """
    child = Path(child).expanduser().resolve()
    validate_song(child)
    recipe_path = child / "locked_edit_recipe.json"
    if not recipe_path.is_file():
        raise ValueError("child has no locked_edit_recipe.json; cannot replay")
    recipe = json.loads(recipe_path.read_text())
    if recipe.get("engine") != "stem_splice":
        raise ValueError("recipe engine is not stem_splice")
    parent = Path(recipe["parent"]["path"]).expanduser()
    if not parent.is_dir():
        raise ValueError(f"recorded parent is unavailable: {parent}")
    checks = (("full_mix.wav", recipe["parent"]["mix_sha256"]),
              ("run_manifest.json", recipe["parent"]["manifest_sha256"]),
              ("score.json", recipe["parent"]["score_sha256"]))
    for name, expected in checks:
        path = parent / name
        if not path.is_file() or _sha(path) != expected:
            raise ValueError(
                f"parent {name} differs from the immutable recipe record")
    edit = dict(recipe["edit"])
    phrase = dict(recipe["phrase"])
    copied = phrase.pop("copied_path", f"sources/{phrase['id']}.wav")
    phrase_path = child / copied
    if not phrase_path.is_file():
        raise ValueError(f"recorded phrase copy is missing: {phrase_path}")
    phrase["path"] = str(phrase_path)
    return render_locked_edit(parent, out, edit, phrase)
