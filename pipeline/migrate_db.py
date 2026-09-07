"""P0 一次性迁移：旧三表 → 新八表（additive，旧表保留不动）。

用法：
    .venv/bin/python pipeline/migrate_db.py [--db PATH]

步骤：
1. 备份 db.sqlite → db.sqlite.bak-<epoch>（备份成功后才动库）；
2. ensure_schema() 建新八表；
3. samples → assets（source_id='legacy_local'）；
4. 迁移的每个 asset 补 rights（license 未知 → needs_review/unknown_license）+ jobs（state=ingested）；
5. beats → runs（legacy 任务已交付，state='exported'）；
6. 旧三表一行不改（历史模块继续可用）。

幂等：已存在的 asset/run 不覆盖，可重复执行。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import DB_PATH, ensure_schema, now_iso  # noqa: E402


def backup(db_path: Path) -> Path:
    bak = db_path.with_name(f"{db_path.name}.bak-{int(time.time())}")
    shutil.copy2(db_path, bak)
    print(f"[备份] {db_path} → {bak.name}")
    return bak


def migrate(db_path: Path | None = None) -> dict:
    """执行迁移，返回 {assets, rights, runs, backup}。"""
    db_path = Path(db_path) if db_path else DB_PATH
    if not db_path.exists():
        ensure_schema(sqlite3.connect(db_path))
        print(f"[跳过] {db_path} 不存在，已仅建 schema（无需迁移）")
        return {"assets": 0, "rights": 0, "runs": 0, "backup": None}

    backup(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    # 3) samples → assets（source_id='legacy_local'）
    n_assets = n_rights = 0
    samples = conn.execute("SELECT * FROM samples").fetchall()
    for s in samples:
        asset_id = s["id"]
        if conn.execute("SELECT id FROM assets WHERE id = ?", (asset_id,)).fetchone():
            continue  # 已迁移，幂等跳过
        category = s["category"] or "unknown"
        library_path = s["library_path"]
        size_bytes = None
        if library_path and Path(library_path).exists():
            size_bytes = Path(library_path).stat().st_size
        try:
            tags_list = json.loads(s["tags"]) if s["tags"] else []
        except (json.JSONDecodeError, TypeError):
            tags_list = [t for t in (s["tags"] or "").split() if t]
        stems_ready = 1 if library_path and (Path(library_path).parent / "stems").exists() else 0
        title = Path(s["orig_path"]).stem if s["orig_path"] else None
        conn.execute(
            """INSERT INTO assets (id, source_id, library_path, orig_path, title,
                                   license, md5, size_bytes, duration_s, bpm, bpm_conf,
                                   key_note, key_conf, category, tags_json, status,
                                   stems_ready, ingested_at)
               VALUES (?, 'legacy_local', ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       'ingested', ?, ?)""",
            (asset_id, library_path, s["orig_path"], title,
             s["md5"], size_bytes, s["duration_s"], s["bpm"], s["bpm_conf"],
             s["key_note"], s["key_conf"], category,
             json.dumps(tags_list, ensure_ascii=False), stems_ready,
             s["ingested_at"]),
        )
        n_assets += 1
        # 4) rights：旧数据无 license → needs_review/unknown_license
        conn.execute(
            """INSERT INTO rights (asset_id, state, basis, snapshot_json, checked_at)
               VALUES (?, 'needs_review', 'unknown_license', ?, ?)""",
            (asset_id, json.dumps({"migrated_from": "samples", "license": None},
                                  ensure_ascii=False), now_iso()),
        )
        n_rights += 1
        # jobs 状态机：legacy 素材视为已摄入
        conn.execute(
            """INSERT INTO jobs (id, type, state, payload_json, updated_at)
               VALUES (?, 'asset', 'ingested', ?, ?)""",
            (asset_id, json.dumps({"migrated_from": "samples"}, ensure_ascii=False),
             now_iso()),
        )

    # 5) beats → runs（legacy 任务已渲染交付 → exported）
    n_runs = 0
    beats = conn.execute("SELECT * FROM beats").fetchall()
    for b in beats:
        beat_id = b["beat_id"]
        if conn.execute("SELECT id FROM runs WHERE id = ?", (beat_id,)).fetchone():
            continue
        conn.execute(
            """INSERT INTO runs (id, hero_moment_id, recipe_ids_json, candidate_ids_json,
                                 state, started_at, finished_at)
               VALUES (?, NULL, '[]', ?, 'exported', ?, ?)""",
            (beat_id, b["sample_ids"] or "[]", b["created_at"], b["created_at"]),
        )
        n_runs += 1

    conn.commit()
    print(f"[迁移] assets {n_assets} / rights {n_rights} / runs {n_runs}（旧三表保留不动）")
    conn.close()
    return {"assets": n_assets, "rights": n_rights, "runs": n_runs, "backup": db_path}


def main() -> int:
    parser = argparse.ArgumentParser(description="BeatLab P0 迁移：旧三表 → 新八表（additive）")
    parser.add_argument("--db", default=None, help="目标 db.sqlite（默认 BEATLAB_ROOT/db.sqlite）")
    args = parser.parse_args()
    migrate(Path(args.db) if args.db else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
