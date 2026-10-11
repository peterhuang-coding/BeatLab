#!/usr/bin/env python3
"""Bounded local candidate WAV exporter (technical candidates only).

Reads a job JSON describing at most 4 local source WAVs (at most 6 time
windows each), verifies the whole job and every source audio file before
touching the output directory, then exports short FLOAT WAV candidates that
preserve the source sample rate/channels with a 5 ms linear endpoint fade.

The exporter never uploads audio, never writes a database and never modifies
source files. It produces technical cut candidates only; it makes no claim
about lyrics, clean vocals, music quality or rights clearance.

Usage:
    python pipeline/supply_cuts.py JOB.json OUTPUT_DIR

Exit code 0 = valid execution (even when every window was skipped as
silent); nonzero = validation/usage/IO failure.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sys
import urllib.parse

import numpy as np
import soundfile as sf

# --- Bounds mandated by the contract ----------------------------------------
MAX_SOURCES = 4
MAX_WINDOWS_PER_SOURCE = 6
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 128 * 1024 * 1024
MIN_WINDOW_SEC = 2.0
MAX_WINDOW_SEC = 16.0
FADE_SEC = 0.005
SILENCE_SKIP_RMS_DB = -50.0

CONTENT_IDENTITY = "unverified"
LISTENING_REVIEW = "pending"
RIGHTS_SCOPE = "requires_per-use-review"

_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}\Z")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")


class JobError(ValueError):
    """Any job-level validation or safe-execution failure."""


# --- Small validation helpers -----------------------------------------------
def _require_dict(value, field):
    if not isinstance(value, dict):
        raise JobError("%s must be an object" % field)
    return value


def _require_str(value, field):
    if not isinstance(value, str) or not value.strip():
        raise JobError("missing or invalid %s" % field)
    return value


def _finite_number(value, field):
    # bool is a subclass of int but is never a meaningful time value.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JobError("%s must be a finite number" % field)
    number = float(value)
    if not math.isfinite(number):
        raise JobError("%s must be finite" % field)
    return number


def _http_url(value, field):
    text = _require_str(value, field)
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise JobError("%s must be an http(s) URL" % field)
    return text


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --- Job schema validation (no audio IO here) -------------------------------
def _validate_window(raw, index):
    _require_dict(raw, "windows[%d]" % index)
    start = _finite_number(raw.get("start_sec"), "windows[%d].start_sec" % index)
    end = _finite_number(raw.get("end_sec"), "windows[%d].end_sec" % index)
    if start < 0.0:
        raise JobError("windows[%d].start_sec is negative" % index)
    if start >= end:
        raise JobError("windows[%d] requires start_sec < end_sec" % index)
    duration = end - start
    if duration < MIN_WINDOW_SEC or duration > MAX_WINDOW_SEC:
        raise JobError(
            "windows[%d] duration %.6fs outside %.0f..%.0f seconds"
            % (index, duration, MIN_WINDOW_SEC, MAX_WINDOW_SEC)
        )
    # type/basis are recorded provenance of the cut; they are technical
    # labels only and never a claim about content identity.
    _require_str(raw.get("type"), "windows[%d].type" % index)
    _require_str(raw.get("basis"), "windows[%d].basis" % index)
    return {"start_sec": start, "end_sec": end,
            "type": raw["type"], "basis": raw["basis"]}


def _validate_source(raw, index, seen_asset_ids):
    prefix = "sources[%d]" % index
    _require_dict(raw, prefix)
    asset_id = _require_str(raw.get("asset_id"), prefix + ".asset_id")
    if not _ASSET_ID_RE.match(asset_id):
        raise JobError(
            "unsafe asset_id %r (allowed: A-Z a-z 0-9 _ - , 1..64 chars)"
            % asset_id
        )
    if asset_id in seen_asset_ids:
        raise JobError("duplicate asset_id %r" % asset_id)
    seen_asset_ids.add(asset_id)

    path = _require_str(raw.get("path"), prefix + ".path")
    if not os.path.isabs(path):
        raise JobError("%s.path must be an absolute path" % prefix)

    sha = _require_str(raw.get("sha256"), prefix + ".sha256")
    if not _SHA256_RE.match(sha):
        raise JobError("%s.sha256 must be 64 lowercase hex characters" % prefix)

    source_url = _http_url(raw.get("source_url"), prefix + ".source_url")
    rights_url = _http_url(
        raw.get("rights_evidence_url"), prefix + ".rights_evidence_url"
    )

    raw_windows = raw.get("windows")
    if not isinstance(raw_windows, list) or not raw_windows:
        raise JobError("%s.windows must be a non-empty list" % prefix)
    if len(raw_windows) > MAX_WINDOWS_PER_SOURCE:
        raise JobError(
            "%s has %d windows; at most %d allowed"
            % (prefix, len(raw_windows), MAX_WINDOWS_PER_SOURCE)
        )
    windows = [_validate_window(item, i) for i, item in enumerate(raw_windows)]

    return {
        "asset_id": asset_id,
        "path": path,
        "sha256": sha,
        "source_url": source_url,
        "rights_evidence_url": rights_url,
        "windows": windows,
    }


def validate_job(doc):
    """Validate the job document structure; returns a normalized plan."""
    _require_dict(doc, "job")
    batch_id = _require_str(doc.get("batch_id"), "batch_id")
    if not _BATCH_ID_RE.match(batch_id):
        raise JobError("unsafe batch_id %r" % batch_id)

    raw_sources = doc.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise JobError("sources must be a non-empty list")
    if len(raw_sources) > MAX_SOURCES:
        raise JobError(
            "%d sources supplied; at most %d allowed"
            % (len(raw_sources), MAX_SOURCES)
        )

    seen = set()
    sources = [_validate_source(item, i, seen)
               for i, item in enumerate(raw_sources)]
    return {"batch_id": batch_id, "sources": sources}


# --- Audio validation (everything is read before any output is made) --------
def _load_and_validate_audio(plan):
    total_bytes = 0
    for source in plan["sources"]:
        path = source["path"]
        if os.path.islink(path) or not os.path.isfile(path):
            raise JobError("source %s is not a regular file: %s"
                           % (source["asset_id"], path))
        size = os.path.getsize(path)
        if size <= 0:
            raise JobError("source %s is empty" % source["asset_id"])
        if size > MAX_SOURCE_BYTES:
            raise JobError(
                "source %s is %d bytes; limit is %d"
                % (source["asset_id"], size, MAX_SOURCE_BYTES)
            )
        total_bytes += size
        if total_bytes > MAX_TOTAL_SOURCE_BYTES:
            raise JobError(
                "total source size %d bytes exceeds limit %d"
                % (total_bytes, MAX_TOTAL_SOURCE_BYTES)
            )

        actual_sha = _sha256_file(path)
        if actual_sha != source["sha256"]:
            raise JobError(
                "sha256 mismatch for %s: job declares %s, file is %s"
                % (source["asset_id"], source["sha256"], actual_sha)
            )

        try:
            data, sample_rate = sf.read(path, dtype="float64",
                                        always_2d=True)
        except Exception as exc:  # libsndfile reports many error types
            raise JobError("cannot read source %s: %s"
                           % (source["asset_id"], exc))

        if sample_rate is None or sample_rate <= 0:
            raise JobError("source %s has invalid sample rate"
                           % source["asset_id"])
        frames, channels = data.shape
        if frames == 0 or channels == 0:
            raise JobError("source %s is empty" % source["asset_id"])
        if not np.isfinite(data).all():
            raise JobError("source %s contains non-finite samples"
                           % source["asset_id"])
        peak = float(np.max(np.abs(data)))
        if peak > 1.0:
            raise JobError(
                "source %s absolute peak %.6f exceeds 1.0"
                % (source["asset_id"], peak)
            )

        duration = float(frames) / float(sample_rate)
        for i, window in enumerate(source["windows"]):
            if window["start_sec"] >= duration:
                raise JobError(
                    "source %s windows[%d] start %.6fs is outside "
                    "audio duration %.6fs"
                    % (source["asset_id"], i, window["start_sec"], duration)
                )
            if window["end_sec"] > duration:
                raise JobError(
                    "source %s windows[%d] end %.6fs is outside "
                    "audio duration %.6fs"
                    % (source["asset_id"], i, window["end_sec"], duration)
                )
            start_frame = int(window["start_sec"] * sample_rate)
            end_frame = int(window["end_sec"] * sample_rate)
            if start_frame < 0 or end_frame > frames or start_frame >= end_frame:
                raise JobError(
                    "source %s windows[%d] frame bounds %d..%d invalid "
                    "for %d frames"
                    % (source["asset_id"], i, start_frame, end_frame, frames)
                )
            window["start_frame"] = start_frame
            window["end_frame"] = end_frame

        source["data"] = data
        source["sample_rate"] = int(sample_rate)
        source["channels"] = int(channels)
        source["frames"] = int(frames)


# --- Export -----------------------------------------------------------------
def _apply_endpoint_fades(segment, sample_rate):
    """5 ms linear fade in/out. Frame count follows int(seconds * sr)."""
    fade_frames = int(FADE_SEC * sample_rate)
    length = segment.shape[0]
    fade_frames = min(fade_frames, length // 2)
    if fade_frames <= 0:
        return segment
    fade_in = np.linspace(0.0, 1.0, fade_frames, dtype=np.float64)
    fade_out = np.linspace(1.0, 0.0, fade_frames, dtype=np.float64)
    column = segment.ndim == 2
    if column:
        fade_in = fade_in[:, np.newaxis]
        fade_out = fade_out[:, np.newaxis]
    segment[:fade_frames, ...] *= fade_in
    segment[-fade_frames:, ...] *= fade_out
    return segment


def _peak_rms_db(segment):
    peak = float(np.max(np.abs(segment)))
    rms = float(np.sqrt(np.mean(np.square(segment, dtype=np.float64))))
    if rms <= 0.0:
        return peak, None
    return peak, 20.0 * math.log10(rms)


def _export(plan, out_dir):
    candidates = []
    skipped = []
    total_windows = 0

    for source in plan["sources"]:
        data = source["data"]
        sample_rate = source["sample_rate"]
        for i, window in enumerate(source["windows"]):
            total_windows += 1
            start_frame = window["start_frame"]
            end_frame = window["end_frame"]
            # Copy: source data must never be mutated and fades are in-place.
            segment = data[start_frame:end_frame, :].copy(order="C")
            _apply_endpoint_fades(segment, sample_rate)
            peak, rms_db = _peak_rms_db(segment)

            if rms_db is None or rms_db < SILENCE_SKIP_RMS_DB:
                skipped.append({
                    "asset_id": source["asset_id"],
                    "window_index": i,
                    "start_sec": window["start_sec"],
                    "end_sec": window["end_sec"],
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "reason": "silence_rms_below_-50dbfs",
                    "rms_db": rms_db,
                })
                continue

            filename = "%s_%02d.wav" % (source["asset_id"], i)
            out_path = os.path.join(out_dir, filename)
            sf.write(out_path, segment, sample_rate,
                     format="WAV", subtype="FLOAT")
            output_sha = _sha256_file(out_path)
            candidates.append({
                "asset_id": source["asset_id"],
                "window_index": i,
                "filename": filename,
                "source_sha256": source["sha256"],
                "source_url": source["source_url"],
                "rights_evidence_url": source["rights_evidence_url"],
                "start_sec": window["start_sec"],
                "end_sec": window["end_sec"],
                "start_frame": start_frame,
                "end_frame": end_frame,
                "sample_rate": sample_rate,
                "channels": source["channels"],
                "frames": int(segment.shape[0]),
                "output_sha256": output_sha,
                "peak": peak,
                "rms_db": rms_db,
            })

    manifest = {
        "schema": "beatlab.supply_cuts.v1",
        "batch": plan["batch_id"],
        "content_identity": CONTENT_IDENTITY,
        "listening_review": LISTENING_REVIEW,
        "rights_scope": RIGHTS_SCOPE,
        "candidates": candidates,
        "skipped": skipped,
    }
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=False,
                  allow_nan=False)
        handle.write("\n")
    return total_windows, len(candidates), len(skipped)


def run(job_path, out_dir):
    with open(job_path, "r", encoding="utf-8") as handle:
        try:
            doc = json.load(handle)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise JobError("invalid job JSON: %s" % exc)

    plan = validate_job(doc)
    # All audio is verified before the output directory is created, so an
    # invalid later source can never leave partial output behind.
    _load_and_validate_audio(plan)

    if os.path.exists(out_dir) or os.path.islink(out_dir):
        raise JobError("output directory already exists: %s" % out_dir)
    os.makedirs(out_dir, exist_ok=False)

    try:
        total_windows, n_candidates, n_skipped = _export(plan, out_dir)
    except BaseException:
        # Only a directory we ourselves just created is removed.
        shutil.rmtree(out_dir, ignore_errors=True)
        raise

    print("batch=%s sources=%d windows=%d candidates=%d skipped=%d"
          % (plan["batch_id"], len(plan["sources"]), total_windows,
             n_candidates, n_skipped))
    return 0


def main(argv):
    if len(argv) != 3:
        sys.stderr.write(
            "usage: python pipeline/supply_cuts.py JOB.json OUTPUT_DIR\n"
        )
        return 2
    try:
        return run(argv[1], argv[2])
    except JobError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    except Exception as exc:  # export/IO failure: partial dir already removed
        sys.stderr.write("error: %s\n" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
