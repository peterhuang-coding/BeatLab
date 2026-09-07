"""BeatLab Review：候选 A/B/C 对比试听 + 分维度反馈页生成器（原 report.py 升级，PRD §7/§10）。

用法:
    .venv/bin/python pipeline/report.py <target> [target...]

target 两种形态（自动识别）:
- run 结构（P0 新，Dev-3 契约）: ROOT/beats/<run_id>/<candidate_id>/ 各含
  spec.json + beat.wav（+ manifests.json / manifests/）→ A/B/C 三栏对比页
- 旧单候选（开发期兼容）: ROOT/beats/<beat_id>/ 直接含 spec.json + beat.wav
  → 单栏 Review 页并标注「兼容模式」

输出（暗色单文件 HTML，无外部资源）:
- MIRROR_ROOT/<today>/<target>.html          Review 页（file:// 直接可开）
- MIRROR_ROOT/<today>/<target>__<candidate>/ 候选交付物镜像（扁平两层）
- MIRROR_ROOT/<today>/index.html             当日总览

页面功能: 每候选 <audio> 试听 + 静态波形 SVG + Recipe 摘要 + Moment 使用信息 +
分维度 1-5 星（素材/切法/鼓/Bass/结构/整体）+ 快捷原因 + Keep/Reject/Regenerate/
Export 按钮。JS 探测本地反馈服务（127.0.0.1:8765，feedback.py serve）:
服务未启动时顶部给出提示，但浏览与试听不依赖服务（audio 走相对路径）。

同一页面由 feedback.py 服务复用: /review/<target> 动态生成（audio 走 /media，
反馈走 /api）。feedback.py 依赖本文件的 detect_target/candidate_dir/
load_manifests/load_spec/build_review_page/_copy_candidate_files。

只 import common（+ 标准库 wave/array；soundfile 作为可选 fallback）。
"""
from __future__ import annotations

import array
import html
import json
import math
import re
import shutil
import sys
import urllib.parse
from pathlib import Path
from typing import Callable

import common

# ---------- 反馈契约常量（feedback.py 复用） ----------
# 分维度打分: code -> 展示名（PRD §7 Review Feature）
DIMS = [
    ("sample", "素材"), ("chop", "切法"), ("drums", "鼓"),
    ("bass", "Bass"), ("structure", "结构"), ("overall", "整体"),
]
DIM_KEYS = tuple(code for code, _ in DIMS)
# 快捷原因: code -> 展示名（PRD §7 快捷原因）
REASONS = [
    ("sample_meh", "素材没感觉"), ("chop_fragmented", "切得太碎"),
    ("too_similar", "太像原曲"), ("drums_off", "鼓不对"),
    ("too_rigid", "太规整"), ("no_space", "没有空间"),
    ("worth_keeping", "值得继续"),
]
REASON_CODES = tuple(code for code, _ in REASONS)
CAND_COLORS = ("#6ee7b7", "#f0c674", "#7aa2f7")   # A/B/C 主题色
DEFAULT_HOST = "127.0.0.1:8765"

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; background: #0d0d0d; color: #e8e6e3;
       font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Segoe UI", sans-serif; }
header { padding: 24px 32px 18px; border-bottom: 1px solid #2a2a28; }
h1 { margin: 0 0 6px; font-size: 22px; font-weight: 650; word-break: break-all; }
.sub { color: #9a9892; font-size: 13px; }
.svc { padding: 10px 16px; border-radius: 8px; font-size: 13px; margin: 12px 0 0; }
.svc.on  { background: #12251c; border: 1px solid #1f4d33; color: #6ee7b7; }
.svc.off { background: #2a2416; border: 1px solid #4d3d1f; color: #f0c674; }
.kpi { display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0 0; }
.kpi .card { background: #1a1a19; border: 1px solid #2a2a28; border-radius: 10px;
             padding: 10px 18px; min-width: 104px; }
.kpi .k { color: #8f8d87; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
.kpi .v { font-size: 20px; font-weight: 600; color: #6ee7b7; margin-top: 2px; }
.kpi .v.mono { font-size: 13px; padding-top: 5px; }
main { max-width: 1500px; margin: 0 auto; padding: 20px 32px 64px; }
section { margin: 20px 0; }
h2 { font-size: 15px; color: #b8b6b0; margin: 0 0 10px; letter-spacing: .04em; }
h3 { font-size: 12px; color: #8f8d87; margin: 14px 0 6px; letter-spacing: .06em; text-transform: uppercase; }
.card { background: #1a1a19; border: 1px solid #2a2a28; border-radius: 10px; padding: 16px 20px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { text-align: left; color: #8f8d87; font-weight: 500; font-size: 12px; padding: 6px 10px;
     border-bottom: 1px solid #2a2a28; }
td { padding: 8px 10px; border-bottom: 1px solid #22221f; }
tr:last-child td { border-bottom: none; }
audio { width: 100%; margin-top: 6px; }
a { color: #6ee7b7; text-decoration: none; }
a:hover { text-decoration: underline; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: #9a9892; }
footer { color: #6b6964; font-size: 12px; padding: 0 32px 40px; max-width: 1500px; margin: 0 auto; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 14px; }
.grid .card { display: block; color: #e8e6e3; }
.grid .card:hover { border-color: #3f3f3a; text-decoration: none; }
.grid .t { font-weight: 600; margin-bottom: 6px; word-break: break-all; }
.grid .m { color: #8f8d87; font-size: 12px; }
/* ---------- Review 专有 ---------- */
.columns { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; align-items: start; }
.columns.single { grid-template-columns: minmax(320px, 520px); }
.column { background: #1a1a19; border: 1px solid #2a2a28; border-radius: 10px;
          padding: 16px 18px; display: flex; flex-direction: column; gap: 4px; }
.column.legacy { border-style: dashed; }
.chead { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.clabel { font-size: 22px; font-weight: 700; }
.cid { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px;
       color: #9a9892; word-break: break-all; flex: 1; min-width: 0; }
.badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; border: 1px solid #3f3f3a; color: #b8b6b0; }
.badge.recipe { color: #7aa2f7; border-color: #2b3a5e; }
.compat { border: 1px dashed #4d3d1f; color: #f0c674; font-size: 11px; padding: 2px 8px; border-radius: 6px; }
.waveform { width: 100%; height: 60px; display: block; margin-top: 8px; }
.waveform rect { fill: currentColor; opacity: .8; }
table.mini th, table.mini td { padding: 3px 8px; font-size: 12px; border-bottom: 1px solid #1e1e1c; }
table.mini th { border-bottom: 1px solid #2a2a28; }
table.mini td:first-child { color: #8f8d87; white-space: nowrap; }
table.mini tr:last-child td { border-bottom: none; }
.dimrow { display: flex; justify-content: space-between; align-items: center; padding: 3px 0; }
.dlab { font-size: 13px; color: #b8b6b0; }
.stars { display: inline-flex; gap: 3px; cursor: pointer; user-select: none; }
.stars .star { color: #3a3a37; font-size: 16px; transition: color .08s; }
.stars .star.on { color: #f0c674; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { font-size: 12px; padding: 4px 10px; border-radius: 999px; border: 1px solid #3a3a37;
        background: transparent; color: #b8b6b0; cursor: pointer; }
.chip.on { border-color: #f0c674; color: #f0c674; background: #2a2416; }
.acts { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.act { font-size: 13px; padding: 7px 14px; border-radius: 8px; border: 1px solid #3a3a37;
       background: #232322; color: #e8e6e3; cursor: pointer; }
.act:hover { border-color: #6e6b64; }
.act.keep:hover { border-color: #6ee7b7; color: #6ee7b7; }
.act.reject:hover { border-color: #e08a8a; color: #e08a8a; }
.act.regen:hover { border-color: #c3b7f5; color: #c3b7f5; }
.act.export { background: #1c2b26; border-color: #2f5c48; color: #6ee7b7; }
.act.submit { background: #23212a; border-color: #4a3f6e; color: #c3b7f5; }
.status { font-size: 12px; color: #9a9892; min-height: 16px; margin-top: 8px; }
.status.ok { color: #6ee7b7; }
.status.err { color: #e08a8a; }
.fbadge { font-size: 11px; padding: 2px 8px; border-radius: 6px; border: 1px solid #3f3f3a; color: #b8b6b0; }
.fbadge.keep       { background: #12251c; border-color: #1f4d33; color: #6ee7b7; }
.fbadge.reject     { background: #2a1616; border-color: #542a2a; color: #e08a8a; }
.fbadge.export     { background: #12202b; border-color: #2b3a5e; color: #7aa2f7; }
.fbadge.regenerate { background: #221830; border-color: #4a3f6e; color: #c3b7f5; }
.fbadge.rated      { background: #232323; color: #b8b6b0; }
"""

JS_CORE = r"""
const state = {};
CFG.candidates.forEach(c => state[c] = {dims: {}, reasons: []});
function col(i){ return document.getElementById('col-' + i); }
function setStatus(i, txt, ok){
  const el = col(i).querySelector('.status');
  if (el){ el.textContent = txt; el.className = 'status ' + (ok ? 'ok' : 'err'); }
}
function setBadge(i, verdict){
  const b = col(i).querySelector('.fbadge');
  if (!b) return;
  b.className = 'fbadge ' + verdict;
  b.textContent = CFG.verdictLabels[verdict] || verdict;
}
async function svcProbe(){
  let ok = false;
  try {
    const c = new AbortController();
    const t = setTimeout(() => c.abort(), 1600);
    const r = await fetch('/api/ping', {signal: c.signal});
    ok = !!(r && r.ok);
    clearTimeout(t);
  } catch (e) { ok = false; }
  const b = document.getElementById('svcbanner');
  b.className = 'svc ' + (ok ? 'on' : 'off');
  b.textContent = ok
    ? ('本地反馈服务已连接 · ' + CFG.host + ' — 打分 / Keep / Reject / Export 可用')
    : '本地服务未启动 — 页面仍可浏览试听；提交反馈 / Keep / Export 需先运行: .venv/bin/python pipeline/feedback.py serve';
}
async function postAPI(path, payload){
  const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                               body: JSON.stringify(payload)});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ('HTTP ' + r.status));
  return data;
}
function payloadOf(cand, verdict){
  return {run_id: CFG.runId, candidate_id: cand, verdict: verdict,
          dims: state[cand].dims, reasons: state[cand].reasons};
}
const ACTIONS = {
  keep:       {path: '/api/keep',       verdict: 'keep'},
  reject:     {path: '/api/reject',     verdict: 'reject'},
  regenerate: {path: '/api/regenerate', verdict: 'regenerate'},
  export:     {path: '/api/export',     verdict: 'export'},
  rate:       {path: '/api/feedback',   verdict: 'rated'},
};
async function loadBadges(){
  let rows = [];
  try {
    const r = await fetch('/api/feedback?run_id=' + encodeURIComponent(CFG.runId));
    if (r.ok) rows = await r.json();
  } catch (e) { return; }
  rows.forEach(row => {
    const i = CFG.candidates.indexOf(row.candidate_id);
    if (i < 0) return;
    setBadge(i, row.verdict || 'rated');
    const rs = (row.reasons || []).map(c => CFG.reasonLabels[c] || c);
    if (rs.length) col(i).querySelector('.fbadge').title = '原因: ' + rs.join(' / ');
  });
}
document.addEventListener('DOMContentLoaded', () => {
  svcProbe();
  loadBadges();
  document.querySelectorAll('.stars').forEach(st => {
    st.addEventListener('click', ev => {
      const star = ev.target.closest('.star');
      if (!star) return;
      const i = parseInt(st.dataset.i, 10);
      const v = parseInt(star.dataset.v, 10);
      const dim = st.dataset.dim;
      state[CFG.candidates[i]].dims[dim] = v;
      st.querySelectorAll('.star').forEach(s => s.classList.toggle('on', parseInt(s.dataset.v, 10) <= v));
    });
  });
  document.querySelectorAll('.chip').forEach(ch => {
    ch.addEventListener('click', () => {
      const i = parseInt(ch.dataset.i, 10);
      const reason = ch.dataset.reason;
      const arr = state[CFG.candidates[i]].reasons;
      const idx = arr.indexOf(reason);
      if (idx >= 0) arr.splice(idx, 1); else arr.push(reason);
      ch.classList.toggle('on', idx < 0);
    });
  });
  document.querySelectorAll('.act').forEach(btn => {
    btn.addEventListener('click', async () => {
      const i = parseInt(btn.dataset.i, 10);
      const cand = CFG.candidates[i];
      const a = ACTIONS[btn.dataset.action];
      if (!a) return;
      try {
        const res = await postAPI(a.path, payloadOf(cand, a.verdict));
        setStatus(i, res.message || '已提交', true);
        setBadge(i, a.verdict);
      } catch (e) {
        setStatus(i, '提交失败: ' + e.message, false);
      }
    });
  });
});
"""


# ---------- 结构识别 ----------
def detect_target(target: str) -> tuple[str, list[str]]:
    """识别 ROOT/beats/<target> 形态，返回 (mode, candidate_ids)。mode: 'run' | 'legacy'。"""
    base = common.ROOT / "beats" / target
    if not base.is_dir():
        raise FileNotFoundError(f"report: beats/{target} 不存在")
    if (base / "spec.json").exists():
        return "legacy", [target]
    cands = sorted(p.name for p in base.iterdir() if p.is_dir() and (
        (p / "spec.json").exists() or (p / "recipe.json").exists() or (p / "full_mix.wav").exists()))
    if cands:
        return "run", cands
    raise FileNotFoundError(f"report: beats/{target} 下既无 spec.json 也无候选子目录（每候选需含 spec/recipe/full_mix）")


def candidate_dir(run_id: str, candidate_id: str) -> Path:
    """候选目录解析：run 结构 beats/<run>/<cand>/ 优先，legacy beats/<beat>/ 回退。"""
    base = common.ROOT / "beats" / run_id
    d = base / candidate_id
    if d.is_dir() and ((d / "spec.json").exists() or (d / "recipe.json").exists() or (d / "full_mix.wav").exists()):
        return d
    if run_id == candidate_id and (base / "spec.json").exists():
        return base
    raise FileNotFoundError(f"feedback: 候选 {run_id}/{candidate_id} 不存在（beats/ 下无对应目录）")


def load_spec(cand_dir: Path):
    """读候选 spec.json → BeatSpec；缺失/损坏返回 None（页面降级展示）。
    P0 run 结构：spec 在 beats/<run>/specs/<cand>.json。"""
    sp = cand_dir / "spec.json"
    if not sp.is_file():
        sp = cand_dir.parent / "specs" / f"{cand_dir.name}.json"
    if not sp.is_file():
        return None
    try:
        return common.BeatSpec.from_json(sp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def load_manifests(cand_dir: Path) -> dict:
    """合并候选 manifests 信息（manifests.json / manifest.json / manifests/*.json 浅合并）；缺失返回 {}。"""
    out: dict = {}
    for name in ("manifests.json", "manifest.json"):
        p = cand_dir / name
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    out.update(data)
            except json.JSONDecodeError:
                pass
    mdir = cand_dir / "manifests"
    if mdir.is_dir():
        for j in sorted(mdir.glob("*.json")):
            try:
                data = json.loads(j.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    out.update(data)
            except json.JSONDecodeError:
                pass
    return out


def _copy_candidate_files(src: Path, dst: Path) -> list[str]:
    """复制候选交付物到 dst（beat.wav/.als/midi/stems/manifests/spec/手记），返回已复制清单。"""
    dst.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for rel in ("beat.wav", "full_mix.wav", "spec.json", "manifests.json", "manifest.json", "ABLETON_HANDOFF.txt"):
        f = src / rel
        if f.is_file():
            shutil.copy2(f, dst / rel)
            copied.append(rel)
    for m in sorted(src.glob("take_*.als")):
        shutil.copy2(m, dst / m.name)
        copied.append(m.name)
    for sub in ("midi", "stems", "manifests"):
        s = src / sub
        if s.is_dir():
            shutil.copytree(s, dst / sub, dirs_exist_ok=True)
            copied.append(sub + "/")
    return copied


# ---------- 音频 ----------
def read_wav_bins(wav: Path, bins: int = 96) -> tuple[float, list[float]]:
    """读 WAV 返回 (时长秒, 每 bin 归一化 RMS 0-1)。标准库 wave 优先（PCM16/32），
    失败时 soundfile 兜底，都失败返回 (0.0, [])（页面降级为占位波形）。"""
    import wave as _wave
    try:
        with _wave.open(str(wav), "rb") as w:
            ch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            frames = w.readframes(n)
        if sw == 2:
            a = array.array("h", frames)
        elif sw == 4:
            a = array.array("i", frames)
        else:
            raise ValueError(f"wave sampwidth={sw} 不支持（走 soundfile）")
        if sys.byteorder == "big":
            a.byteswap()
        if n == 0:
            return 0.0, []
        step = max(1, n // bins)
        rms = []
        for b in range(bins):
            seg = a[b * step * ch:(b + 1) * step * ch:ch]
            if len(seg) == 0:
                break
            rms.append(math.sqrt(sum(x * x for x in seg) / len(seg)))
        if rms:
            mx = max(rms) or 1.0
            rms = [v / mx for v in rms]
        return n / sr, rms
    except Exception:
        pass
    try:
        import soundfile as sf  # 可选 fallback
        y, sr = sf.read(str(wav), always_2d=True, dtype="float32")
        mono = y[:, 0]
        n = len(mono)
        if n == 0:
            return 0.0, []
        step = max(1, n // bins)
        rms = [math.sqrt(float((mono[b * step:(b + 1) * step] ** 2).mean())) for b in range(bins)]
        mx = max(rms) or 1.0
        return n / sr, [v / mx for v in rms]
    except Exception:
        return 0.0, []


def _q(s: str) -> str:
    """URL 编码相对路径（保留 /），file:// 下中文/空格安全。"""
    return urllib.parse.quote(s, safe="/")


def _fmt_dur(sec: float) -> str:
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}"


# ---------- 候选摘要 ----------
def _recipe_rows(spec, manifests: dict) -> list[tuple[str, str]]:
    """Recipe 参数摘要（manifests.recipe 优先，spec 兜底），最多 ~12 行。"""
    rows: list[tuple[str, str]] = []
    r = manifests.get("recipe")
    if isinstance(r, dict):
        for k, label in (("type", "Recipe"), ("bpm", "BPM"), ("key", "Key"), ("seed", "Seed")):
            if r.get(k) is not None:
                rows.append((label, str(r[k])))
        hero = r.get("hero")
        if isinstance(hero, dict):
            for k, label in (("sample_id", "Hero 素材"), ("source_id", "来源"), ("moment_id", "Moment")):
                if hero.get(k):
                    rows.append((label, str(hero[k])))
            if hero.get("start_sec") is not None:
                t0, t1 = hero.get("start_sec"), hero.get("end_sec", hero.get("start_sec"))
                rows.append(("Hero 区间", f"{float(t0):.1f}–{float(t1):.1f}s"))
        params = r.get("params")
        if isinstance(params, dict):
            for k, v in list(params.items())[:6]:
                rows.append((f"param.{k}", str(v)[:44]))
    if spec is not None:
        rows += [
            ("BPM", f"{spec.bpm:g}"),
            ("风格", str(spec.style or "-")),
            ("段落", str(len(spec.sections))),
            ("总小节", str(spec.total_bars or sum(s.bars for s in spec.sections) or 1)),
            ("切片数", str(len(spec.chop_placements))),
            ("鼓 pattern bars", str(len(spec.drum_pattern))),
            ("Bass bars", str(len(spec.bass_pattern))),
            ("采样数", str(len(spec.sample_ids))),
        ]
    return rows[:12]


def _moment_rows(spec, manifests: dict) -> list[tuple[str, str, str, str]]:
    """Moment 使用信息：manifests.moments 优先，spec 的 chop/vocal placements 聚合兜底。"""
    rows: list[tuple[str, str, str, str]] = []
    moments = manifests.get("moments")
    if isinstance(moments, list):
        for m in moments:
            if not isinstance(m, dict):
                continue
            sid = str(m.get("sample_id") or m.get("asset_id") or "-")
            typ = str(m.get("type") or "-")
            t0, t1 = m.get("start_sec"), m.get("end_sec")
            span = f"{float(t0):.1f}–{float(t1):.1f}s" if t0 is not None else "-"
            rows.append((sid, typ, "1", span))
    if spec is not None:
        agg: dict = {}
        for p in list(getattr(spec, "chop_placements", [])) + list(getattr(spec, "vocal_placements", [])):
            if not isinstance(p, dict):
                continue
            sid = str(p.get("sample_id", "-"))
            a = agg.setdefault(sid, {"n": 0, "lo": None, "hi": None})
            a["n"] += 1
            s, e = p.get("start_sec"), p.get("end_sec")
            if s is not None:
                a["lo"] = s if a["lo"] is None else min(a["lo"], float(s))
            if e is not None:
                a["hi"] = e if a["hi"] is None else max(a["hi"], float(e))
        for sid, a in sorted(agg.items()):
            span = f"{a['lo']:.1f}–{a['hi']:.1f}s" if a["lo"] is not None else "-"
            rows.append((sid, "chop/vocal", str(a["n"]), span))
    return rows[:10]


def _wave_svg(bins: list[float], color: str) -> str:
    """静态波形条 SVG（对称竖条）。"""
    if not bins:
        return (f'<svg class="waveform" viewBox="0 0 96 48" preserveAspectRatio="none" style="color:{color}">'
                f'<rect x="0" y="23" width="96" height="2" opacity="0.25"/></svg>')
    bars = []
    n = len(bins)
    for k, v in enumerate(bins):
        h = max(1.0, v * 44.0)
        x = k * 96.0 / n
        w = max(0.4, 96.0 / n * 0.7)
        y = (48 - h) / 2
        bars.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}"/>')
    return (f'<svg class="waveform" viewBox="0 0 96 48" preserveAspectRatio="none" '
            f'style="color:{color}">{"".join(bars)}</svg>')


def _dim_rows(i: int) -> str:
    return "".join(
        f'<div class="dimrow"><span class="dlab">{label}</span>'
        f'<span class="stars" data-i="{i}" data-dim="{code}">'
        + "".join(f'<span class="star" data-v="{v}">★</span>' for v in range(1, 6))
        + "</span></div>"
        for code, label in DIMS
    )


def _chips(i: int) -> str:
    return "".join(
        f'<button class="chip" data-i="{i}" data-reason="{code}">{label}</button>'
        for code, label in REASONS
    )


# ---------- 候选列 ----------
def _cand_data(run_id: str, cand_id: str):
    cd = candidate_dir(run_id, cand_id)
    manifests = load_manifests(cd)
    spec = load_spec(cd)
    wav = cd / "full_mix.wav"
    if not wav.exists():
        wav = cd / "beat.wav"
    dur, bins = read_wav_bins(wav) if wav.exists() else (0.0, [])
    return cd, manifests, spec, wav, dur, bins


def _column_html(i: int, run_id: str, cand_id: str, media_base: Callable[[str], str],
                 mode: str, color: str) -> str:
    cd, manifests, spec, wav, dur, bins = _cand_data(run_id, cand_id)
    label = "ABC"[i] if mode == "run" else ""
    recipe_type = (manifests.get("recipe") or {}).get("type") if isinstance(manifests.get("recipe"), dict) else None
    recipe_type = recipe_type or ((spec.style if spec else "") or "-")
    sub = f"{_fmt_dur(dur)}" if dur else "无 beat.wav"
    if wav.exists():
        sub += f" · {wav.stat().st_size // 1024}KB"
    recipe_table = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{html.escape(v)}</td></tr>"
        for k, v in _recipe_rows(spec, manifests)
    ) or '<tr><td colspan="2">（无 Recipe 数据）</td></tr>'
    moment_table = "".join(
        f"<tr><td>{html.escape(a)}</td><td>{html.escape(b)}</td>"
        f"<td>{html.escape(c)}</td><td>{html.escape(d)}</td></tr>"
        for a, b, c, d in _moment_rows(spec, manifests)
    ) or '<tr><td colspan="4">（无 Moment 数据）</td></tr>'
    compat = '<span class="compat">兼容模式 · 单候选</span>' if mode == "legacy" else ""
    audio_src = _q(media_base(cand_id) + "beat.wav")
    return f"""
<div class="column{' legacy' if mode == 'legacy' else ''}" id="col-{i}" data-cand="{html.escape(cand_id)}">
  <div class="chead">
    <span class="clabel" style="color:{color}">{label}</span>
    <span class="cid">{html.escape(cand_id)}</span>
    <span class="badge recipe">{html.escape(str(recipe_type))}</span>
    {compat}
    <span class="fbadge rated">未提交</span>
  </div>
  <audio controls preload="none" src="{audio_src}"></audio>
  {_wave_svg(bins, color)}
  <div class="mono" style="margin-top:4px">beat.wav · {sub}</div>
  <h3>Recipe 摘要</h3>
  <table class="mini">{recipe_table}</table>
  <h3>Moment 使用</h3>
  <table class="mini">
    <tr><th>素材</th><th>类型</th><th>次数</th><th>时间范围</th></tr>
    {moment_table}
  </table>
  <h3>分维度打分</h3>
  {_dim_rows(i)}
  <h3>快捷原因</h3>
  <div class="chips">{_chips(i)}</div>
  <div class="acts">
    <button class="act keep" data-i="{i}" data-action="keep">Keep</button>
    <button class="act reject" data-i="{i}" data-action="reject">Reject</button>
    <button class="act regen" data-i="{i}" data-action="regenerate">Regenerate</button>
    <button class="act export" data-i="{i}" data-action="export">Export</button>
    <button class="act submit" data-i="{i}" data-action="rate">提交打分</button>
  </div>
  <div class="status"></div>
</div>
"""


# ---------- 页面 ----------
def build_review_page(target: str, mode: str, candidates: list[str],
                      media_base: Callable[[str], str],
                      cfg_extra: dict | None = None) -> str:
    """生成暗色单文件 Review HTML。media_base(cand_id) 返回候选文件 URL 基路径
    （file 模式: '<target>__<cand>/' 相对路径；服务模式: '/media/<target>/<cand>/'）。"""
    cols = "".join(
        _column_html(i, target, c, media_base, mode, CAND_COLORS[i % len(CAND_COLORS)])
        for i, c in enumerate(candidates)
    )
    cfg = {
        "runId": target,
        "host": DEFAULT_HOST,
        "candidates": list(candidates),
        "dimLabels": dict(DIMS),
        "reasonLabels": dict(REASONS),
        "verdictLabels": {
            "keep": "已保留 Keep", "reject": "已淘汰 Reject", "regenerate": "请求重生成",
            "export": "已导出 Export", "rated": "已打分",
        },
    }
    if cfg_extra:
        cfg.update(cfg_extra)
    cfg_json = json.dumps(cfg, ensure_ascii=False).replace("</", "<\\/")
    mode_label = "Run · A/B/C 对比" if mode == "run" else "兼容模式 · 旧单候选"
    kpis = [
        ("候选数", str(len(candidates))),
        ("模式", mode_label),
        ("北极星", "Kept Beat Rate"),
    ]
    kpi_html = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div></div>'
        for k, v in kpis
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(target)} · BeatLab Review</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>{html.escape(target)} · Review</h1>
  <div class="sub">BeatLab P0 候选对比试听 · {common.today_str()} · {mode_label}</div>
  <div class="svc off" id="svcbanner">正在探测本地反馈服务…</div>
  <div class="kpi">{kpi_html}</div>
</header>
<main>
  <div class="columns{' single' if mode == 'legacy' else ''}">{cols}</div>
</main>
<footer>BeatLab · Review · 反馈契约: POST /api/feedback &#123;run_id, candidate_id, dims, verdict, reasons&#125; · <a href="index.html">当日总览</a></footer>
<script>const CFG = {cfg_json};</script>
<script>{JS_CORE}</script>
</body>
</html>
"""


# ---------- 镜像交付 ----------
def _mirror_target(target: str, mode: str, candidates: list[str], today_dir: Path) -> None:
    """拷贝候选交付物到 MIRROR_ROOT/<today>/（run: <target>__<cand>/，legacy: <target>/）。"""
    for cand in candidates:
        src = candidate_dir(target, cand)
        dst = today_dir / (f"{target}__{cand}" if mode == "run" else target)
        copied = _copy_candidate_files(src, dst)
        if not copied:
            print(f"[report] 警告: {src} 无可镜像交付物", file=sys.stderr)


def _index_html(today_dir: Path) -> Path:
    """当日总览 index.html：列出全部 Review 页（run 候选数 / 兼容单候选）。"""
    entries = []
    for p in sorted(today_dir.glob("*.html")):
        if p.name == "index.html":
            continue
        target = p.stem
        cand_dirs = sorted(d for d in today_dir.glob(f"{target}__*") if d.is_dir())
        legacy = (today_dir / target).is_dir()
        wav = cand_dirs[0] / "beat.wav" if cand_dirs else today_dir / target / "beat.wav"
        dur = ""
        if wav.exists():
            d0, _ = read_wav_bins(wav)
            if d0:
                dur = f" · {_fmt_dur(d0)}"
        if cand_dirs:
            meta = f"Run · {len(cand_dirs)} 候选{dur}"
        elif legacy:
            meta = f"兼容 · 单候选{dur}"
        else:
            meta = "-"
        entries.append(
            f'<a class="card" href="{_q(p.name)}">'
            f'<div class="t">{html.escape(target)}</div>'
            f'<div class="m">{meta}</div></a>'
        )
    grid = "".join(entries) or '<div class="card" style="grid-column:1/-1">今日暂无 Review 页</div>'
    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BeatLab · {common.today_str()} Review 总览</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>AI_音乐_Demos · {common.today_str()}</h1>
  <div class="sub">BeatLab Review 当日总览 · {len(entries)} 个页面</div>
</header>
<main>
  <div class="grid">{grid}</div>
</main>
<footer>BeatLab · 交付镜像: {today_dir}</footer>
</body>
</html>
"""
    out = today_dir / "index.html"
    out.write_text(page, encoding="utf-8")
    return out


def process(target: str, today_dir: Path) -> Path:
    """单 target 全流程：识别结构 -> 镜像拷贝 -> 生成 Review HTML，返回 HTML 路径。"""
    mode, candidates = detect_target(target)
    _mirror_target(target, mode, candidates, today_dir)
    page = build_review_page(
        target, mode, candidates,
        media_base=lambda c: f"{target}__{c}/" if mode == "run" else f"{target}/",
    )
    out = today_dir / f"{target}.html"
    out.write_text(page, encoding="utf-8")
    print(f"[report] {out}")
    return out


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: report.py <target> [target...]", file=sys.stderr)
        print("  target: beats/<run_id>（3 候选 A/B/C）或 beats/<beat_id>（兼容单候选）", file=sys.stderr)
        sys.exit(1)
    today_dir = common.MIRROR_ROOT / common.today_str()
    today_dir.mkdir(parents=True, exist_ok=True)
    n_ok = 0
    for target in sys.argv[1:]:
        try:
            process(target, today_dir)
            n_ok += 1
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as exc:
            print(f"[report] 跳过 {target}: {exc}", file=sys.stderr)
    index = _index_html(today_dir)
    print(f"[report] 总览: {index}（{n_ok} 个 Review 页成功）")


if __name__ == "__main__":
    main()
