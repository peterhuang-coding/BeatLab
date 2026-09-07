"""BeatLab 统一摄入入口（Connector Framework，P0）。

用法：
    .venv/bin/python pipeline/ingest.py --source local_dir --path <目录> [--limit N] [--force]
    .venv/bin/python pipeline/ingest.py <文件|目录...>          # 兼容旧用法（等价 local_dir）

流程：
1. Connector 增量扫描：跳过 assets 表已入库 md5（--force 重扫）；
2. 内容寻址入库：library/<category>/<id>/source.wav（44.1k WAV，ffmpeg 优先）；
3. 写 provenance（orig_path/md5/size_bytes/duration_s/ingested_at）到 assets；
4. 写 rights（默认 needs_review；license 未知时 basis="unknown_license"）；
5. 写 jobs 状态机（state=ingested）+ sources.crawl_state/last_cursor。

BPM/调性/结构等深度分析不在此层（理解层负责，见 assets.analyzed_at）。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    get_db, now_iso, upsert_asset, upsert_job, upsert_rights,
)
import crawler  # noqa: E402
import library  # noqa: E402

# 文件名命中即视为 speech 的关键词（沿用旧 ingest 约定；无音频级判定）
SPEECH_KEYWORDS = (
    "speech", "访谈", "对白", "lecture", "名言", "podcast", "播客",
    "interview", "对话", "口播", "独白", "talk", "voice", "vocal",
)


def guess_category(duration_s: float | None, title: str) -> str:
    """启发式分类（仅时长+文件名；音频级质量理解在分析层做）。"""
    lower = title.lower()
    if any(k in lower for k in SPEECH_KEYWORDS):
        return "speech"
    if duration_s is None:
        return "unknown"
    if duration_s < 3:
        return "one_shots"
    if duration_s < 20:
        return "loops"
    if duration_s < 60:
        return "fx"
    return "songs"


def extract_tags(name: str, category: str) -> list[str]:
    """文件名关键词拆分（去重、小写、长度>=2），末尾附类别。"""
    tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9一-鿿]+", name) if len(t) >= 2]
    seen: set[str] = set()
    tags: list[str] = []
    for t in tokens + [category]:
        if t not in seen:
            seen.add(t)
            tags.append(t)
    return tags[:20]


def ingest_entry(conn, entry: dict, source: str, force: bool) -> tuple[str, str]:
    """摄入 manifest 单条目。返回 (状态, 说明)；状态 ∈ {"ok", "skip", "fail"}。"""
    orig = Path(entry["orig_path"])
    digest = entry["md5"]
    asset_id = digest[:16]
    try:
        # 增量跳过：md5 已入库且非 --force
        existing = conn.execute(
            "SELECT id, library_path FROM assets WHERE md5 = ?", (digest,)).fetchone()
        if existing and not force:
            return "skip", f"已入库 {existing['id']}"

        category = guess_category(entry.get("duration_s"), entry["title"])
        final, duration = library.store_asset(orig, category, asset_id, force=force)

        ingested_at = now_iso()
        asset = {
            "id": asset_id,
            "source_id": source,
            "library_path": str(final),
            "orig_path": str(orig.resolve()),
            "title": entry["title"],
            "license": None,
            "md5": digest,
            "size_bytes": entry.get("size_bytes"),
            "duration_s": round(duration, 3),
            "category": category,
            "tags": extract_tags(entry["title"], category),
            "status": "ingested",
            "stems_ready": 0,
            "ingested_at": ingested_at,
        }
        upsert_asset(conn, asset)
        # rights：本地目录无法确认许可 → needs_review + unknown_license
        upsert_rights(conn, asset_id, state="needs_review", basis="unknown_license",
                      snapshot={"orig_path": asset["orig_path"], "license": None,
                                "source": source, "ingested_at": ingested_at})
        # 状态机：discovered → ingested（后续层用 mark_job 推进）
        upsert_job(conn, {
            "id": asset_id, "type": "asset", "state": "ingested",
            "payload": {"source": source, "orig_path": asset["orig_path"],
                        "category": category, "duration_s": round(duration, 3)},
        })
        library.write_meta(final.parent, {
            "id": asset_id, "category": category, "orig_path": asset["orig_path"],
            "md5": digest, "duration_s": round(duration, 3),
            "source": source, "ingested_at": ingested_at,
            "rights": "needs_review/unknown_license",
        })
        return "ok", f"{category}/{asset_id} {duration:.1f}s"
    except Exception as e:
        return "fail", f"处理失败: {e}"


def ingest_dir(conn, path: str, source: str = "local_dir",
               limit: int | None = None, timeout_s: float | None = None,
               force: bool = False) -> dict:
    """爬取并摄入一个来源目录。返回 {ok, skip, fail, discovered, failures}。"""
    result = crawler.crawl(source, path, limit=limit, timeout_s=timeout_s,
                           force=force, conn=conn)
    stats = {"ok": 0, "skip": result["skipped"], "fail": 0,
             "discovered": len(result["discovered"]), "failures": result["failures"]}
    if result["skipped"]:
        print(f"[跳过] {result['skipped']} 个文件已入库（增量扫描）")
    for entry in result["discovered"]:
        status, msg = ingest_entry(conn, entry, source, force)
        stats[status] += 1
        label = {"ok": "成功", "skip": "跳过", "fail": "失败"}[status]
        print(f"[{label}] {entry['orig_path']} → {msg}")
    for entry in result["failures"]:
        print(f"[失败] {entry['orig_path']} → {entry['error']}")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="BeatLab ingest：Connector 统一摄入入口（P0）")
    parser.add_argument("paths", nargs="*", help="文件或目录（兼容旧用法，等价 --source local_dir --path）")
    parser.add_argument("--source", default="local_dir", help="connector 名（P0 仅 local_dir）")
    parser.add_argument("--path", dest="source_path", help="来源目录")
    parser.add_argument("--limit", type=int, default=None, help="本次最多摄入 N 个新文件")
    parser.add_argument("--timeout", type=float, default=None, help="扫描超时（秒）")
    parser.add_argument("--force", action="store_true", help="已入库也重做")
    args = parser.parse_args()

    if args.source != "local_dir":
        print(f"P0 仅支持 --source local_dir，收到: {args.source}")
        return 2
    targets: list[str] = []
    if args.source_path:
        targets.append(args.source_path)
    targets += args.paths
    if not targets:
        parser.error("需要 --path <目录> 或位置路径")

    conn = get_db()
    ok = skip = fail = 0
    for t in targets:
        stats = ingest_dir(conn, t, source=args.source, limit=args.limit,
                           timeout_s=args.timeout, force=args.force)
        ok += stats["ok"]
        skip += stats["skip"]
        fail += stats["fail"]
    print(f"\n汇总: 成功 {ok} / 跳过 {skip} / 失败 {fail}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
