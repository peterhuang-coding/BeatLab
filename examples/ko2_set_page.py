#!/usr/bin/env python3
"""Offline static KO II set page generator.

Usage:
    python examples/ko2_set_page.py SET_DIR

Reads ``SET_DIR/set.json`` (schema ``beatlab.ko2-set.v1``) and writes
``SET_DIR/index.html``. The page is fully self contained: inline CSS/JS,
no CDN/network access, relative media URLs only, works from file:// or a
plain static HTTP server. An existing index.html is never overwritten.
"""

import argparse
import hashlib
import json
import math
import re
import sys
from html import escape
from pathlib import Path
from urllib.parse import quote

SCHEMA = "beatlab.ko2-set.v1"

GROUPS = "ABCD"
# MIDI note base per group, per official EP-133 mapping.
GROUP_BASES = {"A": 36, "B": 48, "C": 60, "D": 72}
# Slot order within a group: ".", "0", "ENTER", "1".."9".
SLOT_LABELS = [".", "0", "ENTER", "1", "2", "3", "4", "5", "6", "7", "8", "9"]
# Physical 4x3 pad layout, top row first: [7,8,9],[4,5,6],[1,2,3],[.,0,ENTER].
PHYSICAL_SLOTS = [9, 10, 11, 6, 7, 8, 3, 4, 5, 0, 1, 2]

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_SET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

OFFICIAL_TOOL_URL = (
    "https://support.teenage.engineering/hc/en-us/articles/"
    "11639171890204-how-to-use-the-EP-sample-tool"
)
OFFICIAL_SYSTEM_URL = "https://teenage.engineering/guides/ep-133/system"


def h(value):
    """HTML-escape any value for use in text or double-quoted attributes."""
    return escape(str(value), quote=True)


def safe_relpath(value, what):
    """Reject anything that is not a simple within-tree relative path.

    No absolute paths, no schemes (http/file/data/...), no '..' or empty
    segments, no backslashes, no percent/sharp/question characters (which
    could encode traversal or change URL resolution in a browser).
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what}: expected a non-empty relative path string")
    if any(ch in value for ch in ("\\", "\x00", "%", "#", "?")):
        raise ValueError(f"{what}: unsafe character in path {value!r}")
    if value.startswith("/") or _SCHEME_RE.match(value):
        raise ValueError(f"{what}: only relative paths are allowed: {value!r}")
    parts = value.split("/")
    if any(seg == "" or seg == ".." for seg in parts):
        raise ValueError(f"{what}: empty or '..' segments not allowed: {value!r}")
    return value


def _safe_set_id(value):
    if not isinstance(value, str) or not _SET_ID_RE.match(value) or ".." in value:
        raise ValueError(f"set_id: unsafe value {value!r}")
    return value


def _number(value, what, *, integer=False, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what}: expected a number, got {type(value).__name__}")
    if not math.isfinite(float(value)):
        raise ValueError(f"{what}: must be finite")
    if integer and not isinstance(value, int):
        raise ValueError(f"{what}: expected an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{what}: must be >= {minimum}")
    return value


def _str(value, what):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what}: expected a non-empty string")
    return value


def validate_manifest(raw):
    """Validate the manifest and return a normalized dict or raise ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("manifest: expected a JSON object")
    if raw.get("schema") != SCHEMA:
        raise ValueError(f"schema: expected {SCHEMA!r}, got {raw.get('schema')!r}")

    set_id = _safe_set_id(raw.get("set_id"))
    title = _str(raw.get("title"), "title")
    bpm = _number(raw.get("bpm"), "bpm", minimum=1)
    bars = _number(raw.get("bars"), "bars", integer=True, minimum=1)
    parent_version = raw.get("parent_version", "")
    if not isinstance(parent_version, str):
        raise ValueError("parent_version: expected a string")
    hardware_verified = raw.get("hardware_verified", False)
    if not isinstance(hardware_verified, bool):
        raise ValueError("hardware_verified: expected true/false")
    listening_review = raw.get("listening_review", "")
    if not isinstance(listening_review, str):
        raise ValueError("listening_review: expected a string")

    pads_raw = raw.get("pads")
    if not isinstance(pads_raw, list):
        raise ValueError("pads: expected a list")

    pads = {}
    pad_ids = set()
    for i, pad in enumerate(pads_raw):
        if not isinstance(pad, dict):
            raise ValueError(f"pads[{i}]: expected an object")
        where = f"pads[{i}] ({pad.get('id', '?')})"
        pad_id = _str(pad.get("id"), f"{where}.id")
        if pad_id in pad_ids:
            raise ValueError(f"pads: duplicate id {pad_id!r}")
        pad_ids.add(pad_id)

        group = pad.get("group")
        if group not in GROUP_BASES:
            raise ValueError(f"{where}.group: must be one of A/B/C/D")
        slot = _number(pad.get("slot"), f"{where}.slot", integer=True)
        if not 0 <= slot <= 11:
            raise ValueError(f"{where}.slot: must be 0..11")
        if (group, slot) in pads:
            raise ValueError(f"pads: duplicate assignment {group} slot {slot}")

        pad_label = pad.get("pad_label")
        if pad_label != SLOT_LABELS[slot]:
            raise ValueError(
                f"{where}.pad_label: expected {SLOT_LABELS[slot]!r} for slot {slot}"
            )
        midi_note = _number(pad.get("midi_note"), f"{where}.midi_note", integer=True)
        if midi_note != GROUP_BASES[group] + slot:
            raise ValueError(
                f"{where}.midi_note: expected {GROUP_BASES[group] + slot} "
                f"(group {group} base {GROUP_BASES[group]} + slot {slot})"
            )

        label = _str(pad.get("label"), f"{where}.label")
        file_path = safe_relpath(pad.get("file"), f"{where}.file")

        provenance = pad.get("provenance")
        if provenance is not None:
            if not isinstance(provenance, dict):
                raise ValueError(f"{where}.provenance: expected an object")
            for key, val in provenance.items():
                if not isinstance(key, str) or not isinstance(
                    val, (str, int, float, bool)
                ):
                    raise ValueError(f"{where}.provenance.{key}: unsupported value")
        provenance = provenance or {}

        duration = pad.get("duration_seconds")
        if duration is not None:
            duration = _number(duration, f"{where}.duration_seconds", minimum=0)
        gain = pad.get("gain_db")
        if gain is not None:
            gain = _number(gain, f"{where}.gain_db")

        pads[(group, slot)] = {
            "id": pad_id,
            "group": group,
            "slot": slot,
            "pad_label": pad_label,
            "midi_note": midi_note,
            "label": label,
            "file": file_path,
            "sha256": str(pad.get("sha256", "") or ""),
            "duration_seconds": duration,
            "gain_db": gain,
            "provenance": {str(k): str(v) for k, v in provenance.items()},
        }

    demo_raw = raw.get("demo")
    if not isinstance(demo_raw, dict):
        raise ValueError("demo: expected an object")
    demo = {
        "file": safe_relpath(demo_raw.get("file"), "demo.file"),
        "sha256": str(demo_raw.get("sha256", "") or ""),
        "duration_seconds": demo_raw.get("duration_seconds"),
        "gain_db": demo_raw.get("gain_db"),
    }

    midi = None
    midi_raw = raw.get("midi")
    if midi_raw is not None:
        if not isinstance(midi_raw, dict):
            raise ValueError("midi: expected an object")
        midi = {
            "file": safe_relpath(midi_raw.get("file"), "midi.file"),
            "sha256": str(midi_raw.get("sha256", "") or ""),
        }

    scenes = []
    for i, scene in enumerate(raw.get("scenes", []) or []):
        if not isinstance(scene, dict):
            raise ValueError(f"scenes[{i}]: expected an object")
        scenes.append(
            {
                "name": _str(scene.get("name"), f"scenes[{i}].name"),
                "start_bar": _number(
                    scene.get("start_bar"), f"scenes[{i}].start_bar",
                    integer=True, minimum=0,
                ),
                "bars": _number(
                    scene.get("bars"), f"scenes[{i}].bars",
                    integer=True, minimum=1,
                ),
                "instruction": _str(
                    scene.get("instruction"), f"scenes[{i}].instruction"
                ),
            }
        )

    return {
        "set_id": set_id,
        "title": title,
        "bpm": bpm,
        "bars": bars,
        "parent_version": parent_version,
        "hardware_verified": hardware_verified,
        "listening_review": listening_review,
        "pads": pads,
        "demo": demo,
        "midi": midi,
        "scenes": scenes,
    }


CSS = """
:root{--bg:#1b1510;--card:#261e16;--line:#3d3023;--text:#f1e7d8;--muted:#b7a58d;
--accent:#e2a462;--accent-dim:#b5793d;--cell:#2f251b;--empty:#28201a;--danger:#d97757;}
*{box-sizing:border-box}
body{margin:0;padding:18px;background:var(--bg);color:var(--text);
font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
line-height:1.55;font-size:15px}
main{max-width:1080px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
h1{margin:0 0 6px;font-size:1.5rem}
h2{margin:0 0 10px;font-size:1.15rem;color:var(--accent)}
h3{margin:0 0 8px;font-size:1rem}
p{margin:6px 0}
.meta{display:flex;flex-wrap:wrap;gap:10px;align-items:center;color:var(--muted)}
.bpm{font-size:1.35rem;font-weight:700;color:var(--accent)}
.badge{border:1px solid var(--accent-dim);border-radius:999px;padding:2px 10px;
font-size:.8rem;color:var(--accent)}
.badge.off{border-color:var(--danger);color:var(--danger)}
.note{color:var(--muted);font-size:.86rem}
a{color:var(--accent);word-break:break-all}
a:hover{color:#f0bd84}
.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,460px),1fr));gap:12px}
.panel{border:1px solid var(--line);border-radius:12px;padding:10px;background:#221a13}
.panel-head{display:flex;justify-content:space-between;align-items:baseline;gap:6px}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin-top:8px}
.pad{min-width:0;overflow-wrap:anywhere;border:1px solid var(--line);border-radius:10px;padding:6px;min-height:92px;
background:var(--cell);display:flex;flex-direction:column;gap:3px}
.pad.playing{border-color:var(--accent);box-shadow:0 0 0 2px rgba(226,164,98,.35)}
.pad.empty{background:var(--empty);color:var(--muted);justify-content:space-between}
.key{display:inline-block;min-width:1.4em;font-weight:700;color:var(--accent);
border:1px solid var(--accent-dim);border-radius:6px;padding:0 4px;text-align:center;
font-size:.8rem;align-self:flex-start}
.empty .key{color:var(--muted);border-color:var(--line)}
.emptylabel{font-size:.75rem;align-self:flex-end}
button.play{appearance:none;background:none;border:0;padding:0;color:inherit;
font:inherit;text-align:left;cursor:pointer;display:flex;flex-direction:column;gap:2px}
.plabel{font-weight:600}
.hint{font-size:.72rem;color:var(--accent-dim)}
.pmeta,.dl,.prov{font-size:.72rem;color:var(--muted)}
.dl{display:block}
.prov{border-top:1px dashed var(--line);padding-top:2px}
audio{width:100%;margin:6px 0}
ol,ul{margin:6px 0;padding-left:1.25em}
li{margin:4px 0}
code{background:#1c150f;border:1px solid var(--line);border-radius:4px;padding:0 4px}
footer{color:var(--muted);font-size:.78rem;text-align:center}
"""

JS = """
(function(){
  var current=null, currentPad=null;
  function stop(){
    if(current){current.pause(); current.src=''; current=null;}
    if(currentPad){currentPad.classList.remove('playing'); currentPad=null;}
  }
  document.addEventListener('click', function(e){
    var btn=e.target.closest ? e.target.closest('button.play') : null;
    if(!btn) return;
    var pad=btn.parentNode;
    if(currentPad===pad){stop(); return;}
    stop();
    current=new Audio(btn.getAttribute('data-src'));
    currentPad=pad;
    pad.classList.add('playing');
    var selected=current;
    function stopSelected(){if(current===selected){stop();}}
    current.addEventListener('ended', stopSelected);
    current.addEventListener('error', stopSelected);
    current.play()['catch'](stopSelected);
  });
})();
"""


def _render_cell(group, slot, pad):
    key = SLOT_LABELS[slot]
    midi = GROUP_BASES[group] + slot
    attr = (
        f'<div class="pad empty" data-slot="{slot}" data-key="{h(key)}" '
        f'data-midi="{midi}" data-assigned="0">'
        f'<span class="key">{h(key)}</span>'
        f'<span class="emptylabel">空</span></div>'
    )
    if pad is None:
        return attr

    bits = [
        f'<div class="pad assigned" data-slot="{slot}" data-key="{h(key)}" '
        f'data-midi="{midi}" data-assigned="1">',
        f'<button type="button" class="play" data-src="{h(pad["file"])}">'
        f'<span class="key">{h(key)}</span>'
        f'<span class="plabel">{h(pad["label"])}</span>'
        f'<span class="hint">▶ 试听（同时仅一个预览）</span></button>',
    ]
    meta = [f"MIDI {midi}"]
    if pad["duration_seconds"] is not None:
        meta.append(f'{pad["duration_seconds"]:.2f}s')
    if pad["gain_db"] is not None:
        meta.append(f'{pad["gain_db"]:.1f} dB')
    bits.append(f'<span class="pmeta">{h(" · ".join(meta))}</span>')
    bits.append(
        f'<a class="dl" href="{h(pad["file"])}" download>{h(pad["file"])}</a>'
    )
    prov = pad["provenance"]
    if prov:
        note = prov.get("note", "")
        source = prov.get("source_url", "")
        summary = " · ".join(v for v in (prov.get("kind", ""), note) if v)
        title_bits = [v for v in (f"source: {source}" if source else "", note) if v]
        bits.append(
            f'<details class="prov" title="{h(" | ".join(title_bits))}"><summary>来源</summary>'
            f"{h(summary)}</details>"
        )
    bits.append("</div>")
    return "".join(bits)


def _render_panel(group, pads):
    rows = "".join(_render_cell(group, slot, pads.get((group, slot)))
                   for slot in PHYSICAL_SLOTS)
    return (
        f'<div class="panel" data-group="{group}">'
        f'<div class="panel-head"><h3>{group} 组 · MIDI {GROUP_BASES[group]}–'
        f'{GROUP_BASES[group] + 11}</h3>'
        f'<a href="{group}.zip" download>{group}.zip</a></div>'
        f'<div class="grid">{rows}</div>'
        f"</div>"
    )


def _feedback_href(set_id, manifest_sha256=None):
    template = {
        "set_id": set_id,
        "manifest_sha256": manifest_sha256,
        "recording": "30-60 秒弹奏录音（文件名或回传链接）",
        "feedback": [
            {
                "pad_id": "例如 kick，或填 A:0",
                "time": "例如 0:12（可选）",
                "decision": "keep 或 change",
                "comment": "",
            }
        ],
    }
    body = json.dumps(template, ensure_ascii=False, indent=2)
    return "data:application/json;charset=utf-8," + quote(body, safe="")


def render_html(data):
    pads = data["pads"]
    panels = "".join(_render_panel(g, pads) for g in GROUPS)

    demo = data["demo"]
    demo_block = (
        f'<audio controls preload="none" src="{h(demo["file"])}"></audio>'
        f'<p class="meta"><a href="{h(demo["file"])}" download>{h(demo["file"])}</a>'
    )
    if data["midi"]:
        midi = data["midi"]
        demo_block += f' · <a href="{h(midi["file"])}" download>{h(midi["file"])}</a>'
    demo_block += "</p>"

    if data["scenes"]:
        scene_items = "".join(
            f'<li><strong>{h(s["name"])}</strong>：第 {s["start_bar"] + 1}–'
            f'{s["start_bar"] + s["bars"]} 小节（共 {s["bars"]} 小节）'
            f'— {h(s["instruction"])}</li>'
            for s in data["scenes"]
        )
    else:
        scene_items = "<li>（无）</li>"

    steps = [
        "用 USB 连接 KO II，在桌面版 <strong>Chrome 或 Edge</strong>"
        "（支持 Web MIDI）打开官方 EP sample tool。",
        "先在工具中<strong>备份当前所选项目</strong>，再选一个未使用的空项目作为导入目标。"
        "不要执行“删除全部/恢复出厂设置”，也不要依赖并不存在的项目备份。",
        "对 A、B、C、D 每组分别操作：在工具中激活对应组，把该组列出的 WAV 拖入"
        "（每组 ≤ 12 个文件）。",
        "拖入后务必核对工具中<strong>红色虚线框</strong>标出的受影响垫："
        "多文件的分配顺序不保证与文件名一致，请对照本页垫位图逐垫人工确认。",
        "组内是稀疏排列，没有采样的垫保持空位；落错槽位的采样必须人工拖改到正确的垫。",
        "采样会自动上传到设备中<strong>空闲的音色槽</strong>；文件名不会预留、"
        "也不会预定音色槽位号，不能按文件名推断落点。",
        "在工程里设置本页 BPM，把相关垫设为 <strong>one-shot</strong>，"
        "音量先调到偏低的稳妥水平再试。",
        "硬件上的播放、保存、重新打开均<strong>未验证</strong>，请自行检查确认。",
    ]
    steps_html = "".join(f"<li>{s}</li>" for s in steps)

    feedback_href = h(_feedback_href(data["set_id"], data.get("manifest_sha256")))

    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{h(data['title'])} · KO II set</title>\n"
        f"<style>{CSS}</style>\n</head>\n<body>\n<main>\n"
        '<section class="card">'
        f"<h1>{h(data['title'])}</h1>"
        '<div class="meta">'
        f'<span class="bpm">♩ = {h(data["bpm"])} BPM</span>'
        f'<span>{h(data["bars"])} 小节</span>'
        f'<span>set_id: <code>{h(data["set_id"])}</code></span>'
        '<span class="badge off">硬件未验证（hardware_verified = false）</span>'
        f'<span class="badge">听音复核：{h(data["listening_review"] or "n/a")}</span>'
        "</div>"
        '<p class="note">本页是 WAV + 垫位映射的离线练习页，'
        "不是 KO II 原生 .pak，不会向设备写入或自动安装任何内容；"
        "下载 ZIP 后解压，按分组手动导入。</p>"
        "</section>\n"
        '<section class="card">'
        "<h2>示范音频</h2>"
        f"{demo_block}"
        "</section>\n"
        '<section class="card">'
        "<h2>分组垫位图（A–D，物理 4×3）</h2>"
        '<p class="note">点击有采样的垫可试听，同一时间只有一个预览；'
        "点击文件名可单独下载该 WAV。每组的分组 ZIP 与整套 "
        f'<a href="../{h(data["set_id"])}.zip" download>'
        f'../{h(data["set_id"])}.zip</a> 下载包需要解压后手动导入。</p>'
        f'<div class="panels">{panels}</div>'
        "</section>\n"
        '<section class="card">'
        "<h2>段落 / 场景（仅练习提示，不会渲染或发送到 KO II）</h2>"
        f"<ol>{scene_items}</ol></section>\n"
        '<section class="card">'
        "<h2>用 EP sample tool 导入的步骤</h2>"
        '<p class="note">官方说明摘要：EP sample tool 通过 USB 与桌面 Web MIDI '
        "浏览器工作；可向当前激活组一次拖放多个 WAV（最多 12 个），"
        "红色虚线框会预览受影响的垫，文件上传到设备空闲音色槽，"
        "并可在改动前备份所选项目。</p>"
        f"<ol>{steps_html}</ol>"
        '<p class="note">官方链接：'
        f'<a href="{OFFICIAL_TOOL_URL}" target="_blank" rel="noopener noreferrer">'
        "EP sample tool 说明</a> · "
        f'<a href="{OFFICIAL_SYSTEM_URL}" target="_blank" rel="noopener noreferrer">'
        "EP-133 system</a></p>"
        "</section>\n"
        '<section class="card">'
        "<h2>反馈</h2>"
        "<p>请录制 <strong>30–60 秒</strong>弹奏，并在模板中写明 "
        "<code>set_id</code>、垫 id（或时间点）、保留（keep）/修改（change）"
        "的意见，然后把录音与填好的模板一起回传。</p>"
        f'<p><a href="{feedback_href}" download="feedback-template.json">'
        "下载 feedback-template.json</a></p>"
        "</section>\n"
        f"<footer>BeatLab KO II 离线练习页 · WAV + map（非原生 .pak）· "
        f"parent_version: {h(data['parent_version'] or 'n/a')} · "
        "硬件播放/保存/重开均未验证</footer>\n"
        f"</main>\n<script>{JS}</script>\n</body>\n</html>\n"
    )


def generate(set_dir):
    """Read SET_DIR/set.json and write SET_DIR/index.html; never overwrite."""
    set_dir = Path(set_dir)
    manifest_path = set_dir / "set.json"
    manifest_bytes = manifest_path.read_bytes()
    raw = json.loads(manifest_bytes)
    data = validate_manifest(raw)
    data["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    out = set_dir / "index.html"
    if out.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {out}")
    out.write_text(render_html(data), encoding="utf-8")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate a self-contained KO II set page (index.html)."
    )
    parser.add_argument("set_dir", help="directory containing set.json")
    args = parser.parse_args(argv)
    try:
        out = generate(Path(args.set_dir))
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
