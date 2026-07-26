#!/usr/bin/env python3
"""把实盘机器人拉起为真正脱离终端的守护进程。

macOS 没有 setsid，而 `nohup ... &` 在受控 shell 里启动时，进程会随该 shell
会话被回收一起被杀（实测存活时间从 30 秒到 7 分钟不等，不可靠）。
这里用标准的双重 fork + os.setsid 让它脱离进程组与控制终端。

用法：
    backend/.venv/bin/python scripts/start_live_daemon.py [--log /tmp/a8_bot.log]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = Path("/tmp/a8_bot.log")
PIDFILE = ROOT / "backend" / "pumpfun" / "data" / "live_bot.pid"


def _already_running() -> int:
    """返回在跑的 pid，没有则 0。"""
    try:
        pid = int(PIDFILE.read_text().strip())
    except (OSError, ValueError):
        return 0
    try:
        os.kill(pid, 0)
    except OSError:
        return 0
    return pid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument(
        "--force",
        action="store_true",
        help="已有实例在跑时也强行再起一个（默认拒绝，避免双开重复下单）",
    )
    args = ap.parse_args()

    running = _already_running()
    if running and not args.force:
        print(f"已有实例在跑 pid={running}，拒绝重复启动（--force 可覆盖）")
        return 1

    log_path = Path(args.log)
    interpreter = ROOT / "backend" / ".venv" / "bin" / "python"
    target = ROOT / "run_live_pump.py"
    if not interpreter.exists():
        print(f"❌ 找不到解释器 {interpreter}", file=sys.stderr)
        return 2

    # 第一次 fork：父进程立刻返回，子进程脱离父的进程组
    if os.fork() > 0:
        # 等子进程写好 pidfile 再报告，方便调用方立即校验
        for _ in range(50):
            time.sleep(0.1)
            pid = _already_running()
            if pid:
                print(f"已启动 pid={pid} 日志={log_path}")
                return 0
        print("已 fork，但未在 5 秒内确认 pidfile，请查日志", file=sys.stderr)
        return 0

    os.setsid()  # 新会话首进程，脱离控制终端

    # 第二次 fork：确保不是会话首进程，永远无法再获得控制终端
    if os.fork() > 0:
        os._exit(0)

    os.chdir(str(ROOT))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fd_out = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    fd_in = os.open(os.devnull, os.O_RDONLY)
    os.dup2(fd_in, 0)
    os.dup2(fd_out, 1)
    os.dup2(fd_out, 2)

    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(os.getpid()))

    os.execv(str(interpreter), [str(interpreter), str(target)])
    return 0  # execv 不返回


if __name__ == "__main__":
    raise SystemExit(main())
