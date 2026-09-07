"""BeatLab 生成层 · 模块 H2：三 Recipe → 三候选编排（compose 重写，PRD §5/§6）。

P0 变化：
- 一个 run 围绕单一 Hero Sample 生成三个候选（Loop/Chop/Stem 各一，不是换 seed）。
- 目标时长 75s（60-90s），total_bars = round(75*bpm/240)。
- 结构：Intro / Verse / Hook / Verse Variation / Outro。
- Groove Profiles：boom-bap / loose(Dilla) / straight / halftime，
  沿用 MPC swing 引擎（96 tick/拍，swing 58%=+2 tick / 62%=+3 tick，只延迟奇数 16 分位）
  + ghost notes + 人味化；kind→profile：loop=boom-bap, chop=loose, stem=halftime。
- 最小 Harmonic Alignment（P0 签核范围）：bass 根音跟随 hero key（无 key 用 A1=33），
  只走 I/V/VIII 级，不跨调随机。
- 最小 sample-aware drums（P0 签核范围）：hero moment 的 onset 序列 ±1 步内不叠 kick/snare
  （chop kind 用切片起始步；loop/stem 用音频 onset；无音频退化为普通鼓）。
- Section Mutation：滤波/mute（dropout）/chop 密度/辅助素材区分段落。
- BeatSpec 保留旧字段 + hero_moment_id/recipe_kind/run_id（扩展键经 recipes.spec_dump 序列化）。
- jobs 状态：selected → recipes_ready（job_id == run_id）；generated 后重跑跳过。
- 确定性：所有随机来自 sha256(run_id[:kind])，同 run_id 重跑产物可复现。

只依赖 common + recipes + mido；接口经 SQLite 与文件系统交换。
用法：.venv/bin/python pipeline/compose.py <run_id> [--bpm 92] [--groove straight] [--root PATH]
输出：beats/<run_id>/specs/<kind>.json、recipes/<kind>.json、<kind>/midi/{drums,bass,chops}.mid
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime
from pathlib import Path

from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo

import common
import recipes

# ---------- 常量 ----------
STEPS_PER_BAR = 16
TICKS_PER_BEAT = 96                       # MPC 时钟：96 tick/拍
TICKS_PER_STEP = TICKS_PER_BEAT // 4      # 16 分 = 24 tick
TRACKS = ("kick", "snare", "hat", "oh", "perc")
DRUM_NOTES = {"kick": 36, "snare": 38, "hat": 42, "oh": 46, "perc": 39}
SWING_STEPS = frozenset(range(1, STEPS_PER_BAR, 2))    # 只延迟奇数 16 分位
SWING_TICKS = {54: 1, 58: 2, 62: 3}                    # swing% → +tick（96 tick/拍）
SWING_PCT = {"boom-bap": 58, "loose": 62, "straight": 0, "halftime": 58}
KICK_FAMILIES = {  # 各 profile kick 词汇表（每 bar 随机取一组）
    "boom-bap": [[0, 7, 10], [0, 7, 10, 14], [0, 6, 10], [0, 2, 10]],
    "loose": [[0, 7, 10], [0, 10, 11], [0, 7, 11], [0, 6, 10]],
    "straight": [[0, 8], [0, 4, 8, 12], [0, 8, 12]],
    "halftime": [[0, 10], [0, 7, 14], [0, 10, 14]],
}
SNARE_STEPS = {"boom-bap": [4, 12], "loose": [4, 12], "straight": [4, 12], "halftime": [12]}
BASE_TEMPLATE = [   # (name, bars) 基数 26 小节；intro/outro 固定 2，其余等比伸缩
    ("intro", 2), ("verse", 8), ("hook", 8), ("verse_variation", 6), ("outro", 2),
]
# Chop kind 段落样式：重音/呼吸/syncopation（PRD §5 Chop 核心逻辑）
CHOP_PATTERNS = {
    "long_tail": [0],
    "phrase_2bar_even": [0, 5, 8, 13],
    "phrase_2bar_odd": [3, 10],
    "syncopated": [0, 4, 7, 9, 12, 15],
    "breath": [0, 10],
}


# ---------- 结构 ----------
def section_bars(total_bars: int) -> dict[str, int]:
    """intro/outro 固定 2；verse/hook/verse_variation 按基数等比伸缩（最大余数法）。"""
    if total_bars < 8:
        sys.exit(f"[ERROR] bpm 异常导致 total_bars={total_bars} < 8，结构不可生成")
    fixed = {0, 4}
    movable = [1, 2, 3]
    bars = {BASE_TEMPLATE[i][0]: BASE_TEMPLATE[i][1] for i in range(len(BASE_TEMPLATE))}
    target_movable = total_bars - sum(BASE_TEMPLATE[i][1] for i in fixed)
    base_movable = sum(BASE_TEMPLATE[i][1] for i in movable)
    if target_movable != base_movable:
        raw = {i: BASE_TEMPLATE[i][1] * target_movable / base_movable for i in movable}
        alloc = {i: max(1, int(raw[i])) for i in movable}
        remain = target_movable - sum(alloc.values())
        while remain:
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
            bars[BASE_TEMPLATE[i][0]] = v
        remain = total_bars - sum(bars.values())
        if remain:   # 兜底：hook 吸收残差
            bars["hook"] = max(1, bars["hook"] + remain)
    return bars


def build_sections(kind: str, bars_map: dict[str, int]) -> list[common.Section]:
    """按 kind 的段落能量表生成 Section 列表（intro/outro 无 bass、去 hats）。"""
    energy_map = recipes.SECTION_ENERGY[kind]
    return [
        common.Section(name=name, bars=bars_map[name], energy=energy_map.get(name, 0.7),
                       drop_hats=name in ("intro", "outro"),
                       add_bass=name not in ("intro", "outro"))
        for name, _ in BASE_TEMPLATE
    ]


# ---------- 鼓 pattern（16 步/bar，MPC swing 引擎 + onset 避让） ----------
def _avoids(step: float, avoid: set[int]) -> bool:
    """sample-aware：onset ±1 步内不叠鼓（模 16）。"""
    return any((abs(step - a) % STEPS_PER_BAR) <= 1 for a in avoid)


def _filter_steps(steps: list[int], avoid: set[int]) -> list[int]:
    if not avoid:
        return steps
    ok = [s for s in steps if not _avoids(s, avoid)]
    return ok if ok else steps   # 全部被避让时退回原词汇表，保证鼓不断


def _bar_drums(rng: random.Random, profile: str, energy: float, last_bar: bool,
               swing_ms: float, ms_tick: float, avoid: set[int]) -> dict[str, dict]:
    """单 bar 鼓：profile 词汇表 + swing（只延迟奇数 16 分位）+ ghost + 人味化；
    energy<0.5 去 oh/perc 降 hat 密度；avoid 为 sample onset 步（±1 避让 kick/snare）。"""
    bar: dict[str, dict] = {t: {} for t in TRACKS}
    sparse = energy < 0.5

    def place(track: str, step: float, base_vel: int) -> None:
        off = swing_ms if step in SWING_STEPS else _humanize_ms(rng)
        bar[track][step] = {"velocity": _humanize_vel(rng, base_vel), "offset_ms": round(off, 2)}

    def ghost(step: int) -> None:
        off = swing_ms if step in SWING_STEPS else _humanize_ms(rng)
        bar["snare"][step] = {"velocity": rng.randint(35, 58), "offset_ms": off}

    # kick（onset ±1 避让）
    family = rng.choice(KICK_FAMILIES[profile])
    for step in _filter_steps(family, avoid):
        base = rng.randint(92, 110) if step == 0 else rng.randint(70, 100)
        place("kick", step, base)

    # snare + ghost（onset ±1 避让；loose 额外 ghost 3/7/11/15）
    for step in _filter_steps(list(SNARE_STEPS[profile]), avoid):
        place("snare", step, rng.randint(94, 118))
        for g in (step - 1, step + 1):
            if 0 <= g < STEPS_PER_BAR and not _avoids(g, avoid):
                ghost(g)
    if profile == "loose":
        for g in (3, 7, 11, 15):
            if rng.random() < 0.6 and not _avoids(g, avoid):
                ghost(g)
    if last_bar:
        for step in rng.sample([13, 14, 15], k=2):
            if step not in SNARE_STEPS[profile] and not _avoids(step, avoid):
                ghost(step)

    # hat：boom-bap/loose 8 分音（loose 偶发 16 分前移），straight 规整 8 分，halftime 稀疏
    if profile == "straight":
        steps = list(range(0, 16, 2)) if not sparse else [0, 4, 8, 12]
        for step in steps:
            base = rng.randint(56, 76) if step % 4 == 0 else rng.randint(34, 56)
            place("hat", step, base)
    elif profile == "halftime":
        for step in range(0, 16, 2):
            if step % 4 != 0 and rng.random() < 0.4:
                continue
            base = rng.randint(40, 60) if step % 4 == 0 else rng.randint(30, 50)
            place("hat", step, base)
    else:  # boom-bap / loose
        steps = (0, 4, 8, 12) if sparse else range(0, 16, 2)
        for step in steps:
            base = rng.randint(56, 76) if step % 4 == 0 else rng.randint(34, 56)
            place("hat", step, base)
        if profile == "loose" and not sparse and rng.random() < 0.4:
            odd = rng.choice([1, 5, 9, 13])
            place("hat", odd, rng.randint(28, 42))

    # oh / perc：energy<0.5 时去掉
    if not sparse:
        place("oh", rng.choice([5, 13]), rng.randint(46, 70))
        if last_bar:
            perc_steps = rng.sample([1, 3, 6, 9, 11, 14, 15], k=3)
        else:
            perc_steps = rng.sample([2, 5, 7, 10, 14], k=1 if rng.random() < 0.75 else 2)
        for step in perc_steps:
            if step not in SNARE_STEPS[profile]:
                place("perc", step, rng.randint(38, 74))
    return bar


def generate_drum_pattern(rng: random.Random, sections: list[common.Section], profile: str,
                          swing_ms: float, ms_tick: float,
                          avoid_by_bar: dict[int, set[int]]) -> dict[int, dict]:
    """整曲鼓：{bar_index: {track: {step: {velocity, offset_ms}}}}。"""
    pattern: dict[int, dict] = {}
    bar_index = 0
    for sec in sections:
        for b in range(sec.bars):
            pattern[bar_index] = _bar_drums(rng, profile, sec.energy,
                                            b == sec.bars - 1, swing_ms, ms_tick,
                                            avoid_by_bar.get(bar_index, set()))
            bar_index += 1
    return pattern


def _humanize_ms(rng: random.Random) -> float:
    """人味化：非 swing 步 timing ±2-8ms。"""
    return round(rng.uniform(2.0, 8.0) * rng.choice((-1, 1)), 2)


def _humanize_vel(rng: random.Random, base: int) -> int:
    """人味化：velocity ±5-15。"""
    v = base + rng.choice((-1, 1)) * rng.randint(5, 15)
    return max(1, min(127, v))


# ---------- bass（最小 Harmonic Alignment） ----------
def generate_bass(rng: random.Random, sections: list[common.Section], root: int) -> dict[int, dict]:
    """bass 根音跟随 hero key：只走 I(root)/V(+7)/VIII(+12)，不跨调随机；
    intro/outro 静音（Section Mutation：mute）。"""
    pattern: dict[int, dict] = {}
    bar_index = 0
    for sec in sections:
        for _ in range(sec.bars):
            if sec.add_bass:
                steps: dict[int, int] = {0: root}
                if rng.random() < 0.8:
                    steps[8] = root + rng.choice([7, 12])
                if sec.name == "hook" and rng.random() < 0.6:
                    steps[10] = root + 7
                pattern[bar_index] = steps
            bar_index += 1
    return pattern


# ---------- 切片/人声摆放（按 Recipe kind 差异化） ----------
def _placement(section: str, bar: int, step: int, sample_id: str, chop_index: int, pad: int,
               midi_note: int, gain: float, file: str, start: float, end: float,
               stem: str = "", lp_hz: float | None = None, reverse: bool = False,
               fade_gain: float = 1.0) -> dict:
    p = {
        "section": section, "bar": bar, "step": step, "sample_id": sample_id,
        "chop_index": chop_index, "pad": pad, "midi_note": midi_note,
        "gain": round(gain * fade_gain, 3), "file": file,
        "start_sec": round(start, 6), "end_sec": round(end, 6),
    }
    if stem:
        p["stem"] = stem
    if lp_hz is not None:
        p["lp_hz"] = lp_hz
    if reverse:
        p["reverse"] = True
    return p


def _section_mutations(recipe: dict) -> dict[str, dict]:
    return {s["name"]: (s.get("mutation") or {}) for s in recipe["arrangement"]["sections"]}


def _supporting_pools(supporting: list[dict], assets_by_id: dict) -> tuple[list[dict], list[dict]]:
    """supporting 拆成 (texture/transition, vocal) 两池（含 file 解析）。"""
    texture, vocal = [], []
    for m in supporting:
        asset = assets_by_id.get(str(m.get("asset_id"))) or {}
        file = str(asset.get("library_path") or asset.get("path") or "")
        if not file:
            continue
        entry = {"sample_id": str(asset.get("id")), "file": file,
                 "start_sec": float(m.get("start_sec", 0)), "end_sec": float(m.get("end_sec", 0)),
                 "stem": str(m.get("stem") or "source"),
                 "type": str(m.get("type", "")).lower()}
        (vocal if "vocal" in entry["type"] else texture).append(entry)
    return texture, vocal


def place_chops(rng: random.Random, recipe: dict, hero: dict, supporting: list[dict],
                assets_by_id: dict, hero_file: str,
                hook_hero: dict | None = None) -> tuple[list[dict], list[dict]]:
    """按 Recipe 摆放 chop/vocal。返回 (chop_placements, vocal_placements)。
    bar 为全局小节号（0 起）；段落差异来自 manifest 的 mutation（滤波/mute/密度/辅助素材）。"""
    kind = recipe["kind"]
    mutations = _section_mutations(recipe)
    chops: list[dict] = [c for c in recipe["chops"] if not c.get("role")]   # hero 切片
    hero_asset_id = str(hero.get("asset_id"))
    hero_stem = str(hero.get("stem") or "source")
    hero_entry = {"file": hero_file, "start_sec": float(hero["start_sec"]),
                  "end_sec": float(hero["end_sec"])}
    # 乐句触发周期：hero 窗口能覆盖几小节，就隔几小节触发一次（不再每小节重触发同一片段）
    bpm = float((recipe.get("transform") or {}).get("bpm") or 92)
    bar_s = 60.0 / bpm * 4
    hero_dur = max(0.5, float(hero.get("end_sec", 0)) - float(hero.get("start_sec", 0)))
    phrase_bars = max(1, round(hero_dur / bar_s))
    # hook 段对比：同 hero 素材的第二个 Moment（若存在）
    hook_hero_entry = None
    if hook_hero and str(hook_hero.get("asset_id")) == hero_asset_id:
        hook_hero_entry = {"file": hero_file,
                           "start_sec": float(hook_hero["start_sec"]),
                           "end_sec": float(hook_hero["end_sec"])}
    texture_pool, vocal_pool = _supporting_pools(supporting, assets_by_id)
    placements: list[dict] = []
    vocals: list[dict] = []

    def hero_p(section: str, bar: int, gain: float, pad: int = 1, midi: int | None = None,
               lp_hz: float | None = None, reverse: bool = False, fade_gain: float = 1.0) -> dict:
        return _placement(section, bar, 0, hero_asset_id, 0, pad,
                          midi if midi is not None else common.MIDI_CHOP_BASE + pad - 1,
                          gain, hero_file, hero_entry["start_sec"], hero_entry["end_sec"],
                          stem=hero_stem, lp_hz=lp_hz, reverse=reverse, fade_gain=fade_gain)

    bar_idx = 0
    n_pads = max(1, len(chops))
    # 每段 pad 使用顺序：verse 按 1..n 循环（phrase 序）；hook 重排（rng 确定性）
    verse_order = list(range(n_pads))
    hook_order = rng.sample(range(n_pads), k=min(n_pads, 12))
    verse_i = hook_i = 0
    for sec in recipe["arrangement"]["sections"]:
        sec_name = sec["name"]
        mut = mutations.get(sec_name, {})
        if kind == "loop":
            for k in range(sec["bars"]):
                if mut.get("dropout") == "odd_bars" and k % 2 == 1:
                    bar_idx += 1
                    continue
                if k % phrase_bars:          # 乐句触发：hero 音频自然放完再触发下一次
                    bar_idx += 1
                    continue
                lp = mut.get("lp_hz")
                fade = 1.0
                if mut.get("fade_out") and sec["bars"] > 0:
                    fade = 1.0 - 0.35 * k / sec["bars"]
                entry = hook_hero_entry if sec_name == "hook" and hook_hero_entry else hero_entry
                p = _placement(sec_name, bar_idx, 0, hero_asset_id, 0, 1,
                               common.MIDI_CHOP_BASE, mut.get("hero_gain", 0.9),
                               hero_file, entry["start_sec"], entry["end_sec"],
                               stem=hero_stem, lp_hz=lp, fade_gain=fade)
                placements.append(p)
                bar_idx += 1
        elif kind == "chop":
            style = mut.get("chop_style", "long_tail")
            for k in range(sec["bars"]):
                fade = 1.0
                if mut.get("fade_out") and sec["bars"] > 0:
                    fade = 1.0 - 0.35 * k / sec["bars"]
                if style == "phrase_2bar":
                    steps = CHOP_PATTERNS["phrase_2bar_even" if k % 2 == 0 else "phrase_2bar_odd"]
                else:
                    steps = CHOP_PATTERNS.get(style, [0])
                for step in steps:
                    if style in ("syncopated", "breath", "long_tail"):
                        idx = hook_order[hook_i % len(hook_order)]
                        hook_i += 1
                    else:
                        idx = verse_order[verse_i % len(verse_order)]
                        verse_i += 1
                    c = chops[idx] if chops else hero_entry
                    reverse = bool(c.get("reverse", False)) and mut.get("reverse_hits", False)
                    gain = 0.9 if sec_name == "hook" else 0.75
                    placements.append(_placement(
                        sec_name, bar_idx, step, hero_asset_id, idx,
                        int(c.get("pad", 1)), int(c.get("midi_note", common.MIDI_CHOP_BASE)),
                        gain, c.get("file", hero_file), float(c.get("start_sec", hero["start_sec"])),
                        float(c.get("end_sec", hero["end_sec"])), stem=hero_stem,
                        reverse=reverse, fade_gain=fade))
                bar_idx += 1
        else:  # stem：hero 目标 stem 全段 loop；variation 静音（drums carry）
            for k in range(sec["bars"]):
                if mut.get("dropout") == "hero_off":
                    bar_idx += 1
                    continue
                if k % phrase_bars:          # 乐句触发，同上
                    bar_idx += 1
                    continue
                fade = 1.0
                if mut.get("fade_out") and sec["bars"] > 0:
                    fade = 1.0 - 0.35 * k / sec["bars"]
                entry = hook_hero_entry if sec_name == "hook" and hook_hero_entry else hero_entry
                p = _placement(sec_name, bar_idx, 0, hero_asset_id, 0, 1,
                               common.MIDI_CHOP_BASE, 0.85,
                               hero_file, entry["start_sec"], entry["end_sec"],
                               stem=hero_stem, fade_gain=fade)
                if hero_stem == "vocal":   # 目标 stem 是人声 → 走 vocal 层（render 分轨路由）
                    vocals.append(p)
                else:
                    placements.append(p)
                bar_idx += 1

    # 辅助素材：每 verse/intro 一个 texture 垫、每 hook 一个人声 phrase
    # （此前整首只有 2-3 个事件 → 听感"只有一个采样"）
    ti = vi = 0
    bar_pos = 0
    for sec in recipe["arrangement"]["sections"]:
        sec_name = sec["name"]
        bars = int(sec["bars"])
        if sec_name in ("intro", "verse", "verse_variation", "outro") and ti < len(texture_pool):
            t = texture_pool[ti]
            placements.append(_placement(
                sec_name, bar_pos, 8, t["sample_id"], 0, 15 + (ti % 2),
                common.MIDI_CHOP_BASE + 14 + (ti % 2), 0.35, t["file"],
                t["start_sec"], t["end_sec"], stem=t["stem"]))
            ti += 1
        if sec_name == "hook" and vi < len(vocal_pool):
            v = vocal_pool[vi]
            vocals.append(_placement(
                sec_name, bar_pos, 12, v["sample_id"], 0, 16,
                common.MIDI_CHOP_BASE + 15, 0.5, v["file"],
                v["start_sec"], v["end_sec"], stem=v["stem"]))
            vi += 1
        bar_pos += bars
    return placements, vocals


# ---------- 单候选 BeatSpec ----------
def build_spec_for_recipe(recipe: dict, kind: str, run_id: str, hero: dict,
                          supporting: list[dict], assets_by_id: dict, bpm: float,
                          groove_override: str | None) -> common.BeatSpec:
    """一个 Recipe → 一个 BeatSpec（鼓/bass/摆放全部来自 manifest 参数）。"""
    hero_asset = assets_by_id.get(str(hero.get("asset_id"))) or {}
    hero_file = str(hero_asset.get("library_path") or hero_asset.get("path") or "")
    profile = groove_override or recipe["groove_profile"]
    if profile not in recipes.GROOVE_PROFILES:
        print(f"[WARN] 未知 groove {profile}，回退 boom-bap")
        profile = "boom-bap"
    recipe["groove_profile"] = profile           # manifest 同步（render 只读 manifest）
    bars_map = {s["name"]: int(s["bars"]) for s in recipe["arrangement"]["sections"]}
    sections = build_sections(kind, bars_map)
    total_bars = sum(s.bars for s in sections)
    rng = random.Random(recipes._seed(run_id, kind))
    swing_ms = SWING_TICKS.get(SWING_PCT[profile], 0) * _ms_per_tick(bpm)
    ms_tick = _ms_per_tick(bpm)

    # 先摆切片（chop kind 的起始步 = sample onset），再生成带避让的鼓
    hook_hero = next(
        (s for s in supporting
         if str(s.get("asset_id")) == str(hero.get("asset_id")) and s.get("id") != hero.get("id")),
        None)
    chop_placements, vocal_placements = place_chops(rng, recipe, hero, supporting,
                                                    assets_by_id, hero_file,
                                                    hook_hero=hook_hero)
    avoid_by_bar: dict[int, set[int]] = {}
    if kind == "chop":
        for p in chop_placements:
            if p.get("sample_id") == str(hero.get("asset_id")):
                avoid_by_bar.setdefault(int(p["bar"]), set()).add(int(p["step"]))
    else:
        hero_path = common.ROOT / hero_file if hero_file else None
        onset_steps = recipes.hero_onset_steps(hero_path, float(hero["start_sec"]),
                                               float(hero["end_sec"]), bpm)
        for bar in range(total_bars):
            avoid_by_bar[bar] = onset_steps
    drum_pattern = generate_drum_pattern(rng, sections, profile, swing_ms, ms_tick, avoid_by_bar)

    root = recipe["bass_root"]
    bass_pattern = generate_bass(rng, sections, root)

    sample_ids = [str(hero.get("asset_id"))] + [str(m.get("asset_id")) for m in supporting]
    spec = common.BeatSpec(
        beat_id=f"{run_id}-{kind}", bpm=bpm,
        style=f"{profile} ({kind} recipe)", sections=sections,
        sample_ids=list(dict.fromkeys(sample_ids)), drum_pattern=drum_pattern,
        chop_placements=chop_placements, vocal_placements=vocal_placements,
        bass_pattern=bass_pattern, total_bars=total_bars,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    spec.hero_moment_id = str(hero.get("id"))
    spec.recipe_kind = kind
    spec.run_id = run_id
    return spec


def _ms_per_tick(bpm: float) -> float:
    return 60000.0 / bpm / TICKS_PER_BEAT


# ---------- MIDI 导出（沿用旧实现） ----------
def _write_midi(path: Path, notes: list[tuple], bpm: float, track_name: str) -> None:
    midi = MidiFile(ticks_per_beat=TICKS_PER_BEAT)
    track = MidiTrack()
    midi.tracks.append(track)
    track.append(MetaMessage("track_name", name=track_name, time=0))
    track.append(MetaMessage("set_tempo", tempo=bpm2tempo(int(round(bpm))), time=0))
    events: list[tuple[int, Message]] = []
    for start, duration, note, velocity, channel in notes:
        start = max(0, start)
        events.append((start, Message("note_on", note=note,
                                      velocity=max(1, min(127, velocity)), channel=channel, time=0)))
        events.append((start + max(1, duration), Message("note_off", note=note, velocity=0,
                                                         channel=channel, time=0)))
    events.sort(key=lambda item: (item[0], 0 if item[1].type == "note_off" else 1))
    last_tick = 0
    for tick, message in events:
        message.time = max(0, tick - last_tick)
        track.append(message)
        last_tick = tick
    track.append(MetaMessage("end_of_track", time=TICKS_PER_BEAT))
    midi.save(str(path))


def export_midi(spec: common.BeatSpec, midi_dir: Path) -> dict[str, Path]:
    """产出 drums.mid / bass.mid / chops.mid（velocity/offset_ms → 96 PPQ tick）。"""
    drums: list[tuple] = []
    bass: list[tuple] = []
    chops: list[tuple] = []
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
    midi_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for key, notes in files.items():
        path = midi_dir / f"{key}.mid"
        _write_midi(path, notes, spec.bpm, names[key])
        out[key] = path
    return out


# ---------- 入口 ----------
def compose(run_id: str, *, bpm: float | None = None, groove: str | None = None,
            root: str | Path | None = None) -> dict[str, common.BeatSpec] | None:
    """生成一个 run 的三个候选（Loop/Chop/Stem 各一）。
    job 状态 generated 后重跑跳过；同 run_id 重跑（未 generated）产物可复现。"""
    if root:
        recipes.set_test_root(root)
    if recipes.job_status(run_id) == "generated":
        print(f"[compose] job {run_id} 已 generated，跳过（任务可恢复）")
        return None
    moments = recipes.get_moments()
    if not moments:
        sys.exit("[ERROR] 没有可用 moments：请先运行理解层（score/moments 产出）")
    assets = recipes.get_assets()
    assets_by_id = {str(a.get("id")): a for a in assets}
    hero = recipes.select_hero(moments)
    supporting = recipes.select_supporting(moments, hero)
    priors = recipes.recipe_prior(recipes.get_feedback_rows())
    hero_asset = assets_by_id.get(str(hero.get("asset_id"))) or {}
    target_bpm = float(bpm or hero_asset.get("bpm") or hero_asset.get("bpm_est") or 92)
    total_bars = max(8, round(75.0 * target_bpm / 240))
    bars_map = section_bars(total_bars)

    recipes.upsert_job(run_id, "selected")
    manifests = recipes.build_recipes(run_id, hero, supporting, assets_by_id,
                                      target_bpm, bars_map, priors)
    run_dir = common.ROOT / "beats" / run_id
    specs: dict[str, common.BeatSpec] = {}
    for kind in recipes.RECIPE_KINDS:
        manifest = manifests[kind]
        spec = build_spec_for_recipe(manifest, kind, run_id, hero, supporting,
                                     assets_by_id, target_bpm, groove)
        cand_dir = run_dir / kind
        (run_dir / "specs").mkdir(parents=True, exist_ok=True)
        (run_dir / "recipes").mkdir(parents=True, exist_ok=True)
        (run_dir / "specs" / f"{kind}.json").write_text(recipes.spec_dump(spec), encoding="utf-8")
        (run_dir / "recipes" / f"{kind}.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        export_midi(spec, cand_dir / "midi")
        specs[kind] = spec
        duration_s = spec.total_bars * 240.0 / spec.bpm
        print(f"[compose] {kind}: beat_id={spec.beat_id} bpm={spec.bpm:g} "
              f"bars={spec.total_bars} duration_s={duration_s:.1f} "
              f"chops={len(spec.chop_placements)} vocals={len(spec.vocal_placements)} "
              f"hero={spec.hero_moment_id}")
    recipes.mark_job(run_id, "recipes_ready")
    recipes.upsert_run(run_id, status="recipes_ready",
                       hero_moment_id=str(hero.get("id")),
                       hero_asset_id=str(hero.get("asset_id")),
                       seed=recipes._seed(run_id), bpm=round(target_bpm, 2),
                       candidates=[{"kind": k, "recipe_id": manifests[k]["recipe_id"],
                                    "beat_id": f"{run_id}-{k}",
                                    "groove_profile": manifests[k]["groove_profile"]}
                                   for k in recipes.RECIPE_KINDS],
                       recipe_prior=priors, created_at=datetime.now().isoformat(timespec="seconds"))
    print(f"[compose] run={run_id} hero_moment={hero.get('id')} "
          f"priors={priors} → {run_dir}")
    return specs


def main() -> None:
    p = argparse.ArgumentParser(description="BeatLab 生成层：Hero Sample 三 Recipe → 三候选编排")
    p.add_argument("run_id", help="本次生成 run id（job_id == run_id）")
    p.add_argument("--bpm", type=float, help="目标 BPM（默认取 hero asset 的 bpm，再默认 92）")
    p.add_argument("--groove", choices=recipes.GROOVE_PROFILES,
                   help="覆盖三个候选的 groove profile（默认 kind 映射：loop=boom-bap/chop=loose/stem=halftime）")
    p.add_argument("--root", help="隔离根目录（默认 common.ROOT）")
    args = p.parse_args()
    compose(args.run_id, bpm=args.bpm, groove=args.groove, root=args.root)


if __name__ == "__main__":
    main()
