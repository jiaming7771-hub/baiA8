"""小资金实盘 Micro-Live：仓位硬顶 / 优先费 / 止损卖出滑点升级重试。"""

from __future__ import annotations

import importlib
import os

import pytest

from pumpfun import config as C
from pumpfun import live_swap as L
from pumpfun.risk import RiskBlocked, RiskGuard


def _reload_config(env: dict[str, str]):
    """写入 env 并 reload config；显式回写关键字段，防止 .env override=True 盖掉测试值。"""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    importlib.reload(C)
    if "PUMP_LIVE_SIZE_SOL" in env:
        raw = float(env["PUMP_LIVE_SIZE_SOL"])
        C.LIVE_SIZE_SOL = max(C.LIVE_SIZE_SOL_HARD_MIN, min(raw, C.LIVE_SIZE_SOL_HARD_MAX))
    if "PUMP_MICRO_LIVE" in env:
        C.MICRO_LIVE = env["PUMP_MICRO_LIVE"].strip().lower() in ("1", "true", "yes", "on")
    if "SHADOW_MODE" in env:
        C.SHADOW_MODE = env["SHADOW_MODE"].strip().lower() in ("1", "true", "yes", "on")
    # 互斥规则与 config 一致
    if C.MICRO_LIVE and C.SHADOW_MODE:
        C.SHADOW_MODE = False
    return saved

def _restore_config(saved: dict[str, str | None]):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    importlib.reload(C)


def test_live_size_clamped_to_hard_range():
    saved = _reload_config({"PUMP_LIVE_SIZE_SOL": "0.5", "PUMP_MICRO_LIVE": "1"})
    try:
        assert C.LIVE_SIZE_SOL == pytest.approx(0.10)  # 超配 0.5 → 硬顶 0.10
    finally:
        _restore_config(saved)
    saved = _reload_config({"PUMP_LIVE_SIZE_SOL": "0.001", "PUMP_MICRO_LIVE": "1"})
    try:
        assert C.LIVE_SIZE_SOL == pytest.approx(0.01)  # 过小 → 硬下限 0.01
    finally:
        _restore_config(saved)


def test_micro_live_forces_shadow_off():
    saved = _reload_config({"PUMP_MICRO_LIVE": "1", "SHADOW_MODE": "1"})
    try:
        assert C.MICRO_LIVE is True
        assert C.SHADOW_MODE is False  # 互斥：Micro-Live 优先
    finally:
        _restore_config(saved)


def test_micro_live_position_fixed_size(monkeypatch):
    monkeypatch.setattr(C, "MICRO_LIVE", True)
    monkeypatch.setattr(C, "LIVE_SIZE_SOL", 0.05)
    g = RiskGuard()
    # 现金充足 → 固定 0.05，不走 0.02~0.04 百分比夹紧
    assert g.clamp_position_sol(1.0, equity=1.0, cash=1.0) == pytest.approx(0.05)
    # 现金不足（0.05 > 0.04*0.9）→ 拦截
    with pytest.raises(RiskBlocked):
        g.clamp_position_sol(1.0, equity=1.0, cash=0.04)


def test_priority_fee_field_modes(monkeypatch):
    monkeypatch.setattr(C, "JITO_TIP_LAMPORTS", 500_000)
    assert L._priority_fee_field() == {"jitoTipLamports": 500_000}

    monkeypatch.setattr(C, "JITO_TIP_LAMPORTS", 0)
    monkeypatch.setattr(C, "PRIORITY_FEE_MAX_LAMPORTS", 2_000_000)
    monkeypatch.setattr(C, "PRIORITY_LEVEL", "veryHigh")
    field = L._priority_fee_field()
    assert field["priorityLevelWithMaxLamports"]["maxLamports"] == 2_000_000
    assert field["priorityLevelWithMaxLamports"]["priorityLevel"] == "veryHigh"

    monkeypatch.setattr(C, "PRIORITY_FEE_MAX_LAMPORTS", 0)
    assert L._priority_fee_field() == "auto"


def test_urgent_sell_escalates_slippage_and_retries(monkeypatch):
    """止损卖出失败 → 抬滑点重试，第三次成功；非 urgent 一次失败即抛。"""
    monkeypatch.setattr(C, "EXIT_SELL_MAX_RETRIES", 3)
    monkeypatch.setattr(C, "EXIT_SELL_SLIP_STEP_BPS", 200)

    class _FakeKp:
        def pubkey(self):
            return "FakePubkey111"

    monkeypatch.setattr(L, "keypair_for_live", lambda: _FakeKp())

    calls: list[int] = []

    def fake_sell_once(*, token_mint, token_amount_raw, decimals, bps, pubkey, routing="default", expect_sol=0.0, force=False, urgent=False):
        calls.append(bps)
        if len(calls) < 3:
            raise L.LiveSwapError(f"模拟拥堵失败 #{len(calls)}")
        return {"side": "sell", "sol_amount": 0.05, "slippage_bps": bps, "signature": "sig"}

    monkeypatch.setattr(L, "_sell_once", fake_sell_once)

    out = L.sell_token_for_sol(
        token_mint="MintX",
        token_amount_raw=1_000_000,
        decimals=6,
        slippage_bps=500,
        equity=1.0,
        approx_sol=0.05,
        urgent=True,
    )
    assert out["signature"] == "sig"
    # 500 → 失败 → 700 → 失败 → 900 成功
    assert calls == [500, 700, 900]

    # 非紧急 + 关掉非紧急重试与兜底 salvage：只试一次
    calls.clear()
    monkeypatch.setattr(C, "EXIT_SELL_RETRY_NON_URGENT", False)
    monkeypatch.setattr(C, "EXIT_FORCE_SALVAGE", False)

    def fail_once(**kwargs):
        calls.append(kwargs["bps"])
        raise L.LiveSwapError("失败")

    monkeypatch.setattr(L, "_sell_once", fail_once)

    def _sell_non_urgent():
        L.sell_token_for_sol(
            token_mint="MintX",
            token_amount_raw=1_000_000,
            decimals=6,
            slippage_bps=500,
            equity=1.0,
            approx_sol=0.05,
            urgent=False,
        )

    with pytest.raises(L.LiveSwapError):
        _sell_non_urgent()
    assert len(calls) == 1

    # salvage 打开时：耗尽后必须再用 urgent 上限强砸一次，杜绝 write_off=0
    calls.clear()
    monkeypatch.setattr(C, "EXIT_FORCE_SALVAGE", True)
    with pytest.raises(L.LiveSwapError):
        _sell_non_urgent()
    assert calls == [500, C.URGENT_SLIPPAGE_BPS_MAX]


def test_rent_block_when_wallet_below_floor(monkeypatch):
    monkeypatch.setattr(C, "WALLET_MIN_SOL_FLOOR", 0.2)
    monkeypatch.setattr(C, "ATA_RENT_SOL", 0.00203928)
    monkeypatch.setattr(C, "WALLET_RESERVE_SOL", 0.05)

    monkeypatch.setattr("pumpfun.rpc.get_balance_sol", lambda owner: 0.15)
    monkeypatch.setattr(
        "pumpfun.rpc.rpc_call",
        lambda *a, **k: {"value": []},
    )

    with pytest.raises(RiskBlocked) as ei:
        L.assert_wallet_rent_safe_for_buy(owner="OwnerX", buy_sol=0.05, mint="MintY")
    assert "地板" in str(ei.value)


def test_rent_block_when_remaining_below_reserve(monkeypatch):
    monkeypatch.setattr(C, "WALLET_MIN_SOL_FLOOR", 0.1)
    monkeypatch.setattr(C, "ATA_RENT_SOL", 0.002)
    monkeypatch.setattr(C, "WALLET_RESERVE_SOL", 0.05)

    # 0.22 - 0.05 - 0.002 = 0.168 >= 0.05 OK for reserve, but check gas pad path
    # 0.12 - 0.05 - 0.002 = 0.068 >= 0.05 OK
    # 0.11 - 0.05 - 0.002 = 0.058 >= 0.05 OK
    # 0.101 - 0.05 - 0.002 = 0.049 < 0.05 → block
    monkeypatch.setattr("pumpfun.rpc.get_balance_sol", lambda owner: 0.101)
    monkeypatch.setattr("pumpfun.rpc.rpc_call", lambda *a, **k: {"value": []})

    with pytest.raises(RiskBlocked):
        L.assert_wallet_rent_safe_for_buy(owner="OwnerX", buy_sol=0.05, mint="MintY")


def test_rent_ok_when_ata_already_exists(monkeypatch):
    monkeypatch.setattr(C, "WALLET_MIN_SOL_FLOOR", 0.2)
    monkeypatch.setattr(C, "ATA_RENT_SOL", 0.002)
    monkeypatch.setattr(C, "WALLET_RESERVE_SOL", 0.05)

    # 0.26 - 0.05 - 0(ata) = 0.21 >= 0.05，且 bal >= floor
    monkeypatch.setattr("pumpfun.rpc.get_balance_sol", lambda owner: 0.26)
    monkeypatch.setattr(
        "pumpfun.rpc.rpc_call",
        lambda *a, **k: {"value": [{"account": {}}]},  # ATA 已存在
    )
    info = L.assert_wallet_rent_safe_for_buy(owner="OwnerX", buy_sol=0.05, mint="MintY")
    assert info["need_ata"] is False
    assert info["ata_rent_sol"] == 0.0


def test_graduation_failover_switches_routing(monkeypatch):
    """泵曲线失效 → 自动切 graduated 聚合路由成功。"""
    monkeypatch.setattr(C, "EXIT_SELL_MAX_RETRIES", 2)

    class _FakeKp:
        def pubkey(self):
            return "FakePubkey111"

    monkeypatch.setattr(L, "keypair_for_live", lambda: _FakeKp())

    routes: list[str] = []

    def fake_sell_once(*, token_mint, token_amount_raw, decimals, bps, pubkey, routing="default", expect_sol=0.0, force=False, urgent=False):
        routes.append(routing)
        if routing == "default":
            raise L.LiveSwapError("No routes found / bonding curve migrated")
        return {
            "side": "sell",
            "sol_amount": 0.04,
            "slippage_bps": bps,
            "signature": "grad-sig",
            "routing": routing,
        }

    monkeypatch.setattr(L, "_sell_once", fake_sell_once)
    monkeypatch.setattr(L, "_log_alert_to_journal", lambda **kw: None)

    out = L.sell_token_for_sol(
        token_mint="MintGrad",
        token_amount_raw=1_000_000,
        decimals=6,
        slippage_bps=500,
        equity=1.0,
        approx_sol=0.05,
        urgent=True,
    )
    assert out["signature"] == "grad-sig"
    assert "default" in routes and "graduated" in routes


def test_looks_like_graduation_markers():
    assert L.looks_like_graduation_or_route_failure("No routes found")
    assert L.looks_like_graduation_or_route_failure("bonding curve complete")
    assert L.looks_like_graduation_or_route_failure("insufficient liquidity")
    assert not L.looks_like_graduation_or_route_failure("random network blip xyz")


def test_entry_block_for_exposes_execution_gates(monkeypatch):
    """看板的 ✓ 不许骗人：执行层每个闸门都要能被查出来。"""
    import time

    from pumpfun.execution import PaperBroker

    b = PaperBroker()
    assert b.entry_block_for("MintFree", "FREE") is None

    b.positions["MintHeld"] = {"symbol": "HELD"}
    assert b.entry_block_for("MintHeld", "HELD")["label"] == "持仓中"

    monkeypatch.setattr(C, "SYMBOL_PERMANENT_BAN_ENABLED", True)
    b._symbol_cooldown_until[b._norm_symbol("PERM")] = 253402300799.0
    assert b.entry_block_for("MintPerm", "PERM")["label"] == "同名永久禁"

    b._symbol_cooldown_until[b._norm_symbol("SOFT")] = time.time() + 900
    assert b.entry_block_for("MintSoft", "SOFT")["label"] == "同名冷却"

    b._mint_cooldown_until["MintCool"] = time.time() + 600
    assert b.entry_block_for("MintCool", "COOL")["label"] == "熔断冷却"

    monkeypatch.setattr(C, "MAX_OPEN_POSITIONS", 1)
    assert b.entry_block_for("MintOther", "OTHER")["label"] == "仓位已满"


def test_looks_like_slippage_markers():
    assert L.looks_like_slippage_failure(
        "Transaction simulation failed: custom program error: 0x1771"
    )
    assert L.looks_like_slippage_failure("InstructionError Custom': 6001")
    assert L.looks_like_slippage_failure("SlippageToleranceExceeded")
    assert not L.looks_like_slippage_failure("MissingAccount vault ata")
    assert not L.looks_like_slippage_failure("random network blip xyz")
