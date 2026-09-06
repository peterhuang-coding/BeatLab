"""BeatLab 模块 E：编排生成（compose）。

功能：选材 → 结构伸缩 → 鼓/bass/切片编排 → BeatSpec + 3 个 MIDI。
- 选材：默认取 scores.passed 总分最高的 N 个样本（不足按总分补），可 --samples 指定。
- 结构：total_bars = round(DEFAULT_BEAT_DURATION_S * bpm / 240)，72 小节模板等比伸缩。
- 鼓：MPC swing 引擎（96 tick/拍，swing 58%=+2 tick，只延迟 steps 1,3,...,15 的奇数 16 分位）。
- 确定性：seed = sha256(beat_id) 前 8 位，同 beat_id 重跑结果一致。

只依赖 common 与 mido；接口经 SQLite 与文件系统交换。
用法：.venv/bin/python pipeline/compose.py [--bpm 92] [--style boom-bap] [--best N] [--samples id1,id2] [--out ROOT/beats/<beat_id>]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo

from common import (
    MIDI_CHOP_BASE,
    DEFAULT_BEAT_DURATION_S,
    ROOT,
    BeatSpec,
    Chop,
    Section,
    get_db,
)

# ---------- 常量 ----------
STEPS_PER_BAR = 16
TICKS_PER_BEAT = 96        # MPC 时钟：96 tick/拍
TICKS_PER_STEP = TICKS_PER_BEAT // 4   # 16 分 = 24 tick
TRACKS = ("kick", "snare", "hat", "oh", "perc")
DRUM_NOTES = {"kick": 36, "snare": 38, "hat": 42, "oh": 46, "perc": 39}
SWING_STEPS = frozenset(range(1, STEPS_PER_BAR, 2))   # 只延迟偶数 16 分位（steps 1,3,...,15）
SWING_TICKS = {54: 1, 58: 2, 62: 3}                    # swing% → +tick（96 tick/拍）
SECTION_ENERGY = {"intro": 0.4, "verse": 0.7, "chorus": 1.0, "bridge": 0.5, "outro": 0.3}
BASE_TEMPLATE = [   # (name, bars) 基数 72 小节；intro/bridge/outro 不伸缩
    ("intro", 4), ("verse", 16), ("chorus", 8), ("verse", 16),
    ("chorus", 8), ("bridge", 8), ("verse", 8), ("outro", 4),
]
KICK_FAMILIES = {   # 各风格 kick 词汇表（每 bar 随机取一组）
    "boom-bap": [[0, 7, 10], [0, 7, 10, 14], [0, 6, 10], [0, 2, 10]],
    "trap": [[0, 6, 10], [0, 7, 10, 15], [0, 6, 11]],
    "halftime": [[0, 10], [0, 7, 14], [0, 10, 14]],
}
SNARE_STEPS = {"boom-bap": [4, 12], "trap": [8], "halftime": [12]}

# ---------- 小工具 ----------
def _seed(beat_id: str) -> int:
    """确定性随机：seed = sha256(beat_id) 前 8 位十六进制。"""
    return int(hashlib.sha256(f"compose:{beat_id}".encode("utf-8")).hexdigest()[:8], 16)


def _ms_per_tick(bpm: float) -> float:
    return 60000.0 / bpm / TICKS_PER_BEAT


def _swing_ms(bpm: float, style: str) -> float:
    """风格默认 swing 58%（loose 感风格 62%），换算为延迟毫秒数。"""
    pct = 62 if any(w in style.lower() for w in ("loose", "dilla", "swing", "behind")) else 58
    return SWING_TICKS[pct] * _ms_per_tick(bpm)


def _humanize_ms(rng: random.Random) -> float:
    """人味化：非 swing 步 timing ±2-8ms。"""
    return round(rng.uniform(2.0, 8.0) * rng.choice((-1, 1)), 2)


def _humanize_vel(rng: random.Random, base: int) -> int:
    """人味化：velocity ±5-15。"""
    v = base + rng.choice((-1, 1)) * rng.randint(5, 15)
    return max(1, min(127, v))


def parse_key_note(text: Any) -> int | None:
    """把 key_note 字符串转 MIDI 音符：'A1'→33；支持 'C#2'/'Bb1' 与数字字符串；失败返回 None。"""
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    if text.isdigit():
        n = int(text)
        return n if 0 <= n <= 127 else None
    m = re.match(r"^([A-Ga-g])([#b]?)(-?\d)$", text)
    if not m:
        return None
    base = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[m.group(1).upper()]
    if m.group(2) == "#":
        base += 1
    elif m.group(2) == "b":
        base -= 1
    return (int(m.group(3)) + 1) * 12 + base

# ---------- 选材 ----------
def select_samples(conn, ids_arg: str | None, best: int) -> list[dict]:
    """选材：--samples 指定则按序取；否则 scores.passed 总分 Top-N，不足按总分补齐。"""
    if ids_arg:
        ids = [x.strip() for x in ids_arg.split(",") if x.strip()]
        marks = ",".join("?" * len(ids))
        rows = conn.execute(f"SELECT * FROM samples WHERE id IN ({marks})", ids).fetchall()
        by_id = {r["id"]: dict(r) for r in rows}
        missing = [i for i in ids if i not in by_id]
        if missing:
            sys.exit(f"[ERROR] 样本不存在: {missing}；请先运行 ingest（或去掉 --samples 自动选材）")
        return [by_id[i] for i in ids]
    rows = conn.execute(
        "SELECT s.* FROM samples s JOIN scores sc ON s.id = sc.sample_id "
        "WHERE sc.passed = 1 ORDER BY sc.total DESC LIMIT ?", (best,),
    ).fetchall()
    selected = [dict(r) for r in rows]
    if len(selected) < best:
        all_rows = conn.execute(
            "SELECT s.* FROM samples s JOIN scores sc ON s.id = sc.sample_id "
            "ORDER BY sc.total DESC",
        ).fetchall()
        have = {s["id"] for s in selected}
        for r in all_rows:
            if len(selected) >= best:
                break
            if r["id"] not in have:
                selected.append(dict(r))
    if not selected:
        sys.exit("[ERROR] 没有可用样本：请先运行 ingest/score（或用 --samples 指定样本 id）")
    return selected


# ---------- 结构 ----------
def scale_sections(total_bars: int) -> list[Section]:
    """按 total_bars/72 对 verse/chorus 成对等比扩缩（intro/bridge/outro 固定），保证总和 == total_bars。"""
    if total_bars < 8:
        sys.exit(f"[ERROR] bpm 过低导致 total_bars={total_bars} < 8，结构不可生成")
    fixed = {0, 5, 7}   # intro/bridge/outro
    movable = [i for i in range(len(BASE_TEMPLATE)) if i not in fixed]
    movable_base = sum(BASE_TEMPLATE[i][1] for i in movable)
    target_movable = total_bars - sum(BASE_TEMPLATE[i][1] for i in fixed)
    bars = [b for _, b in BASE_TEMPLATE]
    if target_movable != movable_base:
        raw = {i: BASE_TEMPLATE[i][1] * target_movable / movable_base for i in movable}
        alloc = {i: max(1, int(raw[i])) for i in movable}
        remain = target_movable - sum(alloc.values())
        while remain:   # 最大余数法：加给小数部分最大者 / 从小数部分最小者扣
            sign = 1 if remain > 0 else -1
            order = sorted(movable, key=lambda i: raw[i] - int(raw[i]), reverse=remain > 0)
            moved = False
            for i in order:
                if remain == 0:
                    break
                if sign < 0 and alloc[i] <= 1:
                    continue
                alloc[i] += sign
                remain -= sign
                moved = True
            if not moved:
                break
        for i, v in alloc.items():
            bars[i] = v
        remain = total_bars - sum(bars)
        if remain:      # 兜底：bridge 吸收残差，仍不齐再动 intro
            bars[5] = max(1, bars[5] + remain)
            remain = total_bars - sum(bars)
            if remain:
                bars[0] = max(1, bars[0] + remain)
    return [
        Section(name=name, bars=b, energy=SECTION_ENERGY[name],
                drop_hats=name not in ("intro", "outro"),
                add_bass=name not in ("intro", "outro"))
        for (name, _), b in zip(BASE_TEMPLATE, bars)
    ]

# ---------- 鼓 pattern（16 步/bar，MPC swing 引擎） ----------
def _bar_drums(rng: random.Random, style_key: str, energy: float,
               last_bar: bool, swing_ms: float, ms_tick: float) -> dict[str, dict]:
    """生成单 bar 鼓：按风格选词汇表；swing 只延迟奇数 16 分位；energy<0.5 去 oh/perc 降 hat 密度。"""
    bar: dict[str, dict] = {t: {} for t in TRACKS}
    sparse = energy < 0.5
    snares = SNARE_STEPS[style_key]

    def place(track: str, step: float, base_vel: int, extra_offset: float = 0.0) -> None:
        """落一个音符：swing 步精确 +swing_ms；非 swing 步 ±2-8ms；velocity ±5-15。"""
        if step in SWING_STEPS:
            off = swing_ms
        else:
            off = _humanize_ms(rng)
        bar[track][step] = {"velocity": _humanize_vel(rng, base_vel),
                            "offset_ms": round(off + extra_offset, 2)}

    def ghost(step: int) -> None:
        """低 velocity(35-58) ghost note，timing 规则同主音符。"""
        off = swing_ms if step in SWING_STEPS else _humanize_ms(rng)
        bar["snare"][step] = {"velocity": rng.randint(35, 58), "offset_ms": off}

    # kick
    if style_key == "trap":
        sync = rng.sample([6, 7, 10, 11, 14, 15], k=rng.randint(1, 2))   # 808 切分
        for step in [0, *sorted(sync)]:
            push = 12 * ms_tick if (step in SWING_STEPS and rng.random() < 0.3) else 0.0
            place("kick", step, rng.randint(95, 112) if step == 0 else rng.randint(72, 92), push)
    else:
        for step in rng.choice(KICK_FAMILIES[style_key]):
            base = rng.randint(92, 110) if step == 0 else rng.randint(70, 100)
            place("kick", step, base)

    # snare + ghost（主 snare 前后 1 步）+ 段末 fill
    for step in snares:
        place("snare", step, rng.randint(94, 118))
        for g in (step - 1, step + 1):
            if 0 <= g < STEPS_PER_BAR and g not in snares:
                ghost(g)
    if last_bar:
        for step in rng.sample([13, 14, 15], k=2):
            if step not in snares:
                ghost(step)

    # hat
    if style_key == "trap":
        steps = range(0, 16, 2) if sparse else range(16)
        for step in steps:
            base = rng.randint(58, 78) if step % 4 == 0 else rng.randint(38, 58)
            place("hat", step, base)
        if not sparse and energy >= 1.0:   # chorus 全开：32 分滚奏（velocity 爬升）
            for step in (3, 7, 11, 15):
                bar["hat"][step + 0.5] = {"velocity": rng.randint(30, 45), "offset_ms": _humanize_ms(rng)}
                bar["hat"][step + 0.75] = {"velocity": rng.randint(50, 68), "offset_ms": _humanize_ms(rng)}
    elif style_key == "halftime":
        for step in range(0, 16, 2):
            if step % 4 != 0 and rng.random() < 0.4:
                continue
            base = rng.randint(40, 60) if step % 4 == 0 else rng.randint(30, 50)
            place("hat", step, base)
    else:   # boom-bap：8 分音
        steps = (0, 4, 8, 12) if sparse else range(0, 16, 2)
        for step in steps:
            base = rng.randint(56, 76) if step % 4 == 0 else rng.randint(34, 56)
            place("hat", step, base)

    # oh / perc：energy<0.5 时去掉
    if not sparse:
        oh_pool = [7, 11, 13, 15] if style_key == "trap" else [5, 13]
        place("oh", rng.choice(oh_pool), rng.randint(46, 70))
        if last_bar:
            perc_steps = rng.sample([1, 3, 6, 9, 11, 14, 15], k=3)
        else:
            perc_steps = rng.sample([2, 5, 7, 10, 14], k=1 if rng.random() < 0.75 else 2)
        for step in perc_steps:
            if step not in snares:
                place("perc", step, rng.randint(38, 74))
    return bar


def generate_drum_pattern(rng: random.Random, sections: list[Section], style_key: str,
                          swing_ms: float, ms_tick: float) -> dict[int, dict]:
    """整曲鼓：{bar_index: {track: {step: {velocity, offset_ms}}}}；step 可为 0.5 步（trap 32 分滚奏）。"""
    pattern: dict[int, dict] = {}
    bar_index = 0
    for sec in sections:
        for b in range(sec.bars):
            pattern[bar_index] = _bar_drums(rng, style_key, sec.energy,
                                            b == sec.bars - 1, swing_ms, ms_tick)
            bar_index += 1
    return pattern

# ---------- bass ----------
def generate_bass(rng: random.Random, sections: list[Section], root: int) -> dict[int, dict]:
    """每 bar 根音在 step 0 与 8（step 8 可选八度跳 +12）；bridge 走 IV 级（+5 半音）。"""
    pattern: dict[int, dict] = {}
    bar_index = 0
    for sec in sections:
        note = root + 5 if sec.name == "bridge" else root
        for _ in range(sec.bars):
            steps: dict[int, int] = {0: note}
            if rng.random() < 0.75:
                steps[8] = note + (12 if rng.random() < 0.4 else 0)
            pattern[bar_index] = steps
            bar_index += 1
    return pattern


# ---------- 切片摆放 ----------
def load_chops(sample_row: dict) -> list[Chop]:
    """从 library/<category>/<id>/slice_map.json 读切片；缺失/损坏返回 []（由调用方告警）。"""
    lib = sample_row.get("library_path") or ""
    if not lib:
        return []
    p = Path(lib)
    base = p.parent if p.suffix else p   # source.wav → 其所在目录；目录路径 → 自身
    sm = base / "slice_map.json"
    if not sm.exists():
        return []
    try:
        data = json.loads(sm.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"[WARN] 读取 {sm} 失败：{e}")
        return []
    items = data if isinstance(data, list) else (data.get("chops", []) if isinstance(data, dict) else [])
    chops: list[Chop] = []
    for i, it in enumerate(items):
        try:
            c = Chop(
                stem=str(it.get("stem", "source")),
                file=str(it.get("file") or it.get("slice_path") or ""),
                start_sec=float(it["start_sec"]),
                end_sec=float(it["end_sec"]),
                pad=int(it.get("pad", 0)),
                midi_note=int(it.get("midi_note", 0)),
                confidence=float(it.get("confidence", 0.0)),
            )
            if c.midi_note == 0 and c.pad:
                c.midi_note = MIDI_CHOP_BASE + c.pad - 1
            if c.end_sec > c.start_sec:
                chops.append(c)
        except (KeyError, TypeError, ValueError) as e:
            print(f"[WARN] {sm} 切片条目 {i} 解析失败：{e}")
    return chops


def load_vocal_phrases(sample_row: dict) -> list[dict]:
    """从 slice_map.json 的 vocal_phrases 读候选（separate 模块产物）；缺失返回 []。"""
    lib = sample_row.get("library_path") or ""
    if not lib:
        return []
    p = Path(lib)
    sm = (p.parent if p.suffix else p) / "slice_map.json"
    if not sm.exists():
        return []
    try:
        data = json.loads(sm.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    phrases = data.get("vocal_phrases", []) if isinstance(data, dict) else []
    out: list[dict] = []
    for it in phrases:
        if it.get("file") and it.get("start_sec") is not None and it.get("end_sec") is not None:
            out.append({"file": str(it["file"]), "start_sec": float(it["start_sec"]),
                        "end_sec": float(it["end_sec"]), "gain_hint": float(it.get("gain_hint", 1.0))})
    return out


def _best_chop(chops: list[Chop], key, exclude: set[int]) -> tuple[Chop, int] | None:
    """按 key 取最优切片（排除已用 index）；全被排除则退回全局最优。"""
    pool = [i for i in range(len(chops)) if i not in exclude]
    if not pool:
        pool = list(range(len(chops)))
    idx = max(pool, key=lambda i: key(chops[i]))
    return chops[idx], idx


def place_chops(rng: random.Random, sections: list[Section],
                chops_by_sample: dict[str, list[Chop]],
                phrases_by_sample: dict[str, list[dict]],
                style_key: str) -> tuple[list[dict], list[dict]]:
    """切片与人声摆放。chorus 每 bar：step 0 最强 stab + snare 后短应答；verse 每 2 bar 一个；
    intro/outro 各 1 个长 chop 垫；chorus 每 8 bar 一个 vocal phrase。bar 为全局小节号（0 起）。"""
    placements: list[dict] = []
    vocals: list[dict] = []
    sample_ids = [sid for sid, cs in chops_by_sample.items() if cs]
    if not sample_ids:
        print("[WARN] 所选样本均无可用 slice_map.json，跳过切片摆放（可先跑 separate 模块）")
    else:
        missing = [sid for sid, cs in chops_by_sample.items() if not cs]
        if missing:
            print(f"[WARN] 样本无 slice_map.json 或为空：{missing}，其切片摆放已跳过")
    vocal_pool: dict[str, list] = {sid: list(ps) for sid, ps in phrases_by_sample.items() if ps}
    if not vocal_pool:   # 兜底：无 vocal_phrases 时退回 vocal 切片
        vocal_pool = {sid: [c for c in cs if c.stem == "vocal"]
                      for sid, cs in chops_by_sample.items() if cs}
        vocal_pool = {sid: v for sid, v in vocal_pool.items() if v}
    vocal_sids = list(vocal_pool)
    if not vocal_sids and sample_ids:
        print("[WARN] 无 vocal_phrases/vocal 切片，vocal_placements 为空")

    answer_sides = {   # snare 后的应答位置（按风格 snare 位）
        "boom-bap": [[4, 5], [12, 13]],
        "halftime": [[12, 13]],
        "trap": [[8, 9], [12, 13]],
    }[style_key]

    def add(sid: str, chop: Chop, ci: int, section: str, bar: int, step: int, gain: float) -> None:
        placements.append({
            "section": section, "bar": bar, "step": step, "sample_id": sid,
            "chop_index": ci, "pad": chop.pad, "midi_note": chop.midi_note,
            "gain": round(gain, 2), "file": chop.file,
            "start_sec": chop.start_sec, "end_sec": chop.end_sec,
        })

    bar_idx = 0
    chorus_count = 0
    for sec in sections:
        if sec.name == "chorus":
            for _ in range(sec.bars):
                if sample_ids:
                    sid = rng.choice(sample_ids)
                    chops = chops_by_sample[sid]
                    used: set[int] = set()
                    stab, si = _best_chop(chops, key=lambda c: c.confidence, exclude=used)
                    add(sid, stab, si, "chorus", bar_idx, 0, 0.9)
                    used.add(si)
                    side = rng.choice(answer_sides)
                    ans, ai = _best_chop(chops, key=lambda c: -(c.end_sec - c.start_sec), exclude=used)
                    add(sid, ans, ai, "chorus", bar_idx, rng.choice(side), rng.uniform(0.5, 0.7))
                if chorus_count % 8 == 0 and vocal_sids:
                    vsid = rng.choice(vocal_sids)
                    vent = rng.choice(vocal_pool[vsid])
                    if isinstance(vent, Chop):   # 兜底：vocal 切片
                        vi = chops_by_sample[vsid].index(vent)
                        vocals.append({
                            "section": "chorus", "bar": bar_idx, "step": rng.randint(0, 4),
                            "sample_id": vsid, "chop_index": vi, "pad": vent.pad,
                            "midi_note": vent.midi_note, "gain": 0.5, "file": vent.file,
                            "start_sec": vent.start_sec, "end_sec": vent.end_sec,
                        })
                    else:   # separate 模块 vocal_phrases
                        vocals.append({
                            "section": "chorus", "bar": bar_idx, "step": rng.randint(0, 4),
                            "sample_id": vsid, "file": vent["file"],
                            "start_sec": vent["start_sec"], "end_sec": vent["end_sec"],
                            "gain": 0.5,
                        })
                chorus_count += 1
                bar_idx += 1
        elif sec.name == "verse":
            for k in range(sec.bars):
                if k % 2 == 0 and sample_ids:   # 稀疏：每 2 bar 一个
                    sid = rng.choice(sample_ids)
                    chops = chops_by_sample[sid]
                    chop, ci = rng.choice([(c, i) for i, c in enumerate(chops)])
                    add(sid, chop, ci, "verse", bar_idx,
                        rng.choice([0, 5, 8, 10, 14]), rng.uniform(0.5, 0.7))
                bar_idx += 1
        else:
            if sec.name in ("intro", "outro") and sample_ids:   # 只 1 个长 chop 垫
                sid = rng.choice(sample_ids)
                chops = chops_by_sample[sid]
                long_chop, ci = _best_chop(chops, key=lambda c: c.end_sec - c.start_sec, exclude=set())
                add(sid, long_chop, ci, sec.name, bar_idx, 0, 0.7)
            bar_idx += sec.bars
    return placements, vocals

# ---------- MIDI 导出 ----------
def _write_midi(path: Path, notes: list[tuple[int, int, int, int, int]],
                bpm: float, track_name: str) -> None:
    """把 (tick, duration, note, velocity, channel) 写为单轨 MIDI。"""
    midi = MidiFile(ticks_per_beat=TICKS_PER_BEAT)
    track = MidiTrack()
    midi.tracks.append(track)
    track.append(MetaMessage("track_name", name=track_name, time=0))
    track.append(MetaMessage("set_tempo", tempo=bpm2tempo(int(round(bpm))), time=0))
    events: list[tuple[int, Message]] = []
    for start, duration, note, velocity, channel in notes:
        start = max(0, start)
        events.append((start, Message("note_on", note=note, velocity=max(1, min(127, velocity)), channel=channel, time=0)))
        events.append((start + max(1, duration), Message("note_off", note=note, velocity=0, channel=channel, time=0)))
    events.sort(key=lambda item: (item[0], 0 if item[1].type == "note_off" else 1))
    last_tick = 0
    for tick, message in events:
        message.time = max(0, tick - last_tick)
        track.append(message)
        last_tick = tick
    track.append(MetaMessage("end_of_track", time=TICKS_PER_BEAT))
    midi.save(str(path))


def export_midi(spec: BeatSpec, beat_dir: Path) -> dict[str, Path]:
    """产出 drums.mid / bass.mid / chops.mid；velocity/offset_ms 换算为 96 PPQ tick。"""
    drums: list[tuple[int, int, int, int, int]] = []
    bass: list[tuple[int, int, int, int, int]] = []
    chops: list[tuple[int, int, int, int, int]] = []
    ms2tick = lambda ms: round(ms * spec.bpm * TICKS_PER_BEAT / 60000)
    bar_ticks = STEPS_PER_BAR * TICKS_PER_STEP

    for bar_index, tracks in spec.drum_pattern.items():
        base = int(bar_index) * bar_ticks
        for track_name, steps in tracks.items():
            for step, hit in steps.items():
                tick = base + round(float(step) * TICKS_PER_STEP) + ms2tick(hit["offset_ms"])
                dur = TICKS_PER_STEP if track_name in ("oh", "perc") else TICKS_PER_STEP // 2
                drums.append((tick, dur, DRUM_NOTES[track_name], int(hit["velocity"]), 9))
    for bar_index, steps in spec.bass_pattern.items():
        base = int(bar_index) * bar_ticks
        for step, note in steps.items():
            bass.append((base + int(step) * TICKS_PER_STEP, TICKS_PER_STEP * 2, int(note), 96, 0))
    for p in spec.chop_placements:
        tick = int(p["bar"]) * bar_ticks + int(p["step"]) * TICKS_PER_STEP
        chops.append((tick, TICKS_PER_STEP * 8, int(p["midi_note"]), round(p["gain"] * 127), 1))

    files = {"drums": drums, "bass": bass, "chops": chops}
    names = {"drums": "BeatLab Drums", "bass": "BeatLab Bass", "chops": "BeatLab Chops"}
    out: dict[str, Path] = {}
    for key, notes in files.items():
        path = beat_dir / f"{key}.mid"
        _write_midi(path, notes, spec.bpm, names[key])
        out[key] = path
    return out


# ---------- 编排入口 ----------
def _default_beat_id(style: str, bpm: float) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"beat_{stamp}_{style.lower().replace(' ', '-')}_{bpm:g}bpm"


def compose(args: argparse.Namespace) -> BeatSpec:
    conn = get_db()
    conn.row_factory = lambda cur, row: {d[0]: row[i] for i, d in enumerate(cur.description)}
    samples = select_samples(conn, args.samples, args.best)
    beat_dir = Path(args.out) if args.out else ROOT / "beats" / _default_beat_id(args.style, args.bpm)
    beat_id = beat_dir.name
    bpm = float(args.bpm)
    total_bars = round(DEFAULT_BEAT_DURATION_S * bpm / 240)
    sections = scale_sections(total_bars)
    style_key = args.style.lower().replace(" ", "-")
    if style_key not in KICK_FAMILIES:
        print(f"[WARN] 未知风格 {args.style}，鼓词汇表回退 boom-bap")
        style_key = "boom-bap"
    rng = random.Random(_seed(beat_id))
    swing_ms = _swing_ms(bpm, args.style)
    ms_tick = _ms_per_tick(bpm)
    drum_pattern = generate_drum_pattern(rng, sections, style_key, swing_ms, ms_tick)
    root = parse_key_note(samples[0].get("key_note")) or 33   # 默认 A1=33
    bass_pattern = generate_bass(rng, sections, root)
    chops_by_sample = {s["id"]: load_chops(s) for s in samples}
    phrases_by_sample = {s["id"]: load_vocal_phrases(s) for s in samples}
    chop_placements, vocal_placements = place_chops(rng, sections, chops_by_sample,
                                                    phrases_by_sample, style_key)

    spec = BeatSpec(
        beat_id=beat_id, bpm=bpm, style=args.style, sections=sections,
        sample_ids=[s["id"] for s in samples], drum_pattern=drum_pattern,
        chop_placements=chop_placements, vocal_placements=vocal_placements,
        bass_pattern=bass_pattern, total_bars=total_bars,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    beat_dir.mkdir(parents=True, exist_ok=True)
    spec_path = beat_dir / "spec.json"
    spec_path.write_text(spec.to_json(), encoding="utf-8")
    export_midi(spec, beat_dir)
    duration_s = spec.total_bars * 240.0 / spec.bpm
    conn.execute(
        "INSERT OR REPLACE INTO beats (beat_id, created_at, bpm, style, duration_s, "
        "sample_ids, spec_path, wav_path, als_path, midi_dir) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (spec.beat_id, spec.created_at, spec.bpm, spec.style, round(duration_s, 1),
         json.dumps(spec.sample_ids), str(spec_path), "", "", str(beat_dir)),
    )
    conn.commit()
    conn.close()

    pct = 62 if any(w in spec.style.lower() for w in ("loose", "dilla", "swing", "behind")) else 58
    print(f"beat_id={spec.beat_id} bpm={spec.bpm:g} total_bars={spec.total_bars} duration_s={duration_s:.1f}")
    print("sections: " + " ".join(f"{s.name}({s.bars},{s.energy:g})" for s in spec.sections))
    print(f"chop_placements={len(spec.chop_placements)} vocal_placements={len(spec.vocal_placements)} "
          f"swing={pct}%(+{SWING_TICKS[pct]}tick)")
    print(f"输出: {beat_dir} (spec.json + drums.mid + bass.mid + chops.mid)")
    return spec


def main() -> None:
    p = argparse.ArgumentParser(description="BeatLab 模块 E：3-4 分钟编排生成")
    p.add_argument("--bpm", type=float, default=92.0, help="BPM（默认 92）")
    p.add_argument("--style", default="boom-bap", help="风格：boom-bap/trap/halftime，loose 感可 62%% swing（默认 58%%）")
    p.add_argument("--best", type=int, default=3, help="自动选材样本数（默认 3）")
    p.add_argument("--samples", help="逗号分隔样本 id，覆盖 --best 自动选材")
    p.add_argument("--out", help="输出目录（默认 ROOT/beats/<beat_id>）")
    compose(p.parse_args())


if __name__ == "__main__":
    main()
