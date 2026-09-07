"""BeatLab 生成层 · 模块 H3：三候选批量渲染 + dry stems + 三 manifests（render 改造，PRD §8）。

用法:
    .venv/bin/python pipeline/render.py <run_id> [--force-kit] [--no-als] [--root PATH]

功能（一次 render 调用渲染一个 run 的 3 个候选）:
1. one-shot kit 自动构建（build_kit）：沿用旧逻辑 —— 扫 library stems/drums.wav，
   onset 切片 → 特征 → 启发式分类 kick/snare/hat/oh/perc，每类响度 top2 存 ROOT/kit/kit.json；
   某类为空时 numpy 合成兜底（ROOT/kit/synth/），保证渲染永不失败。
2. 候选渲染（render_candidate）：按 BeatSpec + Recipe manifest 分层渲染 ——
   drums（one-shot + velocity→增益 + offset_ms→平移）、chops（切片窗 + HP100 + 段落 LP + reverse）、
   bass（正弦 sub，根音来自 manifest）、vocal（人声 phrase + stem 人声 hero）。
   输出 <kind>/full_mix.wav（tanh 软限幅 + 峰值归一 -1dB，44.1k 16bit 立体声）
   + <kind>/stems/{chops,drums,bass,vocal}.wav（dry 分轨：各层直出、峰值归一，不做专业分轨质量）。
3. DAW 交付（PRD §8）：<kind>/chops/ 切片段 WAV（manifest 全部切片物化）、
   <kind>/recipe.json、<kind>/provenance.json（hero source/rights/moment 区间、切片来源、pipeline 版本）、
   根目录 run_manifest.json（三候选清单）；.als patch 沿用旧逻辑（take_<run_id>.als）。
4. 任务可恢复：jobs 状态 generated 后重跑跳过（job_id == run_id）；
   render 只写 beats/<run_id>/（kit 构建沿用旧逻辑写 ROOT/kit/）。

只 import common + recipes（+ numpy/soundfile）；接口经 SQLite 与文件系统交换。
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

import common
import recipes

SR = 44100                 # 渲染统一采样率
STEPS_PER_BAR = 16
PEAK_DBFS = -1.0           # 峰值归一目标（dBFS）
KIT_CLASSES = ("kick", "snare", "hat", "oh", "perc")
KIT_PAN = {"hat": 0.25, "oh": 0.15, "perc": -0.25}   # 非中心鼓件轻微声像展开
STEM_LAYERS = ("chops", "drums", "bass", "vocal")
LIVE12_TEMPLATE = "/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library/Templates/Quick Start Beat.als"


# ---------- 1. one-shot 库自动构建（沿用旧逻辑） ----------
def _scan_drums() -> list:
    hits = sorted(common.LIBRARY.glob("*/stems/drums.wav"))
    hits += sorted(common.LIBRARY.glob("*/*/stems/drums.wav"))
    return hits


def _slice_segments(y: np.ndarray, sr: int) -> list:
    import librosa
    onsets = librosa.onset.onset_detect(y=y, sr=sr, backtrack=True, units="samples")
    bounds = [0] + [int(o) for o in onsets] + [len(y)]
    min_len = int(0.015 * sr)
    segs = []
    for s, e in zip(bounds[:-1], bounds[1:]):
        if e - s >= min_len:
            segs.append((s, e))
    return segs


def _trim_edges(seg: np.ndarray, thr: float = 0.005) -> np.ndarray:
    peak = np.max(np.abs(seg)) + 1e-12
    nz = np.where(np.abs(seg) > peak * thr)[0]
    if nz.size == 0:
        return seg
    return seg[nz[0]: nz[-1] + 1]


def _features(y: np.ndarray, sr: int) -> dict | None:
    y = y - np.mean(y)
    n = len(y)
    if n < int(0.01 * sr):
        return None
    rms = float(np.sqrt(np.mean(y ** 2)))
    if rms < 1e-4:
        return None
    w = np.hanning(n)
    Y = np.abs(np.fft.rfft(y * w))
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    total = float(np.sum(Y ** 2)) + 1e-12

    def band(lo: float, hi: float) -> float:
        m = (freqs >= lo) & (freqs <= hi)
        return float(np.sum(Y[m] ** 2)) / total

    win = max(32, n // 512)
    frames = n // win
    if frames > 0:
        env = np.sqrt(np.mean(y[: frames * win].reshape(frames, win) ** 2, axis=1))
        peak_i = int(np.argmax(env))
        below = np.where(env[peak_i:] < env[peak_i] * 0.1)[0]
        decay = float(below[0] / frames * (n / sr)) if below.size else n / sr
    else:
        decay = n / sr
    return {
        "centroid": float(np.sum(freqs * Y) / (np.sum(Y) + 1e-12)),
        "duration": n / sr,
        "low": band(40, 120),
        "mid": band(150, 500),
        "high": band(6000, sr / 2),
        "decay": decay,
        "rms": rms,
    }


def _classify(f: dict) -> str | None:
    if f["duration"] > 1.2 or f["duration"] < 0.015:
        return None
    if f["centroid"] < 2000 and f["low"] > 0.25 and f["duration"] < 0.6:
        return "kick"
    if f["mid"] > 0.18 and 1200 < f["centroid"] < 6000 and f["duration"] < 0.5 and f["low"] < 0.3:
        return "snare"
    if f["centroid"] > 6000 and f["duration"] < 0.15:
        return "hat"
    if f["centroid"] > 5000 and f["duration"] >= 0.15:
        return "oh"
    return "perc"


def _synth_kick(sr: int) -> np.ndarray:
    dur = 0.45
    t = np.arange(int(dur * sr)) / sr
    f = 45 + (150 - 45) * np.exp(-t * 8)
    phase = 2 * np.pi * np.cumsum(f) / sr
    env = (1 - np.exp(-t * 500)) * np.exp(-t * 7)
    return np.sin(phase) * env


def _synth_snare(sr: int) -> np.ndarray:
    dur = 0.25
    n = int(dur * sr)
    t = np.arange(n) / sr
    noise = np.random.default_rng(11).standard_normal(n)
    Y = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    Y[~((freqs >= 150) & (freqs <= 8000))] = 0
    bp = np.fft.irfft(Y, n)
    tone = np.sin(2 * np.pi * 180 * t) * np.exp(-t * 30)
    env = np.exp(-t * 18)
    return 0.7 * bp * env + 0.4 * tone * env


def _synth_hat(sr: int, dur: float, seed: int) -> np.ndarray:
    n = int(dur * sr)
    t = np.arange(n) / sr
    noise = np.random.default_rng(seed).standard_normal(n)
    Y = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    Y[freqs < 6000] = 0
    hp = np.fft.irfft(Y, n)
    env = np.exp(-t * (3.0 / dur))
    if dur >= 0.1:
        env = np.minimum(t / 0.005, 1.0) * env
    return hp * env


def _synth_perc(sr: int) -> np.ndarray:
    """perc 兜底：短促中频敲击（1.2kHz 正弦 + 快速衰减）。"""
    dur = 0.08
    t = np.arange(int(dur * sr)) / sr
    sig = np.sin(2 * np.pi * 1200 * t)
    return sig * np.exp(-t * 60.0)


def ensure_synth_kit() -> dict:
    synth_dir = common.ROOT / "kit" / "synth"
    synth_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    gens = {
        "kick": _synth_kick,
        "snare": _synth_snare,
        "hat": lambda sr: _synth_hat(sr, 0.02, 13),
        "oh": lambda sr: _synth_hat(sr, 0.3, 17),
        "perc": lambda sr: _synth_perc(sr),
    }
    for cls, gen in gens.items():
        y = gen(SR).astype(np.float32)
        y = y * (0.9 / (np.max(np.abs(y)) + 1e-12))
        p = synth_dir / f"{cls}.wav"
        sf.write(p, y, SR)
        out[cls] = [str(p.relative_to(common.ROOT))]
    return out


def _isolate(cls: str, seg: np.ndarray, sr: int) -> np.ndarray:
    from scipy.signal import butter, sosfiltfilt
    caps = {"kick": 0.25, "snare": 0.20, "hat": 0.10, "oh": 0.40, "perc": 0.20}
    seg = seg[: int(caps[cls] * sr)]
    if len(seg) < int(0.01 * sr):
        return seg
    if cls == "kick":
        sos = butter(4, 150, btype="lowpass", fs=sr, output="sos")
    elif cls == "snare":
        sos = butter(4, [150, 6000], btype="bandpass", fs=sr, output="sos")
    elif cls in ("hat", "oh"):
        sos = butter(4, 5500, btype="highpass", fs=sr, output="sos")
    else:
        sos = butter(4, [400, 5000], btype="bandpass", fs=sr, output="sos")
    y2 = sosfiltfilt(sos, seg)
    t = np.arange(len(y2)) / sr
    env = np.minimum(t / 0.004, 1.0) * np.exp(-t / (0.12 if cls in ("kick", "snare", "hat") else 0.25))
    y2 = y2 * env
    peak = float(np.abs(y2).max())
    if peak > 1e-6:
        y2 = y2 / peak * 0.9
    return y2


def build_kit(force: bool = False) -> dict:
    """构建 one-shot kit：真实切片每类响度 top2 + 缺类合成兜底（沿用旧逻辑）。"""
    kit_json = common.ROOT / "kit" / "kit.json"
    if kit_json.exists() and not force:
        try:
            data = json.loads(kit_json.read_text(encoding="utf-8"))
            if all(k in data for k in KIT_CLASSES):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    (common.ROOT / "kit").mkdir(parents=True, exist_ok=True)
    slices_dir = common.ROOT / "kit" / "slices"
    slices_dir.mkdir(exist_ok=True)
    hits = {k: [] for k in KIT_CLASSES}
    for drums in _scan_drums():
        try:
            y, sr = common.load_audio_mono(drums, sr=SR)
        except Exception as exc:
            print(f"[kit] 跳过 {drums}: {exc}", file=sys.stderr)
            continue
        for s, e in _slice_segments(y, sr):
            seg = _trim_edges(y[s:e])
            f = _features(seg, sr)
            if f is None:
                continue
            cls = _classify(f)
            if cls is None:
                continue
            seg = _isolate(cls, seg, sr)
            hits[cls].append((f["rms"], seg))
    chosen = {}
    for cls in KIT_CLASSES:
        top = sorted(hits[cls], key=lambda t: -t[0])[:2]
        chosen[cls] = []
        for i, (_, seg) in enumerate(top):
            p = slices_dir / f"{cls}_{i:02d}.wav"
            sf.write(p, seg.astype(np.float32), SR)
            chosen[cls].append(str(p.relative_to(common.ROOT)))
    synth = ensure_synth_kit()
    n_synth = 0
    for cls in KIT_CLASSES:
        if not chosen[cls]:
            chosen[cls] = synth[cls]
            n_synth += 1
    kit_json.write_text(json.dumps(chosen, ensure_ascii=False, indent=2), encoding="utf-8")
    return chosen


# ---------- 2. 候选渲染（分层 + dry stems） ----------
_CHOP_HP_SOS = None
_LP_CACHE: dict[float, Any] = {}


def _stretch_to(seg: np.ndarray, target_s: float) -> np.ndarray:
    """把 seg 拉伸到 target_s（保调不变速），网格锁定用；rate 超 0.5-2 截断。"""
    import librosa
    cur = len(seg) / SR
    if cur <= 0 or target_s <= 0:
        return seg
    rate = cur / target_s
    if abs(rate - 1.0) < 0.01:
        return seg
    rate = max(0.5, min(2.0, rate))
    return librosa.effects.time_stretch(seg, rate=rate).astype(np.float32)


def _hp_chop(seg: np.ndarray) -> np.ndarray:
    """切片 100Hz 高通：切掉低频残渣，避免与 kick/bass 互掩。"""
    if len(seg) < 64:
        return seg
    global _CHOP_HP_SOS
    if _CHOP_HP_SOS is None:
        from scipy.signal import butter
        _CHOP_HP_SOS = butter(4, 100, btype="highpass", fs=SR, output="sos")
    from scipy.signal import sosfiltfilt
    return sosfiltfilt(_CHOP_HP_SOS, seg)


def _lp_at(seg: np.ndarray, hz: float) -> np.ndarray:
    """段落级低通（Section Mutation 滤波）；缓存 SOS 系数。"""
    if len(seg) < 64 or not hz or hz <= 0:
        return seg
    from scipy.signal import butter, sosfiltfilt
    hz = float(hz)
    if hz not in _LP_CACHE:
        _LP_CACHE[hz] = butter(2, hz, fs=SR, output="sos")
    return sosfiltfilt(_LP_CACHE[hz], seg)


def _resolve_rel(rel: str) -> Path | None:
    """相对路径解析：优先 ROOT 相对；绝对路径直接用。"""
    if not rel:
        return None
    p = Path(rel)
    if p.is_absolute():
        return p if p.exists() else None
    cands = [common.ROOT / rel, common.LIBRARY / rel]
    for c in cands:
        if c.exists():
            return c
    return None


def _load_cached(cache: dict, rel_path: str) -> np.ndarray | None:
    if rel_path in cache:
        return cache[rel_path]
    p = _resolve_rel(rel_path)
    if p is None:
        return None
    import librosa
    try:
        y, sr = sf.read(p, dtype="float32", always_2d=False)
    except Exception:
        return None
    if y.ndim > 1:
        y = y.mean(axis=1)
    if sr != SR:
        y = librosa.resample(y, orig_sr=sr, target_sr=SR).astype(np.float32)
    cache[rel_path] = y
    return y


def _slice_window(cache: dict, rel: str, start: float, end: float, reverse: bool,
                  lp_hz: float | None) -> np.ndarray | None:
    """读整曲并按窗口切片（+HP100 去低频、+可选段落 LP、+可选 reverse）。"""
    y = _load_cached(cache, rel)
    if y is None:
        return None
    s = int(float(start) * SR)
    e = max(s + 64, int(float(end) * SR))
    seg = y[s: min(e, len(y))]
    if len(seg) < 64:
        return None
    seg = _hp_chop(seg)
    if lp_hz:
        seg = _lp_at(seg, lp_hz)
    if reverse:
        seg = seg[::-1].copy()
    return seg


def _new_layers(n: int) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    return {k: (np.zeros(n, dtype=np.float32), np.zeros(n, dtype=np.float32)) for k in STEM_LAYERS}


def _add(layers: dict, name: str, sig: np.ndarray, t: float, gain: float,
         pan: float, n: int) -> None:
    i = int(t * SR)
    if i >= n or gain <= 0 or i < 0:
        return
    seg = sig[: max(0, n - i)]
    gl = gain * np.cos((pan + 1) * np.pi / 4)     # 等功率声像
    gr = gain * np.sin((pan + 1) * np.pi / 4)
    L, R = layers[name]
    L[i: i + len(seg)] += seg * gl
    R[i: i + len(seg)] += seg * gr


def _kit_for_recipe(recipe: dict, kit_fallback: dict) -> dict[str, list[str]]:
    """manifest drum_kit 优先（recipe 可复现）；路径缺失回退 build_kit 结果。"""
    out = {}
    for cls in KIT_CLASSES:
        paths = [p for p in (recipe.get("drum_kit") or {}).get(cls) or [] if p]
        ok = [p for p in paths if (common.ROOT / p).exists()]
        out[cls] = ok or kit_fallback.get(cls) or []
    return out


def _render_layers(spec, recipe: dict, kit: dict, n: int) -> dict:
    """按 BeatSpec 渲染 4 层（chops/drums/bass/vocal），返回 {layer: (L, R)}。"""
    step_s = 60.0 / spec.bpm / 4
    bar_s = step_s * STEPS_PER_BAR
    layers = _new_layers(n)
    cache: dict[str, np.ndarray] = {}
    rng = random.Random(spec.beat_id)              # 确定性随机选 one-shot
    kit = _kit_for_recipe(recipe, kit)

    # 鼓层
    for bar in range(spec.total_bars):
        pat = spec.drum_pattern.get(str(bar)) or spec.drum_pattern.get(bar) or {}
        for track, steps in pat.items():
            choices = kit.get(track)
            if not choices:
                continue
            for step_k, params in (steps or {}).items():
                params = params or {}
                vel = float(params.get("velocity", 1.0))
                gain = (vel / 127.0 if vel > 1 else vel) * 0.45
                off = float(params.get("offset_ms", 0)) / 1000.0
                t = bar * bar_s + int(step_k) * step_s + off
                y = _load_cached(cache, rng.choice(choices))
                if y is not None:
                    _add(layers, "drums", y, t, common.clamp(gain, 0, 1), KIT_PAN.get(track, 0.0), n)

    # 切片层（hero/supporting 全部走窗切；lp_hz/reverse 为段落 mutation）
    n_chop_played = n_chop_miss = 0
    for pl in spec.chop_placements:
        seg = _slice_window(cache, str(pl.get("file", "")), float(pl.get("start_sec", 0)),
                            float(pl.get("end_sec", 0)), bool(pl.get("reverse", False)),
                            pl.get("lp_hz"))
        if seg is None:
            n_chop_miss += 1
            continue
        if pl.get("stretch_to"):          # 网格锁定：compose 指定目标时长则拉伸对齐
            seg = _stretch_to(seg, float(pl["stretch_to"]))
        t = int(pl.get("bar", 0)) * bar_s + int(pl.get("step", 0)) * step_s
        _add(layers, "chops", seg, t, common.clamp(float(pl.get("gain", 0.7)), 0, 1),
             float(pl.get("pan", 0)), n)
        n_chop_played += 1

    # 人声层（vocal accent + stem 人声 hero）
    n_vocal_played = 0
    for vp in spec.vocal_placements:
        seg = _slice_window(cache, str(vp.get("file", "")), float(vp.get("start_sec", 0)),
                            float(vp.get("end_sec", 0)), bool(vp.get("reverse", False)),
                            vp.get("lp_hz"))
        if seg is None:
            continue
        cap = int(8 * SR)                          # phrase 最长 8s
        t = int(vp.get("bar", 0)) * bar_s + int(vp.get("step", 0)) * step_s
        _add(layers, "vocal", seg[:cap], t, common.clamp(float(vp.get("gain", 0.6)) * 1.5, 0, 1),
             0.0, n)
        n_vocal_played += 1

    # bass：正弦 sub（根音频率）+ velocity 包络 + 轻度软削波（沿旧逻辑）
    for bar in range(spec.total_bars):
        pat = spec.bass_pattern.get(str(bar)) or spec.bass_pattern.get(bar) or {}
        for step_k, note in (pat or {}).items():
            f = 440.0 * 2 ** ((int(note) - 69) / 12)
            t0 = bar * bar_s + int(step_k) * step_s
            dur = step_s * 0.9
            m = int(dur * SR)
            tt = np.arange(m) / SR
            env = np.minimum(tt / 0.005, 1.0) * np.exp(-tt / (dur * 0.6))
            sig = np.sin(2 * np.pi * f * tt) * env * 0.5
            sig = np.tanh(1.5 * sig) * 0.7
            _add(layers, "bass", sig, t0, 1.0, 0.0, n)

    print(f"[render] chops={n_chop_played}/{len(spec.chop_placements)} "
          f"vocals={n_vocal_played}/{len(spec.vocal_placements)}", flush=True)
    return layers


def _write_stereo(path: Path, L: np.ndarray, R: np.ndarray, dry: bool) -> None:
    """dry=False：tanh 软限幅 + 峰值归一 -1dB（master）；dry=True：仅峰值归一（分轨直出）。"""
    mix = np.stack([L, R], axis=1)
    if not dry:
        mix = np.tanh(mix)
    peak = np.max(np.abs(mix)) + 1e-12
    mix = mix * (10 ** (PEAK_DBFS / 20) / peak)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, mix, SR, subtype="PCM_16")


def _materialize_chops(recipe: dict, cand_dir: Path) -> list[Path]:
    """manifest 全部切片物化（DAW 交付：所有使用过的 chop WAV），返回文件列表。"""
    chops_dir = cand_dir / "chops"
    chops_dir.mkdir(parents=True, exist_ok=True)
    cache: dict[str, np.ndarray] = {}
    out: list[Path] = []
    for c in recipe.get("chops", []):
        y = _load_cached(cache, str(c.get("file", "")))
        if y is None:
            print(f"[chops] 素材缺失，跳过物化: {c.get('file')}", file=sys.stderr)
            continue
        s = int(float(c.get("start_sec", 0)) * SR)
        e = max(s + 64, int(float(c.get("end_sec", 0)) * SR))
        seg = y[s: min(e, len(y))]
        if len(seg) < 64:
            continue
        if c.get("reverse"):
            seg = seg[::-1].copy()
        peak = np.max(np.abs(seg)) + 1e-12
        seg = seg / peak * 0.9
        pad = int(c.get("pad", 1))
        role = str(c.get("role", "pad"))
        p = chops_dir / f"{role}_{pad:02d}.wav" if role != "pad" else chops_dir / f"pad{pad:02d}.wav"
        sf.write(p, seg, SR, subtype="PCM_16")
        out.append(p)
    return out


def render_candidate(spec, recipe: dict, kit: dict, cand_dir: Path) -> dict[str, Path]:
    """渲染一个候选：full_mix.wav + 4 dry stems + chops/ 物化，返回产物路径表。"""
    step_s = 60.0 / spec.bpm / 4
    bar_s = step_s * STEPS_PER_BAR
    total_bars = spec.total_bars or sum(s.bars for s in spec.sections) or 1
    n = int(total_bars * bar_s * SR) + int(0.5 * SR)   # 0.5s 尾巴余量
    layers = _render_layers(spec, recipe, kit, n)

    out: dict[str, Path] = {}
    mix_path = cand_dir / "full_mix.wav"
    mix_l = mix_r = None
    for name in STEM_LAYERS:
        L, R = layers[name]
        _write_stereo(cand_dir / "stems" / f"{name}.wav", L, R, dry=True)
        out[f"stem_{name}"] = cand_dir / "stems" / f"{name}.wav"
        if mix_l is None:
            mix_l, mix_r = L.copy(), R.copy()
        else:
            mix_l += L
            mix_r += R
    _write_stereo(mix_path, mix_l, mix_r, dry=False)
    out["full_mix"] = mix_path
    out["chops_dir"] = cand_dir / "chops"
    out["chop_files"] = _materialize_chops(recipe, cand_dir)
    return out


# ---------- 3. manifests ----------
def _run_manifest(run_id: str, specs: dict, recipes_manifest: dict, bpm: float) -> dict:
    loop = recipes_manifest.get("loop") or {}
    priors = (loop.get("pipeline") or {}).get("params", {}).get("recipe_prior", {})
    return {
        "run_id": run_id,
        "seed": recipes._seed(run_id),
        "bpm": round(float(bpm), 2),
        "hero_moment_id": (loop.get("hero") or {}).get("moment_id"),
        "hero_asset_id": (loop.get("hero") or {}).get("asset_id"),
        "status": "generated",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "recipe_prior": priors,
        "stems_note": "dry layer bounces (pre-master, 44.1k 16bit)，非专业分轨质量",
        "candidates": [
            {
                "kind": k, "recipe_id": (recipes_manifest[k].get("recipe_id")),
                "beat_id": f"{run_id}-{k}",
                "groove_profile": recipes_manifest[k].get("groove_profile"),
                "duration_s": round(specs[k].total_bars * 240.0 / specs[k].bpm, 2),
                "full_mix": f"{k}/full_mix.wav",
                "stems": {n: f"{k}/stems/{n}.wav" for n in STEM_LAYERS},
                "recipe": f"{k}/recipe.json",
                "provenance": f"{k}/provenance.json",
                "chops_dir": f"{k}/chops",
                "midi_dir": f"{k}/midi",
            }
            for k in recipes.RECIPE_KINDS
        ],
    }


def _already_generated(run_id: str) -> bool:
    """任务可恢复：run_manifest generated 且三候选产物齐全，或 jobs 状态 == generated。"""
    run_dir = common.ROOT / "beats" / run_id
    rm = run_dir / "run_manifest.json"
    if rm.exists():
        try:
            data = json.loads(rm.read_text(encoding="utf-8"))
            if data.get("status") == "generated" and all(
                    (run_dir / k / "full_mix.wav").exists() for k in recipes.RECIPE_KINDS):
                return True
        except (json.JSONDecodeError, OSError):
            pass
    return recipes.job_status(run_id) == "generated"


# ---------- 4. .als 伴生 + handoff（沿用旧逻辑） ----------
def patch_als(run_id: str, bpm: float, run_dir: Path) -> Path | None:
    src = Path(LIVE12_TEMPLATE)
    if not src.exists():
        print(f"[als] 模板不存在，跳过 .als: {LIVE12_TEMPLATE}", file=sys.stderr)
        return None
    target = run_dir / f"take_{run_id}.als"
    shutil.copy(src, target)
    bpm_s = str(int(bpm)) if float(bpm).is_integer() else f"{float(bpm):.1f}"
    xml = gzip.decompress(target.read_bytes()).decode("utf-8")
    xml, nsub = re.subn(
        r'(<Tempo>.*?<Manual Value=")\d+(")',
        rf"\g<1>{bpm_s}\g<2>",
        xml, count=1, flags=re.DOTALL,
    )
    if nsub == 0:
        print("[als] 警告: 模板中未找到 Tempo/Manual，BPM 未写入", file=sys.stderr)
    xml = xml.replace(
        "<LiveSet>", f"<LiveSet><!-- run_id={run_id}; bpm={bpm_s} -->", 1
    )
    target.write_bytes(gzip.compress(xml.encode("utf-8")))
    return target


def write_handoff(run_id: str, bpm: float, run_dir: Path, als: Path | None) -> Path:
    lines = [
        f"BeatLab run: {run_id} @ {bpm:g} BPM（三个候选：loop / chop / stem）",
        "=" * 64,
    ]
    if als:
        lines += [
            f"双击 {als.name} 在 Ableton Live 12 直接打开（已设 BPM）。",
            "若 macOS 拦截，按住 Control 键右键 -> 打开。",
        ]
    else:
        lines += ["未生成 .als（Live 12 模板缺失），可手动新建 Live Set 并设 BPM。"]
    lines += [
        "",
        "== 三个候选（每候选目录结构相同）==",
        "  <kind>/full_mix.wav         完整混音试听",
        "  <kind>/stems/*.wav          dry 分轨（chops/drums/bass/vocal，",
        "                              直出层 bounce、非专业分轨质量）",
        "  <kind>/chops/               所有使用过的切片段 WAV（拖入 Drum Rack）",
        "  <kind>/midi/drums.mid       拖入 Drums Track",
        "  <kind>/midi/bass.mid        拖入 Bass Track",
        "  <kind>/midi/chops.mid       拖入切片 Track（C3 起 pad）",
        "  <kind>/recipe.json          Recipe manifest（Ableton 逐轨重建依据）",
        "  <kind>/provenance.json      来源/权利/切片/版本 追溯",
        "",
        "== 关联产物 ==",
        f"- 工程目录: {run_dir}",
        f"- 试听/交付镜像: {common.MIRROR_ROOT}/{common.today_str()}/（report 模块产出）",
    ]
    out = run_dir / "ABLETON_HANDOFF.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


# ---------- 5. 入口 ----------
def render_run(run_id: str, *, force_kit: bool = False, no_als: bool = False,
               root: str | Path | None = None) -> dict | None:
    """批量渲染一个 run 的 3 个候选；jobs 状态 generated 后重跑跳过；只写 beats/<run_id>/。"""
    if root:
        recipes.set_test_root(root)
    if _already_generated(run_id):
        print(f"[render] run {run_id} 已 generated，跳过（任务可恢复）")
        return None
    run_dir = common.ROOT / "beats" / run_id
    specs: dict[str, common.BeatSpec] = {}
    manifests: dict[str, dict] = {}
    for kind in recipes.RECIPE_KINDS:
        spec_path = run_dir / "specs" / f"{kind}.json"
        recipe_path = run_dir / "recipes" / f"{kind}.json"
        if not spec_path.exists() or not recipe_path.exists():
            sys.exit(f"[ERROR] run {run_id} 缺少 {kind} 的 spec/recipe：请先运行 compose")
        specs[kind] = recipes.spec_load(spec_path.read_text(encoding="utf-8"))
        manifests[kind] = json.loads(recipe_path.read_text(encoding="utf-8"))

    kit = build_kit(force=force_kit)
    counts = {k: len(v) for k, v in kit.items()}
    synth_n = sum(1 for v in kit.values() if any("synth" in p for p in v))
    print(f"[kit] kick={counts['kick']} snare={counts['snare']} hat={counts['hat']} "
          f"oh={counts['oh']} perc={counts['perc']}（synth 兜底 {synth_n} 类）")

    assets_by_id = {str(a.get("id")): a for a in recipes.get_assets()}
    bpm = specs["loop"].bpm
    for kind in recipes.RECIPE_KINDS:
        cand_dir = run_dir / kind
        cand_dir.mkdir(parents=True, exist_ok=True)
        spec = specs[kind]
        manifest = manifests[kind]
        files = render_candidate(spec, manifest, kit, cand_dir)
        (cand_dir / "recipe.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        prov = recipes.build_provenance(manifest, assets_by_id, run_id)
        prov["generated_at"] = datetime.now().isoformat(timespec="seconds")
        (cand_dir / "provenance.json").write_text(
            json.dumps(prov, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[render] {kind}: {files['full_mix']} "
              f"(+4 dry stems, {len(files['chop_files'])} chop wavs)")

    rm = _run_manifest(run_id, specs, manifests, bpm)
    (run_dir / "run_manifest.json").write_text(
        json.dumps(rm, ensure_ascii=False, indent=2), encoding="utf-8")

    als = None if no_als else patch_als(run_id, bpm, run_dir)
    if als:
        print(f"[als] {als}")
    handoff = write_handoff(run_id, bpm, run_dir, als)
    print(f"[handoff] {handoff}")

    recipes.mark_job(run_id, "generated")
    recipes.upsert_run(run_id, status="generated",
                       generated_at=rm["generated_at"], candidates=rm["candidates"])
    print(f"[render] run {run_id} 完成（3 full_mix + 12 dry stems + 3 manifests）")
    return rm


def main() -> None:
    ap = argparse.ArgumentParser(description="BeatLab 生成层：批量渲染 run 的三候选 + dry stems + manifests")
    ap.add_argument("run_id", help="要渲染的 run id（job_id == run_id）")
    ap.add_argument("--force-kit", action="store_true", help="强制重建 one-shot kit")
    ap.add_argument("--no-als", action="store_true", help="跳过 .als 生成")
    ap.add_argument("--root", help="隔离根目录（默认 common.ROOT）")
    args = ap.parse_args()
    render_run(args.run_id, force_kit=args.force_kit, no_als=args.no_als, root=args.root)


if __name__ == "__main__":
    main()
