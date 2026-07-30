"""金库更新 / WSS 解析 / 抽池标记。"""

from __future__ import annotations

import base64
import struct
import time

import pytest

from pumpfun import config as C
from pumpfun import onchain_price as op


def _pos(**over):
    pos = {
        "id": "p1",
        "mint": "m",
        "symbol": "CAT",
        "entry": 1e-6,
        "entry_mark": 1e-6,
        "mark": 1e-6,
        "entry_sol_vault": 100.0,
        "sol_vault": 100.0,
        "qty_left": 1000.0,
        "dry_run": False,
        "shadow": False,
    }
    pos.update(over)
    return pos


class TestApplyVaultSol:
    def test_seeds_entry_when_missing(self):
        pos = _pos(entry_sol_vault=0, sol_vault=None)
        assert op.apply_vault_sol_to_position(pos, 80.0, mint="m") is False
        assert pos["entry_sol_vault"] == 80.0
        assert pos["sol_vault"] == 80.0
        assert not pos.get("vault_drain")

    def test_marks_drain_at_threshold(self, monkeypatch):
        monkeypatch.setattr(C, "VAULT_DRAIN_DROP_PCT", 0.40)
        pos = _pos()
        assert op.apply_vault_sol_to_position(pos, 55.0, mint="m") is True
        assert pos["vault_drain"] is True
        assert pos["vault_drain_drop"] == pytest.approx(0.45, abs=1e-4)

    def test_second_call_not_newly(self, monkeypatch):
        monkeypatch.setattr(C, "VAULT_DRAIN_DROP_PCT", 0.40)
        pos = _pos()
        assert op.apply_vault_sol_to_position(pos, 50.0, mint="m") is True
        assert op.apply_vault_sol_to_position(pos, 40.0, mint="m") is False
        assert pos["vault_drain"] is True

    def test_below_threshold_no_drain(self, monkeypatch):
        monkeypatch.setattr(C, "VAULT_DRAIN_DROP_PCT", 0.40)
        pos = _pos()
        assert op.apply_vault_sol_to_position(pos, 70.0, mint="m") is False
        assert not pos.get("vault_drain")

    def test_vault_drained_flag_forces(self, monkeypatch):
        monkeypatch.setattr(C, "VAULT_DRAIN_DROP_PCT", 0.40)
        pos = _pos()
        assert op.apply_vault_sol_to_position(pos, 90.0, vault_drained=True, mint="m") is True
        assert pos["vault_drain"] is True


class TestSolAmountParse:
    def test_spl_amount(self):
        # mint(32) + owner(32) + amount(u64) ...
        raw = bytearray(165)
        struct.pack_into("<Q", raw, 64, 42_000_000_000)  # 42 SOL
        b64 = base64.b64encode(bytes(raw)).decode()
        assert op.sol_amount_from_account_data(b64, kind="spl") == pytest.approx(42.0)

    def test_bonding_real_sol(self):
        raw = bytearray(48)
        struct.pack_into("<Q", raw, 32, 17_000_000_000)  # 17 SOL
        b64 = base64.b64encode(bytes(raw)).decode()
        assert op.sol_amount_from_account_data(b64, kind="bonding") == pytest.approx(17.0)


class TestFetchPricesWritesPubkey:
    def test_row_sol_vault_fields_copied(self, monkeypatch):
        positions = {"m": _pos(entry_sol_vault=100.0)}
        monkeypatch.setattr(
            op,
            "fetch_pool_price_row",
            lambda *a, **k: (
                {
                    "price": 1.1e-6,
                    "source": "pumpswap_vaults",
                    "ts": time.time(),
                    "pool": "POOL",
                    "sol_vault": 95.0,
                    "sol_vault_pubkey": "VaultPubkey1111111111111111111111111111111",
                    "sol_vault_kind": "spl",
                    "vault_drained": False,
                },
                "",
            ),
        )
        out = op.fetch_prices_for_positions(positions)
        assert out["m"] == 1.1e-6
        assert positions["m"]["sol_vault_pubkey"].startswith("VaultPubkey")
        assert positions["m"]["sol_vault_kind"] == "spl"
        assert positions["m"]["sol_vault"] == 95.0
        assert not positions["m"].get("vault_drain")


class TestVaultWssHandler:
    def test_notification_marks_drain_and_callbacks(self, monkeypatch):
        import asyncio
        import json
        from pumpfun.vault_wss import VaultWssWatcher

        monkeypatch.setattr(C, "VAULT_DRAIN_DROP_PCT", 0.40)
        positions = {
            "MINT1": _pos(
                sol_vault_pubkey="Vault1111111111111111111111111111111111111",
                sol_vault_kind="spl",
            )
        }
        drains: list[str] = []

        async def on_drain(mint, pos):
            drains.append(mint)

        async def run():
            w = VaultWssWatcher(get_positions=lambda: positions, on_drain=on_drain)
            pk = "Vault1111111111111111111111111111111111111"
            w._desired = {pk: "MINT1"}
            w._kinds = {pk: "spl"}
            w._id_to_pubkey = {7: pk}
            w._sub_ids = {pk: 7}
            raw = bytearray(165)
            struct.pack_into("<Q", raw, 64, 50_000_000_000)
            msg = {
                "jsonrpc": "2.0",
                "method": "accountNotification",
                "params": {
                    "subscription": 7,
                    "result": {
                        "value": {
                            "data": [base64.b64encode(bytes(raw)).decode(), "base64"]
                        }
                    },
                },
            }
            await w._handle_message(json.dumps(msg))

        asyncio.run(run())
        assert positions["MINT1"]["vault_drain"] is True
        assert drains == ["MINT1"]
