"""pytest 路径与共享 fixture。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# 本机 .env 可能正运行 SHADOW/LIVE；测试必须从隔离的纸面默认开始。
# python-dotenv 默认不覆盖显式环境变量，因此这些值会先于模块导入生效。
os.environ["SHADOW_MODE"] = "false"
os.environ["PUMP_DRY_RUN"] = "1"
os.environ["PUMP_LIVE_CONFIRM"] = "0"


@pytest.fixture(autouse=True)
def _reset_global_risk_guard():
    """全局风控状态不能跨测试泄漏峰值/熔断。"""
    from pumpfun.risk import guard

    guard.reset_halt()
    guard.peak_equity = None
    yield
    guard.reset_halt()
    guard.peak_equity = None
