from __future__ import annotations

from dataclasses import dataclass
import math
import numpy as np


@dataclass(frozen=True)
class BeatGrid:
    start_sec: float
    end_sec: float
    beat_period_sec: float
    offset_sec: float
    confidence: float
    source_kind: str
    ambiguity: str
    beat_count: int
    residual_sec: float


def _finite(x, name: str) -> float:
    try:
        y = float(x)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} must be finite") from e
    if not math.isfinite(y):
        raise ValueError(f"{name} must be finite")
    return y


def _circular(x, period):
    return (np.asarray(x, dtype=float) + period / 2.0) % period - period / 2.0


def _normalize_events(events_sec, start, end):
    arr = np.asarray(events_sec, dtype=float)
    if arr.ndim != 1:
        raise ValueError("events must be one-dimensional")
    if arr.size > 10000:
        raise ValueError("events count must not exceed 10000")
    if arr.size and not np.all(np.isfinite(arr)):
        raise ValueError("events must be finite")

    # The contract accepts sorted-unique input.  Sorting and de-duplicating is
    # the explicit normalization rather than rejecting equivalent input.
    arr = np.unique(arr)
    usable = arr[(arr >= start) & (arr <= end)]
    if arr.size and usable.size == 0:
        raise ValueError("all events are outside region")
    return usable


def _fallback(start, end, expected_bpm, min_bpm, max_bpm):
    duration = end - start
    period = duration / 4.0
    if expected_bpm is not None and min_bpm <= expected_bpm <= max_bpm:
        period = 60.0 / expected_bpm

    if not math.isfinite(period) or period <= 0.0:
        period = duration / 4.0
    if not math.isfinite(period) or period <= 0.0:
        period = 0.5

    count = int(math.floor((end - start) / period)) + 1
    return BeatGrid(
        start,
        end,
        float(period),
        0.0,
        0.0,
        "fallback_equal",
        "none",
        max(0, count),
        0.0,
    )


def _candidate_score(events, start, period, provided):
    residues = np.mod(events - start, period)
    phases = np.unique(np.mod(residues, period))

    best = None
    for phase in phases:
        raw_residual = np.abs(_circular(residues - phase, period))
        if provided:
            scored_residual = raw_residual
            clip_radius = period / 2.0
        else:
            clip_radius = period * 0.12
            scored_residual = np.minimum(raw_residual, clip_radius)

        mean_raw = float(np.mean(raw_residual))
        mean_scored = float(np.mean(scored_residual))
        coverage = float(np.mean(raw_residual <= period * 0.18))
        score = mean_scored + (1.0 - coverage) * period * 0.18

        candidate = (score, mean_raw, -coverage, float(phase))
        if best is None or candidate < best:
            best = candidate

    score, mean_raw, neg_coverage, phase = best
    return score, mean_raw, float(-neg_coverage), phase


def _provided_fit(events, start, period):
    score, mean_raw, coverage, phase = _candidate_score(
        events, start, period, True
    )
    confidence = float(
        np.clip(1.0 - mean_raw / (0.08 * period), 0.0, 1.0)
    )
    return phase, confidence, mean_raw, coverage


def _onset_confidence(period, mean_raw, coverage, ambiguity):
    residual_term = np.clip(1.0 - mean_raw / (0.10 * period), 0.0, 1.0)
    coverage_term = np.clip((coverage - 0.45) / 0.50, 0.0, 1.0)
    confidence = 0.70 * float(residual_term) + 0.30 * float(coverage_term)
    if ambiguity == "octave_possible":
        confidence = min(confidence, 0.75)
    return float(np.clip(confidence, 0.0, 1.0))


def fit_beat_grid(
    events_sec,
    *,
    start_sec,
    end_sec,
    expected_bpm=None,
    source_kind="provided_beats",
    min_bpm=40,
    max_bpm=280,
) -> BeatGrid:
    start = _finite(start_sec, "start_sec")
    end = _finite(end_sec, "end_sec")
    if end <= start:
        raise ValueError("end_sec must be greater than start_sec")

    min_bpm = _finite(min_bpm, "min_bpm")
    max_bpm = _finite(max_bpm, "max_bpm")
    if min_bpm <= 0.0 or max_bpm <= min_bpm:
        raise ValueError("invalid bpm range")

    if expected_bpm is not None:
        expected_bpm = _finite(expected_bpm, "expected_bpm")
        if expected_bpm <= 0.0:
            raise ValueError("expected_bpm must be positive")

    if source_kind not in ("provided_beats", "onsets"):
        raise ValueError("source_kind must be 'provided_beats' or 'onsets'")

    usable = _normalize_events(events_sec, start, end)
    if usable.size < 4:
        return _fallback(start, end, expected_bpm, min_bpm, max_bpm)

    pmin, pmax = 60.0 / max_bpm, 60.0 / min_bpm
    intervals = np.diff(usable)
    positive = intervals[intervals > 0.0]
    if positive.size == 0:
        return _fallback(start, end, expected_bpm, min_bpm, max_bpm)

    median_interval = float(np.median(positive))
    provided = (
        source_kind == "provided_beats"
        and pmin <= median_interval <= pmax
    )

    if provided:
        period = median_interval
        phase, confidence, residual, _coverage = _provided_fit(
            usable, start, period
        )
        ambiguity = "none"
    else:
        candidate_periods = {
            median_interval,
            median_interval / 2.0,
            median_interval * 2.0,
        }
        if expected_bpm is not None:
            candidate_periods.add(60.0 / expected_bpm)

        candidates = sorted(
            p
            for p in candidate_periods
            if math.isfinite(p) and p > 0.0 and pmin <= p <= pmax
        )
        if not candidates:
            return _fallback(start, end, expected_bpm, min_bpm, max_bpm)

        scored = []
        expected_period = (
            60.0 / expected_bpm if expected_bpm is not None else None
        )

        for period in candidates:
            score, mean_raw, coverage, phase = _candidate_score(
                usable, start, period, False
            )
            scored.append(
                {
                    "score": score,
                    "period": period,
                    "residual": mean_raw,
                    "coverage": coverage,
                    "phase": phase,
                }
            )

        def preference(item):
            expected_match = 1
            if expected_period is not None and math.isclose(
                item["period"],
                expected_period,
                rel_tol=1e-9,
                abs_tol=1e-11,
            ):
                expected_match = 0
            return (item["score"], expected_match, item["period"])

        minimum_score = min(item["score"] for item in scored)
        tied = [item for item in scored
                if item["score"] <= minimum_score + 1e-9]
        best = min(tied, key=lambda item: preference(item)[1:])
        tolerance = 1e-7 * max(
            1.0,
            max(abs(item["score"]) for item in scored),
        )
        near_best = [
            item
            for item in scored
            if abs(item["score"] - best["score"]) <= tolerance
        ]

        period = best["period"]
        phase = best["phase"]
        residual = best["residual"]
        ambiguity = "octave_possible" if len(near_best) > 1 else "none"
        confidence = _onset_confidence(
            period,
            residual,
            best["coverage"],
            ambiguity,
        )

    phase = float(np.mod(phase, period))
    if math.isclose(phase, period, rel_tol=1e-10, abs_tol=1e-11):
        phase = 0.0

    if end >= start + phase:
        count = int(math.floor((end - (start + phase)) / period)) + 1
    else:
        count = 0

    return BeatGrid(
        start,
        end,
        float(period),
        phase,
        float(confidence),
        "provided_beats" if provided else "onsets",
        ambiguity,
        max(0, count),
        float(residual),
    )


def slice_n_boundaries(grid: BeatGrid, *, end_sec=None, n: int = 16):
    if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
        raise ValueError("n must be a positive integer")

    start = _finite(grid.start_sec, "start_sec")
    stop = _finite(
        grid.end_sec if end_sec is None else end_sec,
        "end_sec",
    )
    if stop <= start:
        raise ValueError("end_sec must be greater than start_sec")

    return [start + (stop - start) * i / n for i in range(n + 1)]
