"""BeatLab Connector Framework：每个来源一个适配器，统一输出 source manifest。

用法：
    from connectors import get_connector
    manifest = get_connector("local_dir")().scan(path, known_md5s)

约定：
- 适配器只做供给（扫描/去重/粗查），不评判质量、不写库；
- 每个 Connector 失败不影响其他来源（上层收集失败原因）；
- 新来源注册：@register("<name>") 并在本文件末尾 import。
"""
from __future__ import annotations

from typing import Any, Callable

_REGISTRY: dict[str, Callable[..., Any]] = {}


def register(name: str) -> Callable:
    def deco(cls):
        _REGISTRY[name] = cls
        return cls
    return deco


def get_connector(name: str):
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"未注册的 connector: {name}（可用: {', '.join(sorted(_REGISTRY)) or '无'}）"
        ) from None


# 内置 connector：本地目录（P0 唯一来源）
from . import local_dir  # noqa: E402,F401
