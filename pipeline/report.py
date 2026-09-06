"""BeatLab 模块 G：HTML 试听交付。

用法:
    .venv/bin/python pipeline/report.py <beat_id> [beat_id...]

功能:
1. 每个 beat 生成暗色单文件 HTML：KPI（bpm/时长/采样数/结构）+ <audio controls>
   播放 beat.wav + .als 与 midi 链接 + 结构段表（section/bars/energy）。
2. 镜像交付：MIRROR_ROOT/<today>/<slug>/ 拷入 beat.wav/.als/midi/，HTML 写
   MIRROR_ROOT/<today>/<slug>.html（扁平，不嵌套超过 3 层）；并在
   MIRROR_ROOT/<today>/index.html 生成当日总览（列表链接全部 beat）。
3. 样式：暗色 #0d0d0d 页面 / #1a1a19 卡片 / 系统 sans / 内联 CSS，不引外部资源。

只 import common（+ soundfile）；接口经文件系统交换。
"""
from __future__ import annotations

import html
import json
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

import soundfile as sf

import common

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; background: #0d0d0d; color: #e8e6e3;
       font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Segoe UI", sans-serif; }
header { padding: 24px 32px 18px; border-bottom: 1px solid #2a2a28; }
h1 { margin: 0 0 6px; font-size: 22px; font-weight: 650; }
.sub { color: #9a9892; font-size: 13px; }
.kpi { display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0 0; }
.kpi .card { background: #1a1a19; border: 1px solid #2a2a28; border-radius: 10px;
             padding: 10px 18px; min-width: 104px; }
.kpi .k { color: #8f8d87; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
.kpi .v { font-size: 20px; font-weight: 600; color: #6ee7b7; margin-top: 2px; }
main { max-width: 960px; margin: 0 auto; padding: 20px 32px 64px; }
section { margin: 20px 0; }
h2 { font-size: 15px; color: #b8b6b0; margin: 0 0 10px; letter-spacing: .04em; }
.card { background: #1a1a19; border: 1px solid #2a2a28; border-radius: 10px; padding: 16px 20px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { text-align: left; color: #8f8d87; font-weight: 500; font-size: 12px; padding: 6px 10px;
     border-bottom: 1px solid #2a2a28; }
td { padding: 8px 10px; border-bottom: 1px solid #22221f; }
tr:last-child td { border-bottom: none; }
.ebar { display: inline-block; width: 120px; height: 8px; border-radius: 4px;
        background: #2a2a28; vertical-align: middle; margin-right: 8px; overflow: hidden; }
.ebar i { display: block; height: 100%; border-radius: 4px;
          background: linear-gradient(90deg, #6ee7b7, #f0c674); }
audio { width: 100%; margin-top: 6px; }
ul.links { list-style: none; margin: 0; padding: 0; }
ul.links li { padding: 5px 0; font-size: 14px; }
a { color: #6ee7b7; text-decoration: none; }
a:hover { text-decoration: underline; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: #9a9892; }
footer { color: #6b6964; font-size: 12px; padding: 0 32px 40px; max-width: 960px; margin: 0 auto; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 14px; }
.grid .card { display: block; color: #e8e6e3; }
.grid .card:hover { border-color: #3f3f3a; text-decoration: none; }
.grid .t { font-weight: 600; margin-bottom: 6px; word-break: break-all; }
.grid .m { color: #8f8d87; font-size: 12px; }
"""


# ---------- spec 解析（与 render.load_spec 同源，report 保持只依赖 common） ----------
def _load_spec(beat_id: str) -> tuple:
    """从 ROOT/beats/<id>/spec.json 或 db beats.spec_path 读 BeatSpec，返回 (spec, beat_dir)。"""
    beat_dir = common.ROOT / "beats" / beat_id
    spec_path = beat_dir / "spec.json"
    if not spec_path.exists():
        conn = common.get_db()
        try:
            row = conn.execute(
                "select spec_path from beats where beat_id=?", (beat_id,)
            ).fetchone()
            if row and row[0]:
                spec_path = Path(row[0])
        finally:
            conn.close()
    if not spec_path.exists():
        raise FileNotFoundError(f"report: 找不到 {beat_id} 的 spec（{spec_path} 与 db 均无）")
    return common.BeatSpec.from_json(spec_path.read_text(encoding="utf-8")), beat_dir


# ---------- 工具 ----------
def _slug(bpm: float, style: str) -> str:
    """镜像名：<beat_id>_<bpm>_<style>，非法字符替换为 _。"""
    bpm_s = str(int(bpm)) if float(bpm).is_integer() else f"{float(bpm):.1f}"
    style = re.sub(r"[^\w一-鿿.-]+", "_", str(style or "untitled")).strip("_") or "untitled"
    return f"{bpm_s}_{style}"


def _q(s: str) -> str:
    """URL 编码相对路径（保留 / 分隔符），file:// 下中文/空格安全。"""
    return urllib.parse.quote(s, safe="/")


def _fmt_dur(sec: float) -> str:
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}"


def _wav_duration(wav: Path, fallback: float) -> float:
    try:
        return float(sf.info(wav).duration)
    except Exception:
        return fallback


# ---------- HTML ----------
def _beat_html(spec, beat_dir: Path, slug: str, out_html: Path) -> Path:
    """生成单 beat 暗色单文件 HTML（KPI + 试听 + 交付物 + 结构段表）。"""
    beat_id = html.escape(spec.beat_id)
    style = html.escape(spec.style or "-")
    wav_rel = _q(f"{slug}/beat.wav")
    total_bars = spec.total_bars or sum(s.bars for s in spec.sections) or 1
    bar_s = 60.0 / spec.bpm / 4 * 16
    dur = _wav_duration(beat_dir / "beat.wav", total_bars * bar_s)

    kpis = [
        ("BPM", f"{spec.bpm:g}"),
        ("时长", _fmt_dur(dur)),
        ("采样数", str(len(spec.sample_ids))),
        ("段数", str(len(spec.sections))),
        ("总小节", str(total_bars)),
        ("风格", style),
        ("创建", html.escape(spec.created_at or "-")),
    ]
    kpi_html = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div></div>'
        for k, v in kpis
    )

    rows = []
    for s in spec.sections:
        e = common.clamp(float(s.energy), 0, 1)
        rows.append(
            f'<tr><td>{html.escape(s.name)}</td><td>{s.bars}</td>'
            f'<td><span class="ebar"><i style="width:{e * 100:.0f}%"></i></span>'
            f'<span class="mono">{e:.2f}</span></td></tr>'
        )
    struct = "".join(rows) or '<tr><td colspan="3">（无结构段数据）</td></tr>'

    als_files = sorted(beat_dir.glob("take_*.als"))
    midi_files = sorted((beat_dir / "midi").glob("*.mid")) if (beat_dir / "midi").exists() else []
    links = [f'<li><a href="{_q(f"{slug}/beat.wav")}">beat.wav</a> '
             f'<span class="mono">{html.escape(beat_dir.name)}/beat.wav</span></li>']
    links += [f'<li><a href="{_q(f"{slug}/{p.name}")}">{p.name}</a> '
              f'<span class="mono">Ableton Live 12 工程（已设 BPM）</span></li>' for p in als_files]
    links += [f'<li><a href="{_q(f"{slug}/midi/{p.name}")}">midi/{p.name}</a></li>'
              for p in midi_files]
    links_html = "".join(links)

    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{beat_id} · BeatLab 试听</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>{beat_id}</h1>
  <div class="sub">BeatLab 渲染交付 · {common.today_str()}</div>
  <div class="kpi">{kpi_html}</div>
</header>
<main>
  <section>
    <h2>试听</h2>
    <div class="card">
      <audio controls preload="none" src="{wav_rel}"></audio>
      <div class="sub" style="margin-top:8px">beat.wav · 44.1kHz 16bit 立体声 · 峰值 -1dBFS</div>
    </div>
  </section>
  <section>
    <h2>结构段</h2>
    <div class="card">
      <table>
        <tr><th>section</th><th>bars</th><th>energy</th></tr>
        {struct}
      </table>
    </div>
  </section>
  <section>
    <h2>交付物</h2>
    <div class="card"><ul class="links">{links_html}</ul></div>
  </section>
</main>
<footer>BeatLab · {beat_id} · <a href="index.html">当日总览</a></footer>
</body>
</html>
"""
    out_html.write_text(page, encoding="utf-8")
    return out_html


def _index_html(today_dir: Path) -> Path:
    """当日总览 index.html：扫描 today 目录下全部 <slug>.html 列表链接。"""
    entries = []
    for p in sorted(today_dir.glob("*.html")):
        if p.name == "index.html":
            continue
        slug = p.stem
        m = re.search(r"_(\d+(?:\.\d+)?)_", slug)
        bpm = m.group(1) if m else "-"
        wav = today_dir / slug / "beat.wav"
        dur = _fmt_dur(_wav_duration(wav, 0)) if wav.exists() else None
        meta = f"BPM {bpm}"
        if dur:
            meta += f" · {dur}"
        entries.append(
            f'<a class="card" href="{_q(p.name)}">'
            f'<div class="t">{html.escape(slug)}</div>'
            f'<div class="m">{meta}</div></a>'
        )
    grid = "".join(entries) or '<div class="card" style="grid-column:1/-1">今日暂无 beat</div>'
    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BeatLab · {common.today_str()} 试听总览</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>AI_音乐_Demos · {common.today_str()}</h1>
  <div class="sub">BeatLab 当日试听总览 · {len(entries)} 个 beat</div>
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


# ---------- 镜像交付 ----------
def _mirror(beat_dir: Path, today_dir: Path, slug: str) -> Path:
    """拷贝 beat.wav/.als/midi/ 到 MIRROR_ROOT/<today>/<slug>/，返回目标目录。"""
    dst = today_dir / slug
    dst.mkdir(parents=True, exist_ok=True)
    copied = []
    wav = beat_dir / "beat.wav"
    if wav.exists():
        shutil.copy2(wav, dst / "beat.wav")
        copied.append("beat.wav")
    for als in sorted(beat_dir.glob("take_*.als")):
        shutil.copy2(als, dst / als.name)
        copied.append(als.name)
    midi_dir = beat_dir / "midi"
    if midi_dir.exists():
        dst_midi = dst / "midi"
        dst_midi.mkdir(exist_ok=True)
        for m in sorted(midi_dir.glob("*.mid")):
            shutil.copy2(m, dst_midi / m.name)
            copied.append(f"midi/{m.name}")
    if not copied:
        print(f"[report] 警告: {beat_dir} 无 wav/.als/midi 可镜像", file=sys.stderr)
    return dst


def process(beat_id: str, today_dir: Path) -> Path:
    """单 beat 全流程：读 spec -> 镜像拷贝 -> 生成 HTML，返回 HTML 路径。"""
    spec, beat_dir = _load_spec(beat_id)
    wav = beat_dir / "beat.wav"
    if not wav.exists():
        print(f"[report] 警告: {wav} 不存在，先跑 render.py {beat_id}", file=sys.stderr)
    slug = f"{beat_id}_{_slug(spec.bpm, spec.style)}"
    _mirror(beat_dir, today_dir, slug)
    out_html = today_dir / f"{slug}.html"
    _beat_html(spec, beat_dir, slug, out_html)
    print(f"[report] {out_html}")
    return out_html


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: report.py <beat_id> [beat_id...]", file=sys.stderr)
        sys.exit(1)
    today_dir = common.MIRROR_ROOT / common.today_str()
    today_dir.mkdir(parents=True, exist_ok=True)
    n_ok = 0
    for beat_id in sys.argv[1:]:
        try:
            process(beat_id, today_dir)
            n_ok += 1
        except (FileNotFoundError, json.JSONDecodeError, KeyError) as exc:
            print(f"[report] 跳过 {beat_id}: {exc}", file=sys.stderr)
    index = _index_html(today_dir)
    print(f"[report] 总览: {index}（{n_ok} 个 beat 成功）")


if __name__ == "__main__":
    main()
