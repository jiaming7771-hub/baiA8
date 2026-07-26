"""平仓资金清算：盈亏回流现金、已实现盈亏累加、重启后持久化恢复（含费用摩擦）。"""

from __future__ import annotations

import json

import pytest

import audit_ledger as AL
from audit_ledger import DoubleEntryLedger
from pumpfun import config as C
from pumpfun import execution, journal
from pumpfun.execution import PaperBroker


@pytest.fixture()
def paper(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DATA_DIR", tmp_path)
    monkeypatch.setattr(C, "TRADING_LOGS_DIR", tmp_path)
    monkeypatch.setattr(C, "ACCOUNT_FILE", tmp_path / "account.json")
    monkeypatch.setattr(C, "POSITIONS_FILE", tmp_path / "open_positions.json")
    monkeypatch.setattr(C, "DAILY_TRADES_FILE", tmp_path / "daily_trades.jsonl")
    monkeypatch.setattr(C, "TRADES_FILE", tmp_path / "trades.jsonl")
    monkeypatch.setattr(C, "EXEC_LOG_FILE", tmp_path / "bot_execution.log")
    monkeypatch.setattr(AL, "AUDIT_DIR", tmp_path)
    led = DoubleEntryLedger("pump_scavenger", currency="SOL")
    monkeypatch.setattr(AL, "pump_ledger", led)
    monkeypatch.setattr(execution, "pump_ledger", led)
    return PaperBroker()


SIGNAL = {"mint": "MINTAAA", "symbol": "GHOST", "price": 0.0000010, "score": 80}


def _open(broker, price=SIGNAL["price"]):
    return broker.open_long({**SIGNAL, "price": price})


def test_open_debits_cash_and_friction(paper):
    assert paper.dry_run is True
    start_cash = paper.cash
    start_eq = paper.equity()
    pos = _open(paper)
    assert pos is not None
    # 现金 = 本金 - 仓位本金 - 开仓摩擦
    assert paper.cash < start_cash - pos["sol_spent"] + 1e-12
    assert paper.total_fees > 0 and paper.total_gas > 0 and paper.total_slippage > 0
    # 权益因摩擦略降
    assert paper.equity() < start_eq


def test_profit_flows_back_into_cash_and_realized(paper):
    pos = _open(paper)
    spent = pos["sol_spent"]
    start_cash = paper.cash
    paper.mark(SIGNAL["mint"], SIGNAL["price"] * 2)
    paper.manage({SIGNAL["mint"]: SIGNAL["price"] * 2})

    assert paper.net_realized() != 0 or paper.gross_realized > 0
    assert paper.cash > start_cash
    assert paper.equity() == pytest.approx(
        C.BANKROLL_SOL + paper.net_realized() + paper.unrealized_pnl(), abs=1e-6
    )
    assert spent > 0


def test_full_close_settles_exactly(paper):
    _open(paper)
    # 浮盈盘已被方案B豁免时间止损，这里用硬止损做确定性全平以校验结算口径
    paper.manage({SIGNAL["mint"]: SIGNAL["price"] * 0.7})

    assert not paper.positions
    assert paper.cash == pytest.approx(C.BANKROLL_SOL + paper.net_realized(), abs=1e-6)
    assert paper.equity() == pytest.approx(paper.cash, abs=1e-6)
    assert paper.run_audit(auto_correct=False)["ok"] is True


def test_account_persisted_and_restored_after_restart(paper, tmp_path, monkeypatch):
    _open(paper)
    paper.manage({SIGNAL["mint"]: SIGNAL["price"] * 0.7})
    realized = paper.net_realized()
    assert realized != 0

    saved = json.loads((tmp_path / "account.json").read_text(encoding="utf-8"))
    assert saved["realized_pnl_sol"] == pytest.approx(realized, rel=1e-6)

    revived = PaperBroker()
    assert revived.net_realized() == pytest.approx(realized, rel=1e-6)
    assert revived.equity() == pytest.approx(C.BANKROLL_SOL + realized, abs=1e-5)


def test_bootstrap_from_journal_when_account_file_missing(paper, tmp_path):
    _open(paper)
    paper.manage({SIGNAL["mint"]: SIGNAL["price"] * 0.7})
    realized = paper.net_realized()

    (tmp_path / "account.json").unlink()
    rebuilt = PaperBroker()
    assert rebuilt.net_realized() == pytest.approx(realized, abs=1e-5)
    assert rebuilt.equity() != C.BANKROLL_SOL or realized == 0


def test_equity_matches_ledger_after_multiple_closes(paper):
    for i in range(3):
        mint = f"MINT{i}"
        paper.open_long({**SIGNAL, "mint": mint, "symbol": f"SYM{i}"})
        # 硬止损做确定性全平（不同亏损幅度制造不同已实现盈亏）
        paper.manage({mint: SIGNAL["price"] * (0.70 - 0.05 * i)})

    assert not paper.positions
    audit = paper.run_audit(auto_correct=False)
    assert audit["ok"] is True
    assert paper.equity() == pytest.approx(C.BANKROLL_SOL + paper.net_realized(), abs=1e-5)


def test_module_exposes_unified_equity(paper):
    assert hasattr(execution.PaperBroker, "unrealized_pnl")
    assert hasattr(execution.PaperBroker, "position_value")
    assert hasattr(execution.PaperBroker, "run_audit")
