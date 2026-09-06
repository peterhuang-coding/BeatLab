"""BeatLab 模块 C：选品评分（100 分 rubric）。

对 samples 表中每个音源计算 9 项 0-1 特征并写入 scores 表：
  drums_presence  闸门1：demucs 鼓 stem RMS 占比；无 stem 时退化为 40-120Hz 频带 onset 占比（备注 fallback）
  vocal_free      闸门2：1 - vocal stem RMS 占比；无 stem 时用 300-3400Hz 包络调制深度估计人声可能性取反
  structure_hit   闸门3：chroma SSM(path_enhance+agglomerative) 分段后按 onset 密度/能量分布判定 1/0.5/0
  key_bpm_conf    beat 追踪周期强度 + chroma 主峰纯度加权
  loopability     首尾 2s 频谱互相关 + 全曲能量平稳度
  timbre_uniqueness  MFCC 时变方差 + 频谱质心偏离
  harmonicity     谱平坦度反比
  dynamics_space  crest factor + 中侧能量比（立体声宽度）
  source_prior    tags/文件名关键词命中加分

用法：
  .venv/bin/python pipeline/score.py <sample_id> [<sample_id> ...]
  .venv/bin/python pipeline/score.py --all       # 全库评分
  .venv/bin/python pipeline/score.py --pool      # 全库评分后输出 passed 清单

总分逻辑（common.Score.total）：三闸门任一为 0 → 总分减半；total ≥ SCORE_PASS 记 passed=1。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import librosa
import numpy as np
from scipy import signal

import common

SR = 22050
HOP = 512

# ---------- 特征计算 ----------

# 各特征映射参数（0-1 归一化阈值，调研/听感标定）
KEYWORDS_PRIOR = {
    "78rpm": 0.5, "breakbeat": 0.5, "vinyl": 0.4, "78": 0.4,
    "funk": 0.3, "soul": 0.3, "jazz": 0.3, "disco": 0.3, "boogie": 0.3,
    "motown": 0.3, "stax": 0.3, "r&b": 0.3, "rnb": 0.3, "hip-hop": 0.2,
    "hiphop": 0.2, "rare": 0.2, "old": 0.2, "vintage": 0.2, "tape": 0.2,
    "groove": 0.2, "latin": 0.2, "afro": 0.2, "fusion": 0.2, "break": 0.3,
    "drum": 0.15, "percussion": 0.15, "kit": 0.15, "loop": 0.1,
}


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if len(x) else 0.0


def _bandpass(y: np.ndarray, sr: float, lo: float, hi: float) -> np.ndarray:
    """4 阶 butterworth 带通（sosfiltfilt 零相位）。"""
    nyq = sr / 2.0
    sos = signal.butter(4, [lo / nyq, hi / nyq], btype="bandpass", output="sos")
    return signal.sosfiltfilt(sos, y)


def _stem_path(lib_path: str, stem: str) -> Path | None:
    """library/<category>/<id>/stems/<stem>.wav，不存在返回 None。"""
    p = Path(lib_path).parent / "stems" / f"{stem}.wav"
    return p if p.exists() else None


def _drums_presence(lib_path: str, y: np.ndarray, sr: float) -> tuple[float, str]:
    """闸门1：鼓存在度。优先 demucs 鼓 stem RMS 占比；否则低频频带 onset 占比代理。"""
    drums_stem = _stem_path(lib_path, "drums")
    if drums_stem is not None:
        yd, _ = common.load_audio_mono(drums_stem, SR)
        src_rms = _rms(y)
        ratio = _rms(yd) / src_rms if src_rms > 1e-6 else 0.0
        return common.clamp(ratio, 0.0, 1.0), ""

    # fallback：40-120Hz（底鼓/桶鼓）频带代理鼓存在度。
    # 两个分量：低频带能量占比（与 stem 口径"鼓 RMS 占比"同构）
    # + 各 onset 帧内低频带 onset 占比（onset_strength 在带通后算，取 75 分位抗噪）。
    y_low = _bandpass(y, sr, 40.0, 120.0)
    src_rms = _rms(y)
    if src_rms < 1e-6:
        return 0.0, "fallback(silent)"
    e_share = _rms(y_low) / src_rms
    if e_share < 0.02:  # 低频带无能量 → 无鼓（纯人声/高音乐器）
        return 0.0, "fallback(no low-band energy)"
    o_low = librosa.onset.onset_strength(y=y_low, sr=sr, hop_length=HOP)
    o_all = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    skip = int(0.5 * sr / HOP)  # 跳过文件起始瞬态（非音乐 onset）
    o_low, o_all = o_low[skip:], o_all[skip:]
    if float(np.max(o_all)) < 1e-4:
        return 0.0, "fallback(no onset)"
    onset_frames = o_all > 0.1 * float(np.max(o_all))
    if int(np.sum(onset_frames)) < 3:
        return 0.0, "fallback(no onset)"
    onset_share = float(np.percentile(o_low[onset_frames] / o_all[onset_frames], 75))
    e_score = common.clamp(e_share / 0.4, 0.0, 1.0)
    o_score = common.clamp(onset_share / 0.06, 0.0, 1.0)
    presence = common.clamp(0.5 * e_score + 0.5 * o_score, 0.0, 1.0)
    return presence, "fallback(no stems/drums.wav)"


def _vocal_free(lib_path: str, y: np.ndarray, sr: float) -> tuple[float, str]:
    """闸门2：无 vocal 程度。优先 vocal stem；否则 300-3400Hz 包络调制深度代理。"""
    vocal_stem = _stem_path(lib_path, "vocal")
    if vocal_stem is not None:
        yv, _ = common.load_audio_mono(vocal_stem, SR)
        src_rms = _rms(y)
        ratio = _rms(yv) / src_rms if src_rms > 1e-6 else 0.0
        return 1.0 - common.clamp(ratio, 0.0, 1.0), ""

    # fallback：人声带（300-3400Hz）能量包络。人声呈音节级平滑调制（2-10Hz），
    # 鼓点呈瞬态尖峰：平滑前后变异系数比 + 平滑包络调制深度 → 人声可能性。
    y_v = _bandpass(y, sr, 300.0, 3400.0)
    env = librosa.feature.rms(y=y_v, frame_length=1024, hop_length=HOP)[0]
    mean = float(np.mean(env))
    if mean < 1e-6:
        return 1.0, "fallback(no vocal-band energy)"
    b, a = signal.butter(2, 10.0 / (sr / HOP / 2.0), btype="low")
    env_s = signal.filtfilt(b, a, env)
    cv_raw = float(np.std(env)) / mean
    if cv_raw < 0.05:  # 近恒定包络 → 非人声
        return 1.0, "fallback(flat envelope)"
    cv_smooth = float(np.std(env_s)) / float(np.mean(env_s))
    smoothness = common.clamp(cv_smooth / cv_raw, 0.0, 1.0)      # 平滑保留度：尖峰低、音节高
    mod_depth = common.clamp(cv_smooth / 0.3, 0.0, 1.0)          # 实际调制深度
    d = np.abs(librosa.stft(y_v, n_fft=2048, hop_length=HOP))
    flat = float(np.mean(librosa.feature.spectral_flatness(S=d ** 2, power=2.0)))
    tonality = common.clamp(1.0 - flat, 0.0, 1.0)                # 人声带需具谐波性（鼓为噪声）
    # 人声带内语音是连续的（音节内无静音），鼓点是孤立瞬态：活跃帧占比区分二者
    active_frac = float(np.mean(env > 0.2 * float(np.max(env))))
    duty = common.clamp((active_frac - 0.25) / 0.55, 0.0, 1.0)
    vocal_lik = smoothness * mod_depth * tonality * duty
    return 1.0 - vocal_lik, "fallback(no stems/vocal.wav)"


def _structure_hit(y: np.ndarray, sr: float) -> float:
    """闸门3：结构命中（1 命中 / 0.5 部分 / 0 无）。

    chroma SSM → path_enhance → agglomerative 分段；命中条件：段间 onset 密度
    差异明显，且存在显著高密度段（break）或首尾低能量段（intro/outro）。
    """
    dur = len(y) / sr
    if dur < 4.0 or _rms(y) < 1e-6:
        return 0.0
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    rms_env = librosa.feature.rms(y=y, frame_length=2048, hop_length=HOP)[0]
    n_frames = min(len(onset), len(rms_env))
    if n_frames < 16 or float(np.max(onset)) < 1e-4:  # 无 onset 活动 → 无结构
        return 0.0
    onset, rms_env = onset[:n_frames], rms_env[:n_frames]

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

    seg_onset: list[float] = []
    seg_energy: list[float] = []
    for i in range(len(bounds) - 1):
        a, b = int(bounds[i]), min(int(bounds[i + 1]), n_frames)
        if b <= a:
            continue
        seg_onset.append(float(np.mean(onset[a:b])))
        seg_energy.append(float(np.mean(rms_env[a:b])))
    if len(seg_onset) < 2:
        return 0.0

    m_on = float(np.mean(seg_onset))
    m_en = float(np.mean(seg_energy))
    spread = max(float(np.std(seg_onset)) / (m_on + 1e-9),
                 float(np.std(seg_energy)) / (m_en + 1e-9))    # 段间 onset/能量差异
    meaningful = spread > 0.25
    # 密集 break 的 onset 通量会被 max-filter 参考谱压低，能量更可靠 → 双指标
    break_like = (float(np.max(seg_onset)) > 1.5 * float(np.median(seg_onset))
                  or float(np.max(seg_energy)) > 1.5 * float(np.median(seg_energy)))
    intro_outro = float(min(seg_energy[0], seg_energy[-1])) < 0.6 * m_en  # 首尾低能量
    if meaningful and (break_like or intro_outro):
        return 1.0
    return 0.5 if meaningful else 0.0


def _key_bpm_conf(y: np.ndarray, sr: float) -> float:
    """beat 追踪周期强度 + chroma 主峰纯度，等权。"""
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    bpm_conf = 0.0
    if float(np.max(onset_env)) > 1e-4:
        try:
            tempo, _ = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr,
                                               hop_length=HOP)
            tempo = float(np.atleast_1d(tempo)[0])
        except Exception:
            tempo = 0.0
        if 30.0 <= tempo <= 300.0:
            lag = int(round(60.0 / tempo * sr / HOP))
            ac = librosa.autocorrelate(onset_env)
            if 0 < lag < len(ac):
                bpm_conf = common.clamp(float(ac[lag]), 0.0, 1.0)

    chroma = librosa.feature.chroma_stft(y=y, sr=sr)
    c = np.sort(np.mean(chroma, axis=1))[::-1]
    key_conf = common.clamp((c[0] - c[1]) / (c[0] + 1e-9), 0.0, 1.0)
    return round(0.5 * bpm_conf + 0.5 * key_conf, 4)


def _loopability(y: np.ndarray, sr: float) -> float:
    """首尾各 2s 频谱互相关（端点连续性）+ 全曲能量平稳度，等权。"""
    n2 = int(sr * 2)
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


def _timbre_uniqueness(y: np.ndarray, sr: float) -> float:
    """MFCC 时变方差 + 频谱质心偏离均值，等权。"""
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    mfcc_var = float(np.mean(np.std(mfcc, axis=1)))
    mfcc_score = common.clamp(mfcc_var / 40.0, 0.0, 1.0)

    centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    mean_c = float(np.mean(centroid))
    if mean_c < 1e-6:
        return mfcc_score
    cent_dev = float(np.std(centroid)) / mean_c
    cent_score = common.clamp(cent_dev / 1.0, 0.0, 1.0)
    return round(0.5 * mfcc_score + 0.5 * cent_score, 4)


def _harmonicity(y: np.ndarray, sr: float) -> float:
    """谱平坦度反比：纯音→1，噪声→0。"""
    if _rms(y) < 1e-6:
        return 0.0
    d = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    flat = librosa.feature.spectral_flatness(S=d ** 2, power=2.0)[0]
    frame_rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=HOP)[0]
    active = frame_rms > 0.1 * float(np.max(frame_rms))  # 静音帧不算
    if not np.any(active):
        return 0.0
    return round(float(1.0 - np.mean(flat[active])), 4)


def _dynamics_space(path: Path, y: np.ndarray, sr: float) -> float:
    """crest factor（峰值/有效值）+ 立体声宽度（中侧能量比）加权。"""
    rms_all = _rms(y)
    if rms_all < 1e-6:
        return 0.0
    peak = float(np.max(np.abs(y)))
    crest = common.clamp(np.log10(max(peak / rms_all, 1.414)) / np.log10(20.0), 0.0, 1.0)

    width = 0.0
    try:
        y_st, _ = librosa.load(path, sr=sr, mono=False)
        if y_st.ndim > 1 and y_st.shape[0] > 1:
            mid, side = (y_st[0] + y_st[1]) / 2.0, (y_st[0] - y_st[1]) / 2.0
            width = common.clamp(_rms(side) / (_rms(mid) + 1e-9), 0.0, 1.0)
    except Exception:
        width = 0.0
    return round(0.7 * crest + 0.3 * width, 4)


def _source_prior(tags: list[str], orig_path: str | None) -> float:
    """tags + 文件名关键词命中加分（封顶 1）。"""
    text = (" ".join(tags or []) + " " + Path(orig_path or "").stem).lower()
    total = sum(w for kw, w in KEYWORDS_PRIOR.items() if kw in text)
    return round(common.clamp(total, 0.0, 1.0), 4)


# ---------- 单条评分 ----------

def score_sample(sample: dict[str, Any]) -> tuple[common.Score, list[str]]:
    """对一条 samples 记录计算 9 项特征。返回 (Score, fallback 备注列表)。"""
    lib_path = sample["library_path"]
    notes: list[str] = []
    path = Path(lib_path)
    if not path.exists():
        raise FileNotFoundError(f"library 文件缺失: {lib_path}")
    y, sr = common.load_audio_mono(path, SR)

    drums, d_note = _drums_presence(lib_path, y, sr)
    if d_note:
        notes.append(f"drums={d_note}")
    vocal_free, v_note = _vocal_free(lib_path, y, sr)
    if v_note:
        notes.append(f"vocal={v_note}")

    tags: list[str] = []
    try:
        tags = json.loads(sample.get("tags") or "[]")
    except json.JSONDecodeError:
        tags = []

    return common.Score(
        sample_id=sample["id"],
        drums_presence=round(drums, 4),
        vocal_free=round(vocal_free, 4),
        structure_hit=_structure_hit(y, sr),
        key_bpm_conf=_key_bpm_conf(y, sr),
        loopability=_loopability(y, sr),
        timbre_uniqueness=_timbre_uniqueness(y, sr),
        harmonicity=_harmonicity(y, sr),
        dynamics_space=_dynamics_space(path, y, sr),
        source_prior=_source_prior(tags, sample.get("orig_path")),
    ), notes


UPSERT_SQL = """
INSERT INTO scores (sample_id, drums_presence, vocal_free, structure_hit,
                    key_bpm_conf, loopability, timbre_uniqueness,
                    harmonicity, dynamics_space, source_prior,
                    total, passed, scored_at)
VALUES (:sample_id, :drums_presence, :vocal_free, :structure_hit,
        :key_bpm_conf, :loopability, :timbre_uniqueness,
        :harmonicity, :dynamics_space, :source_prior,
        :total, :passed, :scored_at)
ON CONFLICT(sample_id) DO UPDATE SET
    drums_presence=excluded.drums_presence,
    vocal_free=excluded.vocal_free,
    structure_hit=excluded.structure_hit,
    key_bpm_conf=excluded.key_bpm_conf,
    loopability=excluded.loopability,
    timbre_uniqueness=excluded.timbre_uniqueness,
    harmonicity=excluded.harmonicity,
    dynamics_space=excluded.dynamics_space,
    source_prior=excluded.source_prior,
    total=excluded.total, passed=excluded.passed, scored_at=excluded.scored_at
"""


def upsert_score(conn, score: common.Score) -> float:
    """按 common.Score.total() 逻辑落库，返回 total。"""
    total = score.total()
    passed = 1 if total >= common.SCORE_PASS else 0
    conn.execute(
        UPSERT_SQL,
        {
            **vars(score),
            "total": total,
            "passed": passed,
            "scored_at": common.today_str(),
        },
    )
    return total


# ---------- CLI ----------

def _print_sample(score: common.Score, total: float, notes: list[str]) -> None:
    gates = " ".join(f"{k}={getattr(score, k):.2f}" for k in common.GATE_KEYS)
    note = f"  [{'; '.join(notes)}]" if notes else ""
    print(
        f"[{score.sample_id}] total={total:5.1f} passed={int(total >= common.SCORE_PASS)} "
        f"gates({gates}) bpm_key={score.key_bpm_conf:.2f} loop={score.loopability:.2f} "
        f"timbre={score.timbre_uniqueness:.2f} harm={score.harmonicity:.2f} "
        f"dyn={score.dynamics_space:.2f} prior={score.source_prior:.2f}{note}"
    )


def _print_pool(conn) -> None:
    rows = conn.execute(
        "SELECT sample_id, total, drums_presence, vocal_free, structure_hit "
        "FROM scores WHERE passed=1 ORDER BY total DESC"
    ).fetchall()
    print(f"POOL (passed, n={len(rows)}):")
    for r in rows:
        print(
            f"  {r[0]}  total={r[1]:5.1f}  drums={r[2]:.2f} "
            f"vocal_free={r[3]:.2f} structure={r[4]:.2f}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BeatLab 模块 C：选品评分")
    parser.add_argument("ids", nargs="*", help="sample_id 列表")
    parser.add_argument("--all", action="store_true", help="全库评分")
    parser.add_argument("--pool", action="store_true", help="评分后输出 passed 清单")
    args = parser.parse_args(argv)

    if not args.ids and not args.all and not args.pool:
        parser.print_help()
        return 2

    conn = common.get_db()
    conn.row_factory = sqlite3.Row  # common.get_db 默认返回元组，这里按列名取值
    if args.ids:
        rows = conn.execute(
            f"SELECT * FROM samples WHERE id IN ({','.join('?' * len(args.ids))})",
            args.ids,
        ).fetchall()
        missing = set(args.ids) - {r["id"] for r in rows}
        if missing:
            print(f"skip（samples 表无记录）: {sorted(missing)}", file=sys.stderr)
    else:
        rows = conn.execute("SELECT * FROM samples ORDER BY id").fetchall()
    if not rows:
        print("samples 表为空，无音源可评分。", file=sys.stderr)

    for row in rows:
        try:
            score, notes = score_sample(dict(row))
        except Exception as e:  # 单条失败不阻断其余
            print(f"[{row['id']}] ERROR: {e}", file=sys.stderr)
            continue
        total = upsert_score(conn, score)
        _print_sample(score, total, notes)
    conn.commit()

    if args.pool:
        _print_pool(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
