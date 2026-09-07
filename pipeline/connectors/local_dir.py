"""本地目录 Connector：扫描 SUPPORTED_EXT、按已入库 md5 去重、输出 source manifest。

manifest 条目（成功）: {orig_path, md5, size_bytes, duration_s, title}
manifest 条目（失败）: {orig_path, error}

只做供给，不评判质量；duration 为粗查（ffprobe/soundfile），失败不阻塞。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import SUPPORTED_EXT, md5_file  # noqa: E402
from connectors import register  # noqa: E402
from library import probe_duration  # noqa: E402


@register("local_dir")
class LocalDirConnector:
    """P0 唯一来源：用户本地目录（个人已拥有/已授权素材）。"""

    name = "local_dir"

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.skipped = 0  # 上次 scan 跳过的已入库文件数

    def scan(self, path: str | Path, known_md5s: set[str] | None = None) -> list[dict]:
        """递归扫描目录（或单个文件），输出未入库文件的 manifest。

        跳过 assets 表已有 md5（增量扫描，计数见 self.skipped）；
        单次扫描内重复 md5 也只列一次。
        """
        self.skipped = 0
        root = Path(path)
        if root.is_file():
            return self._scan_file(root, known_md5s or set())
        if not root.is_dir():
            raise NotADirectoryError(f"目录不存在: {root}")
        known = known_md5s or set()
        manifest: list[dict] = []
        seen: set[str] = set()
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in SUPPORTED_EXT:
                continue
            manifest.extend(self._scan_file(f, known, seen))
        return manifest

    def _scan_file(self, f: Path, known: set[str], seen: set[str] | None = None) -> list[dict]:
        if f.suffix.lower() not in SUPPORTED_EXT:
            return []
        try:
            digest = md5_file(f)
        except OSError as e:
            return [{"orig_path": str(f.resolve()), "error": f"读文件失败: {e}"}]
        if digest in known:
            self.skipped += 1
            return []  # 已入库（增量扫描）
        if seen is not None and digest in seen:
            return []  # 本轮已列
        if seen is not None:
            seen.add(digest)
        return [{
            "orig_path": str(f.resolve()),
            "md5": digest,
            "size_bytes": f.stat().st_size,
            "duration_s": probe_duration(f),
            "title": f.stem,
        }]
