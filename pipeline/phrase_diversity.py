from __future__ import annotations

import json
import math
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _finite_number(value: Any) -> Optional[float]:
    number = _number(value)
    if number is None or not math.isfinite(number):
        return None
    return number


def _valid_interval(value: Any) -> Optional[Tuple[float, float]]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    start = _finite_number(value[0])
    end = _finite_number(value[1])
    if start is None or end is None or start > end:
        return None
    return start, end


def interval_overlap(a: Sequence[float], b: Sequence[float]) -> float:
    first = _valid_interval(a)
    second = _valid_interval(b)
    if first is None or second is None:
        raise ValueError("intervals must be finite two-element lists or tuples with start <= end")
    start_a, end_a = first
    start_b, end_b = second
    return max(0.0, min(end_a, end_b) - max(start_a, start_b))


def interval_iou(a: Sequence[float], b: Sequence[float]) -> float:
    first = _valid_interval(a)
    second = _valid_interval(b)
    if first is None or second is None:
        raise ValueError("intervals must be finite two-element lists or tuples with start <= end")

    start_a, end_a = first
    start_b, end_b = second
    overlap = interval_overlap(a, b)
    union = (end_a - start_a) + (end_b - start_b) - overlap
    return 0.0 if union <= 0.0 else overlap / union


def _identity_part(value: Any) -> Optional[str]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, str)):
        return json.dumps(str(value), separators=(",", ":"), ensure_ascii=False)
    if isinstance(value, float):
        return json.dumps(str(value), separators=(",", ":"), ensure_ascii=False) if math.isfinite(value) else None
    return None


def normalize_source_id(moment: Mapping[str, Any]) -> Optional[str]:
    if not isinstance(moment, Mapping):
        return None

    asset_id = moment.get("asset_id")
    if isinstance(asset_id, str) and not asset_id.strip():
        return None
    asset_part = _identity_part(asset_id)
    if asset_part is None:
        return None

    if "stem" not in moment or moment.get("stem") is None:
        return f"{asset_part}|{json.dumps('defaultsource', separators=(',', ':'), ensure_ascii=False)}"

    stem_part = _identity_part(moment.get("stem"))
    if stem_part is None:
        return None
    return f"{asset_part}|{stem_part}"


def _candidate_interval(moment: Mapping[str, Any]) -> Optional[Tuple[float, float]]:
    value = moment.get("interval")
    interval = _valid_interval(value)
    if interval is None:
        return None

    start, end = interval
    if start < 0.0 or end <= start:
        return None
    return start, end


def _moment_id(moment: Mapping[str, Any], index: int) -> Tuple[Any, ...]:
    value = moment.get("id")
    if isinstance(value, bool) or value is None:
        return ("index", index)
    if isinstance(value, (int, str)):
        return ("id", str(value))
    if isinstance(value, float) and math.isfinite(value):
        return ("id", str(value))
    return ("index", index)


def select_diverse_moments(
    moments: Iterable[Mapping[str, Any]],
    max_keep: int,
    max_same_source: Optional[int] = None,
    max_overlap_seconds: float = 0.5,
    max_iou: float = 0.25,
) -> list[Mapping[str, Any]]:
    if isinstance(max_keep, bool) or not isinstance(max_keep, int) or max_keep < 0:
        raise ValueError("max_keep must be a nonnegative integer")
    if max_same_source is not None and (
        isinstance(max_same_source, bool)
        or not isinstance(max_same_source, int)
        or max_same_source < 0
    ):
        raise ValueError("max_same_source must be a nonnegative integer or None")

    overlap_limit = _finite_number(max_overlap_seconds)
    iou_limit = _finite_number(max_iou)
    if overlap_limit is None or overlap_limit < 0.0:
        raise ValueError("max_overlap_seconds must be a finite nonnegative number")
    if iou_limit is None or iou_limit < 0.0 or iou_limit > 1.0:
        raise ValueError("max_iou must be a finite number between 0 and 1")

    if max_keep == 0:
        return []

    scored = []
    unscored = []

    for index, moment in enumerate(moments):
        if not isinstance(moment, Mapping):
            continue

        interval = _candidate_interval(moment)
        source = normalize_source_id(moment)
        if interval is None or source is None:
            continue

        start, end = interval
        record = {
            "moment": moment,
            "start": start,
            "end": end,
            "source": source,
            "id": _moment_id(moment, index),
            "index": index,
        }

        if "total" not in moment or moment.get("total") is None:
            unscored.append(record)
            continue

        total = _number(moment.get("total"))
        if total is None:
            unscored.append(record)
            continue
        if not math.isfinite(total):
            continue
        record["total"] = total
        scored.append(record)

    scored.sort(
        key=lambda item: (
            -item["total"],
            item["start"],
            item["end"],
            item["source"],
            item["id"],
            item["index"],
        )
    )
    unscored.sort(
        key=lambda item: (
            item["start"],
            item["end"],
            item["source"],
            item["id"],
            item["index"],
        )
    )
    ranked = scored + unscored

    selected = []
    chosen_by_source = {}

    for candidate in ranked:
        if len(selected) >= max_keep:
            break

        source = candidate["source"]
        if max_same_source is not None and chosen_by_source.get(source, 0) >= max_same_source:
            continue

        blocked = False
        for chosen in selected:
            if source != chosen["source"]:
                continue

            a = (candidate["start"], candidate["end"])
            b = (chosen["start"], chosen["end"])
            overlap = interval_overlap(a, b)
            iou = interval_iou(a, b)
            if overlap > overlap_limit or iou > iou_limit:
                blocked = True
                break

        if blocked:
            continue

        selected.append(candidate)
        chosen_by_source[source] = chosen_by_source.get(source, 0) + 1

    selected.sort(
        key=lambda item: (
            item["start"],
            item["end"],
            item["source"],
            item["id"],
            item["index"],
        )
    )
    return [item["moment"] for item in selected]
