#!/usr/bin/env python3
"""Isolated legacy/v1 arrangement A/B; same source, seed, kit, drums and bass.

Comparison audio uses one shared premaster gain, not loudness matching.
No database or source media is modified. Existing output directories are refused.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import soundfile as sf

PIPELINE = Path(__file__).resolve().parents[1] / "pipeline"
import sys
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

import common  # noqa: E402
import arrangement as arrangement_mod  # noqa: E402
import compose  # noqa: E402
import recipes  # noqa: E402
import render  # noqa: E402

VARIANTS = ("legacy", "v1")
DEFAULT_LIMIT = 1


def seed_for(run_id: str, kind: str = "algorithm-ab") -> int:
    digest = hashlib.sha256(f"beatlab:{run_id}:{kind}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def load_input(path: Path, limit: int, asset_id: str | None = None) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not 1 <= limit <= 3:
        raise ValueError("results must be a list; limit must be 1..3")
    eligible = [r for r in rows if (asset_id is None or str(r["asset"]["id"]) == asset_id)
                and any(float(m["end_sec"])-float(m["start_sec"]) >= 4 for m in r.get("moments", []))]
    if not eligible:
        raise ValueError("No selected asset has a moment of at least four seconds")
    return sorted(eligible, key=lambda r: str(r["asset"]["id"]))[:limit]


def moment(record: dict) -> tuple[dict, list[dict], dict[str, dict]]:
    asset = record["asset"]
    eligible = [m for m in record["moments"] if float(m["end_sec"])-float(m["start_sec"]) >= 4]
    hero = copy.deepcopy(sorted(eligible, key=lambda m: (-float(m["total"]), float(m["start_sec"])))[0])
    hero.setdefault("id", f"{asset['id']}:{hero['start_sec']}:{hero['end_sec']}")
    return hero, [], {str(asset["id"]): asset}


def read_stereo(path: Path) -> tuple[np.ndarray, int]:
    audio, sr = sf.read(path, always_2d=True, dtype="float32")
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    return audio[:, :2], sr


def write_stereo(path: Path, audio: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, np.asarray(audio, dtype="float32"), sr, subtype="FLOAT")




def peak(audio: np.ndarray) -> float:
    return float(np.max(np.abs(audio))) if audio.size else 0.0


def rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0


def make_comparison(rendered: list[dict], out_wav: Path, target_peak: float = 0.89) -> dict:
    """Apply one shared premaster gain to all variants and concatenate them."""
    loaded = []
    original_peak = 0.0
    sample_rate = None
    for item in rendered:
        audio, sr = read_stereo(item["premaster_mix"])
        if sample_rate is None:
            sample_rate = sr
        elif sr != sample_rate:
            raise ValueError("all variants must have the same sample rate")
        loaded.append((item, audio))
        original_peak = max(original_peak, peak(audio))

    shared_gain = target_peak / original_peak if original_peak > 0 else 1.0
    blocks = []
    stats = []
    gap = np.zeros((sample_rate // 2, 2), dtype=np.float32)
    for index, (item, audio) in enumerate(loaded):
        adjusted = audio * shared_gain
        blocks.extend((adjusted, gap) if index < len(loaded) - 1 else (adjusted,))
        stats.append({
            "variant": item["variant"],
            "loudness_matched": False,
            "source_premaster": str(item["premaster_mix"]),
            "shared_comparison_gain_linear": shared_gain,
            "original_peak": peak(audio),
            "adjusted_peak": peak(adjusted),
            "adjusted_rms": rms(adjusted),
        })
    write_stereo(out_wav, np.concatenate(blocks, axis=0), sample_rate)
    return {
        "path": str(out_wav),
        "sample_rate": sample_rate,
        "shared_gain_linear": shared_gain,
        "shared_gain_db": float(20.0 * np.log10(shared_gain)),
        "target_peak": target_peak,
        "variants": stats,
    }


def run_one(record: dict, out_root: Path, data_root: Path) -> dict:
    record = copy.deepcopy(record)
    asset = record["asset"]
    source = (data_root / asset["library_path"]).resolve()
    source.relative_to(data_root)
    def signature():
        with source.open("rb") as handle:
            sha = hashlib.file_digest(handle, "sha256").hexdigest()
        return {"sha256": sha, "mtime_ns": source.stat().st_mtime_ns}
    before = signature()
    if record.get("source_sha256") and before["sha256"] != record["source_sha256"]:
        raise ValueError("Source SHA differs from supplied analysis")
    asset["library_path"] = str(source)
    run_id = "algorithm-ab-" + str(asset["id"])
    run_dir = out_root / "beats" / run_id
    run_dir.mkdir(parents=True)
    hero, supporting, assets = moment(record)
    bpm = 92.0
    seed = seed_for(run_id)
    recipe_base = recipes.build_chop_recipe(run_id, hero, supporting, assets, bpm, seed,
        {"intro": 2, "verse": 8, "hook": 8, "verse_variation": 6, "outro": 2}, n_chops=16)
    kit = render.ensure_synth_kit()
    rendered = []
    variants = []
    legacy_spec = None
    for variant in VARIANTS:
        cand_dir = run_dir / variant
        cand_dir.mkdir()
        recipe = copy.deepcopy(recipe_base)
        if variant == "v1":
            recipe["arrangement"]["phrase_scheduler"] = "v1"
        spec = compose.build_spec_for_recipe(recipe, "chop", run_id, hero, supporting, assets, bpm, None)
        if legacy_spec is None:
            legacy_spec = spec
        else:
            spec.drum_pattern = copy.deepcopy(legacy_spec.drum_pattern)
            spec.bass_pattern = copy.deepcopy(legacy_spec.bass_pattern)
        arr = arrangement_mod.build_arrangement(spec, recipe, kit)
        midi = compose.export_midi(spec, cand_dir / "midi")
        files = render.render_candidate(arr, cand_dir)
        for name, value in (("recipe", recipe), ("arrangement", arr)):
            (cand_dir / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2))
        (cand_dir / "spec.json").write_text(recipes.spec_dump(spec))
        rendered.append({"variant": variant, "premaster_mix": files["premaster_mix"]})
        variants.append({"variant": variant, "event_count": len(spec.chop_placements),
            "midi": {k: str(v) for k, v in midi.items()},
            "artifacts": {k: str(v) for k, v in files.items()},
            "metrics": recipe["arrangement"].get("phrase_schedule_metrics")})
    comparison = make_comparison(rendered, run_dir / "shared_gain_comparison.wav")
    audio, sr = sf.read(source, always_2d=True, dtype="float32")
    sf.write(run_dir / "source_region.wav", audio[round(hero["start_sec"]*sr):round(hero["end_sec"]*sr)], sr, subtype="FLOAT")
    after = signature()
    if after != before:
        raise RuntimeError("Source changed during render")
    summary = {"run_id": run_id, "asset_id": asset["id"], "source_path": str(source),
        "source_before": before, "source_after": after, "source_unchanged": True,
        "hero": hero, "bpm": bpm, "seed": seed, "total_bars": 26,
        "comparison_scope": "Arrangement only: same source/chops/drums/bass/seed; no subjective verdict",
        "variants": variants, "shared_premaster_comparison": comparison}
    (run_dir / "algorithm_ab_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-results", required=True, type=Path)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--asset-id")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()
    records = load_input(args.baseline_results.resolve(), args.limit, args.asset_id)
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    old_root = common.ROOT
    try:
        common.ROOT = out
        summaries = [run_one(r, out, args.data_root.resolve()) for r in records]
    finally:
        common.ROOT = old_root
    (out / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2))
    print(json.dumps({"runs": len(summaries), "out_dir": str(out)}))


if __name__ == "__main__":
    main()
