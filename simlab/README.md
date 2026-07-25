# 双子星模拟盘

个人模拟盘：Binance ∩ OKX TOP10 选币 → 多周期量化挂单点位 → 每 15 分钟撮合 → 每小时桌面盈亏通知。

## 快速开始

```bash
cd /Users/bai/Desktop/a8
# 可复用看板虚拟环境
source backend/.venv/bin/activate
pip install -r simlab/requirements.txt

# 只跑一轮（验证连通性）
PYTHONPATH=/Users/bai/Desktop/a8 python -m simlab --once

# 常驻：15 分钟循环 + 每小时桌面盈亏
PYTHONPATH=/Users/bai/Desktop/a8 python -m simlab

# 立即推送一次小时盈亏通知
PYTHONPATH=/Users/bai/Desktop/a8 python -m simlab --hourly-now
```

调试可缩短周期：

```bash
PYTHONPATH=/Users/bai/Desktop/a8 python -m simlab --cycle-seconds 60 --hourly-seconds 300
```

## 综合评分与前三强

权重与硬门槛见 `simlab/scoring/weights.py`。一键试跑：

```bash
PYTHONPATH=/Users/bai/Desktop/a8 python -m simlab.scoring.example
```

输出：完整 TOP10（含硬过滤标记）+ 推荐前三强（分批挂单 30%/70%）。

## 实盘（极度保守，默认 dry-run + 沙盒）

```bash
pip install -r simlab/requirements.txt
cp simlab/.env.example /Users/bai/Desktop/a8/.env   # 填入密钥，勿提交

# 1) 只演练（不发真单）— 需密钥可读余额
export LIVE_CONFIRM_NO_WITHDRAW=1
export $(grep -v '^#' .env | xargs)   # 或手动 export
PYTHONPATH=/Users/bai/Desktop/a8 python -m simlab.live --once

# 2) 测试网真单（仍极保守）
export LIVE_TRADING=1
export LIVE_SANDBOX=1
PYTHONPATH=/Users/bai/Desktop/a8 python -m simlab.live --once --live

# 紧急停机：创建空文件即可
touch simlab/data/KILL_SWITCH
```

安全硬规则：资金池默认 5%（≤20%）、单笔风险默认 0.3%（≤1%）、杠杆≤2x、只做前三强 hard_pass、密钥仅环境变量、启动校验无提现确认。


1. **选币**（每轮）：双所永续交集 · 24h 额 > 5000 万 · |费率| < 0.03% · 24h 强于 BTC · 综合分 TOP10  
2. **点位**：`simlab/levels.py` 中 `calculate_advanced_trading_levels`（算法体保持原样）  
3. **模拟盘**：限价挂多 → 15m 低点触及成交 → 止损/止盈；风险 2%/笔，最多 5 仓  
4. **日志**：`simlab/logs/cycle.log`、`pnl_hourly.log`、`events.jsonl`；状态 `simlab/data/paper_state.json`

## 环境变量（可选）

| 变量 | 默认 | 含义 |
|------|------|------|
| `SIM_INITIAL_EQUITY` | 10000 | 初始资金 |
| `SIM_MAX_POSITIONS` | 5 | 最大持仓+挂单 |
| `SIM_RISK_PER_TRADE` | 0.02 | 单笔风险占比 |
| `SIM_CYCLE_SECONDS` | 900 | 循环间隔 |
| `SIM_HOURLY_SECONDS` | 3600 | 盈亏通知间隔 |

## 目录

```
simlab/
  levels.py          # 点位纯函数
  screener.py        # 双子星 TOP10
  paper/engine.py    # 15m 撮合
  notify/desktop.py  # macOS 通知
  runner.py          # 主循环
  data/              # 状态与成交
  logs/              # 周期与盈亏日志
```
