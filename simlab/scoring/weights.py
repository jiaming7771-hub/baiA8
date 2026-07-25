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
# 止损被刻意拉宽（抗插针）后，风险距离上限相应放宽到 4.5%
HARD_RISK_MAX = 0.045          # (entry-stop)/entry 上限
HARD_RR_MIN = 1.3
HARD_VOL_RATIO_MIN = 0.35      # 近5根均量 / 20根均量，低于则视为极度萎缩

# ---------- 可操作性融合 ----------
OP_W_DISTANCE = 0.70
OP_W_RR = 0.30
RR_SCORE_FLOOR = 1.0           # RR=1 → 偏低分
RR_SCORE_IDEAL = 2.0           # RR>=2 → 满分附近

# ---------- 分批挂单（轻仓埋伏）----------
# 阶梯顺序硬约束：现价 > 第一仓 > 第二仓 > 硬止损
TRANCHE1_RATIO = 0.30          # 第一仓·轻仓试错（靠上，贴支撑）
TRANCHE2_RATIO = 0.70          # 第二仓·下探补仓（更低，留插针空间）

# 第一仓相对现价的最小/最大回撤距离
T1_MIN_GAP_PCT = 0.003         # 至少低于现价 0.3%，避免贴价成交
T1_MAX_GAP_PCT = 0.040         # 最多低于现价 4%，太远无意义

# 两仓之间的间距（优先用 ATR，落在 min~max 之间）
T2_GAP_ATR_MULT = 0.8
T2_GAP_MIN_PCT = 0.004         # 两仓至少拉开 0.4%
T2_GAP_MAX_PCT = 0.020         # 两仓最多拉开 2.0%

# 硬止损：必须在第二仓下方 1.5%~2.5%（按 ATR 在区间内取值）
STOP_BUFFER_MIN_PCT = 0.015
STOP_BUFFER_MAX_PCT = 0.025
STOP_BUFFER_ATR_MULT = 1.2
# 总风险硬顶：(均价 - 止损)/均价
MAX_TOTAL_RISK_PCT = 0.045

# 止盈：至少满足目标盈亏比，并受 ATR 与百分比双重上限约束
TP_RR_TARGET = 1.6
TP_MIN_PCT = 0.010             # 至少 +1%（与原算法一致）
TP_MAX_PCT = 0.080             # 单次波段最多看 +8%
TP_ATR_MULT_CAP = 4.0          # 且不超过 4×ATR

# ---------- 输出 ----------
TOP_N = 10
TOP_K = 3
MAX_ABS_FUNDING = 0.0003
