"""BeatLab 生成层 · 模块 H3：三候选批量渲染 + dry stems + 三 manifests（render 改造，PRD §8）。

用法:
    .venv/bin/python pipeline/render.py <run_id> [--force-kit] [--no-als] [--root PATH]

功能（一次 render 调用渲染一个 run 的 3 个候选）:
1. one-shot kit 自动构建（build_kit）：沿用旧逻辑 —— 扫 library stems/drums.wav，
   onset 切片 → 特征 → 启发式分类 kick/snare/hat/oh/perc，每类响度 top2 存 ROOT/kit/kit.json；
   某类为空时 numpy 合成兜底（ROOT/kit/synth/），保证渲染永不失败。
2. 统一编排清单：先由 BeatSpec + Recipe + 确切 kit 生成 arrangement.json，
   试听渲染与 DAW 工程包都只消费这份清单，避免两套时间线漂移。
3. 候选渲染（render_candidate）：按 arrangement.json 分层渲染 ——
   drums（one-shot + velocity→增益 + offset_ms→平移）、chops（切片窗 + HP100 + 段落 LP + reverse）、
   bass（正弦 sub，根音来自 manifest）、vocal（人声 phrase + stem 人声 hero）。
   输出 <kind>/full_mix.wav（tanh 软限幅 + 峰值归一 -1dB，44.1k 16bit 立体声）
   + <kind>/stems/{chops,drums,bass,vocal}.wav（dry 分轨：各层直出、峰值归一，不做专业分轨质量）。
4. DAW 交付（PRD §8）：<kind>/chops/ 切片段 WAV（manifest 全部切片物化）、
   <kind>/recipe.json、<kind>/provenance.json（hero source/rights/moment 区间、切片来源、pipeline 版本）、
   <kind>/project/ 自包含工程包、根目录 run_manifest.json（三候选清单）。
5. 任务可恢复：jobs 状态 generated 后重跑跳过（job_id == run_id）；
   render 只写 beats/<run_id>/（kit 构建沿用旧逻辑写 ROOT/kit/）。

只 import common + recipes（+ numpy/soundfile）；接口经 SQLite 与文件系统交换。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

import arrangement as arrangement_model
import common
import daw_export
import recipes

SR = 44100                 # 渲染统一采样率
PEAK_DBFS = -1.0           # 峰值归一目标（dBFS）
KIT_CLASSES = ("kick", "snare", "hat", "oh", "perc")
KIT_PAN = {"hat": 0.25, "oh": 0.15, "perc": -0.25}   # 非中心鼓件轻微声像展开
STEM_LAYERS = ("chops", "drums", "bass", "vocal")


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
    """Render the declared duration exactly; never silently clamp the grid."""
    import librosa
    cur = len(seg) / SR
    if cur <= 0 or target_s <= 0:
        return seg
    rate = cur / target_s
    if len(seg) == round(target_s * SR):
        return seg
    stretched = librosa.effects.time_stretch(seg, rate=rate).astype(np.float32)
    return librosa.util.fix_length(stretched, size=round(target_s * SR))


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


def _event_audio(cache: dict, arrangement: dict, event: dict,
                 cand_dir: Path) -> np.ndarray | None:
    """按 arrangement 事件生成唯一 processed clip，并回传渲染用音频。"""
    asset_map = arrangement_model.assets_by_id(arrangement)
    source_asset = asset_map.get(str(event.get("asset_id"))) or {}
    rel = str(source_asset.get("source_path") or "")
    y = _load_cached(cache, rel)
    if y is None:
        return None
    source_sr = max(1, int(event.get("source_sample_rate") or SR))
    start = float(event.get("source_start_frame", 0)) / source_sr
    end = float(event.get("source_end_frame", 0)) / source_sr
    s = max(0, round(start * SR))
    e = min(len(y), max(s + 64, round(end * SR)))
    seg = y[s:e]
    if len(seg) < 64:
        return None
    for operation in event.get("operations", []):
        name = operation.get("op")
        if name == "highpass":
            seg = _hp_chop(seg)
        elif name == "lowpass":
            seg = _lp_at(seg, float(operation.get("hz") or 0))
        elif name == "reverse":
            seg = seg[::-1].copy()
        elif name == "time_stretch":
            seg = _stretch_to(seg, float(operation.get("target_seconds") or 0))
    media_asset_id = str(event.get("media_asset_id") or "")
    if media_asset_id:
        processed = cand_dir / "processed" / f"{media_asset_id}.wav"
        processed.parent.mkdir(parents=True, exist_ok=True)
        sf.write(processed, seg.astype(np.float32), SR, subtype="FLOAT")
    return seg


def _render_layers(arrangement: dict, cand_dir: Path, n: int) -> dict:
    """严格按 arrangement 渲染 4 层，返回 {layer: (L, R)}。"""
    bpm = float(arrangement["bpm"])
    beat_s = 60.0 / bpm
    layers = _new_layers(n)
    cache: dict[str, np.ndarray] = {}
    asset_map = arrangement_model.assets_by_id(arrangement)

    # 鼓层
    for event in arrangement_model.track(arrangement, "track-drums").get("events", []):
        asset = asset_map.get(str(event.get("asset_id"))) or {}
        y = _load_cached(cache, str(asset.get("source_path") or ""))
        if y is None:
            continue
        gain = float(event.get("velocity", 96)) / 127.0 * 0.45
        t = float(event.get("start_beat", 0)) * beat_s
        drum_class = str(event.get("drum_class") or "")
        _add(layers, "drums", y, t, common.clamp(gain, 0, 1),
             KIT_PAN.get(drum_class, 0.0), n)

    # 切片/人声层：所有处理由 arrangement.operations 描述并物化。
    n_chop_played = n_chop_miss = 0
    sample_events = arrangement_model.track(arrangement, "track-samples").get("events", [])
    for event in sample_events:
        seg = _event_audio(cache, arrangement, event, cand_dir)
        if seg is None:
            n_chop_miss += 1
            continue
        t = float(event.get("start_beat", 0)) * beat_s
        _add(layers, "chops", seg, t, common.clamp(float(event.get("gain", 0.7)), 0, 1),
             float(event.get("pan", 0)), n)
        n_chop_played += 1

    n_vocal_played = 0
    vocal_events = arrangement_model.track(arrangement, "track-vocals").get("events", [])
    for event in vocal_events:
        seg = _event_audio(cache, arrangement, event, cand_dir)
        if seg is None:
            continue
        t = float(event.get("start_beat", 0)) * beat_s
        _add(layers, "vocal", seg, t,
             float(event["gain"]),
             float(event.get("pan", 0.0)), n)
        n_vocal_played += 1

    # bass：清单中的 MIDI 事件驱动参考合成器。
    for event in arrangement_model.track(arrangement, "track-bass").get("events", []):
        frequency = 440.0 * 2 ** ((int(event["midi_note"]) - 69) / 12)
        t0 = float(event.get("start_beat", 0)) * beat_s
        dur = max(0.05, float(event.get("duration_beats", 0.5)) * beat_s * 0.9)
        m = int(dur * SR)
        tt = np.arange(m) / SR
        env = np.minimum(tt / 0.005, 1.0) * np.exp(-tt / (dur * 0.6))
        sig = np.sin(2 * np.pi * frequency * tt) * env * 0.5
        sig = np.tanh(1.5 * sig) * 0.7
        gain = float(event.get("velocity", 96)) / 96.0
        _add(layers, "bass", sig, t0, gain, 0.0, n)

    print(f"[render] chops={n_chop_played}/{len(sample_events)} "
          f"vocals={n_vocal_played}/{len(vocal_events)}", flush=True)
    return layers


def _write_stereo(path: Path, L: np.ndarray, R: np.ndarray, dry: bool) -> None:
    """dry=False：tanh 软限幅 + 峰值归一 -1dB（master）；dry=True：保留原始相对增益。"""
    mix = np.stack([L, R], axis=1)
    if not dry:
        mix = np.tanh(mix)
        peak = np.max(np.abs(mix)) + 1e-12
        mix = mix * (10 ** (PEAK_DBFS / 20) / peak)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, mix, SR, subtype="FLOAT" if dry else "PCM_24")


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


def render_candidate(arrangement: dict, cand_dir: Path) -> dict[str, Path]:
    """渲染一个候选：full/premaster mix + 4 dry stems + processed clips。"""
    duration_s = float(arrangement["duration_beats"]) * 60.0 / float(arrangement["bpm"])
    n = int(duration_s * SR) + int(0.5 * SR)   # 0.5s 尾巴余量
    layers = _render_layers(arrangement, cand_dir, n)

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
    premaster_path = cand_dir / "premaster_mix.wav"
    _write_stereo(premaster_path, mix_l, mix_r, dry=True)
    _write_stereo(mix_path, mix_l, mix_r, dry=False)
    out["full_mix"] = mix_path
    out["premaster_mix"] = premaster_path
    out["processed_dir"] = cand_dir / "processed"
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
                "premaster_mix": f"{k}/premaster_mix.wav",
                "stems": {n: f"{k}/stems/{n}.wav" for n in STEM_LAYERS},
                "recipe": f"{k}/recipe.json",
                "provenance": f"{k}/provenance.json",
                "arrangement": f"{k}/arrangement.json",
                "project": f"{k}/project",
                "chops_dir": f"{k}/chops",
                "midi_dir": f"{k}/midi",
                "verification": {
                    "audio_rendered": True,
                    "package_built": True,
                    "structure_validated": True,
                    "als_built": False,
                    "daw_opened": False,
                    "daw_playback_verified": False,
                },
            }
            for k in recipes.RECIPE_KINDS
        ],
    }


def _already_generated(run_id: str) -> bool:
    """任务可恢复：只有新版本的三候选音频与工程包齐全才跳过。"""
    run_dir = common.ROOT / "beats" / run_id
    rm = run_dir / "run_manifest.json"
    complete = all(
        (run_dir / kind / "full_mix.wav").is_file()
        and (run_dir / kind / "arrangement.json").is_file()
        and (run_dir / kind / "project" / "manifest.json").is_file()
        and not daw_export.verify_project_package(run_dir / kind / "project")
        and daw_export._sha256(run_dir / kind / "full_mix.wav") ==
            daw_export._sha256(run_dir / kind / "project/reference/full_mix.wav")
        for kind in recipes.RECIPE_KINDS
    )
    if rm.exists():
        try:
            data = json.loads(rm.read_text(encoding="utf-8"))
            if data.get("status") == "generated" and complete:
                return True
        except (json.JSONDecodeError, OSError):
            pass
    return recipes.job_status(run_id) == "generated" and complete


# ---------- 4. Ableton handoff ----------
def patch_als(run_id: str, bpm: float, run_dir: Path) -> Path | None:
    """保留旧调用入口，但不再生成只有 BPM、没有真实 Clip 的伪工程。"""
    print(
        "[als] 已跳过：需要在 MBP 上用经过 Live 12 验证的导入器消费各候选 project/arrangement.json",
        file=sys.stderr,
    )
    return None


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
        lines += [
            "未生成 .als：旧版仅复制模板并修改 BPM，无法呈现真实剪辑，现已停用。",
            "请在 MBP 上使用各候选 project/arrangement.json 导入，并完成 Live 12 打开/保存/回放验收。",
        ]
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
        "  <kind>/arrangement.json     试听与工程导出的统一事件时间线",
        "  <kind>/project/             自包含、可搬移的 DAW 工程包",
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
        exact_kit = _kit_for_recipe(manifest, kit)
        arrangement = arrangement_model.build_arrangement(spec, manifest, exact_kit)
        structure_errors = arrangement_model.validate(arrangement)
        if structure_errors:
            raise ValueError(f"{kind} arrangement 无效: {'；'.join(structure_errors)}")
        arrangement_path = cand_dir / "arrangement.json"
        arrangement_path.write_text(arrangement_model.dumps(arrangement), encoding="utf-8")

        files = render_candidate(arrangement, cand_dir)
        chop_files = _materialize_chops(manifest, cand_dir)  # 兼容旧拖入工作流
        (cand_dir / "recipe.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        prov = recipes.build_provenance(manifest, assets_by_id, run_id)
        prov["generated_at"] = datetime.now().isoformat(timespec="seconds")
        (cand_dir / "provenance.json").write_text(
            json.dumps(prov, ensure_ascii=False, indent=2), encoding="utf-8")

        arrangement, project_dir = daw_export.build_project_package(
            cand_dir,
            arrangement,
            recipe_path=cand_dir / "recipe.json",
            provenance_path=cand_dir / "provenance.json",
            spec_path=run_dir / "specs" / f"{kind}.json",
        )
        arrangement_path.write_text(arrangement_model.dumps(arrangement), encoding="utf-8")
        print(f"[render] {kind}: {files['full_mix']} "
              f"(+ premaster, 4 dry stems, {len(chop_files)} legacy chop wavs, project={project_dir})")

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
