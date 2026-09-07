#!/usr/bin/env python3
"""Dev-3 生成层验收脚本（隔离测试，不触碰真实 ~/Desktop/BeatLab）。

做法：monkeypatch common.ROOT=/tmp/beatlab-d3-test（先 rm -rf 再建），测试脚本自建
契约表（assets/moments/jobs/runs）并插入 mock 行，monkeypatch common 契约函数后真跑
compose + render。验收项：
① 3 个 Recipe 构建成功且 kind/arrangement 明显不同（Loop 长垫 vs Chop 16 片 vs Stem 单轨）
② compose 出 3 个候选 spec，时长 60-90s，Hero 单一
③ render（synth kit 兜底）出 3 full_mix.wav + 4 dry stems + 3 manifests，时长与 spec 一致
④ 重跑 render 跳过已完成（jobs 状态 generated）
⑤ 同 run_id 重跑 compose 产出可复现（specs/recipes 内容一致）
"""
import io
import json
import math
import shutil
import sqlite3
import struct
import sys
import wave
from contextlib import redirect_stdout
from pathlib import Path

TEST_ROOT = Path("/tmp/beatlab-d3-test")
PIPE = Path(__file__).resolve().parents[1] / "pipeline"
sys.path.insert(0, str(PIPE))

shutil.rmtree(TEST_ROOT, ignore_errors=True)
TEST_ROOT.mkdir(parents=True, exist_ok=True)

import common                 # noqa: E402
import recipes                # noqa: E402
import compose                # noqa: E402
import render                 # noqa: E402
import soundfile as sf        # noqa: E402

recipes.set_test_root(TEST_ROOT)
RUN_ID = "d3test-run-01"
CHECKS = {"pass": 0, "fail": 0}


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS["pass" if cond else "fail"] += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


# ---------- mock 音频（synth kit 兜底场景：无真实 drums stem） ----------
def write_sine(path: Path, dur: float, freq: float, sr: int = 44100,
               amp: float = 0.5, mod_hz: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(dur * sr)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            t = i / sr
            a = amp * (0.6 + 0.4 * math.sin(2 * math.pi * mod_hz * t)) if mod_hz else amp
            v = int(max(-1.0, min(1.0, a)) * 32767 * math.sin(2 * math.pi * freq * t))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))


write_sine(TEST_ROOT / "library/songs/assetA/source.wav", 5.0, 440.0, mod_hz=2.0)
write_sine(TEST_ROOT / "library/songs/assetA/stems/other.wav", 5.0, 330.0)
write_sine(TEST_ROOT / "library/songs/assetB/source.wav", 4.0, 550.0)
write_sine(TEST_ROOT / "library/songs/assetB/stems/other.wav", 4.0, 300.0)
write_sine(TEST_ROOT / "library/songs/assetC/source.wav", 3.0, 220.0)
write_sine(TEST_ROOT / "library/songs/assetC/stems/vocal.wav", 3.0, 260.0)

# ---------- 自建契约表 + mock 行（仅测试用） ----------
conn = sqlite3.connect(str(common.DB_PATH))
conn.executescript("""
CREATE TABLE assets(id TEXT PRIMARY KEY, title TEXT, artist TEXT, source TEXT,
                    rights TEXT, license TEXT, library_path TEXT, bpm REAL,
                    key_note TEXT, duration_s REAL);
CREATE TABLE moments(id TEXT PRIMARY KEY, asset_id TEXT, type TEXT, start_sec REAL,
                     end_sec REAL, bars INTEGER, stem TEXT, scores_json TEXT,
                     explain_json TEXT);
CREATE TABLE jobs(id TEXT PRIMARY KEY, run_id TEXT, status TEXT, updated_at TEXT);
CREATE TABLE runs(run_id TEXT PRIMARY KEY, status TEXT, hero_moment_id TEXT,
                  hero_asset_id TEXT, seed INTEGER, bpm REAL, candidates TEXT,
                  recipe_prior TEXT, created_at TEXT, generated_at TEXT);
""")
conn.executemany("INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?,?)", [
    ("assetA", "Mock Song A", "Tester", "mock://local/assetA", "allowed", "CC-BY",
     "library/songs/assetA/source.wav", 92.0, "A1", 5.0),
    ("assetB", "Mock Song B", "Tester", "mock://local/assetB", "allowed", "CC-BY",
     "library/songs/assetB/source.wav", 92.0, "A1", 4.0),
    ("assetC", "Mock Song C", "Tester", "mock://local/assetC", "needs_review", "CC-BY-NC",
     "library/songs/assetC/source.wav", 92.0, "A1", 3.0),
])
SCORES_HI = {"loopability": 8.5, "memorability": 9.0, "drums_vocal_state": 7.0,
             "key_stability": 8.0, "structure_hit": 7.5, "timbre_uniqueness": 8.5,
             "space": 6.0, "dynamics": 7.0}
SCORES_MID = {"loopability": 6.0, "memorability": 6.5, "drums_vocal_state": 6.0,
              "key_stability": 7.0, "structure_hit": 5.5, "timbre_uniqueness": 6.0,
              "space": 6.5, "dynamics": 5.0}
SCORES_LOW = {"loopability": 3.0, "memorability": 3.5, "drums_vocal_state": 4.0,
              "key_stability": 5.0, "structure_hit": 3.0, "timbre_uniqueness": 3.5,
              "space": 4.0, "dynamics": 3.0}
conn.executemany("INSERT INTO moments VALUES (?,?,?,?,?,?,?,?,?)", [
    ("m1", "assetA", "melodic", 0.5, 2.5, 2, "other",
     json.dumps(SCORES_HI), json.dumps({"reason": "稳定的两小节旋律句，无鼓无vocal，循环性强", "risk": "结尾有轻微衰减"})),
    ("m2", "assetB", "texture", 0.2, 1.2, 1, "other",
     json.dumps(SCORES_MID), json.dumps({"reason": "温暖垫底质感", "risk": "动态较小"})),
    ("m3", "assetC", "vocal phrase", 0.1, 1.0, 1, "vocal",
     json.dumps(SCORES_MID), json.dumps({"reason": "短促人声重音，适合 hook 点缀", "risk": "需要 re-pitch"})),
    ("m4", "assetA", "drum break", 2.0, 3.0, 1, "drums",
     json.dumps(SCORES_LOW), json.dumps({"reason": "过门鼓段", "risk": "与原曲重复度高"})),
    ("m5", "assetB", "transition", 1.0, 1.8, 1, "other",
     json.dumps(SCORES_LOW), json.dumps({"reason": "转折音效", "risk": "单薄"})),
])
conn.commit()
conn.close()


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(str(common.DB_PATH))
    c.row_factory = sqlite3.Row
    return c


# ---------- monkeypatch common 契约函数（与 Dev-1/2 冻结契约对齐） ----------
def _get_moments():
    c = _db()
    try:
        return [dict(r) for r in c.execute("SELECT * FROM moments").fetchall()]
    finally:
        c.close()


def _get_assets():
    c = _db()
    try:
        return [dict(r) for r in c.execute("SELECT * FROM assets").fetchall()]
    finally:
        c.close()


def _upsert_job(job_id, status):
    c = _db()
    c.execute("INSERT INTO jobs(id,run_id,status,updated_at) VALUES(?,?,?,'t') "
              "ON CONFLICT(id) DO UPDATE SET status=excluded.status, updated_at='t'",
              (job_id, job_id, status))
    c.commit()
    c.close()


def _mark_job(job_id, status):
    c = _db()
    c.execute("UPDATE jobs SET status=?, updated_at='t' WHERE id=?", (status, job_id))
    c.commit()
    c.close()


def _upsert_run(run_id, **fields):
    c = _db()
    cols = ["run_id"] + list(fields)
    vals = [run_id] + [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                       for v in fields.values()]
    sets = ", ".join(f"{k}=excluded.{k}" for k in cols[1:])
    c.execute(f"INSERT INTO runs({', '.join(cols)}) VALUES({', '.join('?' * len(cols))}) "
              f"ON CONFLICT(run_id) DO UPDATE SET {sets}", vals)
    c.commit()
    c.close()


def _get_jobs():
    c = _db()
    try:
        return [dict(r) for r in c.execute("SELECT * FROM jobs").fetchall()]
    finally:
        c.close()


common.get_moments = _get_moments
common.get_assets = _get_assets
common.upsert_job = _upsert_job
common.mark_job = _mark_job
common.upsert_run = _upsert_run
common.get_jobs = _get_jobs
common.get_feedback = lambda: []


def job_row(run_id):
    c = _db()
    try:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (run_id,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def norm_spec(path: Path) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    d.pop("created_at", None)
    return d


print(f"== BeatLab Dev-3 验收（隔离根目录 {TEST_ROOT}）==")

# ---------- ①+② compose：三 Recipe + 三候选 ----------
print("\n-- compose 第一轮 --")
specs = compose.compose(RUN_ID)
check("compose 返回 3 个 spec", specs is not None and set(specs) == {"loop", "chop", "stem"})
run_dir = TEST_ROOT / "beats" / RUN_ID
check("specs/*.json 写入", all((run_dir / "specs" / f"{k}.json").exists() for k in ("loop", "chop", "stem")))
check("recipes/*.json 写入", all((run_dir / "recipes" / f"{k}.json").exists() for k in ("loop", "chop", "stem")))
check("midi 写入", all((run_dir / k / "midi" / "drums.mid").exists() for k in ("loop", "chop", "stem")))

manifests = {k: json.loads((run_dir / "recipes" / f"{k}.json").read_text(encoding="utf-8"))
             for k in ("loop", "chop", "stem")}
check("① Recipe kind 齐全", set(manifests) == {"loop", "chop", "stem"})
check("① Hero 单一且为 m1", all(m["hero"]["moment_id"] == "m1" for m in manifests.values()))
check("① Loop=长垫（hero 1 长片+supporting）", len([c for c in manifests["loop"]["chops"] if not c.get("role")]) == 1)
check("① Chop=16 片重排", len([c for c in manifests["chop"]["chops"] if not c.get("role")]) == 16)
check("① Stem=目标 stem 单轨", manifests["stem"]["chops"][0]["file"] == "library/songs/assetA/stems/other.wav")
check("① 段落 mutation 不同（loop 滤波 / chop 密度 / stem 静音）",
      manifests["loop"]["arrangement"]["sections"][0].get("mutation", {}).get("filter") == "lowpass"
      and manifests["chop"]["arrangement"]["sections"][2].get("mutation", {}).get("chop_style") == "syncopated"
      and manifests["stem"]["arrangement"]["sections"][3].get("mutation", {}).get("dropout") == "hero_off")
check("① groove profiles 明显不同", {m["groove_profile"] for m in manifests.values()} == {"boom-bap", "loose", "halftime"})
check("① 每个 manifest 契约字段齐全",
      all(all(k in m for k in ("recipe_id", "kind", "hero", "transform", "chops", "arrangement",
                               "drum_kit", "bass_root", "groove_profile", "pipeline", "seed"))
          for m in manifests.values()))
check("① bass_root 跟随 hero key (A1=33)", all(m["bass_root"] == 33 for m in manifests.values()))
check("① transform BPM 对齐（stretch=1.0 同 bpm）", all(m["transform"]["bpm"] == 92.0 for m in manifests.values()))

specs_d = {k: recipes.spec_load((run_dir / "specs" / f"{k}.json").read_text(encoding="utf-8"))
           for k in ("loop", "chop", "stem")}
durs = {k: s.total_bars * 240.0 / s.bpm for k, s in specs_d.items()}
check("② 时长 60-90s", all(60.0 <= d <= 90.0 for d in durs.values()), f"{durs}")
check("② Hero 单一（spec 扩展字段）",
      all(getattr(s, "hero_moment_id", None) == "m1" and getattr(s, "run_id", None) == RUN_ID
          for s in specs_d.values()))
check("② recipe_kind 各异", {getattr(s, "recipe_kind", None) for s in specs_d.values()} == {"loop", "chop", "stem"})
check("② 结构 Intro/Verse/Hook/Verse Variation/Outro",
      all([sec.name for sec in s.sections] == ["intro", "verse", "hook", "verse_variation", "outro"]
          for s in specs_d.values()))
check("② sample_ids 含 hero asset", all("assetA" in s.sample_ids for s in specs_d.values()))
check("② 三个候选的鼓不同（非换 seed）",
      len({json.dumps(s.drum_pattern, sort_keys=True)[:400] for s in specs_d.values()}) == 3)
check("② bass 只走 I/V/VIII（不跨调随机）",
      all(set(int(n) for st in s.bass_pattern.values() for n in st.values()) <= {33, 40, 45}
          for s in specs_d.values()))

# ---------- ⑤ 确定性：同 run_id 重跑 compose 可复现 ----------
print("\n-- compose 第二轮（确定性复查）--")
specs_1 = {k: norm_spec(run_dir / "specs" / f"{k}.json") for k in ("loop", "chop", "stem")}
recipes_1 = {k: (run_dir / "recipes" / f"{k}.json").read_text(encoding="utf-8") for k in ("loop", "chop", "stem")}
specs2 = compose.compose(RUN_ID)
check("⑤ 重跑 compose 不跳过（未 generated）", specs2 is not None)
specs_2 = {k: norm_spec(run_dir / "specs" / f"{k}.json") for k in ("loop", "chop", "stem")}
recipes_2 = {k: (run_dir / "recipes" / f"{k}.json").read_text(encoding="utf-8") for k in ("loop", "chop", "stem")}
check("⑤ specs 可复现（除 created_at）", specs_1 == specs_2)
check("⑤ recipes 字节级可复现", recipes_1 == recipes_2)

# ---------- ③ render：3 full_mix + 12 dry stems + 3 manifests ----------
print("\n-- render --")
rm = render.render_run(RUN_ID)
check("render 返回 run_manifest", rm is not None and rm.get("status") == "generated")
for k in ("loop", "chop", "stem"):
    cand = run_dir / k
    spec_dur = specs_d[k].total_bars * 240.0 / specs_d[k].bpm
    mix = cand / "full_mix.wav"
    check(f"③ {k}/full_mix.wav 存在", mix.exists())
    mix_dur = sf.info(mix).duration if mix.exists() else 0.0
    check(f"③ {k} 时长与 spec 一致（+0.5s 尾巴）", abs(mix_dur - spec_dur - 0.5) <= 0.05,
          f"mix={mix_dur:.3f} spec={spec_dur:.3f}")
    stems = {n: cand / "stems" / f"{n}.wav" for n in ("chops", "drums", "bass", "vocal")}
    check(f"③ {k} 4 dry stems 存在", all(p.exists() for p in stems.values()))
    if all(p.exists() for p in stems.values()):
        sd = {sf.info(p).duration for p in stems.values()}
        check(f"③ {k} stems 时长一致", len(sd) == 1 and abs(next(iter(sd)) - mix_dur) <= 0.01)
    check(f"③ {k} recipe.json + provenance.json", (cand / "recipe.json").exists() and (cand / "provenance.json").exists())
    chops = list((cand / "chops").glob("*.wav"))
    check(f"③ {k} chop WAV 物化（{len(chops)} 个）", len(chops) >= (16 if k == "chop" else 3), str(len(chops)))
check("③ run_manifest.json 三候选", len(rm["candidates"]) == 3)
check("③ run_manifest 只写 beats/<run_id>/ 内", all(c["full_mix"].startswith(f"{k}/") for k, c in
      zip(("loop", "chop", "stem"), rm["candidates"])))
check("③ provenance 含 source/rights/moment 区间", (lambda p: p["hero"]["source"] == "mock://local/assetA"
      and p["hero"]["rights"] == "allowed" and p["hero"]["start_sec"] == 0.5
      and p["hero"]["end_sec"] == 2.5)(json.loads((run_dir / "loop/provenance.json").read_text(encoding="utf-8"))))
check("③ provenance 含切片来源 + pipeline 版本", (lambda p: len(p["chops"]) >= 1
      and p["pipeline"].get("ver"))(json.loads((run_dir / "chop/provenance.json").read_text(encoding="utf-8"))))

# ---------- ④ 重跑 render 跳过（jobs generated） ----------
print("\n-- render 重跑（任务可恢复）--")
mt_before = {p: p.stat().st_mtime_ns for p in run_dir.rglob("*.wav")}
buf = io.StringIO()
with redirect_stdout(buf):
    rm2 = render.render_run(RUN_ID)
check("④ 重跑 render 跳过", rm2 is None and "跳过" in buf.getvalue(), buf.getvalue().strip()[:120])
mt_after = {p: p.stat().st_mtime_ns for p in run_dir.rglob("*.wav")}
check("④ 产物未重写", mt_before == mt_after)
check("④ jobs 状态 generated", job_row(RUN_ID)["status"] == "generated")

# ---------- compose 在 generated 后跳过 ----------
buf = io.StringIO()
with redirect_stdout(buf):
    specs3 = compose.compose(RUN_ID)
check("compose 在 generated 后跳过", specs3 is None and "已 generated" in buf.getvalue())

# ---------- recipe_prior 单元 ----------
p0 = recipes.recipe_prior([])
check("recipe_prior 默认归一", abs(sum(p0.values()) - 1.0) < 1e-6)
p1 = recipes.recipe_prior([{"reasons": ["切得太碎"]}])
check("recipe_prior feedback 调 chop 概率", p1["chop"] < p0["chop"] and abs(sum(p1.values()) - 1.0) < 1e-6)

# ---------- 隔离检查：未触碰真实 BeatLab ----------
real_root = Path.home() / "Desktop" / "BeatLab"
real_beats = list(real_root.glob("beats/d3test*")) if real_root.exists() else []
check("未触碰真实 ~/Desktop/BeatLab", not real_beats)

print(f"\n== 结果：{CHECKS['pass']} pass / {CHECKS['fail']} fail ==")
sys.exit(1 if CHECKS["fail"] else 0)
