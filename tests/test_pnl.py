"""多仓独立收益率：禁止状态污染与相同百分比。"""

from __future__ import annotations

from pumpfun.execution import PaperBroker


def _pnl_pct(entry: float, mark: float) -> float:
    return ((mark - entry) / entry) * 100.0 if entry else 0.0


class TestIsolatedPnL:
    def test_formula(self):
        assert _pnl_pct(100, 110) == 10.0
        assert abs(_pnl_pct(1e-6, 1.05e-6) - 5.0) < 1e-9

    def test_multiple_positions_independent_snapshot(self):
        broker = PaperBroker()
        broker.dry_run = True
        specs = [
            ("AAA", "mint_aaa_00000000000000000000pump", 1e-6, 1.05e-6),
            ("BBB", "mint_bbb_00000000000000000000pump", 2e-8, 1.8e-8),
            ("CCC", "mint_ccc_00000000000000000000pump", 5e-7, 5e-7 * 1.1234),
        ]
        for sym, mint, entry, mark in specs:
            opened = broker.open_long(
                {
                    "mint": mint,
                    "symbol": sym,
                    "price": entry,
                    "panic_ratio": 3.0,
                    "ath_drop_pct": 85,
                    "whale_dump_pct": 75,
                    "spread_pct": 5,
                    "score": 70,
                    "age_minutes": 50,
                }
            )
            assert opened is not None
            broker.mark(mint, mark)

        rows = broker.snapshot_positions()
        assert len(rows) == 3
        pnls = []
        for sym, mint, entry, mark in specs:
            row = next(r for r in rows if r["mint"] == mint)
            expected = round(_pnl_pct(entry, mark), 2)
            assert row["entry_price"] == entry or abs(row["entry"] - entry) < 1e-18
            assert abs(row["current_price"] - mark) < 1e-18 or abs(row["mark"] - mark) < 1e-18
            assert row["pnl_pct"] == expected
            pnls.append(row["pnl_pct"])
        assert len(set(pnls)) == 3, f"收益率雷同污染: {pnls}"

    def test_frontend_style_binding_per_item(self):
        """模拟前端 getPnLPercent(item) 逐项绑定。"""
        items = [
            {"entry_price": 1.0, "current_price": 1.1},
            {"entry_price": 2.0, "current_price": 1.5},
            {"entry_price": 0.000003, "current_price": 0.00000345},
        ]

        def get_pnl_percent(item: dict) -> float:
            e = float(item["entry_price"])
            c = float(item["current_price"])
            if not e:
                return 0.0
            return ((c - e) / e) * 100.0

        vals = [round(get_pnl_percent(it), 2) for it in items]
        assert vals[0] == 10.0
        assert vals[1] == -25.0
        assert vals[2] == 15.0
        assert len(set(vals)) == 3
