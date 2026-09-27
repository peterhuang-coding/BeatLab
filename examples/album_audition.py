"""Ten-beat audition collection server (Python standard library only).

Serves a bounded localhost listening workspace for a ten-track collection of
first-stage draft beats.  A human reviewer rates five optional dimensions per
track, marks a Keep disposition, writes free notes and timestamped notes; the
server persists that feedback atomically, keyed to the exact submitted
collection version / mix hash.

The module never executes media, never calls models, never publishes anything,
and never rerenders anything in response to a score or Keep decision.  See
docs/album-audition.md and contract.md.
"""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

# ---------------------------------------------------------------------------
# Constants / contract limits
# ---------------------------------------------------------------------------

COLLECTION_FILENAME = "collection.json"
FEEDBACK_FILENAME = "feedback.json"

# rating field key -> Chinese label
RATING_FIELDS = (
    ("groove", "律动"),
    ("sample", "采样记忆点"),
    ("variation", "变化"),
    ("clarity", "清晰度"),
    ("rap_space", "留给人声的空间"),
)
RATING_KEYS = tuple(k for k, _ in RATING_FIELDS)

KEEP_VALUES = ("keep", "revise", "reject", "undecided")

MIN_TRACKS = 1
MAX_TRACKS = 20
EXPECTED_TRACKS = 10

MAX_BODY_BYTES = 64 * 1024  # 64 KiB hard cap on POST bodies
MAX_NOTES_CHARS = 10_000
MAX_TIMESTAMP_NOTES = 200
MAX_TIMESTAMP_TEXT_CHARS = 1_000

MEDIA_MIME_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
}

_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")

CONTENT_FIELDS = ("ratings", "keep", "notes", "timestamp_notes")


# ---------------------------------------------------------------------------
# Manifest (collection.json)
# ---------------------------------------------------------------------------


class ManifestError(ValueError):
    """Raised when collection.json does not satisfy the contract schema."""


def _require_type(obj, types, name, errors):
    if not isinstance(obj, types):
        names = types if isinstance(types, tuple) else (types,)
        want = "/".join(getattr(t, "__name__", str(t)) for t in names)
        errors.append(f"{name} 应为 {want}")
        return False
    return True


def _is_plain_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_relative_media_path(value, name, errors):
    """Return a normalized posix path or None if the value is unsafe."""
    if not _require_type(value, str, name, errors):
        return None
    if "\x00" in value or "\\" in value or value.startswith("/"):
        errors.append(f"{name} 必须是目录内的相对路径")
        return None
    parts = PurePosixPath(value).parts
    if not parts or any(p in ("", "..") for p in parts) or parts[0] == ".":
        errors.append(f"{name} 含有非法路径段")
        return None
    normalized = PurePosixPath(*parts).as_posix()
    return normalized


def load_manifest(collection_dir: Path) -> dict:
    """Load and strictly validate collection.json from *collection_dir*."""
    path = collection_dir / COLLECTION_FILENAME
    if not path.is_file():
        raise ManifestError(f"找不到清单文件: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - filesystem dependent
        raise ManifestError(f"无法读取 {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"collection.json 不是合法 JSON: {exc}") from exc

    errors = []
    if not isinstance(data, dict):
        raise ManifestError("collection.json 顶层必须是对象")

    for key in ("id", "title", "subtitle"):
        value = data.get(key)
        if not _require_type(value, str, key, errors) or not value.strip():
            errors.append(f"{key} 不能为空")

    tracks = data.get("tracks")
    if not _require_type(tracks, list, "tracks", errors):
        raise ManifestError("; ".join(errors))
    if not (MIN_TRACKS <= len(tracks) <= MAX_TRACKS):
        errors.append(
            f"tracks 数量必须在 {MIN_TRACKS}..{MAX_TRACKS} 之间"
            f"（常规为 {EXPECTED_TRACKS} 首），当前 {len(tracks)}"
        )

    seen_ids = set()
    for i, track in enumerate(tracks):
        where = f"tracks[{i}]"
        if not isinstance(track, dict):
            errors.append(f"{where} 必须是对象")
            continue
        tid = track.get("id")
        if not _require_type(tid, str, f"{where}.id", errors) or not tid.strip():
            errors.append(f"{where}.id 不能为空")
        elif tid in seen_ids:
            errors.append(f"{where}.id 重复: {tid}")
        else:
            seen_ids.add(tid)

        if not _is_plain_int(track.get("number")):
            errors.append(f"{where}.number 必须是整数")

        for key in ("title", "direction", "version", "mix_sha256"):
            val = track.get(key)
            if not _require_type(val, str, f"{where}.{key}", errors) or not val.strip():
                errors.append(f"{where}.{key} 不能为空")

        sha = track.get("mix_sha256")
        if isinstance(sha, str) and not _HEX64_RE.match(sha):
            errors.append(f"{where}.mix_sha256 必须是 64 位十六进制 SHA-256")

        bpm = track.get("bpm")
        if not _is_number(bpm) or bpm <= 0:
            errors.append(f"{where}.bpm 必须是正数")
        duration = track.get("duration_seconds")
        if not _is_number(duration) or duration <= 0:
            errors.append(f"{where}.duration_seconds 必须是正数")
        change = track.get("change_point_seconds")
        if not _is_number(change) or change < 0:
            errors.append(f"{where}.change_point_seconds 不能为负")
        elif _is_number(duration) and duration > 0 and change > duration:
            errors.append(f"{where}.change_point_seconds 超过曲目时长")

        audio = _check_relative_media_path(
            track.get("audio"), f"{where}.audio", errors
        )
        original = _check_relative_media_path(
            track.get("original_audio"), f"{where}.original_audio", errors
        )
        if audio:
            track["audio"] = audio
        if original:
            track["original_audio"] = original

        editable = track.get("editable_path")
        if not _require_type(editable, str, f"{where}.editable_path", errors):
            errors.append(f"{where}.editable_path 应为本地绝对路径字符串")

        sources = track.get("source_summary")
        if not _require_type(sources, list, f"{where}.source_summary", errors):
            continue
        for j, src in enumerate(sources):
            sw = f"{where}.source_summary[{j}]"
            if not isinstance(src, dict):
                errors.append(f"{sw} 必须是对象")
                continue
            for key in ("title", "source_url"):
                val = src.get(key)
                if not _require_type(val, str, f"{sw}.{key}", errors):
                    errors.append(f"{sw}.{key} 缺失或不是字符串")

    if errors:
        raise ManifestError("; ".join(errors))
    return data


def build_media_allowlist(manifest: dict) -> dict:
    """Map every declared normalized media path to its owning track id."""
    allowlist = {}
    for track in manifest["tracks"]:
        for key in ("audio", "original_audio"):
            rel = track[key]
            allowlist.setdefault(rel, track["id"])
    return allowlist


# ---------------------------------------------------------------------------
# Feedback persistence (feedback.json)
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def empty_feedback(collection_id: str) -> dict:
    return {"collection_id": collection_id, "ratings": {}}


def load_feedback(path: Path, collection_id: str) -> dict:
    if not path.is_file():
        return empty_feedback(collection_id)
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeedbackStorageError(f"feedback.json 已损坏且不会被覆盖: {exc}") from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("collection_id"), str)
        or not isinstance(data.get("ratings"), dict)
    ):
        raise FeedbackStorageError("feedback.json 结构不合法且不会被覆盖")
    return data


class FeedbackStorageError(RuntimeError):
    """Raised when feedback.json cannot be read or written safely."""


def write_feedback_atomic(path: Path, data: dict) -> None:
    """Write *data* to *path* atomically (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".feedback.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


class PayloadError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _validate_ratings(value, track) -> dict:
    if not isinstance(value, dict):
        raise PayloadError(400, "invalid_ratings", "ratings 必须是对象")
    keys = set(value.keys())
    unknown = keys - set(RATING_KEYS)
    if unknown:
        raise PayloadError(
            400,
            "unknown_field",
            f"ratings 含未知字段: {', '.join(sorted(unknown))}",
        )
    missing = set(RATING_KEYS) - keys
    if missing:
        raise PayloadError(
            400,
            "missing_field",
            f"ratings 缺少字段: {', '.join(sorted(missing))}",
        )
    cleaned = {}
    for key in RATING_KEYS:
        v = value[key]
        if v is None:
            cleaned[key] = None
            continue
        if not _is_plain_int(v) or not (1 <= v <= 5):
            label = dict(RATING_FIELDS)[key]
            raise PayloadError(
                400,
                "invalid_rating",
                f"{label}({key}) 只允许空值或 1..5 的整数，收到: {v!r}",
            )
        cleaned[key] = int(v)
    return cleaned


def _validate_timestamp_notes(value, track) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PayloadError(400, "invalid_timestamp_notes", "timestamp_notes 必须是数组")
    if len(value) > MAX_TIMESTAMP_NOTES:
        raise PayloadError(
            400,
            "too_many_timestamp_notes",
            f"timestamp_notes 最多 {MAX_TIMESTAMP_NOTES} 条",
        )
    duration = float(track["duration_seconds"])
    cleaned = []
    for i, item in enumerate(value):
        where = f"timestamp_notes[{i}]"
        if not isinstance(item, dict):
            raise PayloadError(400, "invalid_timestamp_note", f"{where} 必须是对象")
        extra = set(item.keys()) - {"seconds", "text"}
        if extra:
            raise PayloadError(
                400, "unknown_field", f"{where} 含未知字段: {', '.join(sorted(extra))}"
            )
        if "seconds" not in item or "text" not in item:
            raise PayloadError(
                400, "missing_field", f"{where} 需要 seconds 与 text 两个字段"
            )
        seconds = item["seconds"]
        if (
            not _is_number(seconds)
            or isinstance(seconds, bool)
            or not math.isfinite(float(seconds))
            or seconds < 0
            or float(seconds) > duration
        ):
            raise PayloadError(
                400,
                "invalid_seconds",
                f"{where}.seconds 必须是 0..{duration:g} 秒之间的数字",
            )
        text = item["text"]
        if not isinstance(text, str):
            raise PayloadError(400, "invalid_text", f"{where}.text 必须是字符串")
        if len(text) > MAX_TIMESTAMP_TEXT_CHARS:
            raise PayloadError(
                400,
                "text_too_long",
                f"{where}.text 不能超过 {MAX_TIMESTAMP_TEXT_CHARS} 字",
            )
        cleaned.append({"seconds": round(float(seconds), 3), "text": text})
    return cleaned


def validate_feedback_payload(payload, manifest: dict) -> dict:
    """Validate a decoded POST body and return a normalized content record."""
    if not isinstance(payload, dict):
        raise PayloadError(400, "invalid_body", "请求体必须是 JSON 对象")

    allowed_top = {
        "track_id",
        "version",
        "mix_sha256",
        "ratings",
        "keep",
        "notes",
        "timestamp_notes",
    }
    extra = set(payload.keys()) - allowed_top
    if extra:
        raise PayloadError(
            400, "unknown_field", f"含未知字段: {', '.join(sorted(extra))}"
        )

    missing = {"track_id", "version", "mix_sha256", "ratings", "keep"} - set(
        payload.keys()
    )
    if missing:
        raise PayloadError(
            400, "missing_field", f"缺少必填字段: {', '.join(sorted(missing))}"
        )

    track_id = payload["track_id"]
    if not isinstance(track_id, str) or not track_id:
        raise PayloadError(400, "invalid_track_id", "track_id 必须是非空字符串")
    by_id = {t["id"]: t for t in manifest["tracks"]}
    track = by_id.get(track_id)
    if track is None:
        raise PayloadError(
            404, "unknown_track", f"清单中没有 track_id={track_id!r} 的曲目"
        )

    version = payload["version"]
    mix_sha = payload["mix_sha256"]
    if not isinstance(version, str) or not isinstance(mix_sha, str):
        raise PayloadError(400, "invalid_identity", "version / mix_sha256 必须是字符串")
    if version != track["version"] or mix_sha != track["mix_sha256"]:
        raise PayloadError(
            409,
            "stale_version",
            "version 或 mix_sha256 与当前清单不匹配；你听到的可能是旧混音，"
            "请刷新页面后再评分",
        )

    ratings = _validate_ratings(payload["ratings"], track)

    keep = payload["keep"]
    if keep not in KEEP_VALUES:
        raise PayloadError(
            400,
            "invalid_keep",
            f"keep 只能是 {('/'.join(KEEP_VALUES))}，收到: {keep!r}",
        )

    notes = payload.get("notes", "")
    if not isinstance(notes, str):
        raise PayloadError(400, "invalid_notes", "notes 必须是字符串")
    if len(notes) > MAX_NOTES_CHARS:
        raise PayloadError(
            400, "notes_too_long", f"notes 不能超过 {MAX_NOTES_CHARS} 字"
        )

    timestamp_notes = _validate_timestamp_notes(
        payload.get("timestamp_notes", []), track
    )

    return {
        "track_id": track_id,
        "version": version,
        "mix_sha256": mix_sha,
        "ratings": ratings,
        "keep": keep,
        "notes": notes,
        "timestamp_notes": timestamp_notes,
    }


def content_signature(record: dict) -> str:
    return json.dumps(
        {key: record[key] for key in CONTENT_FIELDS},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# Frontend page (static shell; all metadata rendered through textContent)
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BeatLab · 主题试听工作台</title>
<style>
:root{
  --paper:#f6efe2; --card:#fffdf7; --ink:#342a22; --muted:#7d6f5f;
  --line:#e4d8c3; --accent:#b4502a; --accent-soft:#e9d9c9;
  --ok:#4e7a4e; --warn:#a05a18; --bad:#9c3b2f;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--paper); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  line-height:1.55; font-size:15px;
}
header.page{
  max-width:1020px; margin:0 auto; padding:30px 18px 8px;
}
h1{font-family:Georgia,"Songti SC","STSong",serif; font-size:28px; margin:0 0 4px}
.subtitle{color:var(--muted); margin:0 0 14px; font-size:15px}
.banner{
  background:var(--accent-soft); border:1px solid var(--line);
  border-left:4px solid var(--accent); border-radius:10px;
  padding:12px 14px; font-size:13.5px; color:#5b4a3a; margin:10px 0 6px;
}
.banner strong{color:var(--accent)}
main{max-width:1020px; margin:0 auto; padding:12px 18px 60px}
.card{
  background:var(--card); border:1px solid var(--line); border-radius:14px;
  padding:18px; margin:16px 0; box-shadow:0 1px 2px rgba(90,70,40,.06);
}
.card-head{display:flex; gap:14px; align-items:flex-start; flex-wrap:wrap}
.num{
  font-family:Georgia,serif; font-size:30px; color:var(--accent);
  min-width:44px; line-height:1; padding-top:2px;
}
.titles h2{margin:0; font-size:19px; font-family:Georgia,"Songti SC",serif}
.meta{color:var(--muted); font-size:13px; margin:3px 0 0}
.meta span{display:inline-block; margin-right:12px}
.direction{margin:10px 0 4px; font-size:14px}
.direction b{color:var(--ink)}
.sources{font-size:12px;color:var(--muted);margin:2px 0 8px}
audio{width:100%; margin:8px 0 6px}
.markbtn,.ghost{
  background:none; border:1px solid var(--line); color:var(--warn);
  border-radius:999px; padding:4px 12px; font-size:12.5px; cursor:pointer;
}
.markbtn:hover,.ghost:hover{background:var(--accent-soft)}
.ratings{display:flex; flex-wrap:wrap; gap:8px 16px; margin:12px 0 6px}
.rating{display:flex; flex-direction:column; font-size:12px; color:var(--muted)}
.rating select{margin-top:3px; padding:4px 6px; font-size:14px; min-width:64px}
.keeprow{display:flex; align-items:center; gap:10px; margin:8px 0; flex-wrap:wrap}
.keeprow select{padding:5px 8px; font-size:14px}
label.notes-lab{font-size:12.5px;color:var(--muted);display:block;margin:8px 0 4px}
textarea{
  width:100%; min-height:56px; border:1px solid var(--line); border-radius:8px;
  padding:8px 10px; font:inherit; background:#fffef9; resize:vertical;
}
.ts{margin:10px 0}
.ts-list{list-style:none; margin:8px 0; padding:0}
.ts-list li{display:flex; gap:8px; align-items:center; margin:5px 0}
.ts-time{
  border:1px solid var(--line); background:var(--paper); color:var(--warn);
  border-radius:6px; padding:2px 8px; font-variant-numeric:tabular-nums;
  cursor:pointer; font-size:13px; white-space:nowrap;
}
.ts-list input[type=text]{flex:1; padding:6px 8px}
.ts-del{border:none;background:none;color:var(--bad);cursor:pointer;font-size:15px}
.save-row{display:flex; align-items:center; gap:12px; margin-top:12px; flex-wrap:wrap}
.save{
  background:var(--accent); color:#fff8ef; border:none; border-radius:9px;
  padding:8px 22px; font-size:14.5px; cursor:pointer;
}
.save:disabled{opacity:.55;cursor:default}
.status{font-size:13px; color:var(--muted)}
.status.ok{color:var(--ok)}
.status.err{color:var(--bad)}
.loaderr{color:var(--bad); background:#fbe9e5; border:1px solid #ecc9c0;
  border-radius:10px;padding:12px;margin:14px 0}
footer.page{max-width:1020px;margin:0 auto;padding:0 18px 40px;color:var(--muted);font-size:12px}
@media (max-width:640px){
  .num{font-size:24px;min-width:34px}
  .card{padding:14px}
  .ratings{gap:8px 10px}
  .rating{flex:1 1 42%}
}
</style>
</head>
<body>
<header class="page">
  <h1 id="coll-title">BeatLab 试听工作台</h1>
  <p class="subtitle" id="coll-subtitle">正在载入合集…</p>
  <div class="banner">
    <strong>第一阶段试听草稿。</strong>
    先完整听一轮，再挑你想继续制作的几首。未评分的维度保持空白。
    标记具体秒数，告诉我想改什么、哪些别动；下一版按你的反馈继续做。
    <br>1 = 不满意 · 3 = 有潜力，想修改 · 5 = 愿意保留当前表现
  </div>
</header>
<main id="cards"><p class="meta">正在载入曲目…</p></main>
<footer class="page">
  BeatLab · 本轮风格初筛　<a href="/api/feedback/download">导出本轮反馈</a>
</footer>
<script>
"use strict";
var RATING_FIELDS = [
  ["groove","律动"],["sample","采样记忆点"],["variation","变化"],
  ["clarity","清晰度"],["rap_space","留给人声的空间"]
];
var KEEPS = [
  ["undecided","未定"],["keep","保留"],["revise","待改"],["reject","不要"]
];
var audios = [];

function el(tag, cls, text){
  var e = document.createElement(tag);
  if(cls) e.className = cls;
  if(text !== undefined && text !== null) e.textContent = text;
  return e;
}
function fmtTime(s){
  s = Math.max(0, Math.floor(Number(s) || 0));
  return Math.floor(s/60) + ":" + String(s%60).padStart(2,"0");
}
async function getJSON(u){
  var r = await fetch(u, {headers:{"Accept":"application/json"}});
  var data = null;
  try { data = await r.json(); } catch(e){}
  if(!r.ok){
    var msg = (data && data.error && data.error.message) || ("HTTP " + r.status);
    throw new Error(msg);
  }
  return data;
}

function ratingSelects(track){
  var box = el("div","ratings");
  var selects = {};
  RATING_FIELDS.forEach(function(pair){
    var lab = el("label","rating", pair[1]);
    var sel = document.createElement("select");
    sel.name = pair[0];
    var blank = document.createElement("option");
    blank.value = ""; blank.textContent = "—";
    sel.appendChild(blank);
    for(var v=1; v<=5; v++){
      var o = document.createElement("option");
      o.value = String(v); o.textContent = String(v);
      sel.appendChild(o);
    }
    lab.appendChild(sel);
    box.appendChild(lab);
    selects[pair[0]] = sel;
  });
  return {box:box, selects:selects};
}

function buildCard(track, existing){
  var card = el("section","card");

  var head = el("div","card-head");
  head.appendChild(el("span","num", String(track.number).padStart(2,"0")));
  var titles = el("div","titles");
  titles.appendChild(el("h2", null, track.title));
  var meta = el("p","meta");
  meta.appendChild(el("span", null, "BPM " + track.bpm));
  meta.appendChild(el("span", null, "时长 " + fmtTime(track.duration_seconds)));
  meta.appendChild(el("span", null, "版本 " + track.version));
  titles.appendChild(meta);
  head.appendChild(titles);
  card.appendChild(head);

  if(track.direction){
    var d = el("p","direction");
    d.appendChild(el("b", null, "改编方向："));
    d.appendChild(document.createTextNode(track.direction));
    card.appendChild(d);
  }
  if(track.theme){
    var th = el("p","direction");
    th.appendChild(el("b", null, "主题："));
    th.appendChild(document.createTextNode(track.theme));
    card.appendChild(th);
  }
  if(track.source_summary && track.source_summary.length){
    var s = el("p","sources","参考素材：" +
      track.source_summary.map(function(x){return x.title;}).join(" / "));
    card.appendChild(s);
  }

  var audio = document.createElement("audio");
  audio.controls = true;
  audio.preload = "none";
  audio.src = "/media/" + track.audio;
  audio.addEventListener("error", function(){
    statusEl.textContent = "音频加载失败：" + track.audio;
    statusEl.className = "status err";
  });
  audio.addEventListener("play", function(){
    audios.forEach(function(a){ if(a !== audio){ a.pause(); } });
  });
  audios.push(audio);
  card.appendChild(audio);

  var markRow = el("div", null);
  var mark = el("button","markbtn","⚑ 跳到段落变化 " + fmtTime(track.change_point_seconds));
  mark.type = "button";
  mark.addEventListener("click", function(){
    audio.currentTime = Number(track.change_point_seconds) || 0;
    audio.play().catch(function(){});
  });
  markRow.appendChild(mark);
  if(track.original_audio){
    var orig = el("a","ghost");
    orig.href = "/media/" + track.original_audio;
    orig.textContent = "试听 WAV";
    orig.style.marginLeft = "8px";
    markRow.appendChild(orig);
  }
  card.appendChild(markRow);

  var r = ratingSelects(track);
  card.appendChild(r.box);

  var keepRow = el("div","keeprow");
  keepRow.appendChild(el("label", null, "处理意见"));
  var keepSel = document.createElement("select");
  KEEPS.forEach(function(pair){
    var o = document.createElement("option");
    o.value = pair[0]; o.textContent = pair[1];
    keepSel.appendChild(o);
  });
  keepSel.value = "undecided";
  keepRow.appendChild(keepSel);
  card.appendChild(keepRow);

  card.appendChild(el("label","notes-lab","想改什么？哪些别动？"));
  var notes = document.createElement("textarea");
  notes.placeholder = "例如：鼓组进入太满、副歌采样可以再突出…";
  card.appendChild(notes);

  var tsBox = el("div","ts");
  var tsHead = el("label","notes-lab","时间点记录");
  tsBox.appendChild(tsHead);
  var tsList = el("ul","ts-list");
  tsBox.appendChild(tsList);
  var addBtn = el("button","ghost","＋ 记录当前播放位置");
  addBtn.type = "button";
  tsBox.appendChild(addBtn);
  card.appendChild(tsBox);

  var marks = existing && existing.timestamp_notes
    ? existing.timestamp_notes.map(function(m){return {seconds:m.seconds, text:m.text};})
    : [];
  function drawMarks(){
    while(tsList.firstChild) tsList.removeChild(tsList.firstChild);
    marks.forEach(function(m, idx){
      var li = document.createElement("li");
      var t = el("button","ts-time", fmtTime(m.seconds));
      t.type = "button";
      t.title = m.seconds.toFixed(2) + " 秒（点击跳转）";
      t.addEventListener("click", function(){
        audio.currentTime = Number(m.seconds) || 0;
        audio.play().catch(function(){});
      });
      var inp = document.createElement("input");
      inp.type = "text"; inp.value = m.text;
      inp.maxLength = 1000;
      inp.placeholder = "这里听感如何？";
      inp.addEventListener("input", function(){ marks[idx].text = inp.value; });
      var del = el("button","ts-del","✕");
      del.type = "button";
      del.title = "删除该时间点";
      del.addEventListener("click", function(){
        marks.splice(idx,1); drawMarks();
      });
      li.appendChild(t); li.appendChild(inp); li.appendChild(del);
      tsList.appendChild(li);
    });
  }
  addBtn.addEventListener("click", function(){
    var s = Number(audio.currentTime) || 0;
    marks.push({seconds:Math.round(s*1000)/1000, text:""});
    drawMarks();
    var rows = tsList.querySelectorAll("input[type=text]");
    if(rows.length) rows[rows.length-1].focus();
  });
  drawMarks();

  var saveRow = el("div","save-row");
  var saveBtn = el("button","save","保存本曲反馈");
  saveBtn.type = "button";
  var statusEl = el("span","status");
  saveRow.appendChild(saveBtn);
  saveRow.appendChild(statusEl);
  card.appendChild(saveRow);

  if(existing){
    RATING_FIELDS.forEach(function(pair){
      var v = existing.ratings && existing.ratings[pair[0]];
      if(v === 1 || v === 2 || v === 3 || v === 4 || v === 5){
        r.selects[pair[0]].value = String(v);
      }
    });
    if(KEEPS.some(function(p){return p[0] === existing.keep;})){
      keepSel.value = existing.keep;
    }
    notes.value = existing.notes || "";
    statusEl.textContent = "已载入此前保存的反馈（" +
      (existing.updated_at || "时间未知") + "）";
  }

  saveBtn.addEventListener("click", async function(){
    var ratings = {};
    RATING_FIELDS.forEach(function(pair){
      var v = r.selects[pair[0]].value;
      ratings[pair[0]] = v === "" ? null : parseInt(v, 10);
    });
    var payload = {
      track_id: track.id,
      version: track.version,
      mix_sha256: track.mix_sha256,
      ratings: ratings,
      keep: keepSel.value,
      notes: notes.value,
      timestamp_notes: marks.map(function(m){
        return {seconds:Number(m.seconds)||0, text:m.text};
      })
    };
    saveBtn.disabled = true;
    statusEl.className = "status";
    statusEl.textContent = "保存中…";
    try {
      var resp = await fetch("/api/feedback", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify(payload)
      });
      var data = null;
      try { data = await resp.json(); } catch(e){}
      if(!resp.ok){
        var msg = (data && data.error && data.error.message) || ("保存失败 HTTP " + resp.status);
        throw new Error(msg);
      }
      statusEl.className = "status ok";
      statusEl.textContent = "已保存 · " + (data.updated_at || "");
      marks = (data.timestamp_notes || []).map(function(m){
        return {seconds:m.seconds, text:m.text};
      });
      drawMarks();
    } catch(err){
      statusEl.className = "status err";
      statusEl.textContent = "✕ " + err.message;
    } finally {
      saveBtn.disabled = false;
    }
  });

  return card;
}

async function boot(){
  try{
    var collection = await getJSON("/api/collection");
    document.getElementById("coll-title").textContent = collection.title;
    document.getElementById("coll-subtitle").textContent =
      (collection.subtitle || "") + " · 共 " + collection.tracks.length + " 首 · 本地试听";
    var feedback = await getJSON("/api/feedback");
    var ratings = feedback.ratings || {};
    var host = document.getElementById("cards");
    while(host.firstChild) host.removeChild(host.firstChild);
    collection.tracks.forEach(function(t){
      host.appendChild(buildCard(t, ratings[t.id]));
    });
  }catch(err){
    var host = document.getElementById("cards");
    host.innerHTML = "";
    var box = document.createElement("div");
    box.className = "loaderr";
    box.textContent = "加载失败：" + err.message + "（本地服务是否仍在运行？）";
    host.appendChild(box);
  }
}
boot();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


def _media_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in MEDIA_MIME_TYPES:
        return MEDIA_MIME_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


class AuditionHandler(BaseHTTPRequestHandler):
    server_version = "BeatLabAudition/1.0"
    protocol_version = "HTTP/1.1"

    # silence default stderr logging; tests/operators get real error bodies
    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        return

    # -- response helpers ---------------------------------------------------

    def _send(self, status, body: bytes, content_type, extra_headers=None, write=True):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for key, value in extra_headers:
                self.send_header(key, value)
        self.end_headers()
        if write and self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status, obj, extra_headers=None, write=True):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(
            status,
            body,
            "application/json; charset=utf-8",
            extra_headers,
            write=write,
        )

    def send_error_json(self, status, code, message, extra_headers=None, write=True):
        self.send_json(
            status, {"error": {"code": code, "message": message}}, extra_headers, write=write
        )

    # -- routing ------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/collection":
            self.send_json(200, self.server.manifest)
        elif path == "/api/feedback":
            self._serve_feedback(download=False)
        elif path == "/api/feedback/download":
            self._serve_feedback(download=True)
        elif path.startswith("/media/"):
            self._serve_media(method="GET")
        else:
            self.send_error_json(404, "not_found", f"没有路径 {path}")

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/media/"):
            self._serve_media(method="HEAD")
        elif parsed.path in ("/api/feedback", "/api/feedback/download", "/api/collection", "/"):
            # HEAD reuse: respond with headers only
            path = parsed.path
            if path == "/":
                self._send(
                    200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8", write=False
                )
            elif path == "/api/collection":
                self.send_json(200, self.server.manifest, write=False)
            else:
                self._serve_feedback(download=path == "/api/feedback/download", head=True)
        else:
            self.send_error_json(404, "not_found", f"没有路径 {parsed.path}")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/feedback":
            self.send_error_json(
                404, "not_found", f"没有 POST 路径 {parsed.path}"
            )
            return
        self._post_feedback()

    # -- feedback endpoints -------------------------------------------------

    def _serve_feedback(self, download=False, head=False):
        lock = self.server.feedback_lock
        try:
            with lock:
                data = load_feedback(
                    self.server.feedback_path, self.server.manifest["id"]
                )
        except FeedbackStorageError as exc:
            self.send_error_json(500, "feedback_unreadable", str(exc), write=not head)
            return
        headers = None
        if download:
            headers = [
                (
                    "Content-Disposition",
                    'attachment; filename="feedback.json"',
                )
            ]
        self.send_json(200, data, headers, write=not head)

    def _read_body(self) -> bytes:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise PayloadError(411, "length_required", "请求必须带 Content-Length")
        try:
            length = int(length_header)
            if length < 0:
                raise ValueError
        except ValueError:
            raise PayloadError(400, "bad_content_length", "Content-Length 不合法")
        if length > MAX_BODY_BYTES:
            # Do not swallow an oversize body on a reusable connection.
            self.close_connection = True
            raise PayloadError(
                413,
                "body_too_large",
                f"请求体超过 {MAX_BODY_BYTES} 字节（64 KiB）上限",
            )
        return self.rfile.read(length)

    def _post_feedback(self):
        try:
            raw = self._read_body()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise PayloadError(400, "invalid_json", "请求体不是合法的 UTF-8 JSON")
            record = validate_feedback_payload(payload, self.server.manifest)
        except PayloadError as exc:
            headers = [("Connection", "close")] if exc.status in (411, 413) else None
            self.send_error_json(exc.status, exc.code, exc.message, headers)
            return

        track_id = record["track_id"]
        lock = self.server.feedback_lock
        try:
            with lock:
                data = load_feedback(
                    self.server.feedback_path, self.server.manifest["id"]
                )
                existing = data["ratings"].get(track_id)
                if existing is not None and (
                    existing.get("version") != record["version"]
                    or existing.get("mix_sha256") != record["mix_sha256"]
                ):
                    raise PayloadError(
                        409,
                        "stale_version",
                        "该曲目已按另一个 version / mix_sha256 保存过反馈；"
                        "新版本反馈需要人工确认后另建记录",
                    )
                if (
                    existing is not None
                    and content_signature(existing) == content_signature(record)
                ):
                    # Exact duplicate submission: idempotent, no mutation.
                    self.send_json(200, existing)
                    return
                record["updated_at"] = utc_now_iso()
                data["ratings"][track_id] = record
                write_feedback_atomic(self.server.feedback_path, data)
        except FeedbackStorageError as exc:
            self.send_error_json(500, "feedback_unreadable", str(exc))
            return
        except PayloadError as exc:
            self.send_error_json(exc.status, exc.code, exc.message)
            return
        except OSError as exc:
            self.send_error_json(
                500,
                "write_failed",
                f"反馈写入失败，原有记录未受影响: {exc}",
            )
            return
        self.send_json(200, record)

    # -- media endpoint -----------------------------------------------------

    def _serve_media(self, method):
        rel_encoded = self.path[len("/media/") :]
        rel_encoded = urlparse(rel_encoded).path
        rel = unquote(rel_encoded)

        base = self.server.collection_dir
        allowlist = self.server.media_allowlist
        normalized = PurePosixPath(rel)
        rel_key = normalized.as_posix() if normalized.parts else ""

        if rel_key not in allowlist or normalized.is_absolute() or any(
            p == ".." for p in normalized.parts
        ):
            self.send_error_json(
                404,
                "media_not_allowed",
                f"未在清单中声明的媒体路径，已拒绝: {rel}",
            )
            return

        candidate = (base / rel_key).resolve(strict=False)
        base_real = base.resolve()
        if not _is_within(candidate, base_real):
            self.send_error_json(
                403,
                "media_escape",
                f"媒体路径通过符号链接等方式逃逸出合集目录，已拒绝: {rel}",
            )
            return
        if not candidate.is_file():
            self.send_error_json(
                404, "media_missing", f"清单声明的文件不存在: {rel}"
            )
            return

        try:
            size = candidate.stat().st_size
        except OSError as exc:
            self.send_error_json(500, "media_stat_failed", str(exc))
            return
        content_type = _media_type_for(candidate)

        range_header = self.headers.get("Range")
        if range_header is None:
            headers = [("Accept-Ranges", "bytes")]
            try:
                with open(candidate, "rb") as handle:
                    body = handle.read()
            except OSError as exc:
                self.send_error_json(500, "media_read_failed", str(exc))
                return
            self._send(200, body, content_type, headers, write=(method == "GET"))
            return

        start, end, error = self._parse_single_range(range_header, size)
        if error is not None:
            self.send_response(416)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return

        length = end - start + 1
        headers = [
            ("Content-Range", f"bytes {start}-{end}/{size}"),
            ("Accept-Ranges", "bytes"),
        ]
        self.send_response(206)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        if method == "HEAD":
            return
        try:
            with open(candidate, "rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except OSError as exc:  # pragma: no cover - client disconnects etc.
            self.log_message("media read error: %s", exc)

    @staticmethod
    def _parse_single_range(header, size):
        """Parse a single bytes range. Returns (start, end_inclusive, error)."""
        spec = header.strip()
        if not spec.startswith("bytes="):
            return None, None, "only bytes ranges supported"
        spec = spec[len("bytes=") :]
        if "," in spec:
            return None, None, "only a single range is supported"
        if spec == "":
            return None, None, "empty range"
        try:
            if spec.startswith("-"):
                suffix = int(spec[1:])
                if suffix <= 0:
                    return None, None, "bad suffix length"
                start = max(0, size - suffix)
                end = size - 1
            elif spec.endswith("-"):
                start = int(spec[:-1])
                end = size - 1
            else:
                left, right = spec.split("-", 1)
                start, end = int(left), int(right)
                if end < start:
                    return None, None, "end before start"
        except ValueError:
            return None, None, "non-integer range"
        if start < 0 or start >= size:
            return None, None, "start outside file"
        if end >= size:
            end = size - 1
        return start, end, None


# ---------------------------------------------------------------------------
# Server factory / CLI
# ---------------------------------------------------------------------------


class AuditionHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(collection_dir, host="127.0.0.1", port=8800) -> AuditionHTTPServer:
    """Build a bounded localhost audition server bound to *collection_dir*."""
    base = Path(collection_dir).resolve(strict=False)
    if not base.is_dir():
        raise ManifestError(f"合集目录不存在或不是目录: {base}")
    manifest = load_manifest(base)
    media_allowlist = build_media_allowlist(manifest)

    server = AuditionHTTPServer((host, port), AuditionHandler)
    server.collection_dir = base
    server.manifest = manifest
    server.media_allowlist = media_allowlist
    server.feedback_path = base / FEEDBACK_FILENAME
    server.feedback_lock = threading.Lock()
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="BeatLab 十首试听 localhost 服务（仅标准库）"
    )
    parser.add_argument("--collection", required=True, help="含 collection.json 的目录")
    parser.add_argument("--port", type=int, default=8800, help="端口（默认 8800）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认本机）")
    args = parser.parse_args(argv)

    server = create_server(args.collection, host=args.host, port=args.port)
    print(
        f"BeatLab 试听服务已启动: http://{args.host}:{args.port}/ （合集目录 {server.collection_dir}）"
    )
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
