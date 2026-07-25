"""实盘主循环 CLI。

默认：沙盒 + dry-run（只打印拟下单，不发真单）。

真下单需同时满足：
  1) 环境变量 LIVE_TRADING=1
  2) LIVE_CONFIRM_NO_WITHDRAW=1
  3) CLI 传入 --live
  4) 不存在 data/KILL_SWITCH 文件
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from typing import Any

from simlab.live import config as C
from simlab.live.engine import run_live_cycle
from simlab.live.exchange import (
    LiveSafetyError,
    close_exchange,
    create_exchange,
    verify_api_permissions,
)
from simlab.live.state import kill_switch_active
from simlab.notify.desktop import desktop_notify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("simlab.live.runner")

_STOP = False


def _handle_stop(signum: int, _frame: Any) -> None:
    global _STOP
    logger.info("信号 %s，准备停止", signum)
    _STOP = True


async def _async_main(args: argparse.Namespace) -> int:
    dry_run = not args.live
    if args.live:
        if not C.LIVE_TRADING:
            logger.error("拒绝：--live 需要环境变量 LIVE_TRADING=1")
            return 2
        if C.SANDBOX:
            logger.warning("注意：当前仍是 SANDBOX/测试网（LIVE_SANDBOX 未关闭）")
        else:
            logger.error(
                "!!! 生产实盘模式 !!! 资金池=%.1f%% 单笔风险=%.2f%% 杠杆≤%sx",
                C.POOL_FRACTION * 100,
                C.RISK_PERCENT * 100,
                C.MAX_LEVERAGE,
            )
            desktop_notify("双子星实盘", "生产模式已启动，请监控仓位")

    if kill_switch_active():
        logger.error("KILL_SWITCH 存在于 %s，拒绝启动", C.KILL_SWITCH_PATH)
        return 3

    exchange = None
    try:
        exchange = await create_exchange()
        report = await verify_api_permissions(exchange)
        logger.info("权限自检通过: %s", report)

        result = await run_live_cycle(exchange, dry_run=dry_run)
        logger.info(
            "cycle done equity=%.2f pool=%.2f top3=%s actions=%s",
            result.get("equity") or 0,
            result.get("pool") or 0,
            result.get("top3"),
            len(result.get("actions") or []),
        )
        if args.once:
            return 0

        cycle_sec = args.cycle_seconds or C.CYCLE_SECONDS
        while not _STOP:
            await asyncio.sleep(cycle_sec)
            if _STOP:
                break
            if kill_switch_active():
                logger.error("KILL_SWITCH 激活，退出循环")
                break
            try:
                result = await run_live_cycle(exchange, dry_run=dry_run)
                logger.info("cycle=%s actions=%s", result.get("cycle"), result.get("actions"))
            except Exception:
                logger.exception("live cycle failed")
        return 0
    except LiveSafetyError as exc:
        logger.error("安全拦截: %s", exc)
        desktop_notify("双子星实盘·安全拦截", str(exc)[:120])
        return 4
    finally:
        if exchange is not None:
            await close_exchange(exchange)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="双子星前三强 · 保守实盘")
    parser.add_argument(
        "--live",
        action="store_true",
        help="发送真实订单（仍需 LIVE_TRADING=1）；默认 dry-run",
    )
    parser.add_argument("--once", action="store_true", help="只跑一轮")
    parser.add_argument("--cycle-seconds", type=int, default=None)
    args = parser.parse_args(argv)

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
