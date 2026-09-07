"""BeatLab 模块 D：拆轨（audio-separator，stem cache）+ 切片 + 人声 phrase 候选。

用法：
    .venv/bin/python pipeline/separate.py [sample_id ... | --all | --pool]
                                          [--skip-if-done] [--window start:end]

对每个样本（目录 library/<category>/<id>/）：
1. 拆轨（stem cache 化）：先经 common.get_assets 查 assets.stems_ready，
   已拆且 stems/{drums,vocal,bass,other}.wav 齐全 → 跳过拆轨；否则
   audio-separator + Demucs v4 htdemucs_ft（4 stems）拆轨成功后经
   common.set_stems_ready 标记。helper 缺失（数据层未合入）时打印提示、
   按旧行为直接拆轨，不 crash。
2. 切片：stems/other.wav（无 other 则 source.wav）onset 检测
   → 合并过近 onset → 零点吸附 → 首尾线性 fade → 取 onset_strength 最高的 ≤16 片
   → slices/chop_XX.wav（XX=01..16，pad=XX，midi_note=MIDI_CHOP_BASE+pad-1）
3. 人声：vocal stem 上按 1-2 小节（sample.bpm 算 bar 时长）能量窗口挑 ≤4 段候选
   → vocal_phrases/phrase_XX.wav（XX=01..04，start/end/gain_hint 记入 slice_map.json）
4. 产物：slice_map.json = {"chops": [...], "vocal_phrases": [...], "stems": {...},
   "segments": {...}}。slice_map.json / meta.json 规则：只增键不改键（保留全部旧键）。
   --window start:end 时按时间范围裁剪处理（P0 入口），并新增 "window" 键。

模型下载网络不稳：重试 MODEL_RETRIES 次（间隔递增），仍失败则优雅降级 ——
跳过拆轨、用 source.wav 切片，slice_map.json 的 stems 字段标记失败原因，不 crash。

数据层契约（Dev-1 冻结）：common.get_assets / common.set_stems_ready。
helper 缺失时本模块按旧行为降级（samples 表 SQL 选择 + 不查/不写 cache）。
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import asdict
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

import common

# ---------- 常量 ----------
MODELS = common.ROOT / ".models"        # 模型权重目录（audio-separator model_file_dir）
SEPARATION_MODEL = "htdemucs_ft.yaml"   # audio-separator 文档名（Demucs v4, 4-stem, 优先 htdemucs_ft）
STEM_NAMES = ("drums", "vocal", "bass", "other")
MODEL_RETRIES = 3                       # 模型下载/加载重试次数
MODEL_RETRY_BASE_SLEEP = 5.0            # 重试基础间隔（间隔递增：5s / 10s）

SR = 44100                              # 拆轨/切片/人声统一采样率
MIN_CHOP_GAP_S = 0.20                   # 合并间隔小于该值的相邻 onset
MAX_CHOP_S = 2.0                        # 单片最长（onset 间距超过则截断）
MIN_SEG_S = 0.05                        # 单片最短（不足则跳过）
MAX_CHOPS = 16                          # 最多切片数（取 onset_strength 最高者）
ZERO_SNAP_MS = 5.0                      # 切片边界零点吸附窗口 ±5ms
CHOP_FADE_S = 0.003                     # 切片首尾线性 fade（2-5ms 内取 3ms）

MAX_PHRASES = 4                         # 人声 phrase 候选数上限
PHRASE_BARS = 2.0                       # 能量窗口 2 小节（bpm 快时窗口自然变短，覆盖 1-2 小节）
PHRASE_MAX_S = 6.0                      # 能量窗口时长上限
PHRASE_MIN_S = 0.5                      # 声轨短于该值则不切 phrase
PHRASE_MIN_RMS_DB = -45.0               # 能量窗口 RMS 低于该值视为静音，跳过
PHRASE_FADE_S = 0.005                   # phrase 首尾 fade（防爆音）
PHRASE_TARGET_PEAK_DB = -6.0            # gain_hint 的峰值归一目标
DEFAULT_BPM = 120.0                     # samples.bpm 缺失时的兜底

# ---------- 通用小工具 ----------
_SEPARATOR = None  # audio-separator Separator 单例（模型只加载一次，跨样本复用）


def _get_separator():
    """返回单例 Separator；首次调用会初始化设备并创建模型目录。"""
    global _SEPARATOR
    if _SEPARATOR is None:
        from audio_separator.separator import Separator
        MODELS.mkdir(parents=True, exist_ok=True)
        _SEPARATOR = Separator(
            log_level=logging.INFO,       # 保留下载进度等关键日志
            model_file_dir=str(MODELS),
            output_dir=str(ROOT),         # 每次分离前会重定向到样本的 raw 目录
            output_format="WAV",
            sample_rate=SR,
        )
    return _SEPARATOR


def _clean_partial_model_files() -> None:
    """下载失败时清理可能残留的半截模型文件，避免下次重试被误判为已下载。

    audio-separator 的 download_file_if_not_exists 直接写最终路径且"存在即跳过"，
    半截文件会让重试永远失败，因此失败后必须删除。
    """
    for pattern in ("*.th", "*.yaml", "download_checks.json"):
        for p in MODELS.glob(pattern):
            try:
                p.unlink()
            except OSError:
                pass


def _snap_zero(y: np.ndarray, idx: int, sr: int) -> int:
    """在 idx 附近 ±ZERO_SNAP_MS 内找最接近的零交叉样本；无交叉则取幅值最小点。"""
    win = max(1, int(sr * ZERO_SNAP_MS / 1000.0))
    lo, hi = max(0, idx - win), min(len(y) - 1, idx + win)
    seg = y[lo:hi + 1]
    cross = np.nonzero(np.diff(np.signbit(seg)))[0]
    if cross.size:
        best = None
        for k in cross:
            p = lo + int(k)
            # 过零发生在 p 与 p+1 之间，取两者中幅值更小的端点
            cand = p if abs(seg[int(k)]) <= abs(seg[int(k) + 1]) else p + 1
            if best is None or abs(cand - idx) < abs(best - idx):
                best = cand
        return best
    return lo + int(np.argmin(np.abs(seg)))


def _fade_edges(seg: np.ndarray, sr: int, fade_s: float) -> np.ndarray:
    """首尾线性 fade，避免切片边界爆音。"""
    if len(seg) < 2:
        return seg
    n = max(1, min(int(sr * fade_s), len(seg) // 2))
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    seg[:n] *= ramp
    seg[-n:] *= ramp[::-1]
    return seg


# ---------- 拆轨 ----------
def _find_stem_file(raw_dir: Path, stem: str) -> Path | None:
    """在 audio-separator 输出目录里按 stem 关键词找文件（命名规则不同也能对齐）。"""
    keywords = {
        "drums": ("drums", "drum"),
        "vocal": ("vocals", "vocal"),
        "bass": ("bass",),
        "other": ("other",),
    }[stem]
    for p in sorted(raw_dir.glob("*.wav")):
        lower = p.name.lower()
        if any(k in lower for k in keywords):
            return p
    return None


def _separate_stems(src: Path, stems_dir: Path) -> dict[str, Path]:
    """用 audio-separator 分离 4 stems 到 stems_dir。

    成功返回 {stem 名: 文件路径}；网络/模型问题导致失败时打印提示并返回 {}（优雅降级）。
    """
    try:
        separator = _get_separator()
    except Exception as exc:
        print(f"[separate] audio-separator 不可用（{exc}），跳过拆轨，切片将用 source.wav")
        return {}

    stems_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = stems_dir.parent / ".stems_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # 输出定向到 raw 目录，分离完成后改名对齐到 stems/{drums,vocal,bass,other}.wav
    separator.output_dir = str(raw_dir)
    if separator.model_instance is not None:
        separator.model_instance.output_dir = str(raw_dir)

    for attempt in range(1, MODEL_RETRIES + 1):
        try:
            separator.load_model(SEPARATION_MODEL)
            break
        except Exception as exc:
            print(f"[separate] 模型 {SEPARATION_MODEL} 加载第 {attempt}/{MODEL_RETRIES} 次失败：{exc}")
            _clean_partial_model_files()
            if attempt < MODEL_RETRIES:
                sleep = MODEL_RETRY_BASE_SLEEP * attempt
                print(f"[separate] {sleep:.0f}s 后重试（间隔递增）...")
                time.sleep(sleep)
    else:
        print(f"[separate] 模型加载失败（已重试 {MODEL_RETRIES} 次），降级：跳过拆轨，切片用 source.wav")
        shutil.rmtree(raw_dir, ignore_errors=True)
        return {}

    try:
        custom = {"Vocals": "vocal", "Drums": "drums", "Bass": "bass", "Other": "other"}
        separator.separate(str(src), custom_output_names=custom)
    except Exception as exc:
        print(f"[separate] 分离失败（{exc}），降级：跳过拆轨，切片用 source.wav")
        shutil.rmtree(raw_dir, ignore_errors=True)
        return {}

    found: dict[str, Path] = {}
    for name in STEM_NAMES:
        match = _find_stem_file(raw_dir, name)
        if match is not None:
            target = stems_dir / f"{name}.wav"
            shutil.move(str(match), str(target))
            found[name] = target
    shutil.rmtree(raw_dir, ignore_errors=True)

    missing = [n for n in STEM_NAMES if n not in found]
    if missing:
        print(f"[separate] 分离产物缺失 stems：{missing}")
    return found


# ---------- 切片 ----------
def _merge_onsets(times: np.ndarray, strengths: np.ndarray, min_gap: float) -> tuple[np.ndarray, np.ndarray]:
    """合并间隔 < min_gap 的相邻 onset，保留强度更高者。"""
    if len(times) == 0:
        return times, strengths
    order = np.argsort(times)
    t, s = times[order], strengths[order]
    out_t, out_s = [t[0]], [s[0]]
    for k in range(1, len(t)):
        if t[k] - out_t[-1] < min_gap:
            if s[k] > out_s[-1]:
                out_t[-1], out_s[-1] = t[k], s[k]
        else:
            out_t.append(t[k])
            out_s.append(s[k])
    return np.asarray(out_t), np.asarray(out_s)


def _detect_chop_points(y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """onset_strength + onset_detect(backtrack) → 合并过近 onset → 返回 (时间, 强度)。"""
    oenv = librosa.onset.onset_strength(y=y, sr=sr)
    frames = librosa.onset.onset_detect(onset_envelope=oenv, sr=sr, backtrack=True)
    if len(frames) == 0:
        return np.asarray([], dtype=np.float64), np.asarray([], dtype=np.float64)
    times = librosa.frames_to_time(frames, sr=sr)
    strengths = oenv[frames]
    return _merge_onsets(times, strengths, MIN_CHOP_GAP_S)


def _build_segments(onset_t: np.ndarray, dur: float) -> list[tuple[float, float]]:
    """每片 [t_i, min(t_{i+1}, t_i + MAX_CHOP_S)]，末片封顶 MAX_CHOP_S 与文件时长。"""
    segs = []
    for i, t in enumerate(onset_t):
        nxt = onset_t[i + 1] if i + 1 < len(onset_t) else t + MAX_CHOP_S
        end = min(float(nxt), float(t) + MAX_CHOP_S, dur)
        segs.append((float(t), max(end, float(t) + MIN_SEG_S)))
    return segs


def _make_chops(y: np.ndarray, sr: int, stem: str, slices_dir: Path) -> list[Chop]:
    """切片并写 slices/chop_XX.wav，返回 Chop 列表（按时间升序，pad=1..16）。"""
    onset_t, strengths = _detect_chop_points(y, sr)
    if len(onset_t) == 0:
        # 极端情况（无 onset）：兜底出一个全曲起始切片，保证 ≥1 片
        onset_t = np.asarray([0.0])
        strengths = np.asarray([0.0])
    dur = len(y) / sr
    segs = _build_segments(onset_t, dur)

    max_strength = float(strengths.max()) if len(strengths) else 1.0
    order = sorted(np.argsort(-strengths)[:MAX_CHOPS].tolist(), key=lambda i: float(onset_t[i]))
    chops: list[Chop] = []
    pad = 0
    for i in order:
        start, end = segs[i]
        s0 = _snap_zero(y, int(round(start * sr)), sr)
        s1 = _snap_zero(y, int(round(end * sr)), sr)
        if s1 - s0 < int(MIN_SEG_S * sr):
            s1 = min(len(y), s0 + int(MIN_SEG_S * sr))
        seg = y[s0:s1].copy()
        seg = _fade_edges(seg, sr, CHOP_FADE_S)
        pad += 1
        sf.write(str(slices_dir / f"chop_{pad:02d}.wav"), seg, sr)
        chops.append(Chop(
            stem=stem,
            file=f"slices/chop_{pad:02d}.wav",
            start_sec=round(s0 / sr, 3),
            end_sec=round(s1 / sr, 3),
            pad=pad,
            midi_note=MIDI_CHOP_BASE + pad - 1,
            confidence=round(float(strengths[i]) / max_strength, 3) if max_strength > 0 else 0.0,
        ))
    return chops


# ---------- 人声 phrase ----------
def _phrase_windows(y: np.ndarray, sr: int, bpm: float) -> tuple[np.ndarray, float]:
    """按 bpm 算 1-2 小节能量窗口：返回 (窗口起点数组, 窗口秒数)。"""
    bpm = clamp(float(bpm), 40.0, 240.0)
    bar_s = 4.0 * 60.0 / bpm
    win_s = clamp(bar_s * PHRASE_BARS, 1.0, PHRASE_MAX_S)
    dur = len(y) / sr
    if dur < PHRASE_MIN_S:
        return np.asarray([]), win_s
    if dur < win_s:
        return np.asarray([0.0]), win_s
    return np.arange(0.0, dur - win_s + 1e-6, win_s / 2.0), win_s


def _make_vocal_phrases(y: np.ndarray, sr: int, bpm: float, phrases_dir: Path) -> list[dict]:
    """vocal stem 上按能量窗口挑 ≤4 段候选，写 phrase_XX.wav。

    返回 [{"file", "start_sec", "end_sec", "gain_hint"}]（file 相对 library/<id>/）。
    """
    starts, win_s = _phrase_windows(y, sr, bpm)
    if len(starts) == 0:
        return []
    hop = int(sr * win_s)
    rms_db = []
    for t in starts:
        seg = y[int(t * sr): int(t * sr) + hop]
        rms = float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0
        rms_db.append(20.0 * math.log10(rms + 1e-9))

    order = sorted(range(len(starts)), key=lambda i: -rms_db[i])[:MAX_PHRASES]
    order = sorted(order, key=lambda i: float(starts[i]))  # 候选按时间升序编号
    phrases: list[dict] = []
    n = 0
    for i in order:
        if rms_db[i] < PHRASE_MIN_RMS_DB:
            continue
        n += 1  # 编号只在实际写出的 phrase 上递增（静音窗口不占号）
        start, end = float(starts[i]), min(float(starts[i]) + win_s, len(y) / sr)
        seg = y[int(start * sr): int(end * sr)].copy()
        if len(seg) < 2:
            continue
        seg = _fade_edges(seg, sr, PHRASE_FADE_S)
        sf.write(str(phrases_dir / f"phrase_{n:02d}.wav"), seg, sr)
        peak = float(np.max(np.abs(seg))) if len(seg) else 0.0
        gain_hint = round(clamp(PHRASE_TARGET_PEAK_DB - 20.0 * math.log10(peak), -30.0, 30.0), 1) if peak > 1e-6 else 0.0
        phrases.append({
            "file": f"vocal_phrases/phrase_{n:02d}.wav",
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "gain_hint": gain_hint,
        })
    return phrases


# ---------- 单样本处理 ----------
def process_sample(row: sqlite3.Row, skip_if_done: bool) -> bool:
    """处理单个样本：拆轨 → 切片 → 人声 phrase → slice_map.json。返回是否成功。"""
    sid = row["id"]
    src = Path(row["library_path"])
    map_path = src.parent / "slice_map.json"
    if skip_if_done and map_path.exists():
        print(f"[separate] {sid}: slice_map.json 已存在，跳过（--skip-if-done）")
        return True
    if not src.exists():
        print(f"[separate] {sid}: source 不存在（{src}），跳过")
        return False
    print(f"[separate] {sid}: 开始处理（{src}）")

    sample_dir = src.parent
    stems_dir = sample_dir / "stems"
    slices_dir = sample_dir / "slices"
    phrases_dir = sample_dir / "vocal_phrases"
    for d in (stems_dir, slices_dir, phrases_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 1) 拆轨
    stems = _separate_stems(src, stems_dir)
    stems_status = {
        "ok": len(stems) == len(STEM_NAMES),
        "model": SEPARATION_MODEL,
        "missing": sorted(set(STEM_NAMES) - set(stems)),
        "error": None,
    }
    if not stems:
        stems_status["error"] = "拆轨失败或未生成 stems，已降级用 source.wav 切片"
    elif stems_status["missing"]:
        stems_status["error"] = f"部分 stems 缺失：{stems_status['missing']}"

    # 2) 切片（other 缺失则用 source.wav）
    chop_src = stems.get("other") or src
    chop_stem = "other" if "other" in stems else "source"
    print(f"[separate] {sid}: 切片源 = {chop_stem}")
    y, sr = load_audio_mono(chop_src, sr=SR)
    sr = int(round(sr))  # common.load_audio_mono 返回 float sr，soundfile 写文件需要 int
    chops = _make_chops(y, sr, chop_stem, slices_dir)

    # 3) 人声 phrase 候选（用 sample 的 bpm 算 bar 时长）
    phrases: list[dict] = []
    if "vocal" in stems:
        vy, vsr = load_audio_mono(stems["vocal"], sr=SR)
        vsr = int(round(vsr))
        bpm = float(row["bpm"] or DEFAULT_BPM)
        phrases = _make_vocal_phrases(vy, vsr, bpm, phrases_dir)
    else:
        print(f"[separate] {sid}: 无 vocal stem，跳过人声 phrase 候选")

    # 4) slice_map.json
    slice_map = {
        "chops": [asdict(c) for c in chops],
        "vocal_phrases": phrases,
        "stems": stems_status,
    }
    map_path.write_text(json.dumps(slice_map, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[separate] {sid}: 完成 — chops={len(chops)} vocal_phrases={len(phrases)} "
          f"stems_ok={stems_status['ok']} → {map_path}")
    # samples 表暂无 stems/slices 字段，不写库
    return True


# ---------- 样本选择 ----------
def _select_rows(conn: sqlite3.Connection, ids: list[str], all_flag: bool, pool_flag: bool) -> list[sqlite3.Row]:
    """按 CLI 参数选择样本行。--pool = 已通过评分闸门（scores.passed=1）的样本。"""
    if ids:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT * FROM samples WHERE id IN ({placeholders}) ORDER BY ingested_at, id", ids
        ).fetchall()
        found = {r["id"] for r in rows}
        for sid in ids:
            if sid not in found:
                print(f"[separate] {sid}: 不在 samples 表中，跳过")
        return rows
    if all_flag:
        return conn.execute("SELECT * FROM samples ORDER BY ingested_at, id").fetchall()
    return conn.execute(
        "SELECT s.* FROM samples s JOIN scores sc ON sc.sample_id = s.id "
        "WHERE sc.passed = 1 ORDER BY s.ingested_at, s.id"
    ).fetchall()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="separate.py",
        description="BeatLab 模块 D：拆轨 + 切片 + 人声 phrase 候选",
    )
    parser.add_argument("sample_ids", nargs="*", help="指定样本 id（可多个）")
    parser.add_argument("--all", action="store_true", help="处理 samples 表全部样本")
    parser.add_argument("--pool", action="store_true", help="处理已通过评分闸门的样本")
    parser.add_argument("--skip-if-done", action="store_true", help="已存在 slice_map.json 的样本跳过")
    args = parser.parse_args(argv)

    if not args.sample_ids and not args.all and not args.pool:
        parser.print_help()
        return 0

    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = _select_rows(conn, args.sample_ids, args.all, args.pool)
    if not rows:
        print("[separate] 没有可处理的样本")
        conn.close()
        return 0

    failed = 0
    for row in rows:
        try:
            if not process_sample(row, args.skip_if_done):
                failed += 1
        except Exception as exc:
            failed += 1
            print(f"[separate] {row['id']}: 处理异常，跳过：{exc}")
    conn.close()
    print(f"[separate] 完成：共 {len(rows)} 个样本，失败 {failed} 个")
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    # audio-separator/torch/onnxruntime 在解释器退出阶段偶发 native 崩溃
    # （libc++abi recursive_mutex lock failed），会让成功运行返回 134。
    # 冲刷输出后直接 os._exit，跳过线程销毁，保证退出码可信。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
