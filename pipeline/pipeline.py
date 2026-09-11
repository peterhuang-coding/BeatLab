#!/usr/bin/env python3
"""BeatLab P0 CLI 骨架：ingest / separate / score / compose / render / report / all。

子命令：
    pipeline.py ingest [--source local_dir] --path <目录> [--limit N] [--force]   # 供给层
    pipeline.py separate <参数...>      # 理解层（转调 separate.py，旧命令保持可用）
    pipeline.py score <参数...>         # 理解层（转调 score.py）
    pipeline.py compose <参数...>       # 生成层（转调 compose.py）
    pipeline.py render <参数...>        # 渲染（转调 render.py）
    pipeline.py report <参数...>        # Review（转调 report.py）
    pipeline.py all [--run-id <ID>] [--path <目录>]  # P0 端到端闭环
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import common  # noqa: E402

# PIPE 指向本文件所在目录（部署=ROOT/pipeline；BEATLAB_ROOT 测试隔离时仍可转调）
PIPE = Path(__file__).resolve().parent
_VENV_PY = common.ROOT / ".venv" / "bin" / "python"
PY = _VENV_PY if _VENV_PY.exists() else Path(sys.executable)

DELEGATED = ("separate", "score", "compose", "render", "report")


def run(script: str, *args: str) -> None:
    cmd = [str(PY), str(PIPE / script), *args]
    print(f"\n==> {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(prog="pipeline.py",
                                     description="BeatLab P0 总编排 CLI（各节点也可单独运行）")
    sub = parser.add_subparsers(dest="cmd")

    p_ingest = sub.add_parser("ingest", help="Connector 统一摄入（增量 + rights + provenance）")
    p_ingest.add_argument("paths", nargs="*", help="位置路径（兼容旧用法）")
    p_ingest.add_argument("--source", default="local_dir", help="connector 名（P0 仅 local_dir）")
    p_ingest.add_argument("--path", dest="source_path", help="来源目录")
    p_ingest.add_argument("--limit", type=int, default=None)
    p_ingest.add_argument("--force", action="store_true")

    for name in DELEGATED:
        sp = sub.add_parser(name, help=f"转调 {name}.py（旧子命令保持可用）")
        sp.add_argument("rest", nargs=argparse.REMAINDER, help=f"{name}.py 的参数（透传）")

    p_all = sub.add_parser("all", help="P0 端到端闭环一次")
    p_all.add_argument("paths", nargs="*", help="位置路径（兼容 ingest 旧用法）")
    p_all.add_argument("--source", default="local_dir", help="connector 名（P0 仅 local_dir）")
    p_all.add_argument("--path", dest="source_path", help="来源目录")
    p_all.add_argument("--limit", type=int, default=None)
    p_all.add_argument("--force", action="store_true")
    p_all.add_argument("--run-id", help="compose/render/report 共用的 run id（默认按当前时间生成）")
    args = parser.parse_args()

    if args.cmd is None:
        parser.print_help()
        return 0
    if args.cmd == "ingest":
        argv: list[str] = []
        if args.source_path:
            argv += ["--source", args.source, "--path", args.source_path]
        if args.limit is not None:
            argv += ["--limit", str(args.limit)]
        if args.force:
            argv += ["--force"]
        run("ingest.py", *argv, *args.paths)
    elif args.cmd in DELEGATED:
        run(f"{args.cmd}.py", *args.rest)
    elif args.cmd == "all":
        run_id = args.run_id or datetime.now().strftime("run-%Y%m%d-%H%M%S")
        argv = []
        if args.source_path:
            argv += ["--source", args.source, "--path", args.source_path]
        if args.limit is not None:
            argv += ["--limit", str(args.limit)]
        if args.force:
            argv += ["--force"]
        if args.source_path or args.paths:
            run("ingest.py", *argv, *args.paths)
        run("separate.py", "--all")
        run("score.py", "--all")
        run("compose.py", run_id)
        run("render.py", run_id)
        run("report.py", run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
