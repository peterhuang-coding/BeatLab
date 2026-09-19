"""Deterministic phrase-level motif event scheduling.

This module only plans non-overlapping step events.  It deliberately does not
describe recombination, transposition, reversal, or rendered audio silence.
"""
from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Mapping, Sequence

SLOTS = (0, 4, 8)
RESERVED_STEP = 12
STEPS_PER_BAR = 16
MAX_RUN = 2
_DEFAULT_EVENTS_PER_BAR = {
    "intro": 1,
    "verse": 2,
    "verse_variation": 2,
    "hook": 3,
    "bridge": 1,
    "outro": 1,
    "unknown": 1,
}
_ANCHOR_SECTIONS = frozenset({"intro", "hook", "outro"})


def _is_real_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validated_sections(sections: Any) -> List[Mapping[str, Any]]:
    if not isinstance(sections, Sequence) or isinstance(sections, (str, bytes)):
        raise TypeError("sections must be a list of section dictionaries")

    clean: List[Mapping[str, Any]] = []
    for index, section in enumerate(sections):
        if not isinstance(section, Mapping):
            raise TypeError(f"section {index} must be a mapping")
        name = section.get("name")
        bars = section.get("bars")
        if not isinstance(name, str) or not name:
            raise TypeError(f"section {index} name must be a non-empty string")
        if not _is_real_int(bars) or bars < 0:
            raise ValueError(f"section {index} bars must be a non-negative integer")
        clean.append(section)
    return clean


def _validated_overrides(overrides: Any) -> Dict[str, int]:
    if overrides is None:
        return {}
    if not isinstance(overrides, Mapping):
        raise TypeError("events_per_bar must be a mapping")

    result: Dict[str, int] = {}
    for name, count in overrides.items():
        if not isinstance(name, str) or not name:
            raise ValueError("events_per_bar keys must be non-empty strings")
        if not _is_real_int(count) or not 0 <= count <= 3:
            raise ValueError(f"events_per_bar[{name!r}] must be an integer from 0 to 3")
        result[name] = count
    return result


def _choose_motif(
    rng: random.Random,
    n_motifs: int,
    recent: Sequence[int],
    *,
    anchor: bool = False,
    preferred: Sequence[int] = (),
) -> int:
    """Choose a motif while preventing three consecutive choices in a section."""
    if anchor:
        return 0

    forbidden = recent[-2] if len(recent) >= 2 and recent[-1] == recent[-2] else None
    pool = [motif for motif in range(n_motifs) if motif != forbidden]
    if not pool:
        # n_motifs == 1: the caller is responsible for resting in this slot
        # whenever a third consecutive event would otherwise be emitted.
        return 0

    preferred_pool = [motif for motif in preferred if motif in pool]
    if preferred_pool:
        return rng.choice(preferred_pool)

    varied_pool = [motif for motif in pool if motif not in recent[-2:]]
    return rng.choice(varied_pool or pool)


def plan_phrase_events(
    sections: Any,
    n_motifs: Any,
    seed: Any,
    *,
    events_per_bar: Any = None,
    phrase_steps: Any = 4,
) -> Dict[str, Any]:
    """Return deterministic scheduled ``events`` and indexed ``metrics``.

    Counts in each section are nominal: planned events receive fixed
    ``phrase_steps`` durations and never overlap.  The final four steps of
    every bar remain reserved.

    Multiple motifs have no three identical consecutive events per section.
    With one motif, each bar emits at most two events and then rests; a run
    across separate bars is unavoidable and explicitly exempt.
    """
    clean_sections = _validated_sections(sections)
    if not _is_real_int(n_motifs) or n_motifs <= 0:
        raise ValueError("n_motifs must be a positive integer")
    if (
        not isinstance(seed, (int, float))
        or isinstance(seed, bool)
        or not math.isfinite(seed)
    ):
        raise ValueError("seed must be a finite number")
    if not _is_real_int(phrase_steps) or not 1 <= phrase_steps <= 4:
        raise ValueError("phrase_steps must be an integer from 1 to 4")

    overrides = _validated_overrides(events_per_bar)
    rng = random.Random(seed)

    events: List[Dict[str, Any]] = []
    metrics: List[Dict[str, Any]] = []
    global_bar = 0

    for section_index, raw_section in enumerate(clean_sections):
        name = str(raw_section["name"])
        bars = int(raw_section["bars"])
        nominal_count = overrides.get(name, _DEFAULT_EVENTS_PER_BAR.get(name, 1))
        section_events_before = len(events)
        section_recent: List[int] = []

        hook_theme_seen = False
        hook_recall_needed = False

        for section_bar in range(bars):
            bar_global = global_bar + section_bar
            bar_events: List[Dict[str, Any]] = []
            if name == "hook" and section_bar == 1:
                hook_recall_needed = True

            for slot_index in range(nominal_count):
                step = SLOTS[slot_index]
                first_event_in_section = (
                    len(events) == section_events_before
                    and not bar_events
                )
                anchor = (
                    name in _ANCHOR_SECTIONS
                    and first_event_in_section
                    and slot_index == 0
                )

                preferred: List[int] = []
                if name == "hook" and hook_recall_needed and not anchor:
                    # Prefer an actual return to motif 0 after the first hook
                    # bar has established it.  Variety choices remain subject
                    # to the section-local run limit.
                    preferred = [0]

                # With one motif, rest before creating a run longer than two.
                # An anchor is still emitted because intro/hook/outro must be
                # able to restart on motif 0 across a section boundary.
                if (
                    n_motifs == 1
                    and not anchor
                    and len(section_recent) >= 2
                    and section_recent[-1] == 0
                    and section_recent[-2] == 0
                ):
                    break

                motif_id = _choose_motif(
                    rng,
                    n_motifs,
                    section_recent,
                    anchor=anchor,
                    preferred=preferred,
                )
                event = {
                    "section_index": section_index,
                    "section": name,
                    "bar": bar_global,
                    "section_bar": section_bar,
                    "step": step,
                    "motif_id": motif_id,
                    "duration_steps": phrase_steps,
                }
                bar_events.append(event)
                section_recent.append(motif_id)

                if name == "hook":
                    if motif_id == 0:
                        if hook_theme_seen:
                            hook_recall_needed = False
                        hook_theme_seen = True
                    elif hook_theme_seen and section_bar >= 1:
                        # A non-theme event after the opening hook bar creates
                        # an opportunity to bring theme 0 back later.
                        hook_recall_needed = True

            if bar_events:
                events.extend(bar_events)
                # Only a single-motif schedule uses the bar rest to reset a run.
                if n_motifs == 1:
                    section_recent = []

        section_total = len(events) - section_events_before
        active_steps = section_total * phrase_steps
        available_steps = bars * STEPS_PER_BAR
        metrics.append(
            {
                "section_index": section_index,
                "section": name,
                "bars": bars,
                "global_start_bar": global_bar,
                "event_count": section_total,
                "active_steps": active_steps,
                "density": (active_steps / available_steps) if available_steps else 0.0,
                "nominal_events_per_bar": nominal_count,
            }
        )
        global_bar += bars

    return {"events": events, "metrics": metrics}
