"""可运行示例：对带点位的候选列表做综合排序并打印前三强。

用法（在仓库根目录）::

    PYTHONPATH=/Users/bai/Desktop/a8 python -m simlab.scoring.example
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# 保证可从任意 cwd 导入
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from simlab.levels import calculate_advanced_trading_levels
from simlab.market.klines import load_mtf_frames
from simlab.scoring.ranker import rank_ambush_rotation
from simlab.screener import screen_top10

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("scoring.example")


def main() -> int:
    screen = screen_top10()
    items = screen.get("items") or []
    if not items:
        print("无候选币种")
        return 1

    prepared = []
    for c in items:
        frames = load_mtf_frames(c["pair"], 120)
        levels = calculate_advanced_trading_levels(
            frames["df_4h"], frames["df_1h"], frames["df_15m"]
        )
        if not levels:
            continue
        vol = frames["df_15m"]["volume"] if "volume" in frames["df_15m"].columns else None
        prepared.append(
            {
                **c,
                "vs_btc": c.get("vs_btc_24h") or c.get("vs_btc_1h") or 0,
                "levels": levels,
                "volume_15m": vol,
            }
        )

    result = rank_ambush_rotation(prepared)
    print("\n======== TOP10 ========")
    for it in result["top10"]:
        flag = "✓" if it.get("hard_pass") else "✗"
        print(
            f"#{it['rank']} {it['symbol']:8} score={it['total_score']:5.1f} "
            f"hard={flag} dist={it.get('distance_pct')}% RR={it.get('risk_reward_ratio')}"
        )

    print("\n======== 推荐前三强（轻仓埋伏）========")
    if result["top3_fallback"]:
        print("（硬过滤通过数不足，已降级补足）")
    for it in result["top3"]:
        b = it.get("batch_orders") or {}
        t1, t2 = b.get("tranche_1") or {}, b.get("tranche_2") or {}
        print(
            f"#{it['rank']} {it['symbol']} 总分={it['total_score']} "
            f"量={it['score_volume']} 强度={it['score_rel_strength']} "
            f"费率={it['score_funding']} 波动={it['score_volatility']} "
            f"可操作={it['score_operability']}"
        )
        print(
            f"   买={it['entry']} 损={it['stop_loss']} 盈={it['take_profit']} "
            f"距现价={it.get('distance_pct')}% RR={it.get('risk_reward_ratio')}"
        )
        hs = b.get("hard_stop") or {}
        print(
            f"   阶梯: 现价{it['price']} > ①{t1.get('price')}({int((t1.get('ratio') or 0)*100)}%) "
            f"> ②{t2.get('price')}({int((t2.get('ratio') or 0)*100)}%) > 止损{hs.get('price')}"
        )
        print(
            f"   安全边际: 两仓差{it.get('tranche_gap_pct')}% · "
            f"止损距②{it.get('stop_gap_pct')}%"
        )

    out = _ROOT / "simlab" / "data" / "last_rank.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
