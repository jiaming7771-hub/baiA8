"""动态价格精度：微单价不可被截断成相同显示值。"""

from __future__ import annotations

from simlab.price_format import price_decimals, round_price


class TestPricePrecision:
    def test_micro_prices_remain_distinct(self):
        a = 0.00000345
        b = 0.00000340
        ra, rb = round_price(a), round_price(b)
        assert ra != rb
        assert abs(ra - a) / a < 1e-6
        assert abs(rb - b) / b < 1e-6

    def test_ultra_micro_not_truncated_to_zero(self):
        p = 0.00000008
        r = round_price(p)
        assert r > 0
        assert price_decimals(p) >= 8

    def test_entry_stop_take_not_collapsed(self):
        entry = 0.0000034521
        stop = entry * 0.99
        take = entry * 1.01
        re, rs, rt = round_price(entry), round_price(stop), round_price(take)
        assert len({re, rs, rt}) == 3
        assert rs < re < rt

    def test_old_six_decimal_bug_avoided(self):
        entry, stop = 0.0000034521, 0.0000033890
        assert round(entry, 6) == round(stop, 6)  # 旧逻辑会雷同
        assert round_price(entry) != round_price(stop)

    def test_btc_eth_scale_still_sane(self):
        assert price_decimals(95000.12) == 2
        assert price_decimals(3200.55) == 2
        assert price_decimals(1.23456) == 4
