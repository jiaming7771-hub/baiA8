"""综合评分与硬性过滤阈值（独立配置，便于调参）。"""

from __future__ import annotations

# ---------- 总分权重（合计应为 1.0）----------
W_VOLUME = 0.25          # 成交额（对数归一化）
W_REL_STRENGTH = 0.25    # 相对强度 vs BTC
W_FUNDING = 0.15         # 资金费率健康度
W_VOLATILITY = 0.15      # 波动率质量 ATR%
W_OPERABILITY = 0.20     # 点位可操作性

# ---------- 成交额对数归一化基准（USDT）----------
VOL_LOG_FLOOR = 50_000_000      # 5000 万
VOL_LOG_CEIL = 1_000_000_000    # 10 亿

# ---------- 相对强度基准（百分点，vs BTC）----------
RS_FLOOR = -5.0
RS_CEIL = 15.0

# ---------- ATR% 理想区间 ----------
ATR_PCT_IDEAL_LO = 3.0
ATR_PCT_IDEAL_HI = 8.0
ATR_PCT_HARD_LO = 1.0
ATR_PCT_HARD_HI = 15.0

# ---------- 点位距离（现价相对 entry 的下方距离 %）----------
DIST_IDEAL_LO = 0.8
DIST_IDEAL_HI = 2.5
DIST_HARD_LO = 0.6
DIST_HARD_HI = 3.5

# ---------- 硬性过滤门槛 ----------
HARD_DIST_LO = 0.6
HARD_DIST_HI = 3.5
HARD_RISK_MAX = 0.028          # (entry-stop)/entry <= 2.8%
HARD_RR_MIN = 1.3
HARD_VOL_RATIO_MIN = 0.35      # 近5根均量 / 20根均量，低于则视为极度萎缩

# ---------- 可操作性融合 ----------
OP_W_DISTANCE = 0.70
OP_W_RR = 0.30
RR_SCORE_FLOOR = 1.0           # RR=1 → 偏低分
RR_SCORE_IDEAL = 2.0           # RR>=2 → 满分附近

# ---------- 分批挂单（轻仓埋伏）----------
TRANCHE1_RATIO = 0.30          # 近现价试错
TRANCHE2_RATIO = 0.70          # 下探支撑补仓
TRANCHE1_OFFSET = 0.001        # 试错挂在现价下方 0.1%

# ---------- 输出 ----------
TOP_N = 10
TOP_K = 3
MAX_ABS_FUNDING = 0.0003
