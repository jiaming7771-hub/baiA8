"""ccxt 交易所客户端：鉴权、沙盒、权限自检、下单封装。"""

from __future__ import annotations

import logging
import os
from typing import Any

import ccxt.async_support as ccxt

from simlab.live import config as C

logger = logging.getLogger("simlab.live.exchange")


class LiveSafetyError(RuntimeError):
    """资金安全相关致命错误。"""


def _require_env(*names: str) -> dict[str, str]:
    out: dict[str, str] = {}
    missing = []
    for n in names:
        v = os.getenv(n, "").strip()
        if not v:
            missing.append(n)
        else:
            out[n] = v
    if missing:
        raise LiveSafetyError(
            "缺少环境变量（禁止硬编码密钥）: " + ", ".join(missing)
        )
    return out


async def create_exchange() -> Any:
    """创建交易所实例。默认 sandbox；密钥仅来自环境变量。"""
    eid = C.EXCHANGE_ID
    if eid == "okx":
        creds = _require_env(C.ENV_OKX_KEY, C.ENV_OKX_SECRET, C.ENV_OKX_PASSPHRASE)
        ex = ccxt.okx(
            {
                "apiKey": creds[C.ENV_OKX_KEY],
                "secret": creds[C.ENV_OKX_SECRET],
                "password": creds[C.ENV_OKX_PASSPHRASE],
                "enableRateLimit": True,
                "options": {
                    "defaultType": "swap",
                },
            }
        )
        if C.SANDBOX:
            ex.set_sandbox_mode(True)
            logger.warning("OKX 沙盒/模拟盘模式已开启")
    elif eid in ("binance", "binanceusdm"):
        creds = _require_env(C.ENV_BINANCE_KEY, C.ENV_BINANCE_SECRET)
        ex = ccxt.binanceusdm(
            {
                "apiKey": creds[C.ENV_BINANCE_KEY],
                "secret": creds[C.ENV_BINANCE_SECRET],
                "enableRateLimit": True,
                "options": {"defaultType": "future"},
            }
        )
        if C.SANDBOX:
            ex.set_sandbox_mode(True)
            logger.warning("Binance 合约测试网模式已开启")
    else:
        raise LiveSafetyError(f"不支持的交易所: {eid}")

    await ex.load_markets()
    return ex


async def verify_api_permissions(exchange: Any) -> dict[str, Any]:
    """启动自检：必须能读余额；尽量确认不可提现。

    策略：
    - fetch_balance 成功 → 具备读取
    - 若 API 返回权限字段含 withdraw，直接拒绝启动
    - 尝试极小额提现探测会被禁止；我们只读权限元数据，绝不发起提现请求
    """
    report: dict[str, Any] = {
        "exchange": C.EXCHANGE_ID,
        "sandbox": C.SANDBOX,
        "balance_ok": False,
        "withdraw_blocked": True,
        "notes": [],
    }

    # 读取余额 = 交易/读取权限基本可用
    try:
        bal = await exchange.fetch_balance()
        report["balance_ok"] = True
        total = 0.0
        if isinstance(bal, dict):
            # USDT 权益近似
            usdt = (bal.get("USDT") or {}) if isinstance(bal.get("USDT"), dict) else {}
            total = float(usdt.get("total") or usdt.get("free") or 0)
            if total <= 0 and "info" in bal:
                report["notes"].append("USDT 余额为 0 或未解析到，请确认账户有资金")
        report["usdt_total"] = total
    except Exception as exc:
        raise LiveSafetyError(f"无法读取余额，API 读取权限异常: {exc}") from exc

    # 权限探测：不同交易所字段不同
    try:
        if C.EXCHANGE_ID == "okx":
            # OKX: GET /api/v5/account/config 或 account/account-position-risk
            if hasattr(exchange, "privateGetAccountConfig"):
                cfg = await exchange.privateGetAccountConfig()
                data = (cfg or {}).get("data") or []
                row = data[0] if data else {}
                # perm 字段若存在
                perm = str(row.get("perm") or row.get("acctLv") or "")
                report["okx_perm"] = perm or row
                text = str(row).lower()
                if "withdraw" in text and "true" in text:
                    raise LiveSafetyError("检测到提现相关权限标记，已拒绝启动")
                report["notes"].append("OKX account config 已读取；请确保 API 未勾选提现")
            else:
                report["notes"].append(
                    "无法自动枚举 OKX 权限位：请人工确认 API 仅勾选「交易+读取」、关闭提现"
                )
        else:
            # Binance: apiRestrictions
            if hasattr(exchange, "sapiGetAccountApiRestrictions"):
                rest = await exchange.sapiGetAccountApiRestrictions()
                report["binance_restrictions"] = rest
                if rest.get("enableWithdrawals") is True:
                    raise LiveSafetyError("Binance API 允许提现，已拒绝启动")
                if rest.get("enableInternalTransfer") is True:
                    report["notes"].append("警告：API 允许站内划转，建议关闭")
                # 合约交易
                if rest.get("enableFutures") is False:
                    raise LiveSafetyError("Binance API 未开启合约交易权限")
                report["withdraw_blocked"] = not bool(rest.get("enableWithdrawals"))
            else:
                report["notes"].append(
                    "无法自动读取 Binance apiRestrictions：请人工确认关闭提现"
                )
    except LiveSafetyError:
        raise
    except Exception as exc:
        report["notes"].append(f"权限元数据读取失败（不阻断若余额可读）: {exc}")
        logger.warning("permission probe soft-fail: %s", exc)

    # 硬性要求：用户必须确认无提现——用环境变量双重确认
    confirm = os.getenv("LIVE_CONFIRM_NO_WITHDRAW", "").strip()
    if confirm not in ("1", "true", "yes", "on"):
        raise LiveSafetyError(
            "启动拒绝：请设置环境变量 LIVE_CONFIRM_NO_WITHDRAW=1，"
            "表示你已确认该 API Key 关闭了提现权限"
        )
    report["user_confirmed_no_withdraw"] = True
    return report


def to_swap_symbol(base: str, exchange: Any) -> str:
    """山寨 base → ccxt 永续符号，如 BTC/USDT:USDT。"""
    base = base.upper()
    candidates = [
        f"{base}/USDT:USDT",
        f"{base}/USDT",
    ]
    markets = exchange.markets or {}
    for s in candidates:
        if s in markets:
            m = markets[s]
            # 只要 swap/future
            if m.get("swap") or m.get("future") or ":USDT" in s:
                return s
    # 模糊匹配
    for sym, m in markets.items():
        if m.get("base") == base and m.get("quote") == "USDT" and (m.get("swap") or m.get("future")):
            return sym
    raise LiveSafetyError(f"交易所无此 USDT 永续: {base}")


async def fetch_equity_usdt(exchange: Any) -> float:
    bal = await exchange.fetch_balance()
    usdt = bal.get("USDT") or {}
    for k in ("total", "free", "used"):
        try:
            v = float(usdt.get(k) or 0)
            if v > 0 and k == "total":
                return v
        except (TypeError, ValueError):
            continue
    # OKX 有时在 info
    try:
        total = float(usdt.get("total") or 0)
        if total > 0:
            return total
    except (TypeError, ValueError):
        pass
    # fallback: sum free+used
    try:
        return float(usdt.get("free") or 0) + float(usdt.get("used") or 0)
    except (TypeError, ValueError):
        return 0.0


async def set_leverage_safe(exchange: Any, symbol: str, leverage: int) -> None:
    lev = max(1, min(int(leverage), C.MAX_LEVERAGE))
    try:
        await exchange.set_leverage(lev, symbol)
        logger.info("set leverage %sx %s", lev, symbol)
    except Exception as exc:
        logger.warning("set_leverage failed %s: %s（继续，但请确认账户杠杆已手动调低）", symbol, exc)


async def place_limit_buy(
    exchange: Any,
    symbol: str,
    amount: float,
    price: float,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    amount = float(exchange.amount_to_precision(symbol, amount))
    price = float(exchange.price_to_precision(symbol, price))
    if amount <= 0 or price <= 0:
        raise LiveSafetyError("精度处理后数量/价格无效")
    payload = {
        "symbol": symbol,
        "side": "buy",
        "type": "limit",
        "amount": amount,
        "price": price,
        "params": {"tdMode": "cross"} if C.EXCHANGE_ID == "okx" else {},
    }
    if dry_run:
        logger.info("[DRY-RUN] limit buy %s", payload)
        return {"id": "dry-run", "dry_run": True, **payload}
    return await exchange.create_order(
        symbol, "limit", "buy", amount, price, payload["params"]
    )


async def place_stop_loss(
    exchange: Any,
    symbol: str,
    amount: float,
    stop_price: float,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """卖出止损（reduce-only）。"""
    amount = float(exchange.amount_to_precision(symbol, amount))
    stop_price = float(exchange.price_to_precision(symbol, stop_price))
    params: dict[str, Any]
    if C.EXCHANGE_ID == "okx":
        params = {
            "tdMode": "cross",
            "reduceOnly": True,
            "stopLossPrice": stop_price,
        }
        order_type = "market"
        price = None
    else:
        params = {
            "reduceOnly": True,
            "stopPrice": stop_price,
            "workingType": "MARK_PRICE",
        }
        order_type = "STOP_MARKET"
        price = None

    if dry_run:
        logger.info("[DRY-RUN] SL sell %s amt=%s stop=%s", symbol, amount, stop_price)
        return {"id": "dry-run-sl", "dry_run": True, "stop": stop_price, "amount": amount}

    return await exchange.create_order(symbol, order_type, "sell", amount, price, params)


async def place_take_profit(
    exchange: Any,
    symbol: str,
    amount: float,
    tp_price: float,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    amount = float(exchange.amount_to_precision(symbol, amount))
    tp_price = float(exchange.price_to_precision(symbol, tp_price))
    if C.EXCHANGE_ID == "okx":
        params = {
            "tdMode": "cross",
            "reduceOnly": True,
            "takeProfitPrice": tp_price,
        }
        order_type = "market"
        price = None
    else:
        params = {
            "reduceOnly": True,
            "stopPrice": tp_price,
            "workingType": "MARK_PRICE",
        }
        order_type = "TAKE_PROFIT_MARKET"
        price = None

    if dry_run:
        logger.info("[DRY-RUN] TP sell %s amt=%s tp=%s", symbol, amount, tp_price)
        return {"id": "dry-run-tp", "dry_run": True, "tp": tp_price, "amount": amount}

    return await exchange.create_order(symbol, order_type, "sell", amount, price, params)


async def cancel_open_orders(exchange: Any, symbol: str | None = None) -> None:
    try:
        if symbol:
            await exchange.cancel_all_orders(symbol)
        else:
            # 尽力取消；部分交易所不支持无 symbol
            await exchange.cancel_all_orders()
    except Exception as exc:
        logger.warning("cancel_all_orders: %s", exc)


async def close_exchange(exchange: Any) -> None:
    try:
        await exchange.close()
    except Exception:
        pass
