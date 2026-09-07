"""增量爬取：基于 assets 已有 md5 集合增量扫描 + 简单队列语义（limit/超时/失败收集）。

P0 单进程：无并发调度、无分布式锁；只保证新增内容被发现、失败可追溯、来源状态落库。
crawler 只负责供给，不决定素材质量（质量是理解层的事）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    get_db, known_md5s, now_iso, set_source_crawl, upsert_source,
)
from connectors import get_connector  # noqa: E402


def crawl(source_name: str, path: str, limit: int | None = None,
          timeout_s: float | None = None, force: bool = False,
          conn=None) -> dict:
    """增量扫描一个来源。返回 {source, discovered, failures, truncated, duration_s}。

    - limit：本次最多发现多少新文件（队列容量，超量截断）；
    - timeout_s：整次扫描/收集超时（超时停止继续收集，已发现结果保留）；
    - force：忽略已入库 md5（重扫，供 ingest --force 使用）；
    - 失败条目与来源错误落 sources.error，不阻断其他来源。
    """
    own = conn is None
    if own:
        conn = get_db()
    upsert_source(conn, {
        "id": source_name, "name": source_name, "type": source_name,
        "config": {}, "enabled": 1,
    })
    t0 = time.monotonic()
    skipped = 0
    try:
        connector_cls = get_connector(source_name)
        connector = connector_cls()
        known = set() if force else known_md5s(conn)
        entries = connector.scan(path, known)
        skipped = getattr(connector, "skipped", 0)
    except Exception as e:
        set_source_crawl(conn, source_name, "failed", error=str(e))
        if own:
            conn.close()
        raise

    discovered: list[dict] = []
    failures: list[dict] = []
    deadline = (t0 + timeout_s) if timeout_s else None
    truncated = False
    for entry in entries:
        if limit is not None and len(discovered) >= limit:
            truncated = True
            break
        if deadline is not None and time.monotonic() > deadline:
            truncated = True
            break
        if "error" in entry:
            failures.append(entry)
        else:
            discovered.append(entry)
    set_source_crawl(conn, source_name, "ok", last_cursor=now_iso())
    if own:
        conn.close()
    return {
        "source": source_name,
        "discovered": discovered,
        "failures": failures,
        "skipped": skipped,
        "truncated": truncated,
        "duration_s": round(time.monotonic() - t0, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="BeatLab crawler：增量扫描（P0 单进程）")
    parser.add_argument("--source", default="local_dir", help="connector 名（默认 local_dir）")
    parser.add_argument("--path", required=True, help="来源目录（或单个文件）")
    parser.add_argument("--limit", type=int, default=None, help="最多发现 N 个新文件")
    parser.add_argument("--timeout", type=float, default=None, help="扫描超时（秒）")
    args = parser.parse_args()

    result = crawl(args.source, args.path, limit=args.limit, timeout_s=args.timeout)
    print(f"来源 {result['source']}: 新发现 {len(result['discovered'])} / 失败 {len(result['failures'])}"
          + ("（截断）" if result["truncated"] else ""))
    for entry in result["discovered"]:
        print(f"[新] {entry['orig_path']} md5={entry['md5'][:12]}… dur={entry['duration_s']}")
    for entry in result["failures"]:
        print(f"[失败] {entry['orig_path']} → {entry['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
