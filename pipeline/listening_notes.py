"""Score song 时间戳试听笔记：安全路径解析 + SHA256/WAV 实际时长 + SQLite 追加存储。

设计约束（人工试听反馈，不做任何推断/自动改动）:
- 只认 ``ROOT/beats/<run_id>/`` 下的 score song（必须同时存在 run_manifest.json
  与 full_mix.wav）。run_id 走白名单，路径经 resolve() 后做包含校验，拒绝目录
  穿越与符号链接逃逸；从不读取请求体里给出的任意源路径，也不修改任何歌曲文件。
- 笔记写入独立的 ``ROOT/listening-notes.sqlite``（与 db.sqlite 分离），只追加。
- POST 以 ``request_id`` 幂等：UNIQUE(run_id, mix_sha256, request_id)。
  同一 (run, 音频版本, request_id) 重复提交且内容一致 → 返回原笔记；
  内容不一致 → ConflictError（HTTP 409），绝不覆盖。
- 音频重新渲染后 SHA256 变化：旧版本笔记行永久保留，GET 只返回当前版本的笔记；
  提交旧 SHA → StaleAudioError（HTTP 409）明确提示版本过期。

仅依赖标准库；root 由调用方注入（feedback 传 common.ROOT），便于临时目录测试。
"""
from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import struct
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

# ---------- 常量 / 契约 ----------
CATEGORIES = ("noisy", "repetitive", "drums", "transition", "like", "other")
MAX_TEXT_LEN = 1000
MAX_REQUEST_ID_LEN = 128
NOTES_FILENAME = "listening-notes.sqlite"

_RUN_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

NOTE_COLUMNS = ("id", "run_id", "mix_sha256", "request_id",
                "time_seconds", "category", "text", "created_at")

DDL = """
CREATE TABLE IF NOT EXISTS listening_notes (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    mix_sha256 TEXT NOT NULL,
    request_id TEXT NOT NULL,
    time_seconds REAL NOT NULL,
    category TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, mix_sha256, request_id)
);
"""


class ListeningNotesError(Exception):
    """本模块所有自定义错误的基类。"""


class InvalidNoteError(ListeningNotesError, ValueError):
    """请求参数非法（HTTP 400）。同时是 ValueError，便于既有处理器映射。"""


class ConflictError(ListeningNotesError):
    """请求与已保存状态冲突（HTTP 409）。"""


class StaleAudioError(ConflictError):
    """提交的 mix_sha256 已不是当前 full_mix.wav（音频版本过期）。"""


# ---------- 路径安全 ----------
def resolve_run(root, run_id) -> Path:
    """把 run_id 安全解析为 ``ROOT/beats/<run_id>`` 真实目录。

    白名单字符 + resolve() 包含校验，拒绝目录穿越与符号链接逃逸。
    非法 id → InvalidNoteError(400)；不存在/越界 → FileNotFoundError(404)。
    """
    if not isinstance(run_id, str):
        raise InvalidNoteError("run_id 必须是字符串")
    run_id = run_id.strip()
    if not run_id or run_id in (".", "..") or _RUN_ID_RE.fullmatch(run_id) is None:
        raise InvalidNoteError("非法 run_id（只允许字母、数字、点、下划线、连字符）")
    beats = (Path(root) / "beats").resolve()
    run_dir = (beats / run_id).resolve()
    if not run_dir.is_relative_to(beats):
        # beats/<id> 是指向库外的符号链接等逃逸情况
        raise FileNotFoundError(f"beats/{run_id} 路径越界，已拒绝")
    if not run_dir.is_dir():
        raise FileNotFoundError(f"beats/{run_id} 不存在或不是目录")
    return run_dir


def _require_file(parent: Path, name: str) -> Path:
    """要求 parent/name 是真实文件且解析后仍在 parent 内（拒绝符号链接逃逸）。"""
    path = (parent / name).resolve()
    if not path.is_relative_to(parent) or not path.is_file():
        raise FileNotFoundError(f"{parent.name}/{name} 缺失或不是库内真实文件")
    return path


# ---------- 音频指纹 ----------
def sha256_file(path) -> str:
    """分块流式计算 SHA256（不整文件载入内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_wav_duration(path) -> float:
    """用标准库 wave 读真实时长（帧数/采样率）；非有效 WAV → InvalidNoteError。"""
    try:
        with wave.open(str(path), "rb") as wav:
            rate = wav.getframerate()
            frames = wav.getnframes()
    except (wave.Error, EOFError, OSError, struct.error) as exc:
        raise InvalidNoteError(f"{Path(path).name} 不是有效 WAV，无法读取时长") from exc
    if not isinstance(rate, int) or rate <= 0:
        raise InvalidNoteError("WAV 采样率无效")
    return frames / float(rate)


def current_version(root, run_id: str) -> dict:
    """解析 run 并返回当前音频版本上下文（路径/SHA256/真实时长）。"""
    run_dir = resolve_run(root, run_id)
    manifest = _require_file(run_dir, "run_manifest.json")
    mix = _require_file(run_dir, "full_mix.wav")
    return {
        "run_id": run_dir.name,
        "run_dir": run_dir,
        "manifest_path": manifest,
        "mix_path": mix,
        "mix_sha256": sha256_file(mix),
        "duration_seconds": read_wav_duration(mix),
    }


# ---------- 校验 ----------
def validate_note(body: dict, duration_seconds: float) -> dict:
    """纯函数校验/规整 POST 载荷（调用方需先拿到真实时长）。"""
    if not isinstance(body, dict):
        raise InvalidNoteError("请求体必须是 JSON 对象")

    request_id = body.get("request_id")
    if not isinstance(request_id, str):
        raise InvalidNoteError("request_id 必须是非空字符串")
    request_id = request_id.strip()
    if not (1 <= len(request_id) <= MAX_REQUEST_ID_LEN):
        raise InvalidNoteError(f"request_id 长度需为 1-{MAX_REQUEST_ID_LEN} 字符")

    mix_sha = body.get("mix_sha256")
    if not isinstance(mix_sha, str) or _SHA256_RE.fullmatch(mix_sha) is None:
        raise InvalidNoteError("mix_sha256 必须是恰好 64 位小写十六进制字符")

    raw_time = body.get("time_seconds")
    # bool 是 int 子类，必须先排除
    if isinstance(raw_time, bool) or not isinstance(raw_time, (int, float)):
        raise InvalidNoteError("time_seconds 必须是数字（不能是布尔值或字符串）")
    try:
        time_value = float(raw_time)
    except OverflowError as exc:
        raise InvalidNoteError("time_seconds 超出可接受的数值范围") from exc
    duration = float(duration_seconds)
    if not math.isfinite(time_value) or time_value < 0 or time_value > duration:
        raise InvalidNoteError(
            f"time_seconds 必须是 0 到 {duration:.3f} 秒之间的有限数值")

    category = body.get("category")
    if category not in CATEGORIES:
        raise InvalidNoteError(f"category 必须是 {list(CATEGORIES)} 之一")

    text = body.get("text")
    if not isinstance(text, str):
        raise InvalidNoteError("text 必须是字符串")
    text = text.strip()
    if not (1 <= len(text) <= MAX_TEXT_LEN):
        raise InvalidNoteError(f"text 去除首尾空白后需为 1-{MAX_TEXT_LEN} 字符")

    return {
        "request_id": request_id,
        "mix_sha256": mix_sha,
        "time_seconds": time_value,
        "category": category,
        "text": text,
    }


# ---------- SQLite ----------
def db_path(root) -> Path:
    return Path(root) / NOTES_FILENAME


def connect_db(root) -> sqlite3.Connection:
    """打开（必要时创建）独立的 listening-notes.sqlite 并幂等建表。"""
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(DDL)
    conn.commit()
    return conn


def _row_to_note(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in NOTE_COLUMNS}


def add_note(root, body: dict) -> dict:
    """校验并追加一条笔记。

    返回保存的笔记 dict（含 id / UTC created_at；deduplicated 标记是否为
    同一 request_id 的幂等重放）。
    """
    run_raw = body.get("run_id") if isinstance(body, dict) else None
    version = current_version(root, run_raw if isinstance(run_raw, str) else "")
    data = validate_note(body, version["duration_seconds"])
    if data["mix_sha256"] != version["mix_sha256"]:
        raise StaleAudioError(
            "mix_sha256 与当前 full_mix.wav 不一致：音频版本已变化，"
            "请刷新页面读取新版本后再记录")

    conn = connect_db(root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT id, run_id, mix_sha256, request_id, time_seconds, "
                "category, text, created_at FROM listening_notes "
                "WHERE run_id=? AND mix_sha256=? AND request_id=?",
                (version["run_id"], data["mix_sha256"], data["request_id"]),
            ).fetchone()
            if existing is not None:
                note = _row_to_note(existing)
                same = (
                    float(note["time_seconds"]) == data["time_seconds"]
                    and note["category"] == data["category"]
                    and note["text"] == data["text"]
                )
                if not same:
                    raise ConflictError(
                        "同一 request_id 已保存过不同内容，不能覆盖；"
                        "请使用新的 request_id 重试")
                conn.commit()
                note["deduplicated"] = True
                return note

            note = {
                "id": uuid.uuid4().hex,
                "run_id": version["run_id"],
                "mix_sha256": data["mix_sha256"],
                "request_id": data["request_id"],
                "time_seconds": data["time_seconds"],
                "category": data["category"],
                "text": data["text"],
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            conn.execute(
                "INSERT INTO listening_notes "
                "(id, run_id, mix_sha256, request_id, time_seconds, category, "
                "text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(note[key] for key in NOTE_COLUMNS),
            )
            conn.commit()
            note["deduplicated"] = False
            return note
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def list_notes(root, run_id: str) -> dict:
    """返回当前音频版本的 SHA256、真实时长与该版本全部笔记（按时间点排序）。

    旧音频版本的行保留在 SQLite 中，但不会出现在返回里。
    """
    version = current_version(root, run_id)
    conn = connect_db(root)
    try:
        rows = conn.execute(
            "SELECT id, run_id, mix_sha256, request_id, time_seconds, "
            "category, text, created_at FROM listening_notes "
            "WHERE run_id=? AND mix_sha256=? "
            "ORDER BY time_seconds ASC, created_at ASC, id ASC",
            (version["run_id"], version["mix_sha256"]),
        ).fetchall()
    finally:
        conn.close()
    return {
        "run_id": version["run_id"],
        "mix_sha256": version["mix_sha256"],
        "duration_seconds": version["duration_seconds"],
        "notes": [_row_to_note(row) for row in rows],
    }
