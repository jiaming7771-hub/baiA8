"""双子星模拟盘主循环：15m 选币撮合 + 每小时桌面盈亏。"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from typing import Any

from simlab import config
from simlab.notify.desktop import write_hourly_pnl
from simlab.paper.engine import run_cycle
from simlab.paper.portfolio import mark_equity
from simlab.storage import state as store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("simlab.runner")

_STOP = False


def _handle_stop(signum: int, _frame: Any) -> None:
    global _STOP
    logger.info("收到信号 %s，完成当前周期后退出", signum)
    _STOP = True


def emit_hourly() -> str:
    state = store.load_state()
    marks = state.get("last_marks") or {}
    snap = mark_equity(state, marks)
    return write_hourly_pnl(snap)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="双子星 TOP10 模拟盘")
    parser.add_argument(
        "--once",
        action="store_true",
        help="只跑一轮选币+撮合后退出",
    )
    parser.add_argument(
        "--hourly-now",
        action="store_true",
        help="立即输出一小时盈亏桌面通知后退出",
    )
    parser.add_argument(
        "--cycle-seconds",
        type=int,
        default=None,
        help="覆盖 15m 周期秒数（调试用，如 30）",
    )
    parser.add_argument(
        "--hourly-seconds",
        type=int,
        default=None,
        help="覆盖小时盈亏间隔秒数（调试用）",
    )
    args = parser.parse_args(argv)

    if args.hourly_now:
        print(emit_hourly())
        return 0

    cycle_sec = args.cycle_seconds or config.CYCLE_SECONDS
    hourly_sec = args.hourly_seconds or config.HOURLY_PNL_SECONDS

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    logger.info(
        "启动模拟盘 initial_equity=%.2f cycle=%ss hourly=%ss",
        config.INITIAL_EQUITY,
        cycle_sec,
        hourly_sec,
    )
    store.append_event({"type": "runner_start", "cycle_sec": cycle_sec})

    # 启动先跑一轮 + 推一次小时快照
    summary = run_cycle()
    emit_hourly()
    if args.once:
        logger.info("once 模式结束 equity=%.2f", summary["snapshot"]["equity"])
        return 0

    next_cycle = time.time() + cycle_sec
    next_hourly = time.time() + hourly_sec

    while not _STOP:
        now = time.time()
        sleep_for = min(next_cycle, next_hourly) - now
        if sleep_for > 0:
            time.sleep(min(sleep_for, 5.0))
            continue
        if now >= next_cycle:
            try:
                run_cycle()
            except Exception:
                logger.exception("cycle failed")
                store.append_event({"type": "cycle_error"})
            next_cycle = time.time() + cycle_sec
        if now >= next_hourly:
            try:
                emit_hourly()
            except Exception:
                logger.exception("hourly pnl failed")
            next_hourly = time.time() + hourly_sec

    store.append_event({"type": "runner_stop"})
    logger.info("模拟盘已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
