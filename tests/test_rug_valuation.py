"""抽池后的持仓估值：权益必须按「能兑现的 SOL」算，废币要核销掉。"""

from __future__ import annotations

import pytest

from pumpfun import config as C
from pumpfun.execution import PaperBroker


def _rugged_position(**over):
    pos = {
        "id": "rug1",
        "mint": "MintRug",
        "symbol": "NOTCOON",
        "entry": 2.0765280169e-09,
        "qty": 25_060_716.53,
        "qty_left": 12_530_358.265318,
        "qty_raw": 12_530_358_265_318,
        "decimals": 6,
        "sol_spent": 0.05,
        "mark": 1.424907381e-08,  # 抽池后失真的盘口价（+586%）
        "dry_run": False,
        "shadow": False,
    }
    pos.update(over)
    return pos


def test_position_value_uses_realizable_not_fake_mark():
    broker = PaperBroker()
    broker.positions.clear()
    pos = _rugged_position(realizable_sol=0.000111)
    broker.positions["MintRug"] = pos

    nominal = pos["qty_left"] * pos["mark"]
    assert nominal == pytest.approx(0.1785, abs=1e-3), "盘口估值确实虚高"
    assert broker.position_value() == pytest.approx(0.000111)
    assert broker.unrealized_pnl() < 0, "抽池仓位不该显示浮盈"


def test_position_value_falls_back_to_mark_without_quote():
    broker = PaperBroker()
    broker.positions.clear()
    broker.positions["MintRug"] = _rugged_position()

    assert broker.position_value() == pytest.approx(0.1785, abs=1e-3)


def test_realizable_above_mark_does_not_inflate():
    """报价偶发高于盘口时取小，不让权益虚高。"""
    broker = PaperBroker()
    broker.positions.clear()
    broker.positions["MintRug"] = _rugged_position(
        mark=2.0e-09, realizable_sol=999.0
    )

    assert broker.position_value() == pytest.approx(12_530_358.265318 * 2.0e-09)


def test_dust_position_written_off_with_loss():
    broker = PaperBroker()
    broker.positions.clear()
    broker.gross_realized = 0.0
    broker.positions["MintRug"] = _rugged_position(realizable_sol=0.000111)

    written = broker.write_off_dust_positions()

    assert len(written) == 1
    assert "MintRug" not in broker.positions, "仓位槽应被释放"
    cost = 2.0765280169e-09 * 12_530_358.265318
    # 放弃该袋代币（不卖），按全损入账
    assert broker.gross_realized == pytest.approx(-cost)


def test_liquid_position_not_written_off():
    broker = PaperBroker()
    broker.positions.clear()
    broker.positions["MintRug"] = _rugged_position(
        realizable_sol=float(C.DUST_WRITEOFF_SOL) * 10
    )

    assert broker.write_off_dust_positions() == []
    assert "MintRug" in broker.positions
