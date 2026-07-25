"""
运行-测试-自修复循环：启动前强制 pytest 全绿，方可进入稳定运行态。

用法:
  python run_with_self_heal.py
  python run_with_self_heal.py --max-rounds 5 --skip-main
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
TESTS = ROOT / "tests"
MAX_DEFAULT_ROUNDS = 5


def _ensure_paths() -> None:
    for p in (str(ROOT), str(BACKEND)):
        if p not in sys.path:
            sys.path.insert(0, p)


def _python() -> str:
    venv_py = BACKEND / ".venv" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable


def ensure_pytest(py: str) -> None:
    try:
        subprocess.run([py, "-c", "import pytest"], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        print("[HEAL] 安装 pytest …")
        subprocess.run([py, "-m", "pip", "install", "-q", "pytest"], check=True)


def run_pytest(py: str) -> tuple[bool, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(BACKEND), env.get("PYTHONPATH", "")]
    )
    proc = subprocess.run(
        [py, "-m", "pytest", str(TESTS), "-q", "--tb=short"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out


def parse_failures(output: str) -> list[dict[str, str]]:
    """从 pytest 输出提取失败文件/行号/信息。"""
    hits: list[dict[str, str]] = []
    # e.g. tests/test_pnl.py:42: AssertionError
    for m in re.finditer(
        r"(?P<file>[\w./\\-]+\.py):(?P<line>\d+):\s*(?P<err>.+)", output
    ):
        hits.append(
            {
                "file": m.group("file"),
                "line": m.group("line"),
                "error": m.group("err").strip(),
            }
        )
    return hits


def attempt_heal(failures: list[dict[str, str]], output: str) -> list[str]:
    """
    确定性自修复：仅处理已知、安全的环境/依赖问题。
    业务断言失败会记录定位信息，交由本轮人工/代理修复后重跑。
    """
    actions: list[str] = []
    joined = output.lower()

    if "modulenotfounderror: no module named 'pytest'" in joined:
        ensure_pytest(_python())
        actions.append("installed pytest")

    if "modulenotfounderror: no module named 'pumpfun'" in joined:
        # 保证 backend 在 path（下次子进程通过 PYTHONPATH）
        actions.append("ensure PYTHONPATH includes backend/")

    if "modulenotfounderror: no module named 'simlab'" in joined:
        actions.append("ensure PYTHONPATH includes project root/")

    # 创建数据目录
    data = BACKEND / "pumpfun" / "data"
    logs = BACKEND / "pumpfun" / "trading_logs"
    data.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    actions.append("ensured pumpfun data/log dirs")

    for f in failures[:8]:
        print(
            f"[HEAL] 定位失败 → {f.get('file')}:{f.get('line')} · {f.get('error')}"
        )
    if failures and not actions:
        actions.append("logged failure locations for next repair pass")
    return actions


def start_main_loop(py: str) -> int:
    """自检通过后进入后端服务（可选）。"""
    print("[SUCCESS] 系统自检完成，0 Bug，已进入稳定运行状态！")
    print("[MAIN] 启动 FastAPI / Pump scavenger …")
    return subprocess.call(
        [
            py,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        cwd=str(BACKEND),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="自动化测试 + 自修复循环")
    parser.add_argument("--max-rounds", type=int, default=MAX_DEFAULT_ROUNDS)
    parser.add_argument(
        "--skip-main",
        action="store_true",
        help="仅跑测试，通过后不启动 uvicorn",
    )
    args = parser.parse_args()

    _ensure_paths()
    py = _python()
    print(f"[SELF-HEAL] python={py}")
    print(f"[SELF-HEAL] tests={TESTS}")

    try:
        ensure_pytest(py)
    except Exception:
        traceback.print_exc()
        return 2

    for round_i in range(1, args.max_rounds + 1):
        print(f"\n===== 自检第 {round_i}/{args.max_rounds} 轮 =====")
        ok, output = run_pytest(py)
        print(output[-4000:] if len(output) > 4000 else output)
        if ok:
            print("[SUCCESS] 系统自检完成，0 Bug，已进入稳定运行状态！")
            if args.skip_main:
                return 0
            return start_main_loop(py)

        failures = parse_failures(output)
        print(f"[FAIL] 发现 {len(failures) or '若干'} 处失败，启动自修复…")
        actions = attempt_heal(failures, output)
        for a in actions:
            print(f"[HEAL] {a}")

        # 代理侧即时修复：若本轮仍失败，由 Cursor 代理在外层改代码后再次调用本脚本。
        # 此处对「环境类」问题已处理；业务断言失败则退出非零，便于外层继续修。
        if round_i == args.max_rounds:
            print("[FATAL] 达到最大自修复轮次，仍有失败用例。")
            return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
