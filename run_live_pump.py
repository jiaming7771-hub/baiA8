#!/usr/bin/env python3
"""Pump.fun 实盘启动入口。

用法（在项目根目录）:
  cd backend && ../.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000

或本脚本（会检查 .env 实盘开关后启动同一 FastAPI）:
  python run_live_pump.py

前置（项目根 .env，已被 gitignore）:
  WALLET_PRIVATE_KEY=...
  SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=...
  PUMP_DRY_RUN=0
  PUMP_LIVE_CONFIRM=1
  PUMP_DEMO_SCAN=0
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(ROOT))

# 先加载 .env
from wallet import load_dotenv_files, wallet_status  # noqa: E402

load_dotenv_files(override=False)


def _require_live_env() -> None:
    dry = os.getenv("PUMP_DRY_RUN", "1").strip().lower()
    confirm = os.getenv("PUMP_LIVE_CONFIRM", "0").strip().lower()
    want_live = dry in ("0", "false", "no", "off")
    confirmed = confirm in ("1", "true", "yes", "on")
    if not want_live:
        print("❌ PUMP_DRY_RUN 不是 0。实盘请在 .env 设置 PUMP_DRY_RUN=0", file=sys.stderr)
        sys.exit(2)
    if not confirmed:
        print("❌ 缺少 PUMP_LIVE_CONFIRM=1，拒绝启动实盘。", file=sys.stderr)
        sys.exit(2)
    rpc = os.getenv("SOLANA_RPC_URL", "").strip()
    if not rpc:
        print("❌ 未配置 SOLANA_RPC_URL", file=sys.stderr)
        sys.exit(2)
    st = wallet_status()
    if not st.get("load_ok"):
        print(f"❌ 钱包不可用: {st.get('error') or '未配置 WALLET_PRIVATE_KEY'}", file=sys.stderr)
        sys.exit(2)
    print("=" * 60)
    print("🔴 CRYPTO PULSE · Pump 实盘启动检查通过")
    print(f"   钱包: {st.get('pubkey')}")
    print(f"   密钥来源: {st.get('env_var')}")
    print(f"   RPC: (已配置，日志中会脱敏)")
    print("   启动: uvicorn main:app --host 0.0.0.0 --port 8000")
    print("=" * 60)


def main() -> None:
    _require_live_env()
    os.chdir(BACKEND)
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
