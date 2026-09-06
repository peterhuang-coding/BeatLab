"""BeatLab 流水线共享层：路径、SQLite schema、核心 dataclass 与常量。

约定：
- 每个模块一个子命令（pipeline.py 编排），可单独手动运行 —— 对应"每节点可手动介入"。
- 所有模块只 import common（+ 各自依赖），接口经 SQLite 与文件系统交换。
- 音源经 ingest 后统一为 library/<category>/<id>/source.wav（44.1k WAV，声道由各模块按需处理）。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import date
from pathlib import Path
from typing import Any

# ---------- 路径 ----------
ROOT = Path.home() / "Desktop" / "BeatLab"
PIPELINE = ROOT / "pipeline"
LIBRARY = ROOT / "library"
DB_PATH = ROOT / "db.sqlite"
STAGING = ROOT / "staging"          # 摄入时的临时目录
MIRROR_ROOT = Path.home() / "Desktop" / "AI_音乐_Demos"   # 产出镜像（日期扁平目录）
DEFAULT_BEAT_DURATION_S = 3.5 * 60  # 3-4 分钟目标取 3.5min 中值

SUPPORTED_EXT = {".wav", ".mp3", ".flac", ".aif", ".aiff", ".m4a", ".ogg"}
CATEGORIES = ("songs", "loops", "one_shots", "speech", "fx", "unknown")

# MIDI 音符映射（与 ai-beat-sketcher 历史约定一致）
MIDI_KICK = 36    # C1
MIDI_SNARE = 38   # D1
MIDI_HAT = 42     # F#1
MIDI_OH = 46      # A#1
MIDI_PERC = 39    # D#1
MIDI_BASS_MIN, MIDI_BASS_MAX = 24, 48
MIDI_CHOP_BASE = 48   # 切片铺 C3 起（pad B1-B16 → 48..63）

# 评分 rubric 权重（sampling-craft-research 调研定稿，CLAP 权重暂并入音色独特性）
RUBRIC_WEIGHTS = {
    "drums_presence": 20,    # 闸门1：鼓存在度 + onset 密度
    "vocal_free": 15,        # 闸门2：无 vocal（1 - vocal RMS 占比）
    "structure_hit": 15,     # 闸门3：结构位置命中 intro/break/outro
    "key_bpm_conf": 10,
    "loopability": 10,
    "timbre_uniqueness": 15,  # 含原 CLAP 语义 5 分
    "harmonicity": 5,
    "dynamics_space": 5,
    "source_prior": 5,
}
SCORE_PASS = 60
GATE_KEYS = ("drums_presence", "vocal_free", "structure_hit")

# ---------- DB ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id TEXT PRIMARY KEY,              -- md5 前 16 位
    category TEXT NOT NULL,
    orig_path TEXT,
    library_path TEXT,                -- library/<category>/<id>/source.wav
    duration_s REAL, bpm REAL, bpm_conf REAL,
    key_note TEXT, key_conf REAL,
    md5 TEXT, fingerprint TEXT,       -- chromaprint（去重兜底）
    tags TEXT,                        -- JSON list
    ingested_at TEXT
);
CREATE TABLE IF NOT EXISTS scores (
    sample_id TEXT PRIMARY KEY,
    drums_presence REAL, vocal_free REAL, structure_hit REAL,
    key_bpm_conf REAL, loopability REAL, timbre_uniqueness REAL,
    harmonicity REAL, dynamics_space REAL, source_prior REAL,
    total REAL, passed INTEGER, scored_at TEXT
);
CREATE TABLE IF NOT EXISTS beats (
    beat_id TEXT PRIMARY KEY,
    created_at TEXT, bpm REAL, style TEXT,
    duration_s REAL, sample_ids TEXT,  -- JSON list
    spec_path TEXT,                    -- BeatSpec JSON
    wav_path TEXT, als_path TEXT, midi_dir TEXT
);
"""


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def get_db() -> sqlite3.Connection:
    ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


# ---------- dataclass ----------
@dataclass
class SampleEntry:
    id: str
    category: str
    orig_path: str
    library_path: str
    duration_s: float | None = None
    bpm: float | None = None
    bpm_conf: float | None = None
    key_note: str | None = None
    key_conf: float | None = None
    md5: str | None = None
    fingerprint: str | None = None
    tags: list[str] = field(default_factory=list)
    ingested_at: str = ""

    def source_path(self) -> Path:
        return Path(self.library_path)


@dataclass
class Score:
    sample_id: str
    drums_presence: float = 0.0
    vocal_free: float = 0.0
    structure_hit: float = 0.0
    key_bpm_conf: float = 0.0
    loopability: float = 0.0
    timbre_uniqueness: float = 0.0
    harmonicity: float = 0.0
    dynamics_space: float = 0.0
    source_prior: float = 0.0

    def gates_passed(self) -> bool:
        return all(getattr(self, k) > 0 for k in GATE_KEYS)

    def total(self) -> float:
        raw = sum(getattr(self, k) * w for k, w in RUBRIC_WEIGHTS.items())
        return round(raw, 1) if self.gates_passed() else round(raw * 0.5, 1)


@dataclass
class Chop:
    """一个切片：来自哪条 stem、时间点、pad/MIDI 映射。"""
    stem: str                # "other" | "drums" | "vocal" | "bass" | "source"
    file: str                # 相对 library/<id>/ 的路径
    start_sec: float
    end_sec: float
    pad: int = 0             # 1..16
    midi_note: int = 0       # MIDI_CHOP_BASE + pad - 1
    confidence: float = 0.0


@dataclass
class Section:
    """编排段：3-4 分钟结构的最小单元。"""
    name: str                # intro/verse/chorus/bridge/outro
    bars: int                # 每 bar 16 步
    energy: float            # 0-1，决定鼓层密度与切片密度
    drop_hats: bool = True
    add_bass: bool = True


@dataclass
class BeatSpec:
    """compose 输出、render 输入：完整编排规格。"""
    beat_id: str
    bpm: float
    style: str
    sections: list[Section]
    sample_ids: list[str]
    drum_pattern: dict[str, Any]      # 由 compose 定义：{bar_index: {track: {step: {velocity, offset_ms}}}}
    chop_placements: list[dict]       # {section, bar, step, sample_id, chop_index, pad, midi_note, gain}
    vocal_placements: list[dict]      # 人声 phrase 垫底：{section, bar, file, start_sec, gain}
    bass_pattern: dict[str, Any]      # {bar_index: {step: midi_note}}
    total_bars: int = 0
    created_at: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @staticmethod
    def from_json(text: str) -> "BeatSpec":
        data = json.loads(text)
        spec = BeatSpec(
            beat_id=data["beat_id"], bpm=data["bpm"], style=data["style"],
            sections=[Section(**s) for s in data["sections"]],
            sample_ids=data["sample_ids"], drum_pattern=data["drum_pattern"],
            chop_placements=data["chop_placements"],
            vocal_placements=data["vocal_placements"],
            bass_pattern=data["bass_pattern"], total_bars=data["total_bars"],
            created_at=data.get("created_at", ""),
        )
        return spec


# ---------- 通用小工具 ----------
def md5_file(path: Path) -> str:
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_audio_mono(path: Path, sr: int = 22050) -> tuple[Any, float]:
    """统一读音频为单声道。返回 (samples np.ndarray, sample_rate)。"""
    import librosa
    import numpy as np
    y, sr_out = librosa.load(str(path), sr=sr, mono=True)
    return np.asarray(y, dtype=np.float32), float(sr_out)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
