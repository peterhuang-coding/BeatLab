"""BeatLab 模块 F：渲染 + .als 伴生。

用法:
    .venv/bin/python pipeline/render.py <beat_id> [--no-als] [--force-kit] [--no-midi]

功能:
1. one-shot 库自动构建（build_kit）：扫所有已分离样本的 library/<id>/stems/drums.wav，
   onset 切片 -> 每片特征（spectral centroid / 时长 / 40-120Hz 能量占比 / 高频占比 /
   decay）-> 启发式分类 kick/snare/hat/oh/perc，每类取响度 top2 存 ROOT/kit/kit.json
   （{kick:[file...], snare:[...], hat:[...], oh:[...], perc:[...]}，路径相对 ROOT）。
   某类为空时 numpy 合成兜底（ROOT/kit/synth/），保证渲染永不失败。
2. 渲染（render_beat）：按 BeatSpec 逐 bar 混鼓层 one-shot（velocity->增益、
   offset_ms->平移）+ 切片（读 slice_map.json）+ 人声 phrase + 正弦 sub bass，
   峰值归一 -1dB + tanh 软限幅，输出 44.1k 16bit 立体声 ROOT/beats/<id>/beat.wav。
3. .als 伴生（patch_als）：复制 Live 12 模板 Quick Start Beat.als，gzip 解压后
   patch Tempo/Manual 为 bpm 并注入 beat_id 注释（做法同 ai-beat-sketcher 的
   build_live_set.py）；模板不存在则跳过并警告。另写 ABLETON_HANDOFF.txt，并在
   compose 未产出 midi 时按 pattern 兜底生成 midi/drums|bass|chops.mid。

只 import common（+ numpy/soundfile/mido）；接口经文件系统与 SQLite 交换。
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import shutil
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

import common

SR = 44100                 # 渲染统一采样率
STEPS_PER_BAR = 16
PEAK_DBFS = -1.0           # 峰值归一目标（dBFS）
KIT_CLASSES = ("kick", "snare", "hat", "oh", "perc")
KIT_PAN = {"hat": 0.25, "oh": 0.15, "perc": -0.25}   # 非中心鼓件轻微声像展开
LIVE12_TEMPLATE = "/Applications/Ableton Live 12 Suite.app/Contents/App-Resources/Core Library/Templates/Quick Start Beat.als"


# ---------- spec 解析 ----------
def load_spec(beat_id: str) -> tuple:
    """从 ROOT/beats/<id>/spec.json 或 db beats.spec_path 读 BeatSpec，返回 (spec, beat_dir)。"""
    beat_dir = common.ROOT / "beats" / beat_id
    spec_path = beat_dir / "spec.json"
    if not spec_path.exists():
        conn = common.get_db()
        try:
            row = conn.execute(
                "select spec_path from beats where beat_id=?", (beat_id,)
            ).fetchone()
            if row and row[0]:
                spec_path = Path(row[0])
        finally:
            conn.close()
    if not spec_path.exists():
        sys.exit(f"render: 找不到 {beat_id} 的 spec（{spec_path} 与 db 均无）")
    return common.BeatSpec.from_json(spec_path.read_text(encoding="utf-8")), beat_dir


# ---------- 1. one-shot 库自动构建 ----------
def _scan_drums() -> list:
    """扫所有已分离样本的 drums.wav（兼容 library/<id>/ 与 library/<cat>/<id>/ 两级）。"""
    hits = sorted(common.LIBRARY.glob("*/stems/drums.wav"))
    hits += sorted(common.LIBRARY.glob("*/*/stems/drums.wav"))
    return hits


def _slice_segments(y: np.ndarray, sr: int) -> list:
    """onset 切片：backtrack 边界，丢弃 <15ms 的碎缝与过长段。"""
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
    """去掉首尾低于峰值 0.5% 的静音尾巴，让 decay 更干净。"""
    peak = np.max(np.abs(seg)) + 1e-12
    nz = np.where(np.abs(seg) > peak * thr)[0]
    if nz.size == 0:
        return seg
    return seg[nz[0]: nz[-1] + 1]


def _features(y: np.ndarray, sr: int) -> dict | None:
    """单段特征：centroid / 时长 / 低频(40-120Hz)占比 / 中频(150-500Hz)占比 /
    高频(>6kHz)占比 / decay（RMS 包络峰值->10% 时间）/ rms。"""
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

    # RMS 包络（32 样本窗）峰值到 10% 的耗时
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
    """启发式分类：kick（低质心+高低频比）、snare（中频 burst+噪）、
    hat（高质心+短）、oh（高质心+长衰减），其余归 perc。"""
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
    """兜底 kick：150->45Hz 正弦扫频 + 指数衰减。"""
    dur = 0.45
    t = np.arange(int(dur * sr)) / sr
    f = 45 + (150 - 45) * np.exp(-t * 8)
    phase = 2 * np.pi * np.cumsum(f) / sr
    env = (1 - np.exp(-t * 500)) * np.exp(-t * 7)
    return np.sin(phase) * env


def _synth_snare(sr: int) -> np.ndarray:
    """兜底 snare：带通白噪（150-8kHz）+ 180Hz 共振，短衰减。"""
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
    """兜底 hat/oh：高通白噪（>6kHz），hat 20ms、oh 300ms。"""
    n = int(dur * sr)
    t = np.arange(n) / sr
    noise = np.random.default_rng(seed).standard_normal(n)
    Y = np.fft.rfft(noise)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    Y[freqs < 6000] = 0
    hp = np.fft.irfft(Y, n)
    env = np.exp(-t * (3.0 / dur))
    if dur >= 0.1:                      # oh 加 5ms 起音防咔哒
        env = np.minimum(t / 0.005, 1.0) * env
    return hp * env


def ensure_synth_kit() -> dict:
    """numpy 合成兜底 kit，写 ROOT/kit/synth/，返回 {class: [相对 ROOT 路径]}。"""
    synth_dir = common.ROOT / "kit" / "synth"
    synth_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    gens = {
        "kick": _synth_kick,
        "snare": _synth_snare,
        "hat": lambda sr: _synth_hat(sr, 0.02, 13),
        "oh": lambda sr: _synth_hat(sr, 0.3, 17),
    }
    for cls, gen in gens.items():
        y = gen(SR).astype(np.float32)
        y = y * (0.9 / (np.max(np.abs(y)) + 1e-12))
        p = synth_dir / f"{cls}.wav"
        sf.write(p, y, SR)
        out[cls] = [str(p.relative_to(common.ROOT))]
    return out


def _isolate(cls: str, seg: np.ndarray, sr: int) -> np.ndarray:
    """按鼓类别滤波隔离：drums stem 是全组鼓混音，切片里混着其他鼓的串音。
    kick 低通留鼓皮冲击 / snare 带通 / hat·oh 高通截短 / perc 中频带通；
    尾部指数衰减去掉杂散尾音，峰值归一。"""
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
    """构建 one-shot kit：真实切片每类响度 top2 + 缺类合成兜底。"""
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
        except Exception as exc:                     # 单文件损坏不阻塞整体
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


# ---------- 2. 渲染 ----------
_CHOP_HP_SOS = None


def _hp_chop(seg: np.ndarray) -> np.ndarray:
    """切片 100Hz 高通：切掉切片里的低频残渣，避免与 kick/bass 互掩。"""
    if len(seg) < 64:          # 残片（<1.5ms）不滤波，防御 sosfiltfilt 的 padlen 下限
        return seg
    global _CHOP_HP_SOS
    if _CHOP_HP_SOS is None:
        from scipy.signal import butter
        _CHOP_HP_SOS = butter(4, 100, btype="highpass", fs=SR, output="sos")
    from scipy.signal import sosfiltfilt
    return sosfiltfilt(_CHOP_HP_SOS, seg)


def _load_cached(cache: dict, rel_path: str) -> np.ndarray:
    """按相对 ROOT 路径读单声道 float32（缓存；异采样率重采样对齐）。"""
    if rel_path in cache:
        return cache[rel_path]
    import librosa
    p = common.ROOT / rel_path
    y, sr = sf.read(p, dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    if sr != SR:
        y = librosa.resample(y, orig_sr=sr, target_sr=SR).astype(np.float32)
    cache[rel_path] = y
    return y


def _norm_slice_map(data) -> dict:
    """slice_map.json 容错归一为 {sample_id: [entries]}；entry 为 dict 或 [file,start,end]。"""
    if isinstance(data, dict) and "slices" in data:
        data = data["slices"]
    if isinstance(data, dict) and "chops" in data:
        data = data["chops"]
    if isinstance(data, list):
        return {"*": data}
    return data


def _match_entry(entries: list, chop_index: int) -> dict | None:
    """在 entries 里按 index/pad 双约定找 chop_index（0 基）。"""
    for en in entries:
        en = dict(zip(("file", "start_sec", "end_sec"), en)) if isinstance(en, (list, tuple)) else en
        idx = en.get("index")
        pad = en.get("pad")
        if (idx is not None and int(idx) == chop_index) or (pad is not None and int(pad) in (chop_index, chop_index + 1)):
            return en
    if 0 <= chop_index < len(entries):
        en = entries[chop_index]
        return dict(zip(("file", "start_sec", "end_sec"), en)) if isinstance(en, (list, tuple)) else en
    return None


def _find_chop(global_map: dict | None, beat_id: str, sample_id: str, chop_index: int) -> dict | None:
    """定位切片段：先查全局 slice_map，再查 library/<cat>/<sample_id>/slice_map.json（只读一次，不自递归）。"""
    for sm in (global_map,):
        if not sm:
            continue
        entries = sm.get(sample_id) or sm.get("*")
        if entries:
            found = _match_entry(entries, chop_index)
            if found:
                return found
    if sample_id:                        # 逐样本兜底
        for cat in common.CATEGORIES:
            p = common.LIBRARY / cat / sample_id / "slice_map.json"
            if not p.exists():
                continue
            try:
                data = _norm_slice_map(json.loads(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                return None
            entries = data.get(sample_id) or data.get("*") if isinstance(data, dict) else data
            if not entries:
                return None
            return _match_entry(entries, chop_index)
    return None


def _resolve_lib_file(beat_id: str, sample_id: str, file: str) -> Path | None:
    """把切片/人声的 file 解析为绝对路径：绝对路径直接用，否则按
    library/<cat>/<id>/ 与 beats/<id>/ 依次尝试。"""
    if not file:
        return None
    p = Path(file)
    if p.is_absolute():
        return p if p.exists() else None
    cands = []
    if sample_id:
        for cat in common.CATEGORIES:
            cands.append(common.LIBRARY / cat / sample_id / file)
        cands.append(common.LIBRARY / sample_id / file)
    cands += [common.LIBRARY / file, common.ROOT / "beats" / beat_id / file]
    for c in cands:
        if c.exists():
            return c
    return None


def render_beat(spec, kit: dict, beat_dir: Path) -> Path:
    """按 BeatSpec 渲染 beat.wav（44.1k 16bit 立体声），返回输出路径。"""
    step_s = 60.0 / spec.bpm / 4
    bar_s = step_s * STEPS_PER_BAR
    total_bars = spec.total_bars or sum(s.bars for s in spec.sections) or 1
    n = int(total_bars * bar_s * SR) + int(0.5 * SR)     # 0.5s 尾巴余量
    L = np.zeros(n, dtype=np.float32)
    R = np.zeros(n, dtype=np.float32)
    cache = {}
    rng = random.Random(spec.beat_id)                    # 确定性随机选 one-shot
    slice_map = None
    for p in (beat_dir / "slice_map.json", common.ROOT / "slice_map.json"):
        if p.exists():
            try:
                slice_map = _norm_slice_map(json.loads(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass
            break

    def add(sig: np.ndarray, t: float, gain: float, pan: float = 0.0) -> None:
        i = int(t * SR)
        if i >= n or gain <= 0 or i < 0:
            return
        seg = sig[: max(0, n - i)]
        gl = gain * np.cos((pan + 1) * np.pi / 4)        # 等功率声像
        gr = gain * np.sin((pan + 1) * np.pi / 4)
        L[i: i + len(seg)] += seg * gl
        R[i: i + len(seg)] += seg * gr

    # 鼓层：bar 全局序号，step 0..15
    for bar in range(total_bars):
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
                add(y, t, common.clamp(gain, 0, 1), KIT_PAN.get(track, 0.0))

    # 切片：chop_placements 读 slice_map.json 对应片段
    n_chop_played = n_chop_miss = n_vocal_played = n_vocal_miss = 0
    for pl in spec.chop_placements:
        # compose 已内嵌 file/start/end 时直接消费；否则回退 _find_chop 解析
        entry = pl if pl.get("file") else _find_chop(
            slice_map, spec.beat_id, str(pl.get("sample_id", "")), int(pl.get("chop_index", 0))
        )
        if entry is None:
            n_chop_miss += 1
            continue
        path = _resolve_lib_file(spec.beat_id, str(pl.get("sample_id", "")), str(entry.get("file", "")))
        if path is None:
            n_chop_miss += 1
            continue
        rel = str(path.relative_to(common.ROOT)) if path.is_relative_to(common.ROOT) else str(path)
        y = _load_cached(cache, rel)
        t = int(pl.get("bar", 0)) * bar_s + int(pl.get("step", 0)) * step_s
        # 切片文件本身即已切好的片段：整段播放（start_sec/end_sec 是原曲时间戳，
        # 只对整曲文件有意义——此处若再按它切片会全部越界静默丢弃）
        seg = _hp_chop(y)
        if len(seg) > int(4 * SR):
            seg = seg[: int(4 * SR)]
        if len(seg) < 64:
            n_chop_miss += 1
            continue
        add(seg, t, common.clamp(float(pl.get("gain", 0.7)) * 1.0, 0, 1), float(pl.get("pan", 0)))
        n_chop_played += 1

    # 人声 phrase 垫底
    for vp in spec.vocal_placements:
        path = _resolve_lib_file(spec.beat_id, str(vp.get("sample_id", "")), str(vp.get("file", "")))
        if path is None:
            n_vocal_miss += 1
            continue
        rel = str(path.relative_to(common.ROOT)) if path.is_relative_to(common.ROOT) else str(path)
        y = _load_cached(cache, rel)
        cap = int(8 * SR)                                # phrase 最长 8s
        t = int(vp.get("bar", 0)) * bar_s + int(vp.get("step", 0)) * step_s
        # phrase 文件同样已是切好的片段：整段播放
        add(y[:cap], t, common.clamp(float(vp.get("gain", 0.6)) * 1.5, 0, 1))
        n_vocal_played += 1

    # bass：正弦 sub（根音频率）+ velocity 包络 + 轻度软削波
    for bar in range(total_bars):
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
            i = int(t0 * SR)
            seg = sig[: max(0, n - i)]
            L[i: i + len(seg)] += seg
            R[i: i + len(seg)] += seg

    print(f"[mix] chops={n_chop_played}/{len(spec.chop_placements)} "
          f"vocals={n_vocal_played}/{len(spec.vocal_placements)}", flush=True)

    # 混音：tanh 软限幅 -> 峰值归一 -1dB
    mix = np.stack([L, R], axis=1)
    mix = np.tanh(mix)
    mix = mix * (10 ** (PEAK_DBFS / 20) / (np.max(np.abs(mix)) + 1e-12))
    out = beat_dir / "beat.wav"
    sf.write(out, mix, SR, subtype="PCM_16")
    return out


# ---------- 3. .als 伴生 + handoff + midi 兜底 ----------
def patch_als(beat_id: str, bpm: float, beat_dir: Path) -> Path | None:
    """复制 Live 12 模板并 patch Tempo/Manual 为 bpm、注入 beat_id 注释。
    模板不存在时跳过并返回 None。"""
    src = Path(LIVE12_TEMPLATE)
    if not src.exists():
        print(f"[als] 模板不存在，跳过 .als: {LIVE12_TEMPLATE}", file=sys.stderr)
        return None
    target = beat_dir / f"take_{beat_id}.als"
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
        "<LiveSet>", f"<LiveSet><!-- beat_id={beat_id}; bpm={bpm_s} -->", 1
    )
    target.write_bytes(gzip.compress(xml.encode("utf-8")))
    return target


def write_handoff(beat_id: str, bpm: float, beat_dir: Path, als: Path | None) -> Path:
    """ABLETON_HANDOFF.txt：.als 打开与 midi/audio 拖入说明。"""
    lines = [
        f"BeatLab take: {beat_id} @ {bpm:g} BPM",
        "=" * 56,
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
        "== 加载本 beat 的 MIDI（.als 已带 Drums/Bass/切片 轨时拖入即可）==",
        "1. midi/drums.mid -> Drums Track（打击乐轨）",
        "2. midi/bass.mid  -> Bass Track",
        "3. midi/chops.mid -> 切片 Track（C3 起 pad B1-B16）",
        "4. beat.wav -> 任意 Audio Track（本 beat 的完整混音，可直接试听）",
        "",
        "== 关联产物 ==",
        f"- 试听/交付页: {common.MIRROR_ROOT}/{common.today_str()}/{beat_id}_*.html",
        f"- 工程目录: {beat_dir}",
    ]
    out = beat_dir / "ABLETON_HANDOFF.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def export_midi_fallback(spec, beat_dir: Path) -> Path | None:
    """compose 未产出 midi 时按 pattern 兜底生成 midi/drums|bass|chops.mid。"""
    midi_dir = beat_dir / "midi"
    if midi_dir.exists() and any(midi_dir.glob("*.mid")):
        return midi_dir
    import mido
    tpb = 480
    step_ticks = tpb // 4
    total_bars = spec.total_bars or sum(s.bars for s in spec.sections) or 1
    events = []                                          # (tick, on/off, note, vel, chan)

    def add_hit(bar: int, step: int, note: int, vel: float, chan: int) -> None:
        t0 = (bar * STEPS_PER_BAR + step) * step_ticks
        events.append((t0, "on", note, int(common.clamp(vel, 0, 1) * 127), chan))
        events.append((t0 + int(step_ticks * 0.9), "off", note, 0, chan))

    note_map = {"kick": common.MIDI_KICK, "snare": common.MIDI_SNARE,
                "hat": common.MIDI_HAT, "oh": common.MIDI_OH, "perc": common.MIDI_PERC}
    for bar in range(total_bars):
        pat = spec.drum_pattern.get(str(bar)) or spec.drum_pattern.get(bar) or {}
        for track, steps in pat.items():
            if track not in note_map:
                continue
            for step_s, params in (steps or {}).items():
                params = params or {}
                vel = float(params.get("velocity", 1.0))
                vel = vel / 127.0 if vel > 1 else vel
                add_hit(bar, int(step_s), note_map[track], vel, 9)
        bpat = spec.bass_pattern.get(str(bar)) or spec.bass_pattern.get(bar) or {}
        for step_s, note in (bpat or {}).items():
            add_hit(bar, int(step_s), int(note), 0.8, 0)
    for pl in spec.chop_placements:
        note = int(pl.get("midi_note", 0) or common.MIDI_CHOP_BASE + int(pl.get("pad", 1)) - 1)
        add_hit(int(pl.get("bar", 0)), int(pl.get("step", 0)), note,
                float(pl.get("gain", 0.7)), 2)

    midi_dir.mkdir(parents=True, exist_ok=True)
    for name, chan in (("drums", 9), ("bass", 0), ("chops", 2)):
        evs = sorted(e for e in events if e[4] == chan)
        mf = mido.MidiFile(ticks_per_beat=tpb)
        tr = mido.MidiTrack()
        mf.tracks.append(tr)
        if chan == 9:
            tr.append(mido.Message("program_change", channel=9, program=0, time=0))
        last = 0
        for t, typ, note, vel, _c in evs:
            tr.append(mido.Message("note_on" if typ == "on" else "note_off",
                                   channel=chan, note=note, velocity=vel, time=t - last))
            last = t
        mf.save(midi_dir / f"{name}.mid")
    return midi_dir


# ---------- db 落库 ----------
def update_db(spec, wav: Path, als: Path | None, midi_dir: Path | None, spec_path: Path) -> None:
    """beats 表 upsert 渲染产物路径（不覆盖 compose 已写的字段）。"""
    conn = common.get_db()
    try:
        conn.execute(
            """insert or replace into beats
               (beat_id, created_at, bpm, style, duration_s, sample_ids,
                spec_path, wav_path, als_path, midi_dir)
               values (?,?,?,?,?,?,?,?,?,?)""",
            (spec.beat_id, spec.created_at or common.today_str(), spec.bpm, spec.style,
             float(sf.info(wav).duration), json.dumps(spec.sample_ids),
             str(spec_path), str(wav),
             str(als) if als else None, str(midi_dir) if midi_dir else None),
        )
        conn.commit()
    finally:
        conn.close()


# ---------- CLI ----------
def main() -> None:
    ap = argparse.ArgumentParser(description="BeatLab 模块 F：渲染 + .als 伴生")
    ap.add_argument("beat_id", help="要渲染的 beat id")
    ap.add_argument("--no-als", action="store_true", help="跳过 .als 生成")
    ap.add_argument("--force-kit", action="store_true", help="强制重建 one-shot kit")
    ap.add_argument("--no-midi", action="store_true", help="跳过 midi 兜底导出")
    args = ap.parse_args()

    spec, beat_dir = load_spec(args.beat_id)
    beat_dir.mkdir(parents=True, exist_ok=True)
    spec_path = beat_dir / "spec.json"
    if not spec_path.exists():                       # 供下游（report）直接读取
        spec_path.write_text(spec.to_json(), encoding="utf-8")

    kit = build_kit(force=args.force_kit)
    counts = {k: len(v) for k, v in kit.items()}
    synth_n = sum(1 for v in kit.values() if any("synth" in p for p in v))
    print(f"[kit] kick={counts['kick']} snare={counts['snare']} hat={counts['hat']} "
          f"oh={counts['oh']} perc={counts['perc']}（synth 兜底 {synth_n} 类）")

    wav = render_beat(spec, kit, beat_dir)
    print(f"[render] {wav} ({sf.info(wav).duration:.2f}s)")

    als = None if args.no_als else patch_als(spec.beat_id, spec.bpm, beat_dir)
    if als:
        print(f"[als] {als}")

    handoff = write_handoff(spec.beat_id, spec.bpm, beat_dir, als)
    print(f"[handoff] {handoff}")

    midi_dir = None if args.no_midi else export_midi_fallback(spec, beat_dir)
    if midi_dir:
        print(f"[midi] 兜底导出: {midi_dir}")

    update_db(spec, wav, als, midi_dir, spec_path)
    print("[render] 完成")


if __name__ == "__main__":
    main()
