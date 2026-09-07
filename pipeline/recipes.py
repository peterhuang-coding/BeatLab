"""BeatLab 生成层 · 模块 H1：Hero Sample 选择与 Flip Recipe 构建（P0 新增）。

职责（PRD §5 Goal 3）：
1. moment_ranking：按 8 维 scores_json + explain 质量排序 Sample Moments。
2. select_hero：从 Top 中按 explain 质量 + 类型多样性选 1 个 Hero（最多 2 个 supporting）。
3. 三个 Recipe 构建器：
   - Loop：保持原句，BPM 对齐、滤波、dropout 与段落变化，最保留原素材情绪。
   - Chop：按瞬态 + phrase 切分（16 片），重排重音/呼吸/syncopation，变化最大。
   - Stem：只保留目标 stem，重新配鼓/Bass/texture，更干净易混音。
4. recipe_prior：读 feedback 行调整 kind 先验概率（P0 三个 kind 全生成，先验用于排序与记录）。
5. build_provenance：recipe + assets → provenance dict（hero source/rights/moment 区间、
   所有切片来源、pipeline 版本）。

契约（Dev-1/2 冻结，本层只调不改 common）：
- common.get_moments() → rows 含 id/asset_id/type/start_sec/end_sec/bars/stem/scores_json/explain_json
- common.get_assets()  → rows 至少含 id（library_path/bpm/key_note/source/rights 按需）
- common.upsert_job(job_id, status) / common.mark_job(job_id, status) / common.upsert_run(run_id, **fields)
- 生成 job 约定 job_id == run_id（jobs 状态机：selected → recipes_ready → generated）
- 确定性 seed：run_seed = sha256(run_id) 前 8 位十六进制；kind 派生 seed = sha256(run_id:kind)。

Recipe Manifest JSON 结构（冻结契约）：
{recipe_id, kind(loop/chop/stem), hero:{moment_id, asset_id, start_sec, end_sec, stem},
 transform:{bpm, pitch_semitones, stretch, reverse, filter, gain, pan},
 chops:[{file,start_sec,end_sec,pad,midi_note}], arrangement:{sections:[{name,bars,energy}]},
 drum_kit:{kick:[],snare:[],hat:[],oh:[],perc:[]}, bass_root:int, groove_profile:str,
 pipeline:{ver, model, params}, seed}
允许附加键：supporting（辅助 moment 清单）、sections[].mutation、chops[].reverse 等。
"""
from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import Any

import common

PIPELINE_VER = "0.1.0-p0"
RECIPE_KINDS = ("loop", "chop", "stem")
# 三段式主类型优先做 Hero；texture/transition/drum break 优先做 supporting
PRIMARY_TYPES = ("melod", "vocal", "bass", "hook", "solo")
SUPPORTING_TYPES = ("texture", "transition", "drum break", "drum_break", "fx")
# kind → groove profile（PRD §6 四种；straight 可经 --groove 覆盖）
GROOVE_BY_KIND = {"loop": "boom-bap", "chop": "loose", "stem": "halftime"}
GROOVE_PROFILES = ("boom-bap", "loose", "straight", "halftime")
# kind → 段落能量（区分三种 Recipe 的结构逻辑）
SECTION_ENERGY = {
    "loop": {"intro": 0.30, "verse": 0.70, "hook": 0.85, "verse_variation": 0.55, "outro": 0.30},
    "chop": {"intro": 0.25, "verse": 0.65, "hook": 1.00, "verse_variation": 0.50, "outro": 0.25},
    "stem": {"intro": 0.45, "verse": 0.75, "hook": 0.90, "verse_variation": 0.60, "outro": 0.35},
}
SYNTH_KIT = {  # 兜底 kit 相对 ROOT 路径（与 render.ensure_synth_kit 产物名一致）
    "kick": ["kit/synth/kick.wav"],
    "snare": ["kit/synth/snare.wav"],
    "hat": ["kit/synth/hat.wav"],
    "oh": ["kit/synth/oh.wav"],
    "perc": ["kit/synth/hat.wav"],
}
KIT_CLASSES = ("kick", "snare", "hat", "oh", "perc")

# ---------- 确定性 seed ----------
def _seed(run_id: str, kind: str | None = None) -> int:
    """确定性 seed：sha256(run_id[:kind]) 前 8 位十六进制。"""
    s = f"beatlab:{run_id}" if kind is None else f"beatlab:{run_id}:{kind}"
    return int(hashlib.sha256(s.encode("utf-8")).hexdigest()[:8], 16)


# ---------- 契约调用适配 ----------
def _adapt_call(func, *args, **kwargs):
    """适配 common 冻结契约函数：先直调；TypeError 时按签名过滤 kwargs、截断位置参数。"""
    try:
        return func(*args, **kwargs)
    except TypeError:
        pass
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return func(*args, **kwargs)
    params = list(sig.parameters.values())
    if not any(p.kind is p.VAR_KEYWORD for p in params):
        kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
    max_pos = sum(1 for p in params
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
    while len(args) > max_pos:
        args = args[:-1]
    return func(*args, **kwargs)


def _rows(fn, *args) -> list[dict]:
    try:
        out = _adapt_call(fn, *args)
    except Exception as exc:  # 契约缺失/异常：生成层给出明确错误
        raise RuntimeError(f"common.{getattr(fn, '__name__', fn)} 调用失败：{exc}") from exc
    return [dict(r) if not isinstance(r, dict) else r for r in (out or [])]


def get_moments() -> list[dict]:
    return _rows(common.get_moments, common.get_db())


def get_assets() -> list[dict]:
    return _rows(common.get_assets, common.get_db())


def upsert_job(job_id: str, status: str) -> None:
    _adapt_call(common.upsert_job, common.get_db(),
                {"id": job_id, "type": "run", "state": status})


def mark_job(job_id: str, status: str) -> None:
    _adapt_call(common.mark_job, common.get_db(), job_id, status)


def upsert_run(run_id: str, **fields: Any) -> None:
    if "status" in fields:          # 调用方用 status，common 契约用 state
        fields["state"] = fields.pop("status")
    _adapt_call(common.upsert_run, common.get_db(), {"id": run_id, **fields})


def job_status(job_id: str) -> str | None:
    """读 jobs 状态：依次尝试 common.get_jobs()/get_job()；都不可用返回 None。"""
    for name in ("get_jobs", "get_job"):
        fn = getattr(common, name, None)
        if fn is None:
            continue
        try:
            if name == "get_jobs":
                for r in _rows(fn):
                    if str(r.get("id") or r.get("job_id") or "") == job_id:
                        return r.get("status")
            else:
                row = _adapt_call(fn, job_id)
                if row is not None:
                    row = dict(row) if not isinstance(row, dict) else row
                    return row.get("status")
        except Exception:
            continue
    return None


def get_feedback_rows() -> list[dict]:
    """读 feedback 行（Dev-4 契约未冻结时返回 []，不影响生成）。"""
    fn = getattr(common, "get_feedback", None)
    if fn is None:
        return []
    try:
        return _rows(fn)
    except Exception:
        return []


def _json_val(v: Any, default: Any) -> Any:
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


# ---------- Moment 排序与 Hero 选择 ----------
def _moment_total(m: dict) -> float:
    scores = _json_val(m.get("scores_json"), {})
    if isinstance(scores, dict):
        return sum(float(v) for v in scores.values() if isinstance(v, (int, float)))
    return 0.0


def _explain_score(m: dict) -> float:
    """explain 质量：reason 非空 1 分（长度加成）+ risk 非空 0.5 分。"""
    e = _json_val(m.get("explain_json"), {})
    if not isinstance(e, dict):
        return 0.0
    reason = str(e.get("reason") or e.get("why") or "").strip()
    risk = str(e.get("risk") or "").strip()
    return (0.0 if not reason else 1.0 + min(len(reason) / 200.0, 1.0)) + (0.5 if risk else 0.0)


def moment_ranking(moments: list[dict]) -> list[dict]:
    """按 8 维 scores 总分降序；并列时 explain 质量优先（稳定排序）。"""
    return sorted(moments, key=lambda m: (_moment_total(m), _explain_score(m)), reverse=True)


def _is_primary(t: str) -> bool:
    t = t.lower().replace("_", " ")
    return any(k in t for k in PRIMARY_TYPES)


def _is_supporting_type(t: str) -> bool:
    t = t.lower().replace("_", " ")
    return any(k in t for k in SUPPORTING_TYPES)


def select_hero(moments: list[dict]) -> dict:
    """一个 beat 只有一个 Hero：主类型（旋律/人声/bass）优先；Top-3 无则放宽到
    Top-10；全库无主类型才退回得分最高者（texture 是 supporting 素材，不该做 Hero）。"""
    ranked = moment_ranking(moments)
    if not ranked:
        raise ValueError("没有可用 moments：请先运行理解层（Moments 产出）")
    for top_n in (3, 10):
        pool = ranked[: min(top_n, len(ranked))]
        primary = [m for m in pool if _is_primary(str(m.get("type", "")))]
        if primary:
            return max(primary, key=_explain_score)
    return ranked[0]


def select_supporting(moments: list[dict], hero: dict, max_n: int = 2) -> list[dict]:
    """supporting 0-2 个：优先 texture/transition/drum break 等辅助类型、
    不同 asset、不同 moment type；仅用于 texture/vocal accent/transition/鼓层。"""
    rest = [m for m in moments if str(m.get("id")) != str(hero.get("id"))]
    hero_type = str(hero.get("type", ""))

    def key(m: dict) -> tuple:
        t = str(m.get("type", ""))
        return (
            _is_supporting_type(t),                       # 辅助类型优先
            str(m.get("asset_id")) != str(hero.get("asset_id")),  # 不同 asset 优先
            t != hero_type,                               # 类型多样
            _moment_total(m),
            _explain_score(m),
        )

    return sorted(rest, key=key, reverse=True)[:max_n]


# ---------- recipe_prior：feedback → kind 先验 ----------
BASE_PRIOR = {"loop": 0.33, "chop": 0.34, "stem": 0.33}
_PRIOR_ADJUST = {   # 快捷原因文本 → (kind, 乘子)
    "切得太碎": ("chop", 0.85), "切得太多": ("chop", 0.85), "切法": ("chop", 0.9),
    "太像原曲": ("loop", 0.85), "原曲": ("loop", 0.9),
    "没有空间": ("stem", 0.9), "太满": ("stem", 0.9),
    "重新配鼓": ("stem", 1.1), "鼓不对": ("stem", 1.05),
}


def recipe_prior(feedback_rows: list[dict]) -> dict[str, float]:
    """读 feedback（reasons 文本/分维度 scores）调整 kind 先验并归一。
    P0 三个 kind 仍全生成：先验用于候选排序与 run 记录。"""
    prior = dict(BASE_PRIOR)
    for row in feedback_rows or []:
        reasons = row.get("reasons") or row.get("reason") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        for r in reasons:
            for kw, (kind, mul) in _PRIOR_ADJUST.items():
                if kw in str(r):
                    prior[kind] = round(prior[kind] * mul, 4)
        dims = _json_val(row.get("dim_scores"), {})
        if isinstance(dims, dict):   # 分维度低分：素材/切法/鼓/结构 低 → 微调
            chop_v = float(dims.get("切法") or dims.get("chop") or 0)
            loop_v = float(dims.get("素材") or dims.get("material") or 0)
            if chop_v and chop_v < 3:
                prior["chop"] *= 0.9
            if loop_v and loop_v < 3:
                prior["loop"] *= 0.9
    total = sum(prior.values())
    if total <= 0:
        return dict(BASE_PRIOR)
    out = {k: round(v / total, 4) for k, v in prior.items()}
    diff = round(1.0 - sum(out.values()), 4)   # 舍入残差补到最大项，保证总和严格为 1
    if diff:
        k_max = max(out, key=out.get)
        out[k_max] = round(out[k_max] + diff, 4)
    return out


# ---------- kit 选择（recipe 可复现：记录确切文件路径） ----------
def choose_kit() -> dict[str, list[str]]:
    """从 ROOT/kit/kit.json 每类取第 1 个；缺失/损坏用 synth 兜底路径。"""
    kit_json = common.ROOT / "kit" / "kit.json"
    if kit_json.exists():
        try:
            data = json.loads(kit_json.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out = {}
                for cls in KIT_CLASSES:
                    lst = data.get(cls) or []
                    out[cls] = [str(lst[0])] if lst else list(SYNTH_KIT[cls])
                return out
        except (json.JSONDecodeError, OSError):
            pass
    return {cls: list(SYNTH_KIT[cls]) for cls in KIT_CLASSES}


# ---------- Chop 切分（瞬态 + phrase） ----------
def slice_hero_chops(asset_path: Path, start: float, end: float, n: int = 16,
                     sr: int = 22050) -> list[float]:
    """把 hero 区间切成 n 片：onset 检测选最接近等分点的瞬态做切点，失败退回等分。
    返回 n+1 个边界（源文件绝对秒）。"""
    import librosa
    import numpy as np
    try:
        y, sr_out = common.load_audio_mono(asset_path, sr=sr)
    except Exception:
        y, sr_out = None, sr
    if y is not None:
        try:
            seg = y[int(start * sr_out): int(end * sr_out)]
            if len(seg) >= int(0.05 * sr_out):
                onsets = librosa.onset.onset_detect(y=seg, sr=sr_out, units="samples", backtrack=True)
                targets = np.linspace(0, len(seg), n + 1)[1:-1]
                picks: set[int] = set()
                if len(onsets):
                    for t in targets:
                        picks.add(int(min(onsets, key=lambda o: abs(o - t))))
                    picks = {p for p in picks if 0 < p < len(seg)}
                bounds = sorted({0, len(seg)} | picks)
                if len(bounds) - 1 >= max(2, n // 2):   # 瞬态够密才采用，否则等分兜底
                    seg_start = int(start * sr_out)
                    return [start + b / sr_out for b in bounds]
        except Exception:
            pass
    dur = max(0.0, end - start)
    return [round(start + dur * i / n, 6) for i in range(n + 1)]


def hero_onset_steps(asset_path: Path | None, start: float, end: float, bpm: float,
                     sr: int = 22050) -> set[int]:
    """hero 窗内 onset 映射为 16 分步（0-15，模 16）：sample-aware drums 避让用。
    无音频/失败返回空集（鼓照常生成）。"""
    if not asset_path or not Path(asset_path).exists():
        return set()
    import librosa
    try:
        y, sr_out = common.load_audio_mono(Path(asset_path), sr=sr)
    except Exception:
        return set()
    try:
        seg = y[int(start * sr_out): int(end * sr_out)]
        if len(seg) < int(0.05 * sr_out):
            return set()
        onsets = librosa.onset.onset_detect(y=seg, sr=sr_out, units="time")
        step_s = 60.0 / bpm / 4.0
        return {int(o / step_s) % 16 for o in onsets}
    except Exception:
        return set()


# ---------- Recipe 构建 ----------
def _hero_chop_entry(hero: dict, asset_path: str, pad: int, stem_file: str | None = None,
                     reverse: bool = False) -> dict:
    """hero 区间 → chops 条目；stem 版 file 指向目标 stem 文件。"""
    entry = {
        "file": stem_file or asset_path,
        "start_sec": float(hero["start_sec"]),
        "end_sec": float(hero["end_sec"]),
        "pad": pad,
        "midi_note": common.MIDI_CHOP_BASE + pad - 1,
    }
    if reverse:
        entry["reverse"] = True
    return entry


def _supporting_entries(supporting: list[dict], assets_by_id: dict, stem: bool = False) -> list[dict]:
    """supporting moments → chops 条目（texture/vocal accent/transition）。"""
    out = []
    pad = 14
    for m in supporting:
        asset = assets_by_id.get(str(m.get("asset_id"))) or {}
        path = asset.get("library_path") or asset.get("path") or ""
        if not path:
            continue
        file = _stem_file(path, str(m.get("stem", "source"))) if stem else path
        out.append({
            "file": file, "start_sec": float(m.get("start_sec", 0)),
            "end_sec": float(m.get("end_sec", 0)), "pad": pad,
            "midi_note": common.MIDI_CHOP_BASE + pad - 1,
            "role": "texture" if "texture" in str(m.get("type", "")).lower() else "accent",
        })
        pad += 1
    return out


def _stem_file(library_path: str, stem: str) -> str:
    """library/<cat>/<id>/source.wav → library/<cat>/<id>/stems/<stem>.wav。"""
    p = Path(library_path)
    base = p.parent if p.name == "source.wav" else p
    return str(base / "stems" / f"{stem}.wav")


def _base_manifest(run_id: str, kind: str, hero: dict, supporting: list[dict],
                   assets_by_id: dict, bpm: float, seed: int) -> dict:
    hero_asset = assets_by_id.get(str(hero.get("asset_id"))) or {}
    asset_bpm = float(hero_asset.get("bpm") or hero_asset.get("bpm_est") or 0.0)
    stretch = round(bpm / asset_bpm, 4) if asset_bpm and abs(bpm - asset_bpm) > 0.5 else 1.0
    key_text = hero_asset.get("key_note") or hero_asset.get("key")
    bass_root = parse_key_note(key_text) or 33          # 无 key 用 A1=33
    return {
        "recipe_id": f"{run_id}:{kind}",
        "kind": kind,
        "hero": {
            "moment_id": str(hero.get("id")), "asset_id": str(hero.get("asset_id")),
            "start_sec": float(hero["start_sec"]), "end_sec": float(hero["end_sec"]),
            "stem": str(hero.get("stem") or "source"),
        },
        "supporting": [
            {"moment_id": str(m.get("id")), "asset_id": str(m.get("asset_id")),
             "type": str(m.get("type", "")), "start_sec": float(m.get("start_sec", 0)),
             "end_sec": float(m.get("end_sec", 0)), "stem": str(m.get("stem") or "source")}
            for m in supporting
        ],
        "transform": {"bpm": round(float(bpm), 2), "pitch_semitones": 0, "stretch": stretch,
                      "reverse": False, "filter": "none", "gain": 0.9, "pan": 0.0},
        "chops": [],
        "arrangement": {"sections": []},
        "drum_kit": choose_kit(),
        "bass_root": bass_root,
        "groove_profile": GROOVE_BY_KIND[kind],
        "pipeline": {"ver": PIPELINE_VER, "model": "heuristic-v1",
                     "params": {"target_duration_s": 75.0}},
        "seed": seed,
    }


def _section_list(bars_map: dict[str, int], energy_map: dict[str, float],
                  mutation_map: dict[str, dict]) -> list[dict]:
    """arrangement.sections：[{name, bars, energy, mutation}]（mutation 为附加键）。"""
    out = []
    for name, bars in bars_map.items():
        sec = {"name": name, "bars": int(bars), "energy": energy_map.get(name, 0.7)}
        if name in mutation_map:
            sec["mutation"] = mutation_map[name]
        out.append(sec)
    return out


def build_loop_recipe(run_id: str, hero: dict, supporting: list[dict],
                      assets_by_id: dict, bpm: float, seed: int,
                      bars_map: dict[str, int]) -> dict:
    """Loop：原句 BPM 对齐（stretch）+ 滤波/dropout/段落变化；最保留原素材情绪。"""
    m = _base_manifest(run_id, "loop", hero, supporting, assets_by_id, bpm, seed)
    asset = assets_by_id.get(str(hero.get("asset_id"))) or {}
    hero_file = str(asset.get("library_path") or asset.get("path") or "")
    m["chops"] = [_hero_chop_entry(hero, hero_file, pad=1)]
    m["chops"] += _supporting_entries(supporting, assets_by_id, stem=False)
    m["transform"]["filter"] = "lowpass"               # 整曲统一 LP，段落再细化
    m["arrangement"]["sections"] = _section_list(bars_map, SECTION_ENERGY["loop"], {
        "intro": {"filter": "lowpass", "lp_hz": 2000, "hero_gain": 0.8},
        "verse": {"filter": "none", "hero_gain": 0.9},
        "hook": {"filter": "none", "hero_gain": 1.0},
        "verse_variation": {"filter": "lowpass", "lp_hz": 3500, "dropout": "odd_bars"},
        "outro": {"filter": "lowpass", "lp_hz": 1500, "fade_out": True, "hero_gain": 0.7},
    })
    return m


def build_chop_recipe(run_id: str, hero: dict, supporting: list[dict],
                      assets_by_id: dict, bpm: float, seed: int,
                      bars_map: dict[str, int], n_chops: int = 16) -> dict:
    """Chop：瞬态+phrase 切 16 片重排，重音/呼吸/syncopation；变化最大、最体现制作决策。"""
    m = _base_manifest(run_id, "chop", hero, supporting, assets_by_id, bpm, seed)
    asset = assets_by_id.get(str(hero.get("asset_id"))) or {}
    hero_file = str(asset.get("library_path") or asset.get("path") or "")
    asset_path = common.ROOT / hero_file if hero_file else None
    if hero_file:
        bounds = slice_hero_chops(asset_path, float(hero["start_sec"]), float(hero["end_sec"]), n_chops)
        chops = []
        for i, (s, e) in enumerate(zip(bounds[:-1], bounds[1:])):
            if e <= s:
                continue
            c = {"file": hero_file, "start_sec": round(s, 6), "end_sec": round(e, 6),
                 "pad": i + 1, "midi_note": common.MIDI_CHOP_BASE + i}
            if i == 3:                                 # 1 片 reverse 用于 variation 呼吸
                c["reverse"] = True
            chops.append(c)
        m["chops"] = chops or [_hero_chop_entry(hero, hero_file, pad=1)]
    m["chops"] += _supporting_entries(supporting, assets_by_id, stem=False)
    m["transform"]["filter"] = "highpass"
    m["arrangement"]["sections"] = _section_list(bars_map, SECTION_ENERGY["chop"], {
        "intro": {"density": 1, "chop_style": "long_tail"},
        "verse": {"density": 4, "chop_style": "phrase_2bar"},
        "hook": {"density": 6, "chop_style": "syncopated"},
        "verse_variation": {"density": 2, "chop_style": "breath", "reverse_hits": True},
        "outro": {"density": 1, "chop_style": "long_tail", "fade_out": True},
    })
    return m


def build_stem_recipe(run_id: str, hero: dict, supporting: list[dict],
                      assets_by_id: dict, bpm: float, seed: int,
                      bars_map: dict[str, int]) -> dict:
    """Stem：只保留目标 stem 做 loop，围绕它重新配鼓/Bass/texture；更干净易混音。"""
    m = _base_manifest(run_id, "stem", hero, supporting, assets_by_id, bpm, seed)
    asset = assets_by_id.get(str(hero.get("asset_id"))) or {}
    hero_file = str(asset.get("library_path") or asset.get("path") or "")
    stem_file = _stem_file(hero_file, str(hero.get("stem") or "source")) if hero_file else ""
    m["chops"] = [_hero_chop_entry(hero, hero_file, pad=1, stem_file=stem_file)]
    m["chops"] += _supporting_entries(supporting, assets_by_id, stem=True)
    m["transform"]["filter"] = "none"                  # stem 已隔离，无需再滤
    m["arrangement"]["sections"] = _section_list(bars_map, SECTION_ENERGY["stem"], {
        "intro": {"drums_carry": True},
        "verse": {"drums_carry": True},
        "hook": {"texture_on": True, "drums_carry": True},
        "verse_variation": {"dropout": "hero_off", "drums_carry": True},
        "outro": {"fade_out": True, "drums_carry": True},
    })
    return m


def build_recipes(run_id: str, hero: dict, supporting: list[dict], assets_by_id: dict,
                  bpm: float, bars_map: dict[str, int], priors: dict[str, float]) -> dict[str, dict]:
    """三个 Recipe = 三个 kind 各一（不是换 seed）。"""
    out = {}
    for kind in RECIPE_KINDS:
        seed = _seed(run_id, kind)
        if kind == "loop":
            out[kind] = build_loop_recipe(run_id, hero, supporting, assets_by_id, bpm, seed, bars_map)
        elif kind == "chop":
            out[kind] = build_chop_recipe(run_id, hero, supporting, assets_by_id, bpm, seed, bars_map)
        else:
            out[kind] = build_stem_recipe(run_id, hero, supporting, assets_by_id, bpm, seed, bars_map)
        out[kind]["pipeline"]["params"]["recipe_prior"] = dict(priors)
    # 候选顺序按先验排序（P0 三个全生成，先验决定 Review 展示顺序）
    return {k: out[k] for k in sorted(RECIPE_KINDS, key=lambda k: -priors.get(k, 0.0))}


# ---------- provenance ----------
def build_provenance(recipe: dict, assets_by_id: dict, run_id: str) -> dict:
    """provenance.json：hero asset 的 source/rights/moment 区间、所有切片来源、pipeline 版本。"""
    hero = recipe["hero"]
    asset = assets_by_id.get(str(hero.get("asset_id"))) or {}
    hero_chops = [c for c in recipe.get("chops", []) if not c.get("role")]
    chops = [{
        "file": c["file"], "start_sec": c["start_sec"], "end_sec": c["end_sec"],
        "pad": c.get("pad"), "midi_note": c.get("midi_note"),
        "asset_id": hero.get("asset_id"), "stem": hero.get("stem"),
        "reverse": bool(c.get("reverse", False)),
    } for c in hero_chops]
    return {
        "run_id": run_id,
        "recipe_id": recipe["recipe_id"],
        "kind": recipe["kind"],
        "hero": {
            "moment_id": hero.get("moment_id"), "asset_id": hero.get("asset_id"),
            "start_sec": hero.get("start_sec"), "end_sec": hero.get("end_sec"),
            "stem": hero.get("stem"),
            "source": asset.get("source"), "rights": asset.get("rights"),
            "license": asset.get("license"), "title": asset.get("title"),
            "artist": asset.get("artist"),
        },
        "supporting": recipe.get("supporting", []),
        "chops": chops,
        "pipeline": recipe.get("pipeline", {}),
        "seed": recipe.get("seed"),
    }


# ---------- BeatSpec 扩展字段序列化（common 冻结，不修改 BeatSpec） ----------
SPEC_EXTRA_KEYS = ("hero_moment_id", "recipe_kind", "run_id")


def spec_dump(spec) -> str:
    """BeatSpec JSON + 新增字段（hero_moment_id/recipe_kind/run_id）。"""
    data = json.loads(spec.to_json())
    for k in SPEC_EXTRA_KEYS:
        if hasattr(spec, k):
            data[k] = getattr(spec, k)
    return json.dumps(data, ensure_ascii=False, indent=2)


def spec_load(text: str):
    """读取含扩展字段的 BeatSpec JSON（common.BeatSpec.from_json 忽略扩展键，再补回）。"""
    spec = common.BeatSpec.from_json(text)
    data = json.loads(text)
    for k in SPEC_EXTRA_KEYS:
        if k in data:
            setattr(spec, k, data[k])
    return spec


# ---------- 小工具 ----------
def parse_key_note(text: Any) -> int | None:
    """key_note 字符串转 MIDI 音符：'A1'→33；支持 'C#2'/'Bb1' 与数字；失败返回 None。"""
    import re
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    if text.isdigit():
        n = int(text)
        return n if 0 <= n <= 127 else None
    mt = re.match(r"^([A-Ga-g])([#b]?)(-?\d)$", text)
    if not mt:
        return None
    base = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[mt.group(1).upper()]
    if mt.group(2) == "#":
        base += 1
    elif mt.group(2) == "b":
        base -= 1
    return (int(mt.group(3)) + 1) * 12 + base


def set_test_root(path: str | Path) -> None:
    """手动/测试用：重定向 common 路径到隔离根目录（ROOT/DB/LIBRARY/MIRROR/STAGING）。"""
    p = Path(path)
    common.ROOT = p
    common.DB_PATH = p / "db.sqlite"
    common.LIBRARY = p / "library"
    common.MIRROR_ROOT = p / "mirror"
    common.STAGING = p / "staging"
