"""已知恶名钱包 / 部署者黑名单。

来源：
1) 环境变量 PUMP_BLACKLIST_WALLETS（逗号分隔）
2) 文件 PUMP_BLACKLIST_FILE（默认 data/blacklist_wallets.txt，每行一个 base58；# 注释）
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import config as C

logger = logging.getLogger("pumpfun.blacklist")

_file_cache: set[str] | None = None
_file_mtime: float | None = None


def _load_file() -> set[str]:
    global _file_cache, _file_mtime
    path = Path(C.BLACKLIST_FILE)
    try:
        if not path.is_file():
            _file_cache = set()
            _file_mtime = None
            return _file_cache
        mtime = path.stat().st_mtime
        if _file_cache is not None and _file_mtime == mtime:
            return _file_cache
        out: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.add(s)
        _file_cache = out
        _file_mtime = mtime
        return out
    except Exception:
        logger.exception("读取黑名单文件失败 %s", path)
        return _file_cache or set()


def known_bad_wallets() -> set[str]:
    return set(C.BLACKLIST_WALLETS) | _load_file()


def is_blacklisted(*wallets: str | None) -> tuple[bool, str | None]:
    """任一地址命中黑名单 → (True, matched)。"""
    bad = known_bad_wallets()
    if not bad:
        return False, None
    for w in wallets:
        if w and w in bad:
            return True, w
    return False, None


def clear_cache() -> None:
    global _file_cache, _file_mtime
    _file_cache = None
    _file_mtime = None
