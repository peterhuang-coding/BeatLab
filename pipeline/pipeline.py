#!/usr/bin/env python3
"""BeatLab 总编排：A 摄入 → D 拆轨切片 → C 选品评分 → E 编排 → F 渲染+.als → G 交付。

每个节点也可单独手动运行（每节点可手动介入）：
    .venv/bin/python pipeline/pipeline.py ingest <路径...>     # A/B 整合分类
    .venv/bin/python pipeline/pipeline.py separate --best 8   # D 拆轨切片（--best 限制数量）
    .venv/bin/python pipeline/pipeline.py score --all         # C 选品评分
    .venv/bin/python pipeline/pipeline.py compose --best 3    # E 3-4 分钟编排
    .venv/bin/python pipeline/pipeline.py render <beat_id>    # F 渲染 + .als
    .venv/bin/python pipeline/pipeline.py report <beat_id>    # G HTML 交付 + 桌面镜像
    .venv/bin/python pipeline/pipeline.py all [素材路径...]    # 全程（正向循环一次）
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

PY = common.ROOT / ".venv" / "bin" / "python"
PIPE = common.PIPELINE


def run(script: str, *args: str) -> None:
    cmd = [str(PY), str(PIPE / script), *args]
    print(f"\n==> {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True)


def latest_beat_id() -> str:
    beats = common.ROOT / "beats"
    if not beats.exists():
        sys.exit("没有 beats 目录，compose 还没跑？")
    dirs = [p for p in beats.iterdir() if p.is_dir()]
    if not dirs:
        sys.exit("beats 目录为空")
    return max(dirs, key=lambda p: p.stat().st_mtime).name


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    cmd = argv[0]
    rest = argv[1:]

    if cmd == "ingest":
        run("ingest.py", *rest)
    elif cmd == "separate":
        run("separate.py", *rest)
    elif cmd == "score":
        run("score.py", *rest)
    elif cmd == "compose":
        run("compose.py", *rest)
    elif cmd == "render":
        run("render.py", *rest)
    elif cmd == "report":
        run("report.py", *rest)
    elif cmd == "all":
        if rest:
            run("ingest.py", *rest)
        run("separate.py", "--all", "--skip-if-done")
        run("score.py", "--all")
        run("compose.py", "--best", "3")
        beat_id = latest_beat_id()
        run("render.py", beat_id)
        run("report.py", beat_id)
        print(f"\n=== 正向循环完成: {beat_id} ===")
        print(f"试听页: {common.MIRROR_ROOT}/{common.today_str()}/")
    else:
        sys.exit(f"未知命令: {cmd}")


if __name__ == "__main__":
    main()
