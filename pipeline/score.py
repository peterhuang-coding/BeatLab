"""BeatLab 模块 C2：三层评分（asset quality / moment ranking / recipe prior）。

用法：
  .venv/bin/python pipeline/score.py --assets [asset_id ...]   # 层1 资产质量：9 项 rubric + 三闸门
  .venv/bin/python pipeline/score.py --moments [--top N]       # 层2 Moment 排序：8 维加权 + 类型多样
  .venv/bin/python pipeline/score.py --recipe-prior            # 层3 Recipe 先验：feedback → loop/chop/stem
  （兼容旧用法：--all ≡ --assets 全库；--pool ≡ --assets 后输出 passed 清单；裸 id 列表 ≡ --assets id...）

层 1 asset_quality：原 9 项 rubric 逻辑保留（三闸门、任一为 0 总分减半、SCORE_PASS）。
  写回策略：优先 common.upsert_asset_quality（数据层 helper，把 9 项写回 assets 层）；
  否则回退旧 scores 表（兼容旧表）；两不可则仅打印不落库。
层 2 moment_ranking：读 moments 表（common.get_moments），按 8 维默认权重加权
  （loopability .25 / memorability .2 / key_stability .15 / drum_state .1 / vocal_state .1
   / timbre_uniqueness .1 / structure_position .05 / space .05），
  diversity 约束（Top-N 内每类型 ≤2）输出稳定排序（同分按 asset_id/type/start_sec 确定性排序）。
层 3 recipe_prior：读 feedback 表（若有行）对 loop/chop/stem 默认先验
  (0.34/0.33/0.33) 做来源级升权/降权（kept ×(1+0.15n)，rejected ×(1-0.25n)，clamp [0.05,0.9]）；
  无反馈行时输出默认值。

数据层契约（Dev-1 冻结）：common.get_assets / common.get_moments /
common.upsert_asset_quality / common.get_feedback。helper 缺失时 moment 层给出清晰报错；
asset_quality 与 recipe_prior 在 helper 缺失时回退旧表（兼容旧表）。
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

# ---------- 层1 特征计算（原 9 项 rubric，逻辑保留） ----------

# 各特征映射参数（0-1 归一化阈值，调研/听感标定）
KEYWORDS_PRIOR = {
    "78rpm": 0.5, "breakbeat": 0.5, "vinyl": 0.4, "78": 0.4,
    "funk": 0.3, "soul": 0.3, "jazz": 0.3, "disco": 0.3, "boogie": 0.3,
    "motown": 0.3, "stax": 0.3, "r&b": 0.3, "rnb": 0.3, "hip-hop": 0.2,
    "hiphop": 0.2, "rare": 0.2, "old": 0.2, "vintage": 0.2, "tape": 0.2,
    "groove": 0.2, "latin": 0.2, "afro": 0.2, "fusion": 0.2, "break": 0.3,
    "drum": 0.15, "percussion": 0.15, "kit": 0.15, "loop": 0.1,
}

# ---------- 层2 常量 ----------
DEFAULT_MOMENT_WEIGHTS = {
    "loopability": 0.25, "memorability": 0.20, "key_stability": 0.15,
    "drum_state": 0.10, "vocal_state": 0.10, "timbre_uniqueness": 0.10,
    "structure_position": 0.05, "space": 0.05,
}
MOMENT_TOP_N_MAX_PER_TYPE = 2   # diversity 约束：Top-N 内每类型上限

# ---------- 层3 常量 ----------
DEFAULT_RECIPE_PRIORS = {"loop": 0.34, "chop": 0.33, "stem": 0.33}
KEEP_VERDICTS = {"kept", "keep", "liked", "retained"}
REJECT_VERDICTS = {"rejected", "reject", "disliked", "regenerate"}
PRIOR_KEEP_FACTOR = 0.15
PRIOR_REJECT_FACTOR = 0.25


def _row_get(row: Any, key: str, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _row_lib_path(row: Any) -> str:
    """library 路径：优先行内 library_path；否则按 library/<category>/<id>/source.wav 约定。"""
    lp = _row_get(row, "library_path", None)
    if lp:
        return str(lp)
    category = _row_get(row, "category", "unknown")
    return str(common.ROOT / "library" / str(category) / str(row["id"]) / "source.wav")


def _table_exists(conn, name: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


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


# ---------- 层1 单条评分 ----------

def score_sample(sample: dict[str, Any]) -> tuple[common.Score, list[str]]:
    """对一条 samples/assets 记录计算 9 项特征。返回 (Score, fallback 备注列表)。"""
    lib_path = _row_lib_path(sample)
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
        raw_tags = _row_get(sample, "tags", "[]")
        tags = raw_tags if isinstance(raw_tags, list) else json.loads(raw_tags or "[]")
    except (json.JSONDecodeError, TypeError):
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
        source_prior=_source_prior(tags, _row_get(sample, "orig_path", None)),
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
    """旧 scores 表落库（兼容旧表），返回 total。"""
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


def _persist_asset_quality(conn, score: common.Score) -> float:
    """层1 写回策略：优先 common.upsert_asset_quality（数据层 helper）；
    否则回退旧 scores 表（兼容旧表）；两不可则仅打印不落库。"""
    total = score.total()
    upsert_asset_quality = getattr(common, "upsert_asset_quality", None)
    if upsert_asset_quality is not None:
        passed = 1 if total >= common.SCORE_PASS else 0
        upsert_asset_quality(conn, score.sample_id, {**vars(score)}, total, passed)
        return total
    if _table_exists(conn, "scores"):
        return upsert_score(conn, score)
    print(f"[score] {score.sample_id}: 无 common.upsert_asset_quality 且无旧 scores 表，仅打印不落库")
    return total


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
    if not _table_exists(conn, "scores"):
        print("[score] 无旧 scores 表，passed 清单不可用")
        return
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


# ---------- 层2 moment ranking ----------

def load_moments() -> list[dict]:
    """读 moments 表（经 common.get_moments）。helper 缺失时清晰报错。"""
    get_moments = getattr(common, "get_moments", None)
    if get_moments is None:
        raise RuntimeError(
            "[score] common.get_moments 缺失：需要数据层（Dev-1）合入后的 common.py。"
            "当前环境无法访问 moments 表；联调前自测请 monkeypatch common.get_moments。")
    return [dict(m) if not isinstance(m, dict) else m for m in get_moments(common.get_db())]


def _moment_total(scores: dict) -> float:
    return round(sum(scores.get(k, 0.0) * w for k, w in DEFAULT_MOMENT_WEIGHTS.items()), 4)


def rank_moments(moments: list[dict], top_n: int | None = None) -> list[dict]:
    """8 维加权排序 + diversity 约束（Top-N 内每类型 ≤2）。

    排序确定性：总分降序，同分按 (asset_id, type, start_sec) 升序。
    返回带 "_total" 的排序列表（top_n=None 时返回全部，不做类型截断）。
    """
    rows: list[dict] = []
    for m in moments:
        raw = m.get("scores_json") or m.get("scores")
        try:
            scores = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            scores = {}
        rows.append({**m, "_total": _moment_total(scores)})
    rows.sort(key=lambda r: (-r["_total"], str(r.get("asset_id", "")),
                             str(r.get("type", "")), float(r.get("start_sec") or 0.0)))
    if top_n is None:
        return rows
    out: list[dict] = []
    type_counts: dict[str, int] = {}
    for r in rows:
        t = str(r.get("type", ""))
        if type_counts.get(t, 0) >= MOMENT_TOP_N_MAX_PER_TYPE:
            continue
        out.append(r)
        type_counts[t] = type_counts.get(t, 0) + 1
        if len(out) >= top_n:
            break
    return out


def _print_ranking(rows: list[dict], top_n: int) -> None:
    types = {str(r.get("type")) for r in rows}
    print(f"MOMENT RANKING (top {len(rows)} / 请求 {top_n}, 类型数={len(types)}):")
    for i, r in enumerate(rows, 1):
        try:
            raw = r.get("scores_json") or r.get("scores")
            scores = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            scores = {}
        top_dims = " ".join(
            f"{k}={scores.get(k, 0):.2f}" for k in ("loopability", "memorability", "key_stability"))
        print(f"  {i:2d}. {r['_total']:.3f} {r.get('asset_id', '?')} {r.get('type', '?'):16s} "
              f"{r.get('start_sec', 0):6.2f}-{r.get('end_sec', 0):6.2f}s  {top_dims}")
    if len(types) < 3 and len(rows) >= 3:
        print(f"  [提示] Top-{len(rows)} 仅 {len(types)} 种类型，素材库类型覆盖不足")


# ---------- 层3 recipe prior ----------

def load_feedback() -> list[dict]:
    """读 feedback 表。优先 common.get_feedback；否则 get_db + 表存在性守卫（兼容旧表）。"""
    get_feedback = getattr(common, "get_feedback", None)
    if get_feedback is not None:
        return [dict(f) if not isinstance(f, dict) else f for f in get_feedback(common.get_db())]
    conn = common.get_db()
    if not _table_exists(conn, "feedback"):
        return []
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute("SELECT * FROM feedback").fetchall()]


def recipe_priors(feedback: list[dict]) -> dict:
    """来源级先验：kept 升权 ×(1+0.15n)，rejected 降权 ×(1-0.25n)，clamp [0.05, 0.9]。

    无反馈行 → 输出默认先验 (0.34/0.33/0.33)。反馈行假定字段：
    source / recipe_type(loop|chop|stem) / verdict(kept|rejected 等)。
    """
    if not feedback:
        return {"default": dict(DEFAULT_RECIPE_PRIORS), "per_source": {}, "feedback_n": 0}
    per_source: dict[str, dict] = {}
    n_used = 0
    for fb in feedback:
        source = str(fb.get("source") or "unknown")
        rt = str(fb.get("recipe_type") or "").lower()
        verdict = str(fb.get("verdict") or "").lower()
        if rt not in DEFAULT_RECIPE_PRIORS:
            continue
        n_used += 1
        s = per_source.setdefault(source, {"kept": {}, "rejected": {}})
        if verdict in KEEP_VERDICTS:
            s["kept"][rt] = s["kept"].get(rt, 0) + 1
        elif verdict in REJECT_VERDICTS:
            s["rejected"][rt] = s["rejected"].get(rt, 0) + 1
    adjusted: dict[str, dict] = {}
    for source, s in per_source.items():
        priors: dict[str, float] = {}
        for rt, p in DEFAULT_RECIPE_PRIORS.items():
            k = s["kept"].get(rt, 0)
            r = s["rejected"].get(rt, 0)
            factor = (1 + PRIOR_KEEP_FACTOR * k) * (1 - PRIOR_REJECT_FACTOR * r)
            priors[rt] = round(common.clamp(p * factor, 0.05, 0.9), 4)
        adjusted[source] = priors
    return {"default": dict(DEFAULT_RECIPE_PRIORS), "per_source": adjusted, "feedback_n": n_used}


def _print_priors(result: dict) -> None:
    d = result["default"]
    print(f"RECIPE PRIOR (feedback 行数={result['feedback_n']}):")
    print(f"  默认先验  loop={d['loop']}  chop={d['chop']}  stem={d['stem']}")
    for source, priors in sorted(result["per_source"].items()):
        print(f"  来源 {source}:  loop={priors['loop']}  chop={priors['chop']}  stem={priors['stem']}")
    if not result["per_source"]:
        print("  无 feedback 行 → 使用默认先验")


# ---------- CLI ----------

def _run_assets(ids: list[str], pool_flag: bool) -> int:
    """层1 资产质量：评分全部/指定 assets（helper 优先，旧 samples 表兜底）。"""
    get_assets = getattr(common, "get_assets", None)
    conn = common.get_db()
    conn.row_factory = sqlite3.Row
    if get_assets is not None:
        rows = get_assets(conn)
        if ids:
            rows = [r for r in rows if str(r["id"]) in ids]
        if ids:
            found = {str(r["id"]) for r in rows}
            for sid in ids:
                if sid not in found:
                    print(f"skip（assets 表无记录）: {sid}", file=sys.stderr)
    else:
        if ids:
            rows = conn.execute(
                f"SELECT * FROM samples WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            ).fetchall()
            missing = set(ids) - {r["id"] for r in rows}
            if missing:
                print(f"skip（samples 表无记录）: {sorted(missing)}", file=sys.stderr)
        else:
            rows = conn.execute("SELECT * FROM samples ORDER BY id").fetchall()
    if not rows:
        print("assets/samples 表为空，无音源可评分。", file=sys.stderr)
        return 0

    for row in rows:
        sample = dict(row)
        try:
            score, notes = score_sample(sample)
        except Exception as e:  # 单条失败不阻断其余
            print(f"[{sample['id']}] ERROR: {e}", file=sys.stderr)
            continue
        total = _persist_asset_quality(conn, score)
        _print_sample(score, total, notes)
    conn.commit()

    if pool_flag:
        _print_pool(conn)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="score.py",
        description="BeatLab 模块 C2：三层评分（asset quality / moment ranking / recipe prior）",
    )
    parser.add_argument("--assets", action="store_true", help="层1 资产质量：9 项 rubric + 三闸门")
    parser.add_argument("--moments", action="store_true", help="层2 Moment 排序：8 维加权 + 类型多样")
    parser.add_argument("--recipe-prior", action="store_true", help="层3 Recipe 先验：feedback → loop/chop/stem")
    parser.add_argument("--top", type=int, default=5, help="--moments 输出 Top-N（默认 5）")
    parser.add_argument("--all", action="store_true", help="兼容旧用法：等价 --assets（全库）")
    parser.add_argument("--pool", action="store_true", help="兼容旧用法：--assets 后输出 passed 清单")
    parser.add_argument("ids", nargs="*", help="--assets 时可指定 asset/sample id")
    args = parser.parse_args(argv)

    if args.moments:
        try:
            moments = load_moments()
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 2
        if not moments:
            print("moments 表为空：先运行 pipeline/moments.py 生成 Sample Moments。")
            return 0
        ranked = rank_moments(moments, top_n=max(1, args.top))
        _print_ranking(ranked, max(1, args.top))
        return 0

    if args.recipe_prior:
        _print_priors(recipe_priors(load_feedback()))
        return 0

    if args.assets or args.all or args.pool or args.ids:
        return _run_assets(args.ids, args.pool)

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
