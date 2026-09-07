"""BeatLab 流水线共享层：路径、SQLite schema 与数据层 helper API。

约定：
- 每个模块一个子命令（pipeline.py 编排），可单独手动运行 —— 对应"每节点可手动介入"。
- 所有模块只 import common（+ 各自依赖），接口经 SQLite 与文件系统交换。
- 音源经 ingest 后统一为 library/<category>/<id>/source.wav（44.1k WAV，声道由各模块按需处理）。

P0 重构（PRD §3/§8/§13）：
- ROOT 支持环境变量 BEATLAB_ROOT 覆盖（测试隔离、双机部署用）；
- 旧三表 samples/scores/beats 保留不动（历史模块与产物兼容）；
- 新增八表 sources/assets/rights/moments/recipes/feedback/jobs/runs（schema 冻结）；
- 数据层契约：后续流不直接写 SQL，只调本文件 helper（get_db/ensure_schema/upsert_*/get_*/mark_job）。
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

# ---------- 路径 ----------
ROOT = Path(os.environ.get("BEATLAB_ROOT") or Path.home() / "Desktop" / "BeatLab")
PIPELINE = ROOT / "pipeline"
LIBRARY = ROOT / "library"
DB_PATH = ROOT / "db.sqlite"
STAGING = ROOT / "staging"          # 摄入时的临时目录
MIRROR_ROOT = Path.home() / "Desktop" / "AI_音乐_Demos"   # 产出镜像（日期扁平目录）
DEFAULT_BEAT_DURATION_S = 3.5 * 60  # 3-4 分钟目标取 3.5min 中值

SUPPORTED_EXT = {".wav", ".mp3", ".flac", ".aif", ".aiff", ".m4a", ".ogg"}
CATEGORIES = ("songs", "loops", "one_shots", "speech", "fx", "unknown")

# MIDI 音符映射（与 ai-beat-sketcher 历史约定一致）
MIDI_KICK = 36    # C1
MIDI_SNARE = 38   # D1
MIDI_HAT = 42     # F#1
MIDI_OH = 46      # A#1
MIDI_PERC = 39    # D#1
MIDI_BASS_MIN, MIDI_BASS_MAX = 24, 48
MIDI_CHOP_BASE = 48   # 切片铺 C3 起（pad B1-B16 → 48..63）

# 评分 rubric 权重（sampling-craft-research 调研定稿，CLAP 权重暂并入音色独特性）
RUBRIC_WEIGHTS = {
    "drums_presence": 20,    # 闸门1：鼓存在度 + onset 密度
    "vocal_free": 15,        # 闸门2：无 vocal（1 - vocal RMS 占比）
    "structure_hit": 15,     # 闸门3：结构位置命中 intro/break/outro
    "key_bpm_conf": 10,
    "loopability": 10,
    "timbre_uniqueness": 15,  # 含原 CLAP 语义 5 分
    "harmonicity": 5,
    "dynamics_space": 5,
    "source_prior": 5,
}
SCORE_PASS = 60
GATE_KEYS = ("drums_presence", "vocal_free", "structure_hit")

# ---------- DB ----------
# 旧三表（保留不动：历史模块与产物兼容，迁移见 migrate_db.py）
LEGACY_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id TEXT PRIMARY KEY,              -- md5 前 16 位
    category TEXT NOT NULL,
    orig_path TEXT,
    library_path TEXT,                -- library/<category>/<id>/source.wav
    duration_s REAL, bpm REAL, bpm_conf REAL,
    key_note TEXT, key_conf REAL,
    md5 TEXT, fingerprint TEXT,       -- chromaprint（去重兜底）
    tags TEXT,                        -- JSON list
    ingested_at TEXT
);
CREATE TABLE IF NOT EXISTS scores (
    sample_id TEXT PRIMARY KEY,
    drums_presence REAL, vocal_free REAL, structure_hit REAL,
    key_bpm_conf REAL, loopability REAL, timbre_uniqueness REAL,
    harmonicity REAL, dynamics_space REAL, source_prior REAL,
    total REAL, passed INTEGER, scored_at TEXT
);
CREATE TABLE IF NOT EXISTS beats (
    beat_id TEXT PRIMARY KEY,
    created_at TEXT, bpm REAL, style TEXT,
    duration_s REAL, sample_ids TEXT,  -- JSON list
    spec_path TEXT,                    -- BeatSpec JSON
    wav_path TEXT, als_path TEXT, midi_dir TEXT
);
"""

# P0 新八表（schema 冻结，后续流全按此契约）
P0_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,              -- connector 名（如 local_dir）
    name TEXT NOT NULL,
    type TEXT NOT NULL,               -- connector 类型
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_cursor TEXT,                 -- 增量游标（本地目录为最近扫描时间）
    crawl_state TEXT,                 -- ok/failed
    error TEXT
);
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,              -- 内容哈希 md5[:16]
    source_id TEXT,                   -- 来源（sources.id）
    library_path TEXT,                -- library/<category>/<id>/source.wav
    orig_path TEXT,                   -- provenance：原始来源路径
    title TEXT, artist TEXT, year INTEGER, genre TEXT,
    source_url TEXT, license TEXT,
    md5 TEXT, fingerprint TEXT,       -- chromaprint（去重兜底）
    size_bytes INTEGER, duration_s REAL,
    bpm REAL, bpm_conf REAL, key_note TEXT, key_conf REAL,
    category TEXT, tags_json TEXT,    -- JSON list
    status TEXT NOT NULL DEFAULT 'ingested',
    stems_ready INTEGER NOT NULL DEFAULT 0,
    analyzed_at TEXT, ingested_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_source ON assets(source_id);
CREATE TABLE IF NOT EXISTS rights (
    asset_id TEXT PRIMARY KEY REFERENCES assets(id),
    state TEXT NOT NULL DEFAULT 'needs_review'
        CHECK (state IN ('allowed','private_only','needs_review','blocked')),
    basis TEXT,                       -- unknown_license / cc_license / pd / user_owned ...
    snapshot_json TEXT,               -- 许可快照（来源、采集时间、许可原文）
    checked_at TEXT
);
CREATE TABLE IF NOT EXISTS moments (
    id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    type TEXT NOT NULL,               -- melody/drum_break/vocal_phrase/bass_phrase/texture/transition...
    start_sec REAL NOT NULL, end_sec REAL NOT NULL,
    bars REAL, stem TEXT,
    scores_json TEXT, explain_json TEXT, risks_json TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_moments_asset ON moments(asset_id);
CREATE TABLE IF NOT EXISTS recipes (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('loop','chop','stem')),
    hero_moment_id TEXT,
    manifest_json TEXT,               -- PRD §5 Recipe Manifest
    seed INTEGER,
    pipeline_ver TEXT
);
CREATE INDEX IF NOT EXISTS idx_recipes_run ON recipes(run_id);
CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    run_id TEXT, candidate_id TEXT,
    dims_json TEXT,                   -- 分维度评分
    verdict TEXT,                     -- keep/reject/regenerate/extend/export
    reasons_json TEXT,
    ableton_outcome TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,               -- asset（P0 每素材一条）/ 失败分类：source/rights/file/analysis/generation/render
    state TEXT NOT NULL DEFAULT 'discovered',
    payload_json TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    hero_moment_id TEXT,
    recipe_ids_json TEXT,             -- JSON list
    candidate_ids_json TEXT,          -- JSON list
    state TEXT NOT NULL DEFAULT 'selected',
    started_at TEXT, finished_at TEXT
);
"""

SCHEMA = LEGACY_SCHEMA + P0_SCHEMA

# jobs.state 状态机（PRD §8）：
# discovered → rights_checked → fetched → ingested → analyzed → moments_ready
# → selected → recipes_ready → generated → reviewed → kept/rejected → exported
JOB_STATES = (
    "discovered", "rights_checked", "fetched", "ingested", "analyzed",
    "moments_ready", "selected", "recipes_ready", "generated", "reviewed",
    "kept", "rejected", "exported",
)
RIGHTS_STATES = ("allowed", "private_only", "needs_review", "blocked")
RECIPE_KINDS = ("loop", "chop", "stem")


def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    """幂等建表（旧三表 + 新八表）。"""
    own = conn is None
    if own:
        conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    if own:
        conn.close()


# ---------- 数据层 helper（后续流只调这些，不直接写 SQL） ----------
def _loads(text: str | None) -> Any:
    """JSON 反序列化，空/非法返回 None（不抛）。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _dumps(obj: Any) -> str | None:
    """JSON 序列化；已是文本的透传，None → None。"""
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj  # 已是 JSON 文本
    return json.dumps(obj, ensure_ascii=False)


# --- sources ---
def upsert_source(conn: sqlite3.Connection, source: dict) -> None:
    """注册/更新来源。config 传 dict。"""
    s = dict(source)
    s["config_json"] = _dumps(s.get("config") or {})
    conn.execute(
        """INSERT INTO sources (id, name, type, config_json, enabled, last_cursor, crawl_state, error)
           VALUES (:id, :name, :type, :config_json, :enabled, :last_cursor, :crawl_state, :error)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name, type=excluded.type, config_json=excluded.config_json,
             enabled=excluded.enabled,
             last_cursor=COALESCE(excluded.last_cursor, sources.last_cursor),
             crawl_state=excluded.crawl_state, error=excluded.error""",
        {k: s.get(k) for k in ("id", "name", "type", "config_json", "enabled",
                               "last_cursor", "crawl_state", "error")},
    )
    conn.commit()


def get_source(conn: sqlite3.Connection, source_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["config"] = _loads(d.pop("config_json", None)) or {}
    return d


def set_source_crawl(conn: sqlite3.Connection, source_id: str, crawl_state: str,
                     error: str | None = None, last_cursor: str | None = None) -> None:
    """更新来源爬取状态（crawler 调用）。"""
    conn.execute(
        "UPDATE sources SET crawl_state=?, error=?, last_cursor=COALESCE(?, last_cursor) WHERE id=?",
        (crawl_state, error, last_cursor, source_id),
    )
    conn.commit()


# --- assets ---
_ASSET_COLUMNS = (
    "id", "source_id", "library_path", "orig_path", "title", "artist", "year",
    "genre", "source_url", "license", "md5", "fingerprint", "size_bytes",
    "duration_s", "bpm", "bpm_conf", "key_note", "key_conf", "category",
    "tags_json", "status", "stems_ready", "analyzed_at", "ingested_at",
)


def upsert_asset(conn: sqlite3.Connection, asset: dict) -> None:
    """INSERT OR REPLACE。tags 传 list（内部序列化为 tags_json）；未给列置 NULL/默认。"""
    a = {c: None for c in _ASSET_COLUMNS}
    a.update(asset)
    a["tags_json"] = _dumps(asset.get("tags") or [])
    a.setdefault("status", "ingested")
    a.setdefault("stems_ready", 0)
    conn.execute(
        f"INSERT OR REPLACE INTO assets ({', '.join(_ASSET_COLUMNS)}) "
        f"VALUES ({', '.join(':' + c for c in _ASSET_COLUMNS)})",
        a,
    )
    conn.commit()


def _asset_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["tags"] = _loads(d.pop("tags_json", None)) or []
    return d


def get_asset(conn: sqlite3.Connection, asset_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    return _asset_row_to_dict(row) if row else None


def get_assets(conn: sqlite3.Connection, source_id: str | None = None,
               category: str | None = None, status: str | None = None,
               stems_ready: int | None = None,
               analyzed: bool | None = None) -> list[dict]:
    """按条件查资产；analyzed=True 表示 analyzed_at 非空（已过理解层）。"""
    sql, params = "SELECT * FROM assets WHERE 1=1", []
    if source_id is not None:
        sql += " AND source_id = ?"; params.append(source_id)
    if category is not None:
        sql += " AND category = ?"; params.append(category)
    if status is not None:
        sql += " AND status = ?"; params.append(status)
    if stems_ready is not None:
        sql += " AND stems_ready = ?"; params.append(int(stems_ready))
    if analyzed is not None:
        sql += " AND analyzed_at IS " + ("NOT NULL" if analyzed else "NULL")
    return [_asset_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def known_md5s(conn: sqlite3.Connection) -> set[str]:
    """已入库 md5 集合（增量扫描去重用）。"""
    return {r["md5"] for r in conn.execute("SELECT md5 FROM assets WHERE md5 IS NOT NULL") if r["md5"]}


def set_stems_ready(conn: sqlite3.Connection, asset_id: str, ready: bool = True) -> None:
    conn.execute("UPDATE assets SET stems_ready=? WHERE id=?", (1 if ready else 0, asset_id))
    conn.commit()


# --- rights ---
def upsert_rights(conn: sqlite3.Connection, asset_id: str, state: str = "needs_review",
                  basis: str | None = None, snapshot: dict | None = None) -> None:
    """写入/更新权利状态。state 必须 ∈ RIGHTS_STATES。"""
    if state not in RIGHTS_STATES:
        raise ValueError(f"非法 rights state: {state}（应为 {RIGHTS_STATES}）")
    conn.execute(
        """INSERT INTO rights (asset_id, state, basis, snapshot_json, checked_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(asset_id) DO UPDATE SET
             state=excluded.state, basis=excluded.basis,
             snapshot_json=excluded.snapshot_json, checked_at=excluded.checked_at""",
        (asset_id, state, basis, _dumps(snapshot), now_iso()),
    )
    conn.commit()


def get_rights(conn: sqlite3.Connection, asset_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM rights WHERE asset_id = ?", (asset_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["snapshot"] = _loads(d.pop("snapshot_json", None)) or {}
    return d


def rights_states(conn: sqlite3.Connection) -> dict[str, str]:
    """asset_id → state 映射（生成池准入过滤：needs_review 只能进私人实验区）。"""
    return {r["asset_id"]: r["state"] for r in conn.execute("SELECT asset_id, state FROM rights")}


# --- moments ---
_MOMENT_COLUMNS = ("id", "asset_id", "type", "start_sec", "end_sec", "bars",
                   "stem", "scores_json", "explain_json", "risks_json", "created_at")


def upsert_moment(conn: sqlite3.Connection, moment: dict) -> None:
    """INSERT OR REPLACE。scores/explain/risks 传 dict/list（内部序列化）。
    id 缺失时按 asset:type:start-end 自动生成（hero 选择与 supporting 依赖 id 非空）。"""
    m = {c: None for c in _MOMENT_COLUMNS}
    m.update(moment)
    if not m.get("id"):
        m["id"] = f"{m['asset_id']}:{m['type']}:{float(m['start_sec'] or 0):07.3f}-{float(m['end_sec'] or 0):07.3f}"
    m["scores_json"] = _dumps(moment.get("scores", moment.get("scores_json")))
    m["explain_json"] = _dumps(moment.get("explain", moment.get("explain_json")))
    m["risks_json"] = _dumps(moment.get("risks", moment.get("risks_json")))
    m.setdefault("created_at", now_iso())
    conn.execute(
        f"INSERT OR REPLACE INTO moments ({', '.join(_MOMENT_COLUMNS)}) "
        f"VALUES ({', '.join(':' + c for c in _MOMENT_COLUMNS)})",
        m,
    )
    conn.commit()


def get_moments(conn: sqlite3.Connection, asset_id: str | None = None,
                type: str | None = None) -> list[dict]:
    sql, params = "SELECT * FROM moments WHERE 1=1", []
    if asset_id is not None:
        sql += " AND asset_id = ?"; params.append(asset_id)
    if type is not None:
        sql += " AND type = ?"; params.append(type)
    out = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        d["scores"] = _loads(d.pop("scores_json", None))
        d["explain"] = _loads(d.pop("explain_json", None))
        d["risks"] = _loads(d.pop("risks_json", None))
        out.append(d)
    return out


# --- jobs ---
_JOB_COLUMNS = ("id", "type", "state", "payload_json", "error", "attempts", "updated_at")


def upsert_job(conn: sqlite3.Connection, job: dict) -> None:
    """注册/更新任务。attempts 由 mark_job(inc_attempts=True) 管理，此处不动。"""
    j = {c: None for c in _JOB_COLUMNS}
    j.update(job)
    j["payload_json"] = _dumps(job.get("payload"))
    if j["state"] is None:
        j["state"] = "discovered"
    if j["attempts"] is None:
        j["attempts"] = 0
    j["updated_at"] = now_iso()
    conn.execute(
        """INSERT INTO jobs (id, type, state, payload_json, error, attempts, updated_at)
           VALUES (:id, :type, :state, :payload_json, :error, :attempts, :updated_at)
           ON CONFLICT(id) DO UPDATE SET
             type=excluded.type, state=excluded.state, payload_json=excluded.payload_json,
             error=excluded.error, updated_at=excluded.updated_at""",
        j,
    )
    conn.commit()


def get_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["payload"] = _loads(d.pop("payload_json", None)) or {}
    return d


def mark_job(conn: sqlite3.Connection, job_id: str, state: str,
             error: str | None = None, inc_attempts: bool = False) -> None:
    """推进状态机；state 必须 ∈ JOB_STATES。"""
    if state not in JOB_STATES:
        raise ValueError(f"非法 job state: {state}（应为 {JOB_STATES}）")
    conn.execute(
        "UPDATE jobs SET state=?, error=?, attempts=attempts+?, updated_at=? WHERE id=?",
        (state, error, 1 if inc_attempts else 0, now_iso(), job_id),
    )
    conn.commit()


# --- runs ---
_RUN_COLUMNS = ("id", "hero_moment_id", "recipe_ids_json", "candidate_ids_json",
                "state", "started_at", "finished_at")


def upsert_run(conn: sqlite3.Connection, run: dict) -> None:
    """INSERT OR REPLACE。recipe_ids/candidate_ids 传 list（内部序列化）。"""
    r = {c: None for c in _RUN_COLUMNS}
    r.update(run)
    r["recipe_ids_json"] = _dumps(run.get("recipe_ids", run.get("recipe_ids_json")))
    r["candidate_ids_json"] = _dumps(run.get("candidate_ids", run.get("candidate_ids_json")))
    if r["state"] is None:
        r["state"] = "selected"
    conn.execute(
        """INSERT OR REPLACE INTO runs
           (id, hero_moment_id, recipe_ids_json, candidate_ids_json, state, started_at, finished_at)
           VALUES (:id, :hero_moment_id, :recipe_ids_json, :candidate_ids_json, :state, :started_at, :finished_at)""",
        r,
    )
    conn.commit()


def get_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["recipe_ids"] = _loads(d.pop("recipe_ids_json", None)) or []
    d["candidate_ids"] = _loads(d.pop("candidate_ids_json", None)) or []
    return d


# --- feedback ---
_FB_COLUMNS = ("id", "run_id", "candidate_id", "dims_json", "verdict",
               "reasons_json", "ableton_outcome", "created_at")


def upsert_feedback(conn: sqlite3.Connection, fb: dict) -> None:
    """INSERT OR REPLACE。dims/reasons 传 dict/list（内部序列化）。"""
    f = {c: None for c in _FB_COLUMNS}
    f.update(fb)
    f["dims_json"] = _dumps(fb.get("dims", fb.get("dims_json")))
    f["reasons_json"] = _dumps(fb.get("reasons", fb.get("reasons_json")))
    if f["created_at"] is None:
        f["created_at"] = now_iso()
    conn.execute(
        """INSERT OR REPLACE INTO feedback
           (id, run_id, candidate_id, dims_json, verdict, reasons_json, ableton_outcome, created_at)
           VALUES (:id, :run_id, :candidate_id, :dims_json, :verdict, :reasons_json, :ableton_outcome, :created_at)""",
        f,
    )
    conn.commit()


def get_feedback(conn: sqlite3.Connection, run_id: str | None = None,
                 verdict: str | None = None) -> list[dict]:
    """读 feedback 行（dims/reasons 已反序列化）。run_id/verdict 可选过滤。"""
    q = "SELECT * FROM feedback"
    conds, params = [], []
    if run_id:
        conds.append("run_id = ?")
        params.append(run_id)
    if verdict:
        conds.append("verdict = ?")
        params.append(verdict)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    rows = conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["dims"] = _loads(d.pop("dims_json", None))
        d["reasons"] = _loads(d.pop("reasons_json", None))
        out.append(d)
    return out


def upsert_asset_quality(conn: sqlite3.Connection, sample_id: str,
                         fields: dict, total: float, passed: bool) -> None:
    """层1 资产质量写回：落到旧 scores 表（九项特征 + total/passed）。"""
    f = {c: None for c in ("sample_id", "drums_presence", "vocal_free", "structure_hit",
                           "key_bpm_conf", "loopability", "timbre_uniqueness",
                           "harmonicity", "dynamics_space", "source_prior",
                           "total", "passed", "scored_at")}
    f.update({k: fields.get(k) for k in f if k in fields})
    f["sample_id"] = sample_id
    f["total"] = total
    f["passed"] = int(bool(passed))
    f["scored_at"] = now_iso()
    conn.execute(
        """INSERT OR REPLACE INTO scores
           (sample_id, drums_presence, vocal_free, structure_hit, key_bpm_conf,
            loopability, timbre_uniqueness, harmonicity, dynamics_space, source_prior,
            total, passed, scored_at)
           VALUES (:sample_id, :drums_presence, :vocal_free, :structure_hit,
            :key_bpm_conf, :loopability, :timbre_uniqueness, :harmonicity,
            :dynamics_space, :source_prior, :total, :passed, :scored_at)""",
        f,
    )
    conn.commit()


# ---------- dataclass（历史模块兼容） ----------
@dataclass
class SampleEntry:
    id: str
    category: str
    orig_path: str
    library_path: str
    duration_s: float | None = None
    bpm: float | None = None
    bpm_conf: float | None = None
    key_note: str | None = None
    key_conf: float | None = None
    md5: str | None = None
    fingerprint: str | None = None
    tags: list[str] = field(default_factory=list)
    ingested_at: str = ""

    def source_path(self) -> Path:
        return Path(self.library_path)


@dataclass
class Score:
    sample_id: str
    drums_presence: float = 0.0
    vocal_free: float = 0.0
    structure_hit: float = 0.0
    key_bpm_conf: float = 0.0
    loopability: float = 0.0
    timbre_uniqueness: float = 0.0
    harmonicity: float = 0.0
    dynamics_space: float = 0.0
    source_prior: float = 0.0

    def gates_passed(self) -> bool:
        return all(getattr(self, k) > 0 for k in GATE_KEYS)

    def total(self) -> float:
        raw = sum(getattr(self, k) * w for k, w in RUBRIC_WEIGHTS.items())
        return round(raw, 1) if self.gates_passed() else round(raw * 0.5, 1)


@dataclass
class Chop:
    """一个切片：来自哪条 stem、时间点、pad/MIDI 映射。"""
    stem: str                # "other" | "drums" | "vocal" | "bass" | "source"
    file: str                # 相对 library/<id>/ 的路径
    start_sec: float
    end_sec: float
    pad: int = 0             # 1..16
    midi_note: int = 0       # MIDI_CHOP_BASE + pad - 1
    confidence: float = 0.0


@dataclass
class Section:
    """编排段：3-4 分钟结构的最小单元。"""
    name: str                # intro/verse/chorus/bridge/outro
    bars: int                # 每 bar 16 步
    energy: float            # 0-1，决定鼓层密度与切片密度
    drop_hats: bool = True
    add_bass: bool = True


@dataclass
class BeatSpec:
    """compose 输出、render 输入：完整编排规格。"""
    beat_id: str
    bpm: float
    style: str
    sections: list[Section]
    sample_ids: list[str]
    drum_pattern: dict[str, Any]      # 由 compose 定义：{bar_index: {track: {step: {velocity, offset_ms}}}}
    chop_placements: list[dict]       # {section, bar, step, sample_id, chop_index, pad, midi_note, gain}
    vocal_placements: list[dict]      # 人声 phrase 垫底：{section, bar, file, start_sec, gain}
    bass_pattern: dict[str, Any]      # {bar_index: {step: midi_note}}
    total_bars: int = 0
    created_at: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @staticmethod
    def from_json(text: str) -> "BeatSpec":
        data = json.loads(text)
        spec = BeatSpec(
            beat_id=data["beat_id"], bpm=data["bpm"], style=data["style"],
            sections=[Section(**s) for s in data["sections"]],
            sample_ids=data["sample_ids"], drum_pattern=data["drum_pattern"],
            chop_placements=data["chop_placements"],
            vocal_placements=data["vocal_placements"],
            bass_pattern=data["bass_pattern"], total_bars=data["total_bars"],
            created_at=data.get("created_at", ""),
        )
        return spec


# ---------- 通用小工具 ----------
def md5_file(path: Path) -> str:
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_audio_mono(path: Path, sr: int = 22050) -> tuple[Any, float]:
    """统一读音频为单声道。返回 (samples np.ndarray, sample_rate)。"""
    import librosa
    import numpy as np
    y, sr_out = librosa.load(str(path), sr=sr, mono=True)
    return np.asarray(y, dtype=np.float32), float(sr_out)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))
