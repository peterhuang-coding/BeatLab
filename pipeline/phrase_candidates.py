"""Phrase-boundary candidate generation."""
from __future__ import annotations

import numbers
from typing import Iterable

import numpy as np

__all__ = ["phrase_windows"]

_MAX_BOUNDARIES = 10_000
_MAX_CANDIDATES = 500
_EPS = 1e-9


def _finite_number(value) -> bool:
    return (
        isinstance(value, numbers.Real)
        and not isinstance(value, bool)
        and bool(np.isfinite(value))
    )


def phrase_windows(
    duration_s,
    bounds_s,
    bpm=None,
    *,
    min_s=2.0,
    max_s=16.0,
    target_sizes_s=(2.0, 4.0, 8.0, 16.0),
    merge_tol_s=0.12,
):
    """Return candidate phrase windows between normalized boundaries.

    Each item is ``{"start_sec", "end_sec", "bars", "basis"}``.

    Boundary-derived candidates are generated first.  If a gap has no
    boundary-derived window covering it, duration-derived fallback windows are
    generated inside that gap.  The complete result is globally capped at 500
    candidates, with boundary candidates retained before fallbacks.
    """
    if not _finite_number(duration_s) or float(duration_s) < 0.0:
        raise ValueError("duration_s must be a finite non-negative number")
    if not _finite_number(min_s) or not _finite_number(max_s):
        raise ValueError("min_s and max_s must be finite numbers")
    min_s = float(min_s)
    max_s = float(max_s)
    if min_s <= 0.0 or max_s <= 0.0:
        raise ValueError("min_s and max_s must be positive numbers")
    if min_s > max_s:
        raise ValueError("min_s must not exceed max_s")
    if not _finite_number(merge_tol_s) or float(merge_tol_s) < 0.0:
        raise ValueError("merge_tol_s must be a finite non-negative number")
    if bpm is not None and (not _finite_number(bpm) or float(bpm) <= 0.0):
        raise ValueError("bpm must be a finite positive number or None")
    if not isinstance(target_sizes_s, Iterable) or isinstance(
        target_sizes_s, (str, bytes)
    ):
        raise ValueError("target_sizes_s must be an iterable of finite positive numbers")

    raw_targets = []
    for value in target_sizes_s:
        if not _finite_number(value) or float(value) <= 0.0:
            raise ValueError("target_sizes_s must contain finite positive numbers")
        raw_targets.append(float(value))
    if not raw_targets:
        raise ValueError("target_sizes_s must contain at least one positive number")

    if not isinstance(bounds_s, Iterable) or isinstance(bounds_s, (str, bytes)):
        raise ValueError("bounds_s must be an iterable of finite numbers")

    duration = float(duration_s)
    lo = min_s
    hi = max_s
    tol = float(merge_tol_s)

    # Validate and clamp boundaries before counting them.  The mandatory
    # endpoints are added after input validation.
    vals = []
    for value in bounds_s:
        if not _finite_number(value):
            raise ValueError("boundaries must all be finite numbers")
        x = float(value)
        vals.append(min(duration, max(0.0, x)))
        if len(vals) > _MAX_BOUNDARIES:
            raise ValueError(f"too many boundaries; limit is {_MAX_BOUNDARIES}")

    if duration < lo - _EPS:
        return []
    # Merge internal boundaries while preserving both mandatory endpoints.
    bounds = [0.0]
    for x in sorted(vals):
        if x - bounds[-1] > tol and duration - x > tol:
            bounds.append(x)
    bounds.append(duration)

    if bpm is None:
        target_widths = sorted({min(hi, x) for x in raw_targets})
        bar_len = None
    else:
        bar_len = 240.0 / float(bpm)
        target_widths = sorted({min(hi, bar_len * n) for n in (1, 2, 4, 8)})

    # A target wider than a gap can still be used in that gap after clipping.
    target_widths = [min(hi, max(lo, x)) for x in target_widths]
    target_widths = sorted({round(x, 10) for x in target_widths})

    boundary_rows = []
    fallback_rows = []
    seen_boundary = set()
    seen_fallback = set()

    def make_row(start, end, basis):
        start = max(0.0, min(duration, float(start)))
        end = max(0.0, min(duration, float(end)))
        if end < start:
            start, end = end, start
        width = end - start
        if width < lo - _EPS or width > hi + _EPS:
            return None
        bars = None
        if bar_len is not None:
            bars = float(round(width / bar_len, 9))
        return {
            "start_sec": round(start, 9),
            "end_sec": round(end, 9),
            "bars": bars,
            "basis": basis,
        }

    n = len(bounds)

    # For each start boundary choose the closest later boundary to one target
    # width.  A start can therefore contribute at most one boundary pair.
    pair_count = 0
    for i, start in enumerate(bounds[:-1] if n > 2 else []):
        best = None
        for j in range(i + 1, n):
            pair_count += 1
            if pair_count > 500_000:
                raise ValueError("boundary pair candidate work limit exceeded")
            end = bounds[j]
            width = end - start
            if width < lo - _EPS:
                continue
            if width > hi + _EPS:
                break
            for target in target_widths:
                score = (abs(width - target), width, j)
                if best is None or score < best[0]:
                    best = (score, end)
        if best is not None and len(boundary_rows) < _MAX_CANDIDATES:
            row = make_row(start, best[1], "boundary_pair")
            if row is not None:
                key = (row["start_sec"], row["end_sec"])
                if key not in seen_boundary:
                    seen_boundary.add(key)
                    boundary_rows.append(row)

    fallback_cap = _MAX_CANDIDATES - len(boundary_rows)
    seen_fallback.update(seen_boundary)
    for gap_start, gap_end in zip(bounds[:-1], bounds[1:]):
        if fallback_cap <= 0:
            break
        gap_len = gap_end - gap_start
        if n != 2 and gap_len <= hi + _EPS:
            continue

        usable_targets = []
        for target in target_widths:
            width = min(hi, max(lo, min(target, gap_len)))
            if lo - _EPS <= width <= hi + _EPS and gap_len + _EPS >= width:
                usable_targets.append(width)
        if not usable_targets:
            width = min(hi, max(lo, gap_len))
            if lo - _EPS <= width <= hi + _EPS:
                usable_targets.append(width)

        for target in sorted({round(x, 10) for x in usable_targets}):
            if fallback_cap <= 0:
                break
            target = min(target, gap_len)
            hop = target / 2.0
            if hop <= 0.0:
                continue

            # Generate starts in bounded O(cap) work without np.arange; this
            # avoids allocating an enormous array for a very long duration.
            k = 0
            while fallback_cap > 0:
                start = gap_start + k * hop
                stop_width = target
                if start > gap_end - target + _EPS:
                    start = gap_end - target
                    stop_width = target
                if start < gap_start - _EPS:
                    break
                end = min(start + stop_width, gap_end)
                if gap_end - start <= target + _EPS:
                    end = gap_end
                row = make_row(start, end, "duration_fallback")
                if row is not None:
                    key = (row["start_sec"], row["end_sec"])
                    if key not in seen_fallback:
                        seen_fallback.add(key)
                        fallback_rows.append(row)
                        fallback_cap -= 1
                if gap_end - start <= target + _EPS:
                    break
                k += 1
                if k > _MAX_CANDIDATES + 1:
                    break

    result = boundary_rows + fallback_rows
    result.sort(key=lambda r: (r["start_sec"], r["end_sec"], r["basis"]))
    return result
