#!/usr/bin/env python3
"""Offline BeatLab KO II sample-set exporter.

Usage: python pipeline/ko2_set.py JOB_JSON OUTPUT_DIR

All-or-nothing export: build in a temporary sibling, atomically rename
only once everything is written. Existing outputs are never overwritten
and inputs are only read. Pad placement is requested mapping only, not
verified on hardware (hardware_verified=false).
"""

import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile

import mido
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

SAMPLE_RATE = 44100
CHANNELS = 1
SLOT_LABELS = (".", "0", "ENTER", "1", "2", "3", "4", "5", "6", "7", "8", "9")
GROUPS = ("A", "B", "C", "D")
SCHEMA = "beatlab.ko2-set.v1"

FADE_SECONDS = 0.005
MAX_SOURCE_SECONDS = 16.0
MAX_TOTAL_SAMPLE_BYTES = 16 * 1024 * 1024
MIN_GAIN_DB = -60.0
MAX_GAIN_DB = 12.0
MIN_BPM = 40.0
MAX_BPM = 240.0
MIN_BARS = 1
MAX_BARS = 32
SILENCE_FLOOR_DBFS = -65.0

DEMO_TAIL_SECONDS = 2.0
# 0.89125 ~= 10**(-1/20): mixed demo is attenuated only down to -1 dBFS.
DEMO_PEAK_LIMIT = 0.89125
TICKS_PER_BEAT = 480
NOTE_GATE_BEATS = 0.1  # fixed one-shot gate, in beats

ID_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
SHA_RE = re.compile(r"[0-9a-fA-F]{64}\Z")


class JobError(ValueError):
    """Raised for any invalid job; CLI converts this into exit code 1."""


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _check(condition, message):
    if not condition:
        raise JobError(message)


def _require_dict(value, what):
    _check(isinstance(value, dict), "%s must be an object" % what)


def _validate_id(value, what):
    _check(isinstance(value, str) and ID_RE.match(value),
           "%s must be a non-empty safe ASCII id ([A-Za-z0-9_-])" % what)
    return value


def _midi_note(group_index, slot):
    return 36 + group_index * 12 + slot


def _process_pad(pad, index):
    """Validate and render one source WAV to mono float64 at 44.1 kHz."""
    _require_dict(pad, "pad %d" % index)
    pid = _validate_id(pad.get("id"), "pad id")
    group = pad.get("group")
    _check(group in GROUPS, "pad %r: group must be one of A-D" % pid)
    group_index = GROUPS.index(group)
    slot = pad.get("slot")
    _check(_is_int(slot) and 0 <= slot <= 11,
           "pad %r: slot must be an integer 0..11" % pid)
    _check(isinstance(pad.get("label"), str) and pad["label"],
           "pad %r: label must be a non-empty string" % pid)
    gain_db = pad.get("gain_db")
    _check(_is_number(gain_db) and MIN_GAIN_DB <= gain_db <= MAX_GAIN_DB,
           "pad %r: gain_db must be finite in [%g, %g]"
           % (pid, MIN_GAIN_DB, MAX_GAIN_DB))
    provenance = pad.get("provenance")
    _require_dict(provenance, "pad %r provenance" % pid)
    path = pad.get("path")
    _check(isinstance(path, str) and os.path.isabs(path),
           "pad %r: path must be an absolute path" % pid)
    _check(os.path.isfile(path),
           "pad %r: source file not found: %s" % (pid, path))
    want_sha = pad.get("sha256")
    _check(isinstance(want_sha, str) and SHA_RE.match(want_sha),
           "pad %r: sha256 must be 64 hex characters" % pid)
    actual_sha = _sha256_file(path)
    _check(actual_sha.lower() == want_sha.lower(),
           "pad %r: source sha256 mismatch" % pid)

    info = sf.info(path)
    _check(info.channels in (1, 2),
           "pad %r: source must be 1- or 2-channel WAV" % pid)
    _check(info.frames > 0, "pad %r: source has no samples" % pid)
    _check(0.0 < info.frames / float(info.samplerate) <= MAX_SOURCE_SECONDS,
           "pad %r: duration must be positive and <= %g s"
           % (pid, MAX_SOURCE_SECONDS))
    data, sr_in = sf.read(path, dtype="float64")
    _check(np.all(np.isfinite(data)),
           "pad %r: source contains non-finite samples" % pid)
    _check(float(np.max(np.abs(data))) <= 1.0,
           "pad %r: source peak exceeds 1.0" % pid)

    # Resample all channels first, then fold stereo down to mono.
    if sr_in != SAMPLE_RATE:
        g = math.gcd(SAMPLE_RATE, int(sr_in))
        axis = 0 if data.ndim == 2 else -1
        data = resample_poly(data, SAMPLE_RATE // g, int(sr_in) // g,
                             axis=axis)
    if data.ndim == 2:
        data = data.mean(axis=1)
    mono = np.asarray(data, dtype=np.float64) * 10.0 ** (gain_db / 20.0)

    # 5 ms linear fade in / fade out.
    fade_frames = min(int(round(FADE_SECONDS * SAMPLE_RATE)),
                      max(0, len(mono) // 2))
    if fade_frames > 0:
        mono[:fade_frames] *= np.linspace(0.0, 1.0, fade_frames)
        mono[-fade_frames:] *= np.linspace(1.0, 0.0, fade_frames)

    peak = float(np.max(np.abs(mono)))
    _check(peak <= 1.0,
           "pad %r: post-gain peak %.4f exceeds 1.0; refusing to clip"
           % (pid, peak))
    rms = float(np.sqrt(np.mean(mono ** 2)))
    rms_db = 20.0 * math.log10(rms) if rms > 0.0 else float("-inf")
    _check(rms_db >= SILENCE_FLOOR_DBFS,
           "pad %r: RMS %.2f dBFS below silence floor (%g)"
           % (pid, rms_db, SILENCE_FLOOR_DBFS))

    return {
        "id": pid,
        "group": group,
        "group_index": group_index,
        "slot": slot,
        "label": pad["label"],
        "gain_db": float(gain_db),
        "provenance": dict(provenance),
        "source_sha256": actual_sha,
        "audio": mono,
    }


def _validate_events(events, pad_ids, nominal_beats):
    _check(isinstance(events, list), "events must be a list")
    clean = []
    for i, event in enumerate(events):
        _require_dict(event, "event %d" % i)
        pad_id = event.get("pad_id")
        _check(isinstance(pad_id, str) and pad_id in pad_ids,
               "event %d: unknown pad_id %r" % (i, pad_id))
        beat = event.get("beat")
        _check(_is_number(beat) and 0.0 <= beat < nominal_beats,
               "event %d: beat must be finite within [0, %g)"
               % (i, nominal_beats))
        velocity = event.get("velocity")
        _check(_is_number(velocity) and 0.0 <= velocity <= 1.0,
               "event %d: velocity must be finite in [0, 1]" % i)
        clean.append({"pad_id": pad_id, "beat": float(beat),
                      "velocity": float(velocity)})
    return clean


def _validate_scenes(scenes, bars):
    _check(isinstance(scenes, list), "scenes must be a list")
    clean = []
    for i, scene in enumerate(scenes):
        _require_dict(scene, "scene %d" % i)
        _check(isinstance(scene.get("name"), str) and scene["name"],
               "scene %d: name required" % i)
        start_bar = scene.get("start_bar")
        scene_bars = scene.get("bars")
        _check(_is_int(start_bar) and start_bar >= 0,
               "scene %d: start_bar must be a non-negative integer" % i)
        _check(_is_int(scene_bars) and scene_bars >= 1,
               "scene %d: bars must be a positive integer" % i)
        _check(start_bar + scene_bars <= bars,
               "scene %r: extends past the %d-bar set"
               % (scene.get("name"), bars))
        _check(isinstance(scene.get("instruction"), str),
               "scene %d: instruction must be a string" % i)
        clean.append({"name": scene["name"], "start_bar": start_bar,
                      "bars": scene_bars, "instruction": scene["instruction"]})
    return clean


def _validate_job(job):
    _require_dict(job, "job")
    set_id = _validate_id(job.get("set_id"), "set_id")
    _check(isinstance(job.get("title"), str) and job["title"],
           "title must be a non-empty string")
    bpm = job.get("bpm")
    _check(_is_number(bpm) and MIN_BPM <= bpm <= MAX_BPM,
           "bpm must be finite in [%g, %g]" % (MIN_BPM, MAX_BPM))
    bars = job.get("bars")
    _check(_is_int(bars) and MIN_BARS <= bars <= MAX_BARS,
           "bars must be an integer in [%d, %d]" % (MIN_BARS, MAX_BARS))
    _check(isinstance(job.get("parent_version"), str),
           "parent_version must be a string")
    raw_pads = job.get("pads")
    _check(isinstance(raw_pads, list) and raw_pads,
           "pads must be a non-empty list")

    seen_slots, seen_ids, pads = set(), set(), []
    for i, raw in enumerate(raw_pads):
        pad = _process_pad(raw, i)
        _check(pad["id"] not in seen_ids, "duplicate pad id %r" % pad["id"])
        seen_ids.add(pad["id"])
        slot_key = (pad["group"], pad["slot"])
        _check(slot_key not in seen_slots,
               "duplicate pad assignment group %s slot %d"
               % (pad["group"], pad["slot"]))
        seen_slots.add(slot_key)
        pads.append(pad)

    total_sample_bytes = sum(2 * len(p["audio"]) for p in pads)
    _check(total_sample_bytes <= MAX_TOTAL_SAMPLE_BYTES,
           "total PCM sample bytes %d exceed %d"
           % (total_sample_bytes, MAX_TOTAL_SAMPLE_BYTES))

    events = _validate_events(job.get("events"), seen_ids, bars * 4)
    scenes = _validate_scenes(job.get("scenes", []), bars)

    return {
        "set_id": set_id,
        "title": job["title"],
        "bpm": float(bpm),
        "bars": bars,
        "parent_version": job["parent_version"],
        "pads": pads,
        "events": events,
        "scenes": scenes,
        "sample_bytes": total_sample_bytes,
    }


def _write_pad_file(tmp_dir, pad):
    rel = "Samples/%s/%02d-%s.wav" % (pad["group"], pad["slot"], pad["id"])
    full = os.path.join(tmp_dir, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    sf.write(full, pad["audio"], SAMPLE_RATE, subtype="PCM_16")
    frames = len(pad["audio"])
    return {
        "id": pad["id"],
        "group": pad["group"],
        "slot": pad["slot"],
        "pad_label": SLOT_LABELS[pad["slot"]],
        "midi_note": _midi_note(pad["group_index"], pad["slot"]),
        "label": pad["label"],
        "file": rel,
        "sha256": _sha256_file(full),
        "source_sha256": pad["source_sha256"],
        "frames": frames,
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "duration_seconds": frames / float(SAMPLE_RATE),
        "gain_db": pad["gain_db"],
        "provenance": pad["provenance"],
    }


def _render_demo(tmp_dir, pads, events, bpm, bars):
    """Mix one-shot full pad plays; stereo PCM16 render."""
    nominal_beats = bars * 4
    total_seconds = nominal_beats * 60.0 / bpm + DEMO_TAIL_SECONDS
    total_frames = int(round(total_seconds * SAMPLE_RATE))
    mix = np.zeros(total_frames, dtype=np.float64)
    # Re-decode the EXPORTED files so the demo matches shipped samples.
    decoded = {}
    for pad in pads:
        path = os.path.join(tmp_dir, pad["file"])
        audio, sr = sf.read(path, dtype="float64")
        _check(sr == SAMPLE_RATE and audio.ndim == 1,
               "exported sample %s is not mono 44.1 kHz" % pad["file"])
        decoded[pad["id"]] = audio

    for event in events:
        start = int(round(event["beat"] * 60.0 / bpm * SAMPLE_RATE))
        sound = decoded[event["pad_id"]] * event["velocity"]
        if start < total_frames:
            end = min(total_frames, start + len(sound))
            mix[start:end] += sound[: end - start]

    peak = float(np.max(np.abs(mix)))
    if peak > DEMO_PEAK_LIMIT:
        factor = DEMO_PEAK_LIMIT / peak
        mix *= factor
        gain_db = 20.0 * math.log10(factor)
    else:
        gain_db = 0.0

    stereo = np.column_stack((mix, mix)).astype(np.float64)
    full = os.path.join(tmp_dir, "Demo", "performance.wav")
    rel = "Demo/performance.wav"
    os.makedirs(os.path.dirname(full), exist_ok=True)
    sf.write(full, stereo, SAMPLE_RATE, subtype="PCM_16")
    return rel, _sha256_file(full), gain_db, total_frames


def _render_midi(tmp_dir, pads, events, bpm):
    """Type-1 SMF, PPQ480: tempo track + channel-0 one-shot triggers."""
    mid = mido.MidiFile(type=1, ticks_per_beat=TICKS_PER_BEAT)

    tempo_track = mido.MidiTrack()
    mid.tracks.append(tempo_track)
    tempo_track.append(mido.MetaMessage(
        "track_name", name="BeatLab KO2 tempo map", time=0))
    tempo_track.append(mido.MetaMessage(
        "set_tempo", tempo=int(round(60_000_000.0 / bpm)), time=0))
    tempo_track.append(mido.MetaMessage("end_of_track", time=0))

    note_track = mido.MidiTrack()
    mid.tracks.append(note_track)
    note_track.append(mido.MetaMessage(
        "track_name", name="KO2 one-shot performance", time=0))
    # Document the one-shot setting: fixed short gate, no pitch bend.
    note_track.append(mido.MetaMessage(
        "text",
        text=("One-shot full pad playback; fixed note gate 0.1 beat; "
              "PPQ480; channel 0; no pitch bend or synth pitch."),
        time=0))

    note_by_pad = {pad["id"]: pad["midi_note"] for pad in pads}
    entries = []  # (absolute tick, 0=on/1=off, note, velocity)
    for event in events:
        on_tick = int(round(event["beat"] * TICKS_PER_BEAT))
        velocity = max(1, min(127, int(round(event["velocity"] * 127))))
        note = note_by_pad[event["pad_id"]]
        entries.append((on_tick, 0, note, velocity))
        off_tick = on_tick + int(round(NOTE_GATE_BEATS * TICKS_PER_BEAT))
        entries.append((off_tick, 1, note, 0))
    entries.sort(key=lambda item: (item[0], item[1]))

    last_tick = 0
    for tick, kind, note, velocity in entries:
        delta = tick - last_tick
        last_tick = tick
        if kind == 0:
            note_track.append(mido.Message(
                "note_on", note=note, velocity=velocity, channel=0,
                time=delta))
        else:
            note_track.append(mido.Message(
                "note_off", note=note, velocity=0, channel=0, time=delta))
    note_track.append(mido.MetaMessage("end_of_track", time=0))

    rel = "Demo/performance.mid"
    full = os.path.join(tmp_dir, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    mid.save(full)
    return rel, _sha256_file(full)


def export_job(job, output_dir):
    """Validate *job* and export it; return the written manifest dict."""
    final_dir = os.path.abspath(output_dir)
    _check(not os.path.exists(final_dir),
           "output already exists; refusing to overwrite: %s" % final_dir)

    spec = _validate_job(job)
    parent = os.path.dirname(final_dir) or "."
    tmp_dir = tempfile.mkdtemp(
        prefix=".%s.tmp-" % os.path.basename(final_dir), dir=parent)
    try:
        manifest_pads = [_write_pad_file(tmp_dir, p) for p in spec["pads"]]
        demo_rel, demo_sha, demo_gain_db, demo_frames = _render_demo(
            tmp_dir, manifest_pads, spec["events"], spec["bpm"], spec["bars"])
        midi_rel, midi_sha = _render_midi(
            tmp_dir, manifest_pads, spec["events"], spec["bpm"])

        manifest = {
            "schema": SCHEMA,
            "set_id": spec["set_id"],
            "title": spec["title"],
            "bpm": spec["bpm"],
            "bars": spec["bars"],
            "parent_version": spec["parent_version"],
            "hardware_verified": False,
            "listening_review": "pending",
            "pads": manifest_pads,
            "events": spec["events"],
            "scenes": spec["scenes"],
            "demo": {
                "file": demo_rel,
                "sha256": demo_sha,
                "gain_db": demo_gain_db,
                "duration_seconds": demo_frames / float(SAMPLE_RATE),
            },
            "midi": {"file": midi_rel, "sha256": midi_sha},
            "sample_bytes": spec["sample_bytes"],
        }
        with open(os.path.join(tmp_dir, "set.json"), "w") as f:
            json.dump(manifest, f, indent=2)
            f.write("\n")

        os.replace(tmp_dir, final_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return manifest


def main(argv=None):
    if argv is None:
        argv = sys.argv
    if len(argv) != 3:
        sys.stderr.write("usage: python pipeline/ko2_set.py JOB_JSON OUTPUT_DIR\n")
        return 1
    job_path, output_dir = argv[1], argv[2]
    try:
        with open(job_path, "r") as f:
            job = json.load(f)
        export_job(job, output_dir)
    except (JobError, OSError, ValueError, RuntimeError) as exc:
        sys.stderr.write("ko2_set export failed: %s\n" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
