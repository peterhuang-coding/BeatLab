"""BeatLab 模块 A/B：数据集整合与粗略分类（ingest）。

用法：
    .venv/bin/python pipeline/ingest.py <文件|目录|多个路径...> [--force]

流程：
1. 遍历输入路径，收集 SUPPORTED_EXT 文件（递归）；
2. md5 查重：已入库且 source.wav 存在则跳过（--force 重做）；
3. 统一转 44.1kHz WAV 存 library/<category>/<id>/source.wav
   （优先 ffmpeg CLI，缺省回退 librosa + soundfile），保留声道数；
4. 启发式分类：<3s one_shots / 3-20s loops / 20-60s fx / >60s songs；
   高零交叉率 + 人声频带(300-3400Hz)能量占优 + 包络 2-8Hz 调制 → speech；
5. 打标：BPM（仅 loops/songs，含置信）、调性（chroma 粗估，可为 None）、tags（文件名关键词）；
6. 写 meta.json、插入 samples 表；fingerprint 仅在 pyacoustid 可用时填写。

只 import common，接口经 SQLite 与文件系统交换（见 common.py 约定）。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    LIBRARY, STAGING, SUPPORTED_EXT, SampleEntry, clamp, get_db,
    load_audio_mono, md5_file, today_str,
)

# 文件名命中即视为 speech 的关键词（含中英）
SPEECH_KEYWORDS = (
    "speech", "访谈", "对白", "lecture", "名言", "podcast", "播客",
    "interview", "对话", "口播", "独白", "talk", "voice", "vocal",
)

NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
KRUMHANSL_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KRUMHANSL_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


# ---------- 转换 ----------
def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def convert_to_wav(src: Path, dst: Path) -> None:
    """统一转 44.1kHz WAV。优先 ffmpeg CLI，缺省回退 librosa + soundfile。"""
    ff = ffmpeg_path()
    if ff:
        subprocess.run(
            [ff, "-y", "-nostdin", "-loglevel", "error",
             "-i", str(src), "-ar", "44100", "-c:a", "pcm_s16le", str(dst)],
            check=True, capture_output=True,
        )
    else:
        import librosa
        y, _ = librosa.load(str(src), sr=44100, mono=False)
        sf.write(str(dst), y, 44100)


# ---------- 特征与分类 ----------
def _vocal_band_ratio(y: np.ndarray, sr: float) -> float:
    """300-3400Hz 能量占比（人声频带）。"""
    spec = np.abs(np.fft.rfft(y))
    freqs = np.fft.rfftfreq(len(y), 1.0 / sr)
    band = (freqs >= 300.0) & (freqs <= 3400.0)
    return float((spec[band] ** 2).sum() / max(float((spec ** 2).sum()), 1e-12))


def _envelope_mod_ratio(y: np.ndarray, sr: float) -> float:
    """包络在 2-8Hz（音节速率）的能量占比，衡量人声调制。"""
    env = np.abs(y)
    win = max(int(0.05 * sr), 1)
    env = np.convolve(env, np.ones(win) / win, mode="same")
    step = max(int(sr / 100), 1)
    env = env[::step]
    env = env - env.mean()
    spec = np.abs(np.fft.rfft(env))
    freqs = np.fft.rfftfreq(len(env), 1.0 / 100.0)
    mod = spec[(freqs >= 2.0) & (freqs <= 8.0)].sum()
    total = spec[freqs <= 30.0].sum()
    return float(mod / max(total, 1e-12))


def _lowband_ratio(y: np.ndarray, sr: float) -> float:
    """60-300Hz 能量占比：对话低频持续能量显著低于带贝斯/鼓的音乐（风琴、乐队曲目）。"""
    spec = np.abs(np.fft.rfft(y)) ** 2
    freqs = np.fft.rfftfreq(len(y), 1.0 / sr)
    total = float(spec.sum()) + 1e-12
    return float(spec[(freqs >= 60) & (freqs <= 300)].sum() / total)


def detect_speech(y: np.ndarray, sr: float) -> bool:
    """粗略 speech 判定：高零交叉率 + 人声频带能量占优 + 音节级调制 + 低频占比低。"""
    if len(y) < int(sr):  # 短于 1s 不给结论
        return False
    zcr = float(np.mean(np.abs(np.diff(np.signbit(y)))))
    return (
        zcr >= 0.08
        and _vocal_band_ratio(y, sr) >= 0.4
        and _envelope_mod_ratio(y, sr) >= 0.2
        and _lowband_ratio(y, sr) < 0.35
    )


def classify_sample(duration_s: float, y: np.ndarray, sr: float, name: str) -> str:
    """按时长分桶；speech 特征或文件名关键词优先。"""
    lower = name.lower()
    if any(k in lower for k in SPEECH_KEYWORDS) or detect_speech(y, sr):
        return "speech"
    if duration_s < 3:
        return "one_shots"
    if duration_s < 20:
        return "loops"
    if duration_s < 60:
        return "fx"
    return "songs"


def estimate_bpm(y: np.ndarray, sr: float) -> tuple[float | None, float | None]:
    """librosa.beat 估 BPM；置信由拍间隔稳定性（变异系数）给出。"""
    import librosa
    try:
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
        bpm = float(np.asarray(tempo).reshape(-1)[0])
        beats = np.asarray(beats).reshape(-1)
        if bpm <= 0 or len(beats) < 3:  # 无拍点（如纯正弦）视为估不出来
            return None, None
        ibi = np.diff(beats)
        cv = float(ibi.std() / max(ibi.mean(), 1e-9))
        conf = clamp(1.0 - cv / 0.4, 0.05, 0.95)
        return bpm, conf
    except Exception:
        return None, None


def estimate_key(y: np.ndarray, sr: float) -> tuple[str | None, float | None]:
    """chroma 均值对 Krumhansl 大小调轮廓做相关性粗估，失败返回 (None, None)。"""
    import librosa
    try:
        chroma = librosa.feature.chroma_stft(y=y, sr=sr).mean(axis=1)
        corrs: list[tuple[float, str]] = []
        for i in range(12):
            for profile, suffix in ((KRUMHANSL_MAJOR, ""), (KRUMHANSL_MINOR, "m")):
                corr = float(np.corrcoef(chroma, np.roll(profile, i))[0, 1])
                corrs.append((corr, f"{NOTES[i]}{suffix}"))
        vals = np.array([c for c, _ in corrs])
        best, key = max(corrs)
        conf = clamp(0.5 + 0.5 * (best - vals.mean()) / max(vals.std(), 1e-6), 0.0, 1.0)
        return key, conf
    except Exception:
        return None, None


def try_fingerprint(path: Path) -> str:
    """pyacoustid/chromaprint 可用才填，否则空字符串（不阻塞）。"""
    try:
        import acoustid
        result = acoustid.fingerprint_file(str(path))
        return str(result[1] if isinstance(result, tuple) else result)
    except Exception:
        return ""


def extract_tags(name: str, category: str) -> list[str]:
    """文件名关键词拆分（去重、小写、长度>=2），末尾附类别。"""
    tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9一-鿿]+", name) if len(t) >= 2]
    seen: set[str] = set()
    tags: list[str] = []
    for t in tokens + [category]:
        if t not in seen:
            seen.add(t)
            tags.append(t)
    return tags[:20]


# ---------- 单文件摄入 ----------
def ingest_one(path: Path, conn: sqlite3.Connection, force: bool) -> tuple[str, str]:
    """摄入单个文件。返回 (状态, 说明)；状态 ∈ {"ok", "skip", "fail"}。"""
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXT:
        return "fail", f"不支持扩展名 {ext}"
    try:
        digest = md5_file(path)
    except OSError as e:
        return "fail", f"读文件失败: {e}"
    sample_id = digest[:16]

    row = conn.execute("SELECT library_path FROM samples WHERE md5 = ?", (digest,)).fetchone()
    if row and not force and Path(row[0]).exists():
        return "skip", f"已入库 {sample_id}（--force 重做）"

    STAGING.mkdir(parents=True, exist_ok=True)
    tmp = STAGING / f"{sample_id}.wav"
    try:
        # 1) 统一转 44.1kHz WAV
        convert_to_wav(path, tmp)
        info = sf.info(str(tmp))
        duration = float(info.frames) / float(info.samplerate)

        # 2) 特征与分类（分析统一用 common.load_audio_mono：22.05k 单声道）
        y, sr = load_audio_mono(tmp)
        category = classify_sample(duration, y, sr, path.name)

        bpm = bpm_conf = None
        if duration >= 15:  # BPM 不受分类门控：误分类的"speech"也有拍；置信度兜底
            bpm, bpm_conf = estimate_bpm(y, sr)
        key_note, key_conf = estimate_key(y, sr)

        # 3) 落盘 + 写 meta.json + 插入 samples
        dest = LIBRARY / category / sample_id
        dest.mkdir(parents=True, exist_ok=True)
        final = dest / "source.wav"
        shutil.move(str(tmp), str(final))

        entry = SampleEntry(
            id=sample_id, category=category, orig_path=str(path.resolve()),
            library_path=str(final), duration_s=round(duration, 3),
            bpm=round(bpm, 2) if bpm is not None else None,
            bpm_conf=round(bpm_conf, 3) if bpm_conf is not None else None,
            key_note=key_note, key_conf=round(key_conf, 3) if key_conf is not None else None,
            md5=digest, fingerprint=try_fingerprint(final),
            tags=extract_tags(path.stem, category), ingested_at=today_str(),
        )
        (dest / "meta.json").write_text(
            json.dumps(asdict(entry), ensure_ascii=False, indent=2), encoding="utf-8")

        conn.execute(
            """INSERT OR REPLACE INTO samples
               (id, category, orig_path, library_path, duration_s, bpm, bpm_conf,
                key_note, key_conf, md5, fingerprint, tags, ingested_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (entry.id, entry.category, entry.orig_path, entry.library_path,
             entry.duration_s, entry.bpm, entry.bpm_conf, entry.key_note,
             entry.key_conf, entry.md5, entry.fingerprint,
             json.dumps(entry.tags, ensure_ascii=False), entry.ingested_at),
        )
        conn.commit()
        return "ok", f"{category}/{sample_id} {duration:.1f}s bpm={entry.bpm}"
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return "fail", f"处理失败: {e}"


def collect_inputs(paths: list[str]) -> list[Path]:
    """展开目录（递归），只保留 SUPPORTED_EXT；按输入顺序去重。"""
    out: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files = sorted(f for f in p.rglob("*")
                           if f.is_file() and f.suffix.lower() in SUPPORTED_EXT)
        elif p.is_file():
            files = [p]
        else:
            print(f"[忽略] 路径不存在: {p}")
            continue
        for f in files:
            key = str(f.resolve())
            if key not in seen:
                seen.add(key)
                out.append(f)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="BeatLab ingest：数据集整合与粗略分类（模块 A/B）")
    parser.add_argument("paths", nargs="+", help="文件或目录（可多个）")
    parser.add_argument("--force", action="store_true", help="已入库也重做")
    args = parser.parse_args()

    files = collect_inputs(args.paths)
    if not files:
        print("没有可摄入的文件（SUPPORTED_EXT）")
        return 1

    conn = get_db()
    ok = skip = fail = 0
    for f in files:
        status, msg = ingest_one(f, conn, args.force)
        if status == "ok":
            ok += 1
            print(f"[成功] {f} → {msg}")
        elif status == "skip":
            skip += 1
            print(f"[跳过] {f} → {msg}")
        else:
            fail += 1
            print(f"[失败] {f} → {msg}")
    print(f"\n汇总: 成功 {ok} / 跳过 {skip} / 失败 {fail}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
