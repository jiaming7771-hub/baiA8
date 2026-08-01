"""认不出的交易场所必须 fail-closed，不能顶着满分穿过闸门。

两个独立的 fail-open：

(a) fetch_bonding_progress_pct 对认不出的池子程序返回 100.0。100 的含义是
    「已毕业」，恰好是准入侧最严的一档 —— 一个从没被检查过的场所因此拿到了
    「通过了最严检查」的待遇。未知不等于安全。

(b) ALLOWED_DEXES 在两个 ingest 口有，Gecko 批量兜底刷新那一口没有：
    _parse_pool 的 dex 直接来自数据源，不过白名单就写进观察池。

注意范围：这里不新增开仓侧的场所白名单（1d7afea 明确不做，理由是硬编码
program id 会过期，且 stale_mark 逃生已经能把标不了价的仓位强制平掉）。
这里只保证「未知」不会被记成「通过」。
"""

from __future__ import annotations

import json
import time

import pytest

from pumpfun import config as C
from pumpfun import journal
from pumpfun import market_data as M
from pumpfun import onchain_price as op
from pumpfun.execution import PaperBroker

PUMP_OWNER = op.PUMP_PROGRAM
PUMPSWAP_OWNER = op.PUMPSWAP_PROGRAM
STRANGER = "MeTeoRaDbCxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"


class TestUnknownOwnerFailsClosed:
    @pytest.fixture(autouse=True)
    def _stub_chain(self, monkeypatch):
        monkeypatch.setattr(op, "resolve_pool_for_mint", lambda mint, pool=None: "POOL")

    def _progress(self, monkeypatch, owner, data=None):
        monkeypatch.setattr(
            op.rpc, "get_account_info", lambda *a, **k: {"owner": owner, "data": data}
        )
        return op.fetch_bonding_progress_pct("MINT", pool="POOL", dex="pump-fun")

    def test_unknown_owner_is_not_reported_as_graduated(self, monkeypatch):
        prog, src = self._progress(monkeypatch, STRANGER)
        assert prog is None, "认不出的场所报了一个进度数，等于替它作了保证"
        assert prog != 100.0
        assert src.startswith("unknown_owner")

    def test_unknown_is_distinguishable_from_graduated(self, monkeypatch):
        unknown_prog, unknown_src = self._progress(monkeypatch, STRANGER)
        grad_prog, grad_src = self._progress(monkeypatch, PUMPSWAP_OWNER)
        assert grad_prog == 100.0
        assert (unknown_prog, unknown_src) != (grad_prog, grad_src)
        assert "unknown" in unknown_src and "unknown" not in grad_src

    def test_unknown_is_distinguishable_from_read_failure(self, monkeypatch):
        _, unknown_src = self._progress(monkeypatch, STRANGER)
        monkeypatch.setattr(op.rpc, "get_account_info", lambda *a, **k: None)
        empty_prog, empty_src = op.fetch_bonding_progress_pct(
            "MINT", pool="POOL", dex="pump-fun"
        )
        assert empty_prog is None
        # 两者都 fail-closed，但看板要能分清「没有判据」和「这次没读到」
        assert unknown_src != empty_src

    def test_known_pumpswap_owner_still_graduates(self, monkeypatch):
        prog, src = self._progress(monkeypatch, PUMPSWAP_OWNER)
        assert prog == 100.0 and src == "pumpswap_owner"

    def test_pumpswap_dex_short_circuits_without_rpc(self):
        assert op.fetch_bonding_progress_pct("MINT", dex="pumpswap") == (
            100.0,
            "pumpswap",
        )


class TestEntryGateTreatsUnknownAsNotPassing:
    """graduated-only 打开时，未知场所必须被拒，而不是当成 100% 放行。"""

    @pytest.fixture()
    def broker(self, monkeypatch):
        monkeypatch.setattr(C, "ENTRY_GRADUATED_ONLY", True)
        monkeypatch.setattr(C, "BONDING_MIN_PROGRESS_PCT", 20.0)
        b = PaperBroker()
        b.dry_run = True
        return b

    @staticmethod
    def _signal():
        return {
            "mint": "MINTUNK", "symbol": "UNK", "price": 1e-6,
            "pool": "POOL", "dex": "pump-fun",
            "score": float(C.ENTRY_MIN_SCORE) + 5.0, "track": "A",
        }

    def _patch_progress(self, monkeypatch, result):
        monkeypatch.setattr(op, "fetch_bonding_progress_pct", lambda *a, **k: result)

    def test_unknown_venue_is_refused(self, broker, monkeypatch):
        self._patch_progress(monkeypatch, (None, "unknown_owner:MeTeoRaD"))
        assert broker.open_long(self._signal()) is None
        assert broker.positions == {}

    def test_refusal_is_recorded_with_its_own_action(self, broker, monkeypatch):
        self._patch_progress(monkeypatch, (None, "unknown_owner:MeTeoRaD"))
        broker.open_long(self._signal())
        rows = [
            json.loads(line)
            for line in C.DAILY_TRADES_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        blocks = [r for r in rows if r["action"] == "unknown_venue_block"]
        assert blocks, [r["action"] for r in rows]
        assert blocks[-1]["action_label"] == "未知交易场所拦截"
        assert "unknown_owner" in blocks[-1]["exit_reason"]

    def test_read_failure_gets_a_different_action(self, broker, monkeypatch):
        self._patch_progress(monkeypatch, (None, "empty_account"))
        broker.open_long(self._signal())
        rows = [
            json.loads(line)
            for line in C.DAILY_TRADES_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        actions = {r["action"] for r in rows}
        assert "bonding_read_fail" in actions
        assert "unknown_venue_block" not in actions

    def test_graduated_pool_still_passes_the_gate(self, broker, monkeypatch):
        """闸门没被焊死：真毕业的池子仍要能过。"""
        self._patch_progress(monkeypatch, (100.0, "pumpswap_owner"))
        assert broker.open_long(self._signal()) is not None

    def test_end_to_end_unknown_program_never_reaches_a_position(
        self, broker, monkeypatch
    ):
        """不打桩进度函数，直接喂一个认不出的 owner：整条链路都必须拒。

        这条覆盖的正是原来的 fail-open —— 未知 owner 报 100，恰好等于
        graduated-only 想要的那个数，于是它「通过」了一项从没对它做过的检查。
        """
        monkeypatch.setattr(
            op, "resolve_pool_for_mint", lambda mint, pool=None: "POOL"
        )
        monkeypatch.setattr(
            op.rpc, "get_account_info", lambda *a, **k: {"owner": STRANGER}
        )
        assert broker.open_long(self._signal()) is None
        assert broker.positions == {}


class TestVenueNameNormalisation:
    def test_both_spellings_map_to_one_name(self):
        """Gecko 叫 meteora-dbc，Dexscreener 叫 meteoradbc，同一个场所。"""
        assert M.canon_dex("meteora-dbc") == M.canon_dex("meteoradbc")

    @pytest.mark.parametrize("spelling", ["meteora-dbc", "meteoradbc", "Meteora-DBC"])
    def test_neither_spelling_is_allowed(self, spelling):
        assert not M.is_allowed_dex(spelling)

    @pytest.mark.parametrize("spelling", ["pumpswap", "PumpSwap", "pump-fun", "pumpfun"])
    def test_allowed_venues_survive_normalisation(self, spelling):
        assert M.is_allowed_dex(spelling)
        assert M.canon_dex(spelling) in M.ALLOWED_DEXES

    def test_unknown_name_is_refused_not_guessed(self):
        assert not M.is_allowed_dex("some-new-amm")
        assert not M.is_allowed_dex(None)
        assert not M.is_allowed_dex("")


def _gecko_pool(mint, dex, pool="POOLX"):
    return {
        "id": f"solana_{pool}",
        "attributes": {
            "address": pool,
            "name": "TOK / SOL",
            "base_token_price_usd": "1.0",
            "base_token_price_native_currency": "0.001",
            "pool_created_at": None,
            "reserve_in_usd": "5000",
            "transactions": {"m5": {"buys": 5, "sells": 1}},
            "price_change_percentage": {"m5": 1.0},
            "volume_usd": {"m5": "100", "h1": "900"},
        },
        "relationships": {
            "dex": {"data": {"id": dex}},
            "base_token": {"data": {"id": f"solana_{mint}"}},
        },
    }


class TestGeckoMultiRefreshHole:
    """观察池准入：Gecko 批量兜底刷新这一口曾经没过白名单。"""

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        monkeypatch.setattr(M, "_watchlist", {})
        monkeypatch.setattr(M, "_watchlist_loaded", True)
        monkeypatch.setattr(M, "_load_watchlist", lambda: None)
        monkeypatch.setattr(M, "_save_watchlist", lambda: None)
        monkeypatch.setattr(M, "sol_usd_price", lambda: 200.0)

    def test_parse_pool_still_yields_a_disallowed_row(self):
        """前提确认：解析层不拦场所，拦不拦全靠调用方——所以三个口都得拦。"""
        row = M._parse_pool(_gecko_pool("MINTBAD", "meteora-dbc"))
        assert row is not None
        assert not M.is_allowed_dex(row["dex"])

    def test_multi_refresh_does_not_admit_disallowed_venue(self, monkeypatch):
        """端到端跑兜底分支：meteora-dbc 池不得进观察池。"""
        M._watchlist["SEED"] = {
            "mint": "SEED", "pool": "POOLSEED", "dex": "pumpswap",
            "listed_at": time.time(), "updated": 0.0, "peak_price": 0.0,
        }
        # 只放行兜底分支：发现通道全部跳过，且观察池过期比例够高
        monkeypatch.setattr(M, "_dex_discover", lambda: 0)
        monkeypatch.setattr(M, "_dex_refresh_watchlist", lambda: 0)
        monkeypatch.setattr(M, "_evict_stale", lambda: None)
        monkeypatch.setattr(M, "_stale_ratio", lambda: 1.0)
        monkeypatch.setattr(M, "_last_multi_scan", 0.0)
        monkeypatch.setattr(M, "_last_trending_scan", time.time())
        monkeypatch.setattr(M, "_last_new_scan", time.time())
        monkeypatch.setattr(M, "_gecko_blocked_until", {"discover": 0.0, "ohlcv": 0.0})
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            return {"data": [
                _gecko_pool("MINTBAD", "meteora-dbc", "POOLBAD"),
                _gecko_pool("MINTOK", "pumpswap", "POOLOK"),
            ]}

        monkeypatch.setattr(M, "_get_json", fake_get)
        M.refresh_watchlist()

        assert calls, "兜底分支没被触发，这个用例就没在测它想测的东西"
        assert "MINTBAD" not in M._watchlist
        assert "MINTOK" in M._watchlist

    def test_multi_refresh_still_updates_allowed_venues(self, monkeypatch):
        """别把这一口整个关掉：允许的场所仍要能刷新。"""
        monkeypatch.setattr(M, "_dex_discover", lambda: 0)
        monkeypatch.setattr(M, "_dex_refresh_watchlist", lambda: 0)
        monkeypatch.setattr(M, "_evict_stale", lambda: None)
        monkeypatch.setattr(M, "_stale_ratio", lambda: 1.0)
        monkeypatch.setattr(M, "_last_multi_scan", 0.0)
        monkeypatch.setattr(M, "_last_trending_scan", time.time())
        monkeypatch.setattr(M, "_last_new_scan", time.time())
        monkeypatch.setattr(M, "_gecko_blocked_until", {"discover": 0.0, "ohlcv": 0.0})
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        M._watchlist["MINTOK"] = {
            "mint": "MINTOK", "pool": "POOLOK", "dex": "pumpswap",
            "listed_at": time.time(), "updated": 0.0, "peak_price": 0.0,
        }
        monkeypatch.setattr(
            M, "_get_json",
            lambda url, **kw: {"data": [_gecko_pool("MINTOK", "pumpswap", "POOLOK")]},
        )
        M.refresh_watchlist()
        assert M._watchlist["MINTOK"]["price_sol"] > 0

    def test_all_three_ingest_sites_share_one_check(self, monkeypatch):
        """三个口用同一个判据：改白名单不会再漏掉某一口。"""
        bad = _gecko_pool("MINTBAD", "meteora-dbc", "POOLBAD")
        assert M._ingest_pools({"data": [bad]}) == 0
        assert M._watchlist == {}

        row = M._parse_pool(bad)
        assert M._ingest_dex_rows([row]) == 0
        assert M._watchlist == {}


class TestEarlyDiscoveryPrimary:
    """Gecko 新池主发现：独立调度 + graduated-only 只收 pumpswap。"""

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        monkeypatch.setattr(M, "_watchlist", {})
        monkeypatch.setattr(M, "_watchlist_loaded", True)
        monkeypatch.setattr(M, "_load_watchlist", lambda: None)
        monkeypatch.setattr(M, "_save_watchlist", lambda: None)
        monkeypatch.setattr(M, "sol_usd_price", lambda: 200.0)
        monkeypatch.setattr(M, "_apply_onchain_depth", lambda rows: rows)
        monkeypatch.setattr(M, "_evict_stale", lambda: None)

    def test_new_pools_runs_alongside_trending(self, monkeypatch):
        """新池不再挂在 trending 的 elif 下：两者到期时可同轮都跑。"""
        monkeypatch.setattr(C, "GECKO_NEW_POOLS_ENABLED", True)
        monkeypatch.setattr(C, "ENTRY_GRADUATED_ONLY", True)
        monkeypatch.setattr(M, "_dex_discover", lambda: 0)
        monkeypatch.setattr(M, "_dex_refresh_watchlist", lambda: 0)
        monkeypatch.setattr(M, "_stale_ratio", lambda: 0.0)
        monkeypatch.setattr(M, "_last_new_scan", 0.0)
        monkeypatch.setattr(M, "_last_trending_scan", 0.0)
        monkeypatch.setattr(M, "_last_dex_discover", time.time())
        monkeypatch.setattr(M, "_last_dex_refresh", time.time())
        monkeypatch.setattr(M, "_gecko_blocked_until", {"discover": 0.0, "ohlcv": 0.0})

        urls: list[str] = []

        def fake_get(url, **kw):
            urls.append(url)
            if "new_pools" in url:
                return {"data": [_gecko_pool("NEWMINT", "pumpswap", "POOLNEW")]}
            if "trending" in url:
                return {"data": [_gecko_pool("TRENDMINT", "pumpswap", "POOLTREND")]}
            return {"data": []}

        monkeypatch.setattr(M, "_get_json", fake_get)
        M.refresh_watchlist()

        assert any("new_pools" in u for u in urls)
        assert any("trending" in u for u in urls)
        assert "NEWMINT" in M._watchlist
        assert M._watchlist["NEWMINT"]["source"] == "gecko_new"
        assert "TRENDMINT" in M._watchlist
        assert M._watchlist["TRENDMINT"]["source"] == "gecko_trending"

    def test_new_pools_skips_curve_when_graduated_only(self, monkeypatch):
        monkeypatch.setattr(C, "ENTRY_GRADUATED_ONLY", True)
        n = M._ingest_pools(
            {
                "data": [
                    _gecko_pool("CURVE", "pump-fun", "POOLCURVE"),
                    _gecko_pool("SWAP", "pumpswap", "POOLSWAP"),
                ]
            },
            source="gecko_new",
            pumpswap_only=True,
        )
        assert n == 1
        assert "SWAP" in M._watchlist
        assert M._watchlist["SWAP"]["source"] == "gecko_new"
        assert "CURVE" not in M._watchlist


def test_new_block_actions_have_labels():
    for action in ("unknown_venue_block", "bonding_read_fail"):
        label = journal.action_label(action)
        assert label != action, f"{action} 没有中文标签，看板会显示原始英文"
