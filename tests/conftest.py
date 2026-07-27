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


#: 过滤阈值的代码默认值。本机 .env 常年跑「测试宽松档」，用例直接读会变成
#: 断言操作员当下的调参结果，而非过滤逻辑本身。
FILTER_DEFAULTS = {
    "TRACK_A_AGE_MIN": 5.0,
    "TRACK_A_AGE_MAX": 120.0,
    "TRACK_A_REBOUND_MIN": 0.15,
    "TRACK_A_REBOUND_MAX": 0.80,
    "TRACK_A_PULLBACK_MAX": 0.20,
    "TRACK_A_LIQ_MIN": 10.0,
    "TRACK_A_MIN_TX_M5": 10,
    "TRACK_A_MIN_VOL_M5": 3.0,
    "TRACK_A_BUY_SELL_MIN": 1.3,
    "TRACK_B_ENABLED": True,
    "TRACK_B_AGE_MIN": 45.0,
    "TRACK_B_AGE_MAX": 1440.0,
    "TRACK_B_LIQ_MIN": 30.0,
    "TRACK_B_PULLBACK_MAX": 0.08,
    "TRACK_B_MIN_TX_M5": 15,
    "TRACK_B_MIN_VOL_M5": 8.0,
    "TRACK_B_BUY_SELL_MIN": 1.5,
    "TRACK_B_VOL_SPIKE_RATIO": 2.5,
    "ACTIVITY_MULT_LO": 1.0,
    "ACTIVITY_MULT_HI": 100.0,
    "REBOUND_STRICT_FROM": 0.40,
    "REBOUND_STRICT_BUY_SELL": 2.0,
    "REBOUND_STRICT_PULLBACK": 0.08,
    "CRASH_PULLBACK_MAX": 0.30,
    "MDD_BLACKLIST_PCT": 0.50,
    "MOMENTUM_STREAK_MIN": 1,
    "WICK_SPIKE_RATIO": 2.5,
    "AGE_EXEMPT_VOLUME_M5_SOL": 100.0,
    "AGE_EXEMPT_TX_M5": 200,
    "AGE_EXEMPT_BUY_SELL_RATIO": 3.0,
    "ENTRY_CHG_M5_MIN": 3.0,
    "ENTRY_CHG_M5_MAX": 25.0,
    "ENTRY_PULLBACK_MIN": 0.05,
    "ENTRY_CHG_H1_MAX": 999.0,  # 单测默认不拦 1h；逻辑用例单独收紧
    "ENTRY_REQUIRE_REBOUND_SRC": True,
    "ENTRY_REQUIRE_DUAL_WINDOW": True,
    # 共享闸门另读这几个；不钉住就会漏读本机 .env
    "AGE_MIN_MINUTES": 5.0,
    "AGE_MAX_MINUTES": 120.0,
    "ENTRY_GRADUATED_ONLY": False,
    "REBOUND_SELF_MIN_SPAN_MIN": 10.0,
    "REBOUND_SELF_MIN_POINTS": 6,
    "REBOUND_OHLCV_MAX_SELF_RATIO": 10.0,
}


@pytest.fixture
def pin_filter_defaults(monkeypatch):
    """把过滤阈值钉到代码默认值，隔离本机 .env 调参。"""
    from pumpfun import config as C

    for name, value in FILTER_DEFAULTS.items():
        monkeypatch.setattr(C, name, value)


@pytest.fixture(autouse=True)
def _reset_global_risk_guard():
    """全局风控状态不能跨测试泄漏峰值/熔断。"""
    from pumpfun.risk import guard

    guard.reset_halt()
    guard.peak_equity = None
    yield
    guard.reset_halt()
    guard.peak_equity = None


@pytest.fixture(autouse=True)
def _isolate_trading_files(tmp_path, monkeypatch):
    """所有测试的成交/账户文件落到临时目录，禁止污染本机 shadow_trades。

    同时强制纸面安全默认：本机 .env 可能是 PUMP_DRY_RUN=0 + PUMP_LIVE_CONFIRM=1
    （靠 SHADOW_MODE 挡真单），且 config reload 会 override 进程 env——
    测试绝不允许因此走到 Jupiter 真单路径。
    """
    import audit_ledger as AL
    from pumpfun import config as C
    from pumpfun import shadow_report

    logs = tmp_path / "_iso_logs"
    data = tmp_path / "_iso_data"
    logs.mkdir(exist_ok=True)
    data.mkdir(exist_ok=True)
    monkeypatch.setattr(C, "DATA_DIR", data)
    monkeypatch.setattr(C, "TRADING_LOGS_DIR", logs)
    monkeypatch.setattr(C, "ACCOUNT_FILE", data / "account.json")
    monkeypatch.setattr(C, "POSITIONS_FILE", data / "open_positions.json")
    monkeypatch.setattr(C, "STATE_FILE", data / "bot_state.json")
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", data / "daily_trades.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", data / "trades.jsonl")
    monkeypatch.setattr(C, "EXEC_LOG_FILE", logs / "bot_execution.log")
    monkeypatch.setattr(C, "SHADOW_TRADES_FILE", logs / "shadow_trades.jsonl")
    monkeypatch.setattr(C, "SHADOW_SUMMARY_FILE", logs / "shadow_summary.json")
    monkeypatch.setattr(C, "SYMBOL_COOLDOWN_FILE", data / "symbol_cooldowns.json")
    monkeypatch.setattr(C, "MINT_PERMANENT_BAN_FILE", data / "mint_bans.json")
    monkeypatch.setattr(C, "MINT_LOSS_BAN_FILE", data / "mint_loss_bans.json")
    monkeypatch.setattr(C, "CREATOR_STATS_FILE", data / "creator_stats.json")
    # 复式账本没跟着 DATA_DIR 走：它固定写 backend/audit_data/，是运行中机器人
    # 的真实审计流水。不隔离的话每跑一次测试就往里灌一批假成交，而
    # PaperBroker._restore_account 在没有 account.json 时正是拿它重建现金——
    # 于是「上一次跑测试」会决定「这一次跑测试」的起始余额，用例越跑越红。
    monkeypatch.setattr(AL.pump_ledger, "path", data / "pump_ledger.jsonl")
    monkeypatch.setattr(AL.pump_ledger, "alert_path", data / "pump_alerts.log")
    # —— 安全默认：纸面模拟，禁真单 ——
    monkeypatch.setattr(C, "SHADOW_MODE", False)
    monkeypatch.setattr(C, "DRY_RUN_DEFAULT", True)
    monkeypatch.setattr(C, "LIVE_CONFIRM", False)
    monkeypatch.setattr(C, "MICRO_LIVE", False)
    shadow_report._open_book.clear()
    yield
    shadow_report._open_book.clear()
