"""BeatLab 统一编排清单。

`arrangement.json` 是试听渲染与 DAW 工程导出的共同事实源：

- 时间线统一使用 beat，音频源区间同时保留采样帧；
- 每个轨道、事件和素材都有稳定 ID；
- 鼓事件记录实际使用的 one-shot，而不是只记录可选 kit；
- 明确区分 native / baked / reference 三类可编辑能力；
- 不写入运行时随机数或时间戳，同一 run 重建可得到相同清单。
"""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

import common

SCHEMA_VERSION = "1.0.0"
TIME_SIGNATURE = {"numerator": 4, "denominator": 4}
STEPS_PER_BAR = 16
BEATS_PER_BAR = 4.0
DRUM_NOTES = {"kick": 36, "snare": 38, "hat": 42, "oh": 46, "perc": 39}
DRUM_DURATIONS = {"kick": 0.125, "snare": 0.125, "hat": 0.125, "oh": 0.25, "perc": 0.25}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix: str, *parts: Any) -> str:
    payload = _canonical(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:16]}"


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_source_path(path: str | Path) -> Path | None:
    """解析清单中的素材路径；相对路径以 BeatLab ROOT 为基准。"""
    if not path:
        return None
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None
    for base in (common.ROOT, common.LIBRARY):
        resolved = base / candidate
        if resolved.is_file():
            return resolved
    return None


def _portable_source_path(path: str | Path) -> str:
    value = Path(path)
    if not value.is_absolute():
        return value.as_posix()
    try:
        return value.resolve().relative_to(common.ROOT.resolve()).as_posix()
    except ValueError:
        return str(value)


def _audio_info(path: str | Path) -> tuple[int, int]:
    resolved = resolve_source_path(path)
    if resolved is None:
        return 44100, 0
    try:
        import soundfile as sf

        info = sf.info(str(resolved))
        return int(info.samplerate), int(info.frames)
    except Exception:
        return 44100, 0


def _asset(
    source_path: str,
    *,
    role: str,
    package_dir: str,
    origin_asset_id: str | None = None,
) -> dict:
    portable = _portable_source_path(source_path)
    resolved = resolve_source_path(portable)
    asset_id = stable_id("asset", role, origin_asset_id or "", portable)
    suffix = Path(portable).suffix or ".wav"
    return {
        "asset_id": asset_id,
        "origin_asset_id": origin_asset_id,
        "role": role,
        "source_path": portable,
        "package_path": f"Samples/{package_dir}/{asset_id}{suffix}",
        "sha256": _sha256(resolved) if resolved else None,
        "exists": bool(resolved),
    }


def _audio_operations(event: dict) -> list[dict]:
    operations = [
        {"op": "trim", "capability": "native"},
        {"op": "highpass", "hz": 100.0, "capability": "baked"},
    ]
    if event.get("lp_hz"):
        operations.append({"op": "lowpass", "hz": float(event["lp_hz"]), "capability": "baked"})
    if event.get("reverse"):
        operations.append({"op": "reverse", "capability": "baked"})
    if event.get("stretch_to"):
        operations.append({
            "op": "time_stretch",
            "target_seconds": round(float(event["stretch_to"]), 6),
            "capability": "baked",
        })
    operations.extend([
        {"op": "gain", "value": round(float(event.get("gain", 1.0)), 6), "capability": "native"},
        {"op": "pan", "value": round(float(event.get("pan", 0.0)), 6), "capability": "native"},
    ])
    return operations


def _build_audio_events(
    placements: list[dict],
    *,
    track_id: str,
    candidate_id: str,
    bpm: float,
    assets: dict[str, dict],
) -> list[dict]:
    events = []
    for index, placement in enumerate(placements):
        source_path = str(placement.get("file") or "")
        origin_asset_id = str(placement.get("sample_id") or "") or None
        source_asset = _asset(
            source_path,
            role="source_audio",
            package_dir="Original",
            origin_asset_id=origin_asset_id,
        )
        assets[source_asset["asset_id"]] = source_asset
        sample_rate, total_frames = _audio_info(source_path)
        start_sec = max(0.0, float(placement.get("start_sec", 0.0)))
        end_sec = max(start_sec, float(placement.get("end_sec", start_sec)))
        start_frame = min(total_frames, round(start_sec * sample_rate)) if total_frames else round(start_sec * sample_rate)
        end_frame = min(total_frames, round(end_sec * sample_rate)) if total_frames else round(end_sec * sample_rate)
        operations = _audio_operations(placement)
        media_id = stable_id(
            "media", source_asset["asset_id"], start_frame, end_frame,
            [op for op in operations if op["op"] not in ("gain", "pan")],
        )
        processed_asset = {
            "asset_id": media_id,
            "role": "processed_audio",
            "derived_from": source_asset["asset_id"],
            "source_path": f"processed/{media_id}.wav",
            "package_path": f"Samples/Processed/{media_id}.wav",
            "sha256": None,
            "exists": False,
        }
        assets[media_id] = processed_asset
        start_beat = float(placement.get("bar", 0)) * BEATS_PER_BAR + float(placement.get("step", 0)) / 4.0
        duration_seconds = (
            float(placement["stretch_to"])
            if placement.get("stretch_to")
            else max(0.0, end_sec - start_sec)
        )
        clip_id = stable_id(
            "clip", candidate_id, track_id, index, round(start_beat, 6), media_id,
        )
        events.append({
            "clip_id": clip_id,
            "track_id": track_id,
            "asset_id": source_asset["asset_id"],
            "media_asset_id": media_id,
            "origin_asset_id": origin_asset_id,
            "start_beat": round(start_beat, 6),
            "duration_beats": round(duration_seconds * bpm / 60.0, 6),
            "source_start_frame": start_frame,
            "source_end_frame": end_frame,
            "source_sample_rate": sample_rate,
            "gain": round(float(placement.get("gain", 1.0)), 6),
            "pan": round(float(placement.get("pan", 0.0)), 6),
            "section": str(placement.get("section") or ""),
            "pad": placement.get("pad"),
            "midi_note": placement.get("midi_note"),
            "operations": operations,
            "editability": "baked",
        })
    return events


def _build_drum_events(
    spec,
    *,
    candidate_id: str,
    kit: dict[str, list[str]],
    assets: dict[str, dict],
) -> tuple[list[dict], dict[str, dict]]:
    events = []
    mappings: dict[str, dict] = {}
    rng = random.Random(spec.beat_id)
    chosen_by_class = {
        drum_class: str(rng.choice(list(paths)))
        for drum_class, paths in kit.items()
        if paths
    }
    for bar_key in sorted(spec.drum_pattern, key=lambda value: int(value)):
        bar = int(bar_key)
        tracks = spec.drum_pattern[bar_key]
        for drum_class in tracks:
            source_path = chosen_by_class.get(drum_class)
            if not source_path:
                continue
            for step_key in sorted(tracks[drum_class], key=lambda value: float(value)):
                hit = tracks[drum_class][step_key] or {}
                source_asset = _asset(source_path, role="drum_sample", package_dir="DrumKit")
                assets[source_asset["asset_id"]] = source_asset
                note = DRUM_NOTES[drum_class]
                mappings[str(note)] = {
                    "note": note,
                    "drum_class": drum_class,
                    "asset_id": source_asset["asset_id"],
                    "package_path": source_asset["package_path"],
                }
                offset_ms = float(hit.get("offset_ms", 0.0))
                start_beat = bar * BEATS_PER_BAR + float(step_key) / 4.0 + offset_ms * spec.bpm / 60000.0
                clip_id = stable_id(
                    "note", candidate_id, "track-drums", bar, drum_class,
                    float(step_key), source_asset["asset_id"],
                )
                events.append({
                    "clip_id": clip_id,
                    "track_id": "track-drums",
                    "asset_id": source_asset["asset_id"],
                    "start_beat": round(start_beat, 6),
                    "duration_beats": DRUM_DURATIONS.get(drum_class, 0.125),
                    "midi_note": note,
                    "velocity": int(hit.get("velocity", 96)),
                    "micro_timing_ms": round(offset_ms, 6),
                    "drum_class": drum_class,
                    "editability": "native",
                })
    return events, mappings


def _build_bass_events(spec, *, candidate_id: str, assets: dict[str, dict]) -> list[dict]:
    instrument_id = "instrument-bass-sine-v1"
    assets[instrument_id] = {
        "asset_id": instrument_id,
        "role": "instrument",
        "name": "BeatLab Sine Sub",
        "engine": "sine-envelope-v1",
        "capability": "reference",
        "note": "试听渲染可复现；Ableton 设备绑定需由 Live 12 导入器完成",
    }
    events = []
    for bar_key in sorted(spec.bass_pattern, key=lambda value: int(value)):
        bar = int(bar_key)
        for step_key in sorted(spec.bass_pattern[bar_key], key=lambda value: float(value)):
            note = int(spec.bass_pattern[bar_key][step_key])
            start_beat = bar * BEATS_PER_BAR + float(step_key) / 4.0
            events.append({
                "clip_id": stable_id("note", candidate_id, "track-bass", bar, float(step_key), note),
                "track_id": "track-bass",
                "asset_id": instrument_id,
                "start_beat": round(start_beat, 6),
                "duration_beats": 0.5,
                "midi_note": note,
                "velocity": 96,
                "editability": "native",
            })
    return events


def build_arrangement(spec, recipe: dict, kit: dict[str, list[str]]) -> dict:
    """由 BeatSpec、Recipe 和已解析 kit 生成候选的唯一编排清单。"""
    run_id = str(getattr(spec, "run_id", "") or recipe.get("recipe_id", "").split(":", 1)[0])
    candidate_id = str(getattr(spec, "recipe_kind", "") or recipe.get("kind") or "candidate")
    assets: dict[str, dict] = {}

    sample_events = _build_audio_events(
        list(spec.chop_placements), track_id="track-samples", candidate_id=candidate_id,
        bpm=float(spec.bpm), assets=assets,
    )
    vocal_events = _build_audio_events(
        list(spec.vocal_placements), track_id="track-vocals", candidate_id=candidate_id,
        bpm=float(spec.bpm), assets=assets,
    )
    drum_events, drum_mappings = _build_drum_events(
        spec, candidate_id=candidate_id, kit=kit, assets=assets,
    )
    bass_events = _build_bass_events(spec, candidate_id=candidate_id, assets=assets)

    reference_path = f"beats/{run_id}/{candidate_id}/full_mix.wav"
    reference_asset = _asset(reference_path, role="reference_mix", package_dir="Reference")
    reference_asset["package_path"] = "reference/full_mix.wav"
    assets[reference_asset["asset_id"]] = reference_asset

    sections = []
    start_beat = 0.0
    for section in spec.sections:
        duration_beats = float(section.bars) * BEATS_PER_BAR
        sections.append({
            "section_id": stable_id("section", candidate_id, section.name, start_beat, section.bars),
            "name": section.name,
            "start_beat": start_beat,
            "duration_beats": duration_beats,
            "bars": int(section.bars),
            "energy": round(float(section.energy), 6),
        })
        start_beat += duration_beats

    tracks = [
        {
            "track_id": "track-samples",
            "name": "Sample Chops",
            "type": "audio",
            "muted": False,
            "events": sample_events,
            "automation": [],
        },
        {
            "track_id": "track-vocals",
            "name": "Vocal / Texture",
            "type": "audio",
            "muted": False,
            "events": vocal_events,
            "automation": [],
        },
        {
            "track_id": "track-drums",
            "name": "Drums",
            "type": "midi",
            "muted": False,
            "midi_file": "MIDI/drums.mid",
            "device_binding": {
                "format": "ableton_drum_rack",
                "status": "planned",
                "mappings": drum_mappings,
            },
            "events": drum_events,
            "automation": [],
        },
        {
            "track_id": "track-bass",
            "name": "Bass",
            "type": "midi",
            "muted": False,
            "midi_file": "MIDI/bass.mid",
            "device_binding": {
                "format": "ableton_instrument",
                "status": "planned",
                "instrument_asset_id": "instrument-bass-sine-v1",
            },
            "events": bass_events,
            "automation": [],
        },
        {
            "track_id": "track-reference",
            "name": "Reference Mix (A/B)",
            "type": "audio",
            "muted": True,
            "events": [{
                "clip_id": stable_id("clip", candidate_id, "reference"),
                "track_id": "track-reference",
                "asset_id": reference_asset["asset_id"],
                "start_beat": 0.0,
                "duration_beats": round(float(spec.total_bars) * BEATS_PER_BAR, 6),
                "editability": "reference",
            }],
            "automation": [],
        },
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "arrangement_id": stable_id("arrangement", run_id, candidate_id, recipe.get("recipe_id")),
        "run_id": run_id,
        "candidate_id": candidate_id,
        "recipe_id": recipe.get("recipe_id"),
        "recipe_kind": recipe.get("kind"),
        "bpm": round(float(spec.bpm), 6),
        "time_signature": TIME_SIGNATURE,
        "sample_rate": 44100,
        "total_bars": int(spec.total_bars),
        "duration_beats": round(float(spec.total_bars) * BEATS_PER_BAR, 6),
        "sections": sections,
        "tracks": tracks,
        "assets": sorted(assets.values(), key=lambda item: item["asset_id"]),
        "capabilities": {
            "native": "DAW 内可直接移动、改长度、改 MIDI、增益或声像",
            "baked": "已烘焙为独立 WAV，同时保留原始素材与变换参数",
            "reference": "只用于试听或指导导入，不代表已绑定为 DAW 原生设备",
        },
        "verification": {
            "audio_rendered": False,
            "package_built": False,
            "structure_validated": False,
            "als_built": False,
            "daw_opened": False,
            "daw_playback_verified": False,
        },
    }


def track(arrangement: dict, track_id: str) -> dict:
    return next(t for t in arrangement.get("tracks", []) if t.get("track_id") == track_id)


def assets_by_id(arrangement: dict) -> dict[str, dict]:
    return {str(asset["asset_id"]): asset for asset in arrangement.get("assets", [])}


def dumps(arrangement: dict) -> str:
    return json.dumps(arrangement, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def validate(arrangement: dict, *, package_root: Path | None = None) -> list[str]:
    """返回结构错误；空列表表示当前可验证部分通过。"""
    errors: list[str] = []
    if arrangement.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version 不受支持")
    tracks = arrangement.get("tracks") or []
    track_ids = [str(item.get("track_id")) for item in tracks]
    if len(track_ids) != len(set(track_ids)):
        errors.append("track_id 不唯一")
    all_events = [event for item in tracks for event in item.get("events", [])]
    clip_ids = [str(item.get("clip_id")) for item in all_events]
    if not clip_ids or len(clip_ids) != len(set(clip_ids)):
        errors.append("clip_id 缺失或不唯一")
    asset_map = assets_by_id(arrangement)
    for event in all_events:
        if event.get("track_id") not in track_ids:
            errors.append(f"事件 {event.get('clip_id')} 引用了未知 track_id")
        if event.get("asset_id") not in asset_map:
            errors.append(f"事件 {event.get('clip_id')} 引用了未知 asset_id")
        media_asset_id = event.get("media_asset_id")
        if media_asset_id and media_asset_id not in asset_map:
            errors.append(f"事件 {event.get('clip_id')} 引用了未知 media_asset_id")
    try:
        reference_track = track(arrangement, "track-reference")
        if reference_track.get("muted") is not True:
            errors.append("reference track 必须默认静音")
    except StopIteration:
        errors.append("缺少 reference track")
    if package_root is not None:
        for asset in arrangement.get("assets", []):
            package_path = asset.get("package_path")
            if package_path and not (package_root / package_path).is_file():
                errors.append(f"工程包缺少素材: {package_path}")
        for item in tracks:
            midi_file = item.get("midi_file")
            if midi_file and not (package_root / midi_file).is_file():
                errors.append(f"工程包缺少 MIDI: {midi_file}")
    return errors
