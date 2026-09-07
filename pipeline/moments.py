"""BeatLab 模块 D2：Sample Moments —— 把整首素材理解为可搜索的 2-16s 采样候选。

用法：
    .venv/bin/python pipeline/moments.py [asset_id ...] [--all]

对每个 asset（library/<category>/<id>/source.wav，经 common.get_assets 读取）：
1. 读取 source.wav；stems/{drums,vocal,bass,other}.wav 存在则一并读取。
2. 结构分段（chroma SSM → path_enhance → agglomerative + 静音间隙边界），
   供 transition 判定与 structure_position 评分。
3. 窗口：bpm 存在 → 1/2/4/8 小节窗口；缺失 → 2/4/8/16s phrase 窗口。
   滑动步长 = 窗口一半（50% 重叠），窗口时长封顶 16s（Moment 2-16s）。
4. 每窗口 7 类判定 + 8 维评分（各 0-1）+ explain/risks。
5. Diversity Filter：同 asset 同类型最多保留 2 个（按加权总分取高）。
6. 经 common.upsert_moment 写入 moments 表。

7 类判定优先级：
    transition        窗口内含结构边界
    texture           频谱质心高（≥1800Hz）且 onset 密度低（<3/s）
    drum_break        鼓 RMS 占比 ≥0.35 且人声 ≤0.20（无 stems 不可判定）
    vocal_phrase      人声 RMS 占比 ≥0.30
    bass_phrase       bass RMS 占比 ≥0.30 且鼓 ≤0.30
    melody_no_drums   鼓 ≤0.12（无 stems 用低频带代理）且和声明确
    melody            其余

8 维评分（各 0-1）：
    loopability       端点频谱互相关 + 能量平稳度
    memorability      spectral novelty 峰（数量 + 相对强度）
    drum_state        鼓 stem RMS 占比（无 stems 用 40-120Hz 频带代理）
    vocal_state       人声 stem RMS 占比（无 stems 用 300-3400Hz 频带代理）
    key_stability     帧 chroma 与均值 chroma 的平均相似度
    structure_position 距最近结构边界的归一化距离（transition 取近，其余取远）
    timbre_uniqueness MFCC 时变方差 + 频谱质心偏离
    space             低能量帧占比（留白）+ crest factor

数据层契约（Dev-1 已冻结，所有 helper 以 conn 为首参）：
common.get_db() / common.get_asset(conn, asset_id) / common.get_assets(conn, ...) /
common.upsert_moment(conn, moment) / common.get_moments(conn, ...)。
moment 表含 id（本模块生成 f"{asset_id}:{type}:{start:07.3f}-{end:07.3f}"）与
scores_json/explain_json/risks_json（helper 内部序列化，传原生 dict/list 即可）。
helper 缺失时给出清晰报错；自测用 BEATLAB_ROOT 指向测试库隔离。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import librosa
import numpy as np
from scipy import signal

import common

SR = 22050
HOP = 512

# ---------- 常量 ----------
MOMENT_TYPES = (
    "melody", "melody_no_drums", "vocal_phrase",
    "drum_break", "bass_phrase", "texture", "transition",
)
MOMENT_TYPE_CN = {
    "melody": "旋律（带鼓）",
    "melody_no_drums": "无鼓旋律",
    "vocal_phrase": "人声 phrase",
    "drum_break": "鼓 break",
    "bass_phrase": "bass phrase",
    "texture": "texture",
    "transition": "过渡段",
}
BAR_WINDOWS = (1, 2, 4, 8)                 # bpm 存在时的窗口（小节数）
PHRASE_WINDOWS_S = (2.0, 4.0, 8.0, 16.0)  # bpm 缺失时的窗口（秒）
WINDOW_MIN_S = 2.0
WINDOW_MAX_S = 16.0
MIN_BAR_S = 0.2                           # 小节过短（bpm 极快）时窗口下限
MAX_PER_TYPE_PER_ASSET = 2                # diversity filter：同 asset 同类型上限

TEXTURE_CENTROID_HZ = 1800.0
TEXTURE_MAX_ONSET_PER_S = 3.0
DRUM_BREAK_MIN = 0.35
DRUM_BREAK_MAX_VOCAL = 0.20
VOCAL_PHRASE_MIN = 0.30
BASS_PHRASE_MIN = 0.30
BASS_PHRASE_MAX_DRUM = 0.30
MELODY_NODRUM_MAX_DRUM = 0.12
MELODY_NODRUM_MIN_HARMONICITY = 0.15
WINDOW_MIN_RMS_DB = -50.0                 # 低于该 RMS 的窗口视为静音，跳过
SILENCE_RMS = 1e-4                        # 静音间隙判定阈值（线性幅值）

# 8 维默认权重（与 score.py DEFAULT_MOMENT_WEIGHTS 保持一致，联调时核对）
DEFAULT_WEIGHTS = {
    "loopability": 0.25, "memorability": 0.20, "key_stability": 0.15,
    "drum_state": 0.10, "vocal_state": 0.10, "timbre_uniqueness": 0.10,
    "structure_position": 0.05, "space": 0.05,
}

# ---------- 数据层 helper（Dev-1 契约，缺失时清晰报错） ----------


def _require(name: str):
    fn = getattr(common, name, None)
    if fn is None:
        raise RuntimeError(
            f"[moments] common.{name} 缺失：需要数据层（Dev-1）合入后的 common.py。"
            f"当前环境无法访问 {name}；联调前自测请 monkeypatch common.{name}。")
    return fn


def _row_get(row: Any, key: str, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _asset_src(row: Any) -> Path:
    """library/<category>/<id>/source.wav；旧 samples 行带 library_path 时优先。"""
    lp = _row_get(row, "library_path", None)
    if lp:
        return Path(lp)
    category = _row_get(row, "category", "unknown")
    return common.ROOT / "library" / str(category) / str(row["id"]) / "source.wav"


# ---------- 通用小工具 ----------

def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if len(x) else 0.0


def _bandpass(y: np.ndarray, sr: float, lo: float, hi: float) -> np.ndarray:
    """4 阶 butterworth 带通（sosfiltfilt 零相位）。"""
    nyq = sr / 2.0
    sos = signal.butter(4, [lo / nyq, hi / nyq], btype="bandpass", output="sos")
    return signal.sosfiltfilt(sos, y)


def _band_share(y: np.ndarray, sr: float, lo: float, hi: float) -> float:
    """频带能量占比（相对全频带）。"""
    total = _rms(y)
    if total < 1e-6:
        return 0.0
    return _rms(_bandpass(y, sr, lo, hi)) / total


def _harmonicity(y: np.ndarray, sr: float) -> float:
    """谱平坦度反比：纯音→1，噪声→0（静音帧不计）。"""
    if _rms(y) < 1e-6:
        return 0.0
    d = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    flat = librosa.feature.spectral_flatness(S=d ** 2, power=2.0)[0]
    frame_rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=HOP)[0]
    active = frame_rms > 0.1 * float(np.max(frame_rms))
    if not np.any(active):
        return 0.0
    return float(1.0 - np.mean(flat[active]))


# ---------- 结构分段 ----------

def _ssm_bounds(y: np.ndarray, sr: float) -> list[float]:
    """chroma SSM → path_enhance → agglomerative 分段，返回边界秒。"""
    dur = len(y) / sr
    if dur < 4.0 or _rms(y) < 1e-6:
        return [0.0, dur]
    chroma = librosa.feature.chroma_stft(y=y, sr=sr, hop_length=HOP)
    ssm = librosa.segment.recurrence_matrix(chroma, mode="affinity", sym=True)
    ssm = librosa.segment.path_enhance(ssm, n=10)
    n_seg = max(2, min(8, int(dur // 2)))
    bounds = librosa.segment.agglomerative(ssm, n_seg)

    # 合并退化的小段（agglomerative 可能切出 <1s 的边界碎片）
    min_w = int(1.0 * sr / HOP)
    widths = np.diff(bounds).tolist()
    while len(widths) > 2 and min(widths) < min_w:
        i = int(np.argmin(widths))
        if i == 0 or (i < len(widths) - 1 and widths[i - 1] < widths[i + 1]):
            bounds = np.delete(bounds, i + 1)   # 并入右邻
        else:
            bounds = np.delete(bounds, i)       # 并入左邻
        widths = np.diff(bounds).tolist()
    return [round(float(b) * HOP / sr, 3) for b in bounds]


def _silence_bounds(y: np.ndarray, sr: float) -> list[float]:
    """静音间隙边界（无内容对比时的过渡段兜底）。"""
    frame_rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=HOP)[0]
    silent = frame_rms < SILENCE_RMS
    bounds: list[float] = []
    prev = False
    for i, s in enumerate(silent):
        if s != prev:
            bounds.append(round(i * HOP / sr, 3))
            prev = s
    if prev:  # 尾部静音
        bounds.append(round(len(frame_rms) * HOP / sr, 3))
    return bounds


def structure_bounds(y: np.ndarray, sr: float) -> list[float]:
    """结构边界 = SSM 分段边界 + 静音间隙边界，去重合并（<0.25s 视为同一处）。"""
    merged = sorted(set(_ssm_bounds(y, sr) + _silence_bounds(y, sr)))
    if len(merged) < 2:
        return [0.0, round(len(y) / sr, 3)]
    out = [merged[0]]
    for b in merged[1:]:
        if b - out[-1] >= 0.25:
            out.append(b)
    if out[-1] < len(y) / sr - 0.25:
        out.append(round(len(y) / sr, 3))
    return out


# ---------- 窗口生成 ----------

def moment_windows(duration_s: float, bpm: float | None) -> list[dict]:
    """滑动窗口：bpm 存在 → 1/2/4/8 小节；缺失 → 2/4/8/16s phrase。步长 = 窗口一半。"""
    if duration_s < WINDOW_MIN_S:
        return []
    windows: list[dict] = []
    seen: set[tuple[float, float]] = set()
    if bpm:
        bar_s = max(4.0 * 60.0 / common.clamp(float(bpm), 40.0, 240.0), MIN_BAR_S)
        sizes = [(bars, min(bars * bar_s, WINDOW_MAX_S)) for bars in BAR_WINDOWS]
    else:
        bar_s = None
        sizes = [(None, w) for w in PHRASE_WINDOWS_S]
    for bars, w in sizes:
        if w < WINDOW_MIN_S - 1e-6:
            continue
        if w > duration_s:
            w = duration_s
            if w < WINDOW_MIN_S - 1e-6:
                continue
        hop = w / 2.0
        t = 0.0
        while t + w <= duration_s + 1e-6:
            start, end = round(t, 3), round(min(t + w, duration_s), 3)
            if end - start >= WINDOW_MIN_S - 1e-6 and (start, end) not in seen:
                seen.add((start, end))
                windows.append({
                    "start_sec": start, "end_sec": end,
                    "bars": round((end - start) / bar_s, 2) if bar_s else None,
                })
            t += hop
    return windows


# ---------- 8 维评分 ----------

def _score_loopability(y: np.ndarray, sr: float) -> float:
    """端点频谱互相关（连续性）+ 能量平稳度，等权。"""
    n2 = max(int(sr * 0.75), int(len(y) // 4))
    if len(y) < n2 * 2:
        return 0.0

    def profile(seg: np.ndarray) -> np.ndarray:
        d = np.abs(librosa.stft(seg, n_fft=2048, hop_length=HOP))
        return d.mean(axis=1)

    ph, pt = profile(y[:n2]), profile(y[-n2:])
    if float(ph.std()) > 1e-9 and float(pt.std()) > 1e-9:
        c = float(np.corrcoef(ph, pt)[0, 1])
    else:
        c = 0.0
    continuity = common.clamp((c + 1.0) / 2.0, 0.0, 1.0)

    env = librosa.feature.rms(y=y, frame_length=1024, hop_length=HOP)[0]
    mean = float(np.mean(env))
    if mean < 1e-6:
        return 0.0
    cv = float(np.std(env)) / mean
    stability = common.clamp(1.0 - cv / 1.5, 0.0, 1.0)
    return round(0.5 * continuity + 0.5 * stability, 4)


def _score_memorability(y: np.ndarray, sr: float) -> float:
    """spectral novelty 峰：数量（8 个记满分）+ 相对强度，等权。"""
    oenv = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    peak = float(np.max(oenv))
    if peak < 1e-4 or len(oenv) < 10:
        return 0.0
    peaks = librosa.util.peak_pick(
        oenv, pre_max=3, post_max=3, pre_avg=3, post_avg=5, delta=0.05, wait=3)
    count = float(len(peaks))
    strength = float(np.mean(oenv[peaks])) / peak if len(peaks) else 0.0
    return round(common.clamp(0.5 * count / 8.0 + 0.5 * strength, 0.0, 1.0), 4)


def _score_key_stability(y: np.ndarray, sr: float) -> float:
    """帧 chroma 与均值 chroma 的平均余弦相似度：稳定调性→1。"""
    chroma = librosa.feature.chroma_stft(y=y, sr=sr, hop_length=HOP)
    m = chroma.mean(axis=1)
    m_norm = float(np.linalg.norm(m))
    if m_norm < 1e-9:
        return 0.0
    f_norm = np.linalg.norm(chroma, axis=0)
    if float(np.min(f_norm)) < 1e-9:
        return 0.0
    dots = (m @ chroma) / (m_norm * f_norm + 1e-9)
    dev = float(np.mean(1.0 - dots))
    return round(common.clamp(1.0 - dev / 0.4, 0.0, 1.0), 4)


def _stem_states(y: np.ndarray, stems: dict[str, np.ndarray] | None, sr: float) -> tuple[float, float]:
    """(drum_state, vocal_state)。优先 stems RMS 占比；否则频带能量代理。"""
    src_rms = _rms(y)
    if src_rms < 1e-6:
        return 0.0, 0.0
    if stems:
        drum = common.clamp(_rms(stems.get("drums", np.zeros(1))) / src_rms, 0.0, 1.0)
        vocal = common.clamp(_rms(stems.get("vocal", np.zeros(1))) / src_rms, 0.0, 1.0)
        return round(drum, 4), round(vocal, 4)
    drum = common.clamp(_band_share(y, sr, 40.0, 120.0) / 0.4, 0.0, 1.0)
    vocal = common.clamp(_band_share(y, sr, 300.0, 3400.0) / 0.4, 0.0, 1.0)
    return round(drum, 4), round(vocal, 4)


def _score_timbre(y: np.ndarray, sr: float) -> float:
    """MFCC 时变方差 + 频谱质心偏离，等权。"""
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    mfcc_var = float(np.mean(np.std(mfcc, axis=1)))
    mfcc_score = common.clamp(mfcc_var / 40.0, 0.0, 1.0)

    centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    mean_c = float(np.mean(centroid))
    if mean_c < 1e-6:
        return round(mfcc_score, 4)
    cent_dev = float(np.std(centroid)) / mean_c
    cent_score = common.clamp(cent_dev / 1.0, 0.0, 1.0)
    return round(0.5 * mfcc_score + 0.5 * cent_score, 4)


def _score_structure_position(window: dict, bounds: list[float], mtype: str) -> float:
    """距最近结构边界的归一化距离。transition 取近（边界处→1），其余取远（段中央→1）。"""
    if not bounds or len(bounds) < 2:
        return 0.5
    center = (window["start_sec"] + window["end_sec"]) / 2.0
    nearest = min(abs(center - b) for b in bounds)
    scale = max((window["end_sec"] - window["start_sec"]) / 2.0, 1.0)
    proximity = common.clamp(1.0 - nearest / scale, 0.0, 1.0)
    return round(proximity if mtype == "transition" else 1.0 - proximity, 4)


def _score_space(y: np.ndarray, sr: float) -> float:
    """动态留白：低能量帧占比 + crest factor，加权。"""
    rms_all = _rms(y)
    if rms_all < 1e-6:
        return 0.0
    frame_rms = librosa.feature.rms(y=y, frame_length=1024, hop_length=HOP)[0]
    peak_f = float(np.max(frame_rms))
    if peak_f < 1e-6:
        return 0.0
    headroom = 1.0 - float(np.mean(frame_rms > 0.4 * peak_f))
    peak = float(np.max(np.abs(y)))
    crest = common.clamp(math.log10(max(peak / rms_all, 1.414)) / math.log10(20.0), 0.0, 1.0)
    return round(common.clamp(0.6 * headroom + 0.4 * crest, 0.0, 1.0), 4)


# ---------- 窗口判定与评分 ----------

def classify_window(y: np.ndarray, stems: dict[str, np.ndarray] | None,
                    sr: float, window: dict, bounds: list[float]) -> tuple[str, dict]:
    """7 类判定。返回 (type, 判定依据 dict)。"""
    start, end = window["start_sec"], window["end_sec"]
    # 1) transition：窗口内含结构边界
    for b in bounds:
        if start + 1e-6 < b < end - 1e-6:
            return "transition", {"bound_sec": b}

    # 2) texture：高质心低 onset
    centroid = float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr)[0]))
    onset_t = librosa.onset.onset_detect(y=y, sr=sr, units="time")
    onset_density = len(onset_t) / max(end - start, 1e-6)
    if centroid >= TEXTURE_CENTROID_HZ and onset_density <= TEXTURE_MAX_ONSET_PER_S:
        return "texture", {"centroid_hz": round(centroid, 0), "onset_per_s": round(onset_density, 2)}

    # 3) stem 类型（无 stems 时不可判定，走代理/兜底）
    src_rms = _rms(y)
    if src_rms < 1e-6:
        return "melody", {}
    if stems:
        drum_r = _rms(stems.get("drums", np.zeros(1))) / src_rms
        vocal_r = _rms(stems.get("vocal", np.zeros(1))) / src_rms
        bass_r = _rms(stems.get("bass", np.zeros(1))) / src_rms
        if drum_r >= DRUM_BREAK_MIN and vocal_r <= DRUM_BREAK_MAX_VOCAL:
            return "drum_break", {"drum_r": round(drum_r, 3), "vocal_r": round(vocal_r, 3)}
        if vocal_r >= VOCAL_PHRASE_MIN:
            return "vocal_phrase", {"vocal_r": round(vocal_r, 3)}
        if bass_r >= BASS_PHRASE_MIN and drum_r <= BASS_PHRASE_MAX_DRUM:
            return "bass_phrase", {"bass_r": round(bass_r, 3), "drum_r": round(drum_r, 3)}
        drum_p = drum_r
    else:
        drum_p = _band_share(y, sr, 40.0, 120.0) / 0.4

    # 4) 无鼓旋律：鼓低且和声明确
    if drum_p <= MELODY_NODRUM_MAX_DRUM and _harmonicity(y, sr) >= MELODY_NODRUM_MIN_HARMONICITY:
        return "melody_no_drums", {"drum_p": round(drum_p, 3)}
    return "melody", {"drum_p": round(drum_p, 3)}


def score_window(y: np.ndarray, stems: dict[str, np.ndarray] | None,
                 sr: float, window: dict, bounds: list[float], mtype: str) -> dict:
    """8 维评分（各 0-1）。"""
    drum_state, vocal_state = _stem_states(y, stems, sr)
    return {
        "loopability": _score_loopability(y, sr),
        "memorability": _score_memorability(y, sr),
        "key_stability": _score_key_stability(y, sr),
        "drum_state": drum_state,
        "vocal_state": vocal_state,
        "timbre_uniqueness": _score_timbre(y, sr),
        "structure_position": _score_structure_position(window, bounds, mtype),
        "space": _score_space(y, sr),
    }


def moment_total(scores: dict) -> float:
    return round(sum(scores.get(k, 0.0) * w for k, w in DEFAULT_WEIGHTS.items()), 4)


# ---------- explain / risks ----------

def _build_explain(mtype: str, window: dict, scores: dict, basis: dict,
                   stems_available: bool, bpm: float | None) -> list[str]:
    out: list[str] = []
    why = {
        "transition": f"窗口跨结构边界（{basis.get('bound_sec', 0):.1f}s），适合做段落切换素材",
        "texture": f"质心 {basis.get('centroid_hz', 0):.0f}Hz、onset 密度低，适合做 texture 铺垫",
        "drum_break": f"鼓 RMS 占比 {basis.get('drum_r', 0):.2f} 且人声低（{basis.get('vocal_r', 0):.2f}），适合取鼓 break",
        "vocal_phrase": f"人声 RMS 占比 {basis.get('vocal_r', 0):.2f}，可作 vocal 采样",
        "bass_phrase": f"bass RMS 占比 {basis.get('bass_r', 0):.2f} 且鼓低，适合取 bass line",
        "melody_no_drums": f"鼓占比低（{basis.get('drum_p', 0):.2f}）且和声明确，适合旋律采样",
        "melody": "和声内容为主（带鼓），适合整体采样",
    }[mtype]
    out.append(f"类型 {mtype}（{MOMENT_TYPE_CN[mtype]}）：{why}")
    if bpm and window.get("bars") is not None:
        out.append(f"窗口 {window['start_sec']:.1f}-{window['end_sec']:.1f}s"
                   f"（约 {window['bars']:.1f} 小节，BPM={bpm:.1f}）")
    else:
        out.append(f"窗口 {window['start_sec']:.1f}-{window['end_sec']:.1f}s（phrase 窗口，无 BPM）")
    dims = sorted(scores.items(), key=lambda kv: -kv[1])[:3]
    out.append("得分亮点：" + "，".join(f"{k}={v:.2f}" for k, v in dims))
    if not stems_available:
        out.append("无 stems：鼓/人声状态为频带代理估计")
    return out


def _build_risks(mtype: str, window: dict, scores: dict, stems_available: bool,
                 bpm: float | None, dur: float, rms_db: float) -> list[str]:
    out: list[str] = []
    if scores.get("vocal_state", 0.0) > 0.15:
        out.append(f"带人声（vocal_state={scores['vocal_state']:.2f}），纯器乐需求需过滤或取 vocal 外 stems")
    if scores.get("key_stability", 0.0) < 0.4:
        out.append(f"调性不稳（key_stability={scores['key_stability']:.2f}），对拍/变调时注意")
    if scores.get("drum_state", 0.0) > 0.5 and mtype in ("melody", "melody_no_drums"):
        out.append("鼓点较强，旋律采样需注意节拍对齐")
    if window["start_sec"] < 0.5 or window["end_sec"] > dur - 0.5:
        out.append("靠近素材首尾，采样时注意截断")
    if not bpm:
        out.append("无 BPM 信息，小节对齐未知（按秒窗口生成）")
    if not stems_available:
        out.append("无 stems，鼓/人声判定为频带代理，可信度有限")
    if rms_db < -30.0:
        out.append(f"窗口能量偏低（{rms_db:.0f}dB），使用前需增益")
    return out or ["无明显风险"]


# ---------- 单 asset 分析 ----------

def _load_stems(asset_dir: Path, sr: int) -> dict[str, np.ndarray]:
    stems: dict[str, np.ndarray] = {}
    for name in ("drums", "vocal", "bass", "other"):
        p = asset_dir / "stems" / f"{name}.wav"
        if p.exists():
            y, _ = common.load_audio_mono(p, sr)
            stems[name] = np.asarray(y, dtype=np.float32)
    return stems


def _missing_type_reason(types_found: set[str], bounds: list[float],
                         stems_available: bool, n_windows: int, skipped: int) -> str:
    missing = set(MOMENT_TYPES) - types_found
    reasons: list[str] = []
    if "transition" in missing:
        if len(bounds) < 3:
            reasons.append("结构分段不足（<3 段），transition 无法判定")
        else:
            reasons.append("无窗口落在结构边界上，transition 未触发")
    if "texture" in missing:
        reasons.append("无高质心低 onset 段落，texture 未触发")
    if not stems_available:
        reasons.append("无 stems：drum_break/vocal_phrase/bass_phrase 不可判定")
    if "melody_no_drums" in missing and "melody" not in missing:
        reasons.append("鼓低频能量持续存在，无鼓旋律段未触发")
    if n_windows == 0:
        reasons.append("素材过短（<2s）或全部窗口静音，无可用窗口")
    elif skipped:
        reasons.append(f"{skipped}/{n_windows + skipped} 个窗口静音被跳过")
    return "；".join(reasons) if reasons else "素材内容均匀，类型判定集中"


def analyze_asset(asset: Any, write_db: bool = True) -> tuple[list[dict], str]:
    """分析单个 asset：生成 Moment 候选 → diversity filter → upsert。

    返回 (保留的 moment 列表, 类型数不足 3 时的原因说明)。
    """
    asset_id = str(asset["id"])
    src = _asset_src(asset)
    if not src.exists():
        reason = f"source 文件不存在（{src}）"
        print(f"[moments] {asset_id}: 无法分析：{reason}")
        return [], reason

    y, sr = common.load_audio_mono(src, SR)
    sr = int(round(sr))
    dur = len(y) / sr
    bpm = _row_get(asset, "bpm", None)
    stems_available = False
    stems = _load_stems(src.parent, sr)
    if stems:
        stems_available = True
        # stems 时长可能短于 source（对齐到 min）
        min_len = min(len(y), min(len(v) for v in stems.values()))
        y, stems = y[:min_len], {k: v[:min_len] for k, v in stems.items()}
        dur = min_len / sr

    bounds = structure_bounds(y, sr)
    windows = moment_windows(dur, bpm)
    candidates: list[dict] = []
    skipped = 0
    for w in windows:
        s0, s1 = int(w["start_sec"] * sr), int(w["end_sec"] * sr)
        seg = y[s0:s1]
        rms_db = 20.0 * math.log10(_rms(seg) + 1e-9)
        if rms_db < WINDOW_MIN_RMS_DB:
            skipped += 1
            continue
        stems_seg = {k: v[s0:s1] for k, v in stems.items()} if stems else None
        mtype, basis = classify_window(seg, stems_seg, sr, w, bounds)
        scores = score_window(seg, stems_seg, sr, w, bounds, mtype)
        candidates.append({
            "asset_id": asset_id,
            "type": mtype,
            "start_sec": w["start_sec"],
            "end_sec": w["end_sec"],
            "bars": round(w["bars"], 2) if w["bars"] is not None else None,
            "stem": {"drum_break": "drums", "vocal_phrase": "vocal", "bass_phrase": "bass"}
                    .get(mtype, "other" if stems else "source"),
            "scores": scores,
            "total": moment_total(scores),
            "explain": _build_explain(mtype, w, scores, basis, stems_available, bpm),
            "risks": _build_risks(mtype, w, scores, stems_available, bpm, dur, rms_db),
        })

    # diversity filter：同 asset 同类型最多 MAX_PER_TYPE_PER_ASSET 个（按总分取高）
    by_type: dict[str, list[dict]] = {}
    for c in candidates:
        by_type.setdefault(c["type"], []).append(c)
    kept: list[dict] = []
    for mtype, cs in by_type.items():
        cs.sort(key=lambda c: -c["total"])
        kept.extend(cs[:MAX_PER_TYPE_PER_ASSET])
    kept.sort(key=lambda m: (m["start_sec"], m["type"]))

    if write_db:
        upsert_moment = _require("upsert_moment")
        conn = common.get_db()
        for m in kept:
            upsert_moment(conn, {
                "asset_id": m["asset_id"],
                "type": m["type"],
                "start_sec": m["start_sec"],
                "end_sec": m["end_sec"],
                "bars": m["bars"],
                "stem": m["stem"],
                "scores_json": json.dumps(m["scores"], ensure_ascii=False),
                "explain_json": json.dumps(m["explain"], ensure_ascii=False),
                "risks_json": json.dumps(m["risks"], ensure_ascii=False),
            })

    types_found = {m["type"] for m in kept}
    reason = ""
    if len(types_found) < 3:
        reason = _missing_type_reason(types_found, bounds, stems_available, len(windows), skipped)
    type_line = ", ".join(f"{t}×{sum(1 for m in kept if m['type'] == t)}" for t in MOMENT_TYPES
                          if any(m["type"] == t for m in kept))
    print(f"[moments] {asset_id}: 窗口={len(windows)} 静音跳过={skipped} "
          f"候选={len(candidates)} 保留={len(kept)} 类型={type_line or '无'}")
    if reason:
        print(f"[moments] {asset_id}: 仅 {len(types_found)} 种类型，未达 ≥3 种。原因：{reason}")
    return kept, reason


# ---------- CLI ----------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="moments.py",
        description="BeatLab 模块 D2：Sample Moments（7 类判定 + 8 维评分 + diversity filter）",
    )
    parser.add_argument("asset_ids", nargs="*", help="指定 asset id（可多个）")
    parser.add_argument("--all", action="store_true", help="分析全部 assets")
    parser.add_argument("--dry-run", action="store_true", help="只计算不写库（upsert 跳过）")
    args = parser.parse_args(argv)

    if not args.asset_ids and not args.all:
        parser.print_help()
        return 0

    get_assets = _require("get_assets")
    conn = common.get_db()
    assets = get_assets(conn)
    if args.asset_ids:
        assets = [a for a in assets if a["id"] in args.asset_ids]
    if not assets:
        print("[moments] 没有可分析的 assets")
        return 0

    failed = 0
    for asset in assets:
        try:
            analyze_asset(asset, write_db=not args.dry_run)
        except Exception as exc:
            failed += 1
            print(f"[moments] {str(asset.get('id', '?'))}: 分析异常，跳过：{exc}")
    print(f"[moments] 完成：共 {len(assets)} 个 asset，失败 {failed} 个")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
