"""全流程冒烟：导入关键模块 + 聚合关键断言入口。"""

from __future__ import annotations


def test_imports():
    from pumpfun.strategy import pass_hard_filters, Candidate  # noqa: F401
    from pumpfun.execution import PaperBroker  # noqa: F401
    from pumpfun import journal  # noqa: F401
    from simlab.price_format import round_price  # noqa: F401


def test_round_price_smoke():
    from simlab.price_format import round_price

    assert round_price(0.00000345) != round_price(0.00000340)
