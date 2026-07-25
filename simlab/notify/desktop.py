"""桌面通知 + 小时盈亏日志。"""

from __future__ import annotations

import logging
import platform
import subprocess
from typing import Any

from simlab import config
from simlab.storage import state as store

logger = logging.getLogger("simlab.notify")


def desktop_notify(title: str, message: str) -> None:
    """macOS Notification Center；其它平台写日志兜底。"""
    system = platform.system()
    try:
        if system == "Darwin":
            # 转义 AppleScript 字符串
            safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
            safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
            script = (
                f'display notification "{safe_msg}" with title "{safe_title}"'
            )
            subprocess.run(
                ["osascript", "-e", script],
                check=False,
                capture_output=True,
                timeout=5,
            )
        elif system == "Linux":
            subprocess.run(
                ["notify-send", title, message],
                check=False,
                capture_output=True,
                timeout=5,
            )
        else:
            logger.info("[DESKTOP] %s | %s", title, message)
    except Exception as exc:
        logger.warning("desktop notify failed: %s", exc)


def format_hourly_pnl(snapshot: dict[str, Any]) -> str:
    eq = snapshot.get("equity", 0)
    start = snapshot.get("equity_start", config.INITIAL_EQUITY)
    realized = snapshot.get("realized_pnl", 0)
    unreal = snapshot.get("unrealized_pnl", 0)
    cash = snapshot.get("cash", 0)
    open_n = snapshot.get("open_positions", 0)
    pending_n = snapshot.get("pending_orders", 0)
    wins = snapshot.get("wins", 0)
    losses = snapshot.get("losses", 0)
    total_ret = (eq - start) / start * 100 if start else 0
    return (
        f"[{store.utc_now()}] "
        f"权益={eq:.2f}U 现金={cash:.2f}U "
        f"已实现={realized:+.2f}U 浮动={unreal:+.2f}U "
        f"总收益={total_ret:+.2f}% "
        f"持仓={open_n} 挂单={pending_n} "
        f"胜负={wins}/{losses}"
    )


def write_hourly_pnl(snapshot: dict[str, Any]) -> str:
    line = format_hourly_pnl(snapshot)
    store.append_text(config.PNL_HOURLY_PATH, line)
    desktop_notify(
        "双子星模拟盘 · 小时盈亏",
        f"权益 {snapshot.get('equity', 0):.2f}U | "
        f"已实现 {snapshot.get('realized_pnl', 0):+.2f}U | "
        f"浮动 {snapshot.get('unrealized_pnl', 0):+.2f}U",
    )
    logger.info(line)
    return line
