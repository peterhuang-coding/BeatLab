"""Standalone local blinded listening artifact and decision API.

Layout produced by build_blind_review:

    out/
      public/
        index.html          # Chinese UI; labels only, no source identities
        A.wav B.wav [C.wav] # 32-bit float, RMS-level-matched audition copies
      private/
        mapping.json        # random label <-> original id/path + level metrics
        decision.json       # one immutable decision (created on first vote)
        callback_state.json # distinct on_decision callback status per request
        .decision.lock      # fcntl exclusive lock for decision writes

Only the standard library, numpy and soundfile are required (scipy is
permitted but not needed). No network, no edit-engine dependency, and the
original audio files are never mutated.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

import numpy as np
import soundfile as sf

LABELS = ("A", "B", "C")
CEILING = 10 ** (-1.0 / 20.0)       # -1 dBFS common peak ceiling
RMS_TOL_DB = 0.05                   # post-encode measured RMS spread
SILENCE_RMS = 1e-5                  # below this RMS a track counts as silent
MAX_BODY = 65536                    # POST body limit in bytes
MAX_NOTES = 64
MAX_TEXT = 1000
MAX_ERROR = 300                     # bounded error text stored in callback state
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
RMS_LABEL = "RMS音量匹配（非LUFS，原版文件不变）"
TECHNIQUE = (
    "equal RMS in dB measured over the identical full interval; "
    "attenuation only; common -1 dBFS peak ceiling; 32-bit float "
    "audition WAVs; RMS level comparison, not LUFS; original files "
    "left untouched"
)
class ReviewError(Exception):
    """Base error; carries the HTTP status the handler should return."""

    status = 400


class ValidationError(ReviewError):
    status = 400


class ConflictError(ReviewError):
    status = 409


def _paths(out: Path) -> dict:
    out = Path(out)
    private = out / "private"
    return {
        "out": out,
        "public": out / "public",
        "private": private,
        "mapping": private / "mapping.json",
        "decision": private / "decision.json",
        "callback": private / "callback_state.json",
        "lock": private / ".decision.lock",
    }


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _db(value: float) -> float:
    return 20.0 * math.log10(value)


def _write_json_atomic(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="." + path.name + ".",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(obj, stream, ensure_ascii=False, indent=2,
                      allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _write_text_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="." + path.name + ".",
                                    dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


@contextmanager
def _decision_lock(out: Path):
    paths = _paths(out)
    paths["private"].mkdir(parents=True, exist_ok=True)
    lock_file = open(paths["lock"], "a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def _load_mapping(out: Path) -> dict:
    path = _paths(out)["mapping"]
    if not path.is_file():
        raise ValidationError(f"不是有效的评审目录: {out}")
    mapping = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict) or "assignment" not in mapping:
        raise ValidationError(f"mapping 已损坏: {out}")
    return mapping


def _read_decision(out: Path):
    path = _paths(out)["decision"]
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_decision(out) -> dict | None:
    """Return the committed decision record, or None when no vote exists."""
    try:
        return _read_decision(Path(out))
    except FileNotFoundError:
        return None


def _normalize_notes(notes, mapping: dict) -> list:
    if notes is None:
        return []
    if not isinstance(notes, list):
        raise ValidationError("notes 必须是数组")
    if len(notes) > MAX_NOTES:
        raise ValidationError(f"notes 数量超出上限 {MAX_NOTES}")
    duration = float(mapping["audio"]["duration_seconds"])
    normalized = []
    for note in notes:
        if not isinstance(note, dict):
            raise ValidationError("每条笔记必须是对象")
        if set(note.keys()) != {"time_seconds", "text"}:
            raise ValidationError("笔记只允许 time_seconds 和 text 字段")
        when = note["time_seconds"]
        if isinstance(when, bool) or not isinstance(when, (int, float)):
            raise ValidationError("time_seconds 必须是数字")
        when = float(when)
        if not math.isfinite(when) or when < 0:
            raise ValidationError("time_seconds 必须是有限且非负的数字")
        if when > duration + 0.05:
            raise ValidationError("time_seconds 超出音频时长")
        text = note["text"]
        if not isinstance(text, str) or not (1 <= len(text) <= MAX_TEXT):
            raise ValidationError(
                f"text 必须是 1 到 {MAX_TEXT} 字的字符串")
        normalized.append({"time_seconds": when, "text": text})
    return normalized


def _validate_payload(mapping: dict, choice, request_id, notes) -> dict:
    labels = tuple(mapping["labels"])
    allowed = set(labels) | {"tie", "neither"}
    if not isinstance(choice, str) or choice not in allowed:
        raise ValidationError(f"choice 无效，允许: {sorted(allowed)}")
    if (not isinstance(request_id, str)
            or not REQUEST_ID_RE.fullmatch(request_id)):
        raise ValidationError(
            "request_id 必须是 1-128 位字母数字及 ._- 字符")
    clean_notes = _normalize_notes(notes, mapping)
    keep = choice in labels
    selected = mapping["assignment"][choice]["id"] if keep else None
    return {
        "request_id": request_id,
        "choice": choice,
        "keep": keep,
        "selected_id": selected,
        "notes": clean_notes,
        "created_at": _utcnow(),
    }


def _reuse_or_conflict(existing: dict, candidate: dict) -> dict:
    if existing.get("request_id") != candidate["request_id"]:
        raise ConflictError("该评审已有不可更改的决定")
    if (existing.get("choice") != candidate["choice"]
            or existing.get("notes") != candidate["notes"]):
        raise ConflictError("同一 request_id 提交了不同内容")
    result = dict(existing)
    result["reused"] = True
    return result


def _commit(out: Path, mapping: dict, choice, request_id, notes):
    """Must run while holding _decision_lock."""
    candidate = _validate_payload(mapping, choice, request_id, notes)
    existing = _read_decision(out)
    if existing is not None:
        return _reuse_or_conflict(existing, candidate)
    record = {key: candidate[key] for key in
              ("request_id", "choice", "keep", "selected_id",
               "notes", "created_at")}
    _write_json_atomic(_paths(out)["decision"], record)
    result = dict(record)
    result["reused"] = False
    return result


def record_decision(out, choice: str, request_id: str,
                    notes: list | None = None) -> dict:
    """Persist the single immutable decision for review directory ``out``.

    Retries with the same request_id and payload return the stored record
    (``reused=True``); a different request_id or a conflicting payload for
    the same request_id raise ConflictError.
    """
    out = Path(out)
    mapping = _load_mapping(out)
    with _decision_lock(out):
        return _commit(out, mapping, choice, request_id, notes)


def _read_and_validate_items(items) -> list:
    if not isinstance(items, list) or not (2 <= len(items) <= 3):
        raise ValidationError("需要 2 到 3 个候选项")
    seen_ids = set()
    prepared = []
    for item in items:
        if not isinstance(item, dict):
            raise ValidationError("每个候选项必须是对象")
        item_id = item.get("id")
        mix_path = item.get("mix_path")
        if not isinstance(item_id, str) or not item_id:
            raise ValidationError("id 必须是非空字符串")
        if item_id in seen_ids:
            raise ValidationError(f"重复的 id: {item_id}")
        seen_ids.add(item_id)
        if not isinstance(mix_path, str):
            raise ValidationError("mix_path 必须是字符串")
        path = Path(mix_path)
        if not path.is_file():
            raise ValidationError(f"mix_path 不是有效文件: {mix_path}")
        try:
            data, sample_rate = sf.read(str(path), always_2d=True)
        except Exception as exc:  # soundfile raises RuntimeError on bad WAVs
            raise ValidationError(f"无法读取 WAV: {mix_path} ({exc})")
        prepared.append({"id": item_id, "path": path,
                         "data": data, "sr": int(sample_rate)})
    ref = prepared[0]
    frames, channels = ref["data"].shape
    if frames <= 0 or channels <= 0:
        raise ValidationError("空音频")
    for entry in prepared[1:]:
        if entry["sr"] != ref["sr"]:
            raise ValidationError(
                f"采样率不一致: {entry['id']} {entry['sr']} != {ref['sr']}")
        if entry["data"].shape != (frames, channels):
            raise ValidationError(
                f"帧数或声道数不一致: {entry['id']}")
    for entry in prepared:
        data = entry["data"]
        if not np.all(np.isfinite(data)):
            raise ValidationError(f"音频含非有限样本: {entry['id']}")
        rms = float(np.sqrt(np.mean(data.astype(np.float64) ** 2)))
        if rms < SILENCE_RMS:
            raise ValidationError(f"音频接近静音: {entry['id']}")
        entry["rms"] = rms
    for i in range(len(prepared)):
        for j in range(i + 1, len(prepared)):
            if np.array_equal(prepared[i]["data"], prepared[j]["data"]):
                raise ValidationError(
                    f"候选项波形完全相同: {prepared[i]['id']} / {prepared[j]['id']}")
    return prepared


def _level_matched_data(prepared: list, order: list):
    """Return (samples per label, gains per source index, metrics)."""
    target_rms = min(entry["rms"] for entry in prepared)
    base_gains = [target_rms / entry["rms"] for entry in prepared]
    max_new_peak = max(
        float(np.max(np.abs(entry["data"]))) * gain
        for entry, gain in zip(prepared, base_gains))
    common = (min(1.0, CEILING / max_new_peak)
              if max_new_peak > CEILING else 1.0)
    samples, metrics = [], {}
    for source_index in order:
        entry = prepared[source_index]
        gain = base_gains[source_index] * common
        samples.append(entry["data"].astype(np.float64) * gain)
        metrics[source_index] = {
            "original_rms_db": _db(entry["rms"]),
            "gain_db": _db(gain),
            "base_gain_db": _db(base_gains[source_index]),
        }
    return samples, common, target_rms, metrics


def _measure_public(labels: tuple, public: Path) -> dict:
    measured = {}
    for label in labels:
        path = public / f"{label}.wav"
        data, sample_rate = sf.read(str(path), always_2d=True)
        info = sf.info(str(path))
        if info.subtype != "FLOAT":
            raise ValidationError(f"试听文件不是 FLOAT WAV: {path}")
        if not np.all(np.isfinite(data)):
            raise ValidationError(f"试听文件含非有限样本: {path}")
        rms = float(np.sqrt(np.mean(data.astype(np.float64) ** 2)))
        if rms < SILENCE_RMS:
            raise ValidationError(f"试听文件接近静音: {path}")
        peak = float(np.max(np.abs(data)))
        if peak > CEILING + 1e-6:
            raise ValidationError(f"试听文件超过电平上限: {path}")
        measured[label] = {"rms": rms, "peak": peak,
                           "sr": sample_rate, "shape": data.shape}
    rms_values = [m["rms"] for m in measured.values()]
    spread = _db(max(rms_values)) - _db(min(rms_values))
    if spread > RMS_TOL_DB:
        raise ValidationError(
            f"编码后 RMS 相差 {spread:.3f} dB，超过 {RMS_TOL_DB} dB")
    return measured


def build_blind_review(items: list[dict], out) -> dict:
    """Create the blinded review directory; never overwrite an existing one."""
    out = Path(out)
    prepared = _read_and_validate_items(items)
    if out.exists():
        raise FileExistsError(f"评审目录已存在，拒绝覆盖: {out}")
    if not out.parent.exists():
        raise ValidationError(f"父目录不存在: {out.parent}")

    n = len(prepared)
    labels = LABELS[:n]
    order = list(range(n))
    secrets.SystemRandom().shuffle(order)
    samples, common, target_rms, gain_metrics = _level_matched_data(
        prepared, order)

    ref_sr = prepared[0]["sr"]
    frames, channels = prepared[0]["data"].shape
    staging = Path(tempfile.mkdtemp(prefix=".blind-review-",
                                    dir=str(out.parent)))
    try:
        public = staging / "public"
        private = staging / "private"
        public.mkdir()
        private.mkdir()
        for label, data in zip(labels, samples):
            sf.write(str(public / f"{label}.wav"), data, ref_sr,
                     subtype="FLOAT")
        measured = _measure_public(labels, public)

        # sf.write creates fresh sample-only WAVs; no original metadata is
        # copied. Raw sample bytes can contain arbitrary text by coincidence,
        # so searching those bytes for IDs would reject valid short IDs.

        per_label_metrics = {}
        assignment = {}
        for k, label in enumerate(labels):
            source_index = order[k]
            entry = prepared[source_index]
            assignment[label] = {"id": entry["id"],
                                 "mix_path": str(entry["path"])}
            per_label_metrics[label] = {
                "original_rms_db": gain_metrics[source_index]["original_rms_db"],
                "gain_db": gain_metrics[source_index]["gain_db"],
                "rms_db_after": _db(measured[label]["rms"]),
                "peak_after": measured[label]["peak"],
            }
        mapping = {
            "version": 1,
            "created_at": _utcnow(),
            "labels": list(labels),
            "assignment": assignment,
            "audio": {
                "sample_rate": ref_sr,
                "frames": int(frames),
                "channels": int(channels),
                "duration_seconds": frames / ref_sr,
            },
            "level_match": {
                "technique": TECHNIQUE,
                "ceiling": CEILING,
                "target_rms": target_rms,
                "common_gain_db": _db(common),
                "rms_spread_db": _db(max(m["rms"] for m in measured.values()))
                                  - _db(min(m["rms"] for m in measured.values())),
                "metrics": per_label_metrics,
            },
        }
        _write_text_atomic(private / "mapping.json",
                           json.dumps(mapping, ensure_ascii=False, indent=2,
                                      allow_nan=False) + "\n")
        _write_text_atomic(public / "index.html",
                           _render_index(labels, frames / ref_sr))
        os.rename(str(staging), str(out))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    matching = {
        "technique": TECHNIQUE,
        "target_rms_db": _db(target_rms),
        "common_gain_db": _db(common),
        "labels": per_label_metrics,
    }
    return {"review_dir": str(out), "labels": list(labels),
            "matching": matching}


PAGE_CSS = """\
*{box-sizing:border-box}
body{background:#181c21;color:#e8e5de;font:16px/1.6 system-ui,-apple-system,sans-serif;\
max-width:760px;margin:0 auto;padding:20px}
h1{font-size:26px}h2{font-size:20px}
audio{display:block;width:100%;margin:14px 0}
.row{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0}
button{font:inherit;padding:10px 14px;background:#ece6d8;color:#181c21;border:0;\
border-radius:6px;cursor:pointer}
button:disabled{opacity:.55;cursor:default}
button.keep{background:#eac578}
.switch button.active{outline:2px solid #eac578}
input,textarea{font:inherit;padding:8px;background:#ece6d8;color:#181c21;border:0;\
border-radius:6px;width:100%}
#note-time{width:120px}
textarea{resize:vertical;min-height:64px}
small,hint,.hint{color:#b8b6ae}
#status{min-height:22px}#status.err{color:#e08a8a}#status.ok{color:#9adbb8}
.err{color:#e08a8a}
@media (max-width:520px){body{padding:14px}button{padding:10px 12px}}"""

PAGE_JS = r"""function $(id){return document.getElementById(id);}
function newRequestId(){
  if(window.crypto&&typeof crypto.randomUUID==='function'){
    try{return crypto.randomUUID();}catch(e){}
  }
  return 'rn-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,10);
}
const LABELS=CFG.labels;
const player=$('player');
let switchGen=0;
let pendingSwitch=null;
function clampTime(t,d){
  t=Number(t);
  if(!Number.isFinite(t)||t<0){t=0;}
  if(Number.isFinite(d)){t=Math.min(t,d);}
  return t;
}
function switchTo(label){
  const my=++switchGen;
  const previous=pendingSwitch||{playing:!player.paused,
    time:Number.isFinite(player.currentTime)?player.currentTime:0};
  pendingSwitch=previous;
  const wasPlaying=previous.playing;
  const t=previous.time;
  player.pause();
  return new Promise(function(resolve){
    function done(event){
      player.removeEventListener('loadedmetadata',done);
      player.removeEventListener('error',done);
      resolve(event.type==='loadedmetadata');
    }
    player.addEventListener('loadedmetadata',done,{once:true});
    player.addEventListener('error',done,{once:true});
    player.src=encodeURIComponent(label)+'.wav';
  }).then(function(loaded){
    if(my!==switchGen)return;
    pendingSwitch=null;
    if(!loaded)return;
    try{player.currentTime=clampTime(t,player.duration);}catch(e){}
    if(wasPlaying){
      return Promise.resolve(player.play()).catch(function(){});
    }
  }).then(function(){if(my!==switchGen)return;});
}
let requestId=newRequestId();
const statusEl=$('status');
function setStatus(msg,isError){
  statusEl.textContent=msg;
  statusEl.className=isError?'err':'ok';
}
function setBusy(busy){
  document.querySelectorAll('[data-choice]').forEach(function(x){x.disabled=busy;});
}
function submitVote(choice){
  const body={choice:choice,request_id:requestId};
  const text=$('note-text').value.trim();
  if(text){
    body.notes=[{time_seconds:clampTime($('note-time').value,player.duration),text:text}];
  }
  setBusy(true);
  fetch('/api/decision',{method:'POST',
    headers:{'Content-Type':'application/json'},cache:'no-store',
    body:JSON.stringify(body)})
  .then(function(r){
    return r.json().catch(function(){return {};}).then(function(data){
      return {ok:r.ok,status:r.status,data:data};
    });
  })
  .then(function(x){
    if(!x.ok){
      if(x.status===409){requestId=newRequestId();}
      throw new Error(x.data.error||('HTTP '+x.status));
    }
    requestId=newRequestId();
    renderCommitted(x.data);
  })
  .catch(function(e){setStatus('提交失败，可保留原 request_id 重试：'+e.message,true);})
  .finally(function(){setBusy(false);});
}
function renderCommitted(data){
  $('vote').hidden=true;
  const box=$('result');box.hidden=false;box.textContent='';
  let line;
  if(data.keep){
    line='已提交：保留 '+data.choice+'（原始标识 '+String(data.selected_id)+'）。';
  }else if(data.choice==='tie'){
    line='已提交：平局，不保留任何版本。';
  }else{
    line='已提交：都不要，不保留任何版本。';
  }
  const p=document.createElement('p');p.textContent=line;box.appendChild(p);
  if(data.callback&&data.callback.status==='failed'){
    const w=document.createElement('p');w.className='err';
    w.textContent='决定已保存；提交后的回调失败（'+String(data.callback.error)+
      '）。可在命令行用同一请求重试恢复，无需重复投票。';
    box.appendChild(w);
  }
}
document.querySelectorAll('[data-label]').forEach(function(b){
  b.addEventListener('click',function(){
    document.querySelectorAll('[data-label]').forEach(function(x){
      x.classList.toggle('active',x===b);
    });
    switchTo(b.dataset.label);
  });
});
document.querySelectorAll('[data-choice]').forEach(function(b){
  b.addEventListener('click',function(){submitVote(b.dataset.choice);});
});
$('capture').addEventListener('click',function(){
  $('note-time').value=clampTime(player.currentTime,player.duration).toFixed(2);
});
fetch('/api/decision',{cache:'no-store'})
  .then(function(r){return r.json().catch(function(){return {};});})
  .then(function(data){if(data&&data.voted){renderCommitted(data);}})
  .catch(function(){});"""


def _render_index(labels, duration: float) -> str:
    switch_buttons = "".join(
        f'<button type="button" data-label="{label}">试听 {label}</button>'
        for label in labels)
    keep_buttons = "".join(
        f'<button type="button" class="keep" data-choice="{label}">'
        f'保留{label}</button>' for label in labels)
    cfg = json.dumps({"labels": list(labels)},
                     ensure_ascii=False).replace("<", "\\u003c")
    duration_str = f"{float(duration):.2f}"
    return f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>盲听比较 · BeatLab</title>
<style>{PAGE_CSS}</style>
<h1>盲听比较</h1>
<p class="hint">{RMS_LABEL}。以下试听仅做 RMS 电平对齐，方便公平比较；系统不会自动保留任何版本，决定权在你。</p>
<div class="row switch" role="group" aria-label="切换试听版本">{switch_buttons}</div>
<audio id="player" controls preload="auto" src="A.wav"></audio>
<section id="vote">
<h2>你的决定</h2>
<p class="hint">请只凭听感选择（试听时长约 {duration_str} 秒）。</p>
<div class="row" role="group" aria-label="保留决定">
{keep_buttons}
<button type="button" data-choice="tie">平局</button>
<button type="button" data-choice="neither">都不要</button>
</div>
<div class="row">
<label for="note-time">笔记时间（秒）</label>
<input id="note-time" type="number" min="0" step="0.01" inputmode="decimal" value="0">
<button type="button" id="capture">抓取当前位置</button>
</div>
<textarea id="note-text" maxlength="1000" placeholder="可选：写一句听感笔记（最多 1000 字）"></textarea>
<p id="status" role="status" aria-live="polite"></p>
</section>
<section id="result" hidden></section>
<script>const CFG={cfg};</script>
<script>{PAGE_JS}</script>
</html>
"""


def _reject_constant(value: str):
    # json passes NaN / Infinity / -Infinity here; refuse them on input.
    raise ValueError(f"非法数值: {value}")


def _same_origin(origin: str, host: str | None) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme != "http" or not parsed.netloc:
        return False
    return parsed.netloc.lower() == (host or "").lower()


def _read_callback_state(out: Path) -> dict:
    path = _paths(out)["callback"]
    if not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return state if isinstance(state, dict) else {}


def _run_callback(out: Path, record: dict, on_decision) -> dict:
    """Invoke the callback; a failure never undoes the committed vote.

    Runs while the decision lock is held, so concurrent identical retries
    serialize. A previously successful callback is not repeated; a failed
    one is retried on identical POST (CLI recovery without re-voting).
    """
    state = _read_callback_state(out)
    key = record["request_id"]
    previous = state.get(key)
    if isinstance(previous, dict) and previous.get("status") == "ok":
        return {"status": "ok"}
    decision = {k: v for k, v in record.items() if k != "reused"}
    try:
        on_decision(decision)
    except Exception as exc:  # surface failure distinctly, never raise
        result = {"status": "failed",
                  "error": str(exc)[:MAX_ERROR] or exc.__class__.__name__}
    else:
        result = {"status": "ok"}
    state[key] = result
    try:
        _write_json_atomic(_paths(out)["callback"], state)
    except Exception as exc:
        return {"status": "failed",
                "error": ("回调状态无法记录: " + str(exc))[:MAX_ERROR]}
    return result


def make_handler(out, on_decision=None):
    """Return a configured BaseHTTPRequestHandler subclass for review dir."""
    root = Path(out)
    if on_decision is not None and not callable(on_decision):
        raise ValidationError("on_decision 必须是可调用对象")
    mapping = _load_mapping(root)
    labels = tuple(mapping["labels"])
    wav_names = {f"{label}.wav" for label in labels}

    class BlindReviewHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args, **kwargs):
            # Suppress per-request stderr logging from the local tool.
            return

        # -- response helpers --------------------------------------------

        def _send_bytes(self, status, content_type, body: bytes,
                        extra_headers=()):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in extra_headers:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_json(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False,
                              allow_nan=False).encode("utf-8")
            self._send_bytes(status, "application/json; charset=utf-8", body,
                             (("Cache-Control", "no-store"),))

        def _not_found(self):
            self._send_json(404, {"error": "not found"})

        # -- GET / HEAD ----------------------------------------------------

        def do_GET(self):
            self._serve()

        def do_HEAD(self):
            self._serve()

        def _serve(self):
            try:
                path = urlsplit(self.path).path
                if path in ("/", "/index.html"):
                    body = (_paths(root)["public"] / "index.html").read_bytes()
                    self._send_bytes(200, "text/html; charset=utf-8", body,
                                     (("Cache-Control", "no-store"),))
                    return
                if path == "/api/decision":
                    self._serve_state()
                    return
                # Literal generated names only: no decoding, no joining,
                # no traversal, no private directory.
                name = path[1:] if path.startswith("/") else path
                if "/" not in name and name in wav_names:
                    self._serve_wav(name)
                    return
                self._not_found()
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                self._send_json(500, {"error": "internal error"})

        def _serve_state(self):
            record = _read_decision(root)
            if record is None:
                self._send_json(200, {"voted": False})
                return
            self._send_json(200, {
                "voted": True,
                "request_id": record["request_id"],
                "choice": record["choice"],
                "keep": record["keep"],
                "selected_id": record["selected_id"],
                "notes": record["notes"],
                "created_at": record["created_at"],
            })

        def _serve_wav(self, name: str):
            data = (_paths(root)["public"] / name).read_bytes()
            size = len(data)
            common = (("Accept-Ranges", "bytes"),)
            range_header = self.headers.get("Range")
            if range_header and "," not in range_header:
                match = RANGE_RE.fullmatch(range_header.strip())
                if match:
                    start_text, end_text = match.groups()
                    start = end = None
                    if start_text == "" and end_text == "":
                        pass  # "bytes=-" is malformed: ignore, send full
                    elif start_text == "":
                        suffix = int(end_text)
                        if suffix > 0:
                            start = max(0, size - suffix)
                            end = size - 1
                    elif end_text == "":
                        start = int(start_text)
                        end = size - 1
                    else:
                        start = int(start_text)
                        end = int(end_text)
                    if start is not None:
                        end = min(end, size - 1)
                        if start >= size or start > end or start < 0:
                            self._send_bytes(
                                416, "audio/wav", b"",
                                (("Content-Range", f"bytes */{size}"),
                                 ("Accept-Ranges", "bytes")))
                            return
                        chunk = data[start:end + 1]
                        self._send_bytes(
                            206, "audio/wav", chunk,
                            (("Content-Range",
                              f"bytes {start}-{end}/{size}"),
                             ("Accept-Ranges", "bytes")))
                        return
            self._send_bytes(200, "audio/wav", data, common)

        # -- POST ----------------------------------------------------------

        def do_POST(self):
            try:
                path = urlsplit(self.path).path
                if path != "/api/decision":
                    self._not_found()
                    return
                origin = self.headers.get("Origin")
                if origin is not None and not _same_origin(
                        origin, self.headers.get("Host")):
                    self._send_json(403,
                                    {"error": "拒绝跨来源请求"})
                    return
                length_text = self.headers.get("Content-Length")
                try:
                    length = int(length_text)
                except (TypeError, ValueError):
                    self.send_response(411)
                    self.send_header("Content-Type",
                                     "application/json; charset=utf-8")
                    self.send_header("Content-Length", "2")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    if self.command != "HEAD":
                        self.wfile.write(b"{}")
                    return
                if length < 0 or length > MAX_BODY:
                    self.send_response(413)
                    self.send_header("Content-Type",
                                     "application/json; charset=utf-8")
                    self.send_header("Content-Length", "2")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    self.wfile.write(b"{}")
                    return
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw.decode("utf-8"),
                                        parse_constant=_reject_constant)
                except (ValueError, UnicodeDecodeError):
                    self._send_json(400, {"error": "请求不是有效 JSON"})
                    return
                if not isinstance(payload, dict):
                    self._send_json(400,
                                    {"error": "请求必须是 JSON 对象"})
                    return
                with _decision_lock(root):
                    record = _commit(
                        root, mapping,
                        payload.get("choice"),
                        payload.get("request_id"),
                        payload.get("notes"))
                    callback_status = (
                        _run_callback(root, record, on_decision)
                        if on_decision is not None else None)
                response = dict(record)
                if callback_status is not None:
                    response["callback"] = callback_status
                self._send_json(200, response)
            except ReviewError as exc:
                self._send_json(exc.status, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                self._send_json(500, {"error": "internal error"})

    return BlindReviewHandler
