# CRYPTO PULSE · 100U 战神短线监控面板

基于 **FastAPI + Binance WebSocket + Vue 3 + ECharts** 的全栈实时行情大屏：后端订阅币安毫秒级现货成交流 + 合约维度数据（资金费率 / 多空比 / 爆仓流），经 WebSocket 零延迟推送到前端，支持涨跌闪烁、实时走势图、价格预警、强平计算器、声音警报与事件/爆仓日志。

## 项目结构

```
.
├── backend/
│   ├── requirements.txt    # Python 依赖
│   └── main.py             # 交易所订阅 + 前端推送服务
├── frontend/
│   └── index.html          # 可视化大屏（Vue3 / Tailwind / ECharts CDN）
└── README.md
```

## 功能一览

| 模块 | 说明 |
|------|------|
| 顶部状态栏 | 前后端连接状态指示灯（绿/黄/红）、现货/合约在线状态、系统时钟 |
| 核心卡片 | BTC / ETH 最新价大字展示，上涨绿闪 / 下跌红闪，含 24h 高低与涨跌幅 |
| 资金费率 | 实时费率 + **下次结算倒计时**（来自合约 markPrice） |
| 多空比 | 多空持仓人数比 + 多/空偏向进度条 |
| 实时折线图 | ECharts 高频分时曲线，可切换 BTC / ETH |
| 爆仓日志 | 「实时行情 & 爆仓日志」合并信息流，强平事件红色高亮，可设名义价值阈值过滤 |
| 强平计算器 | 输入开仓价/杠杆/方向，估算强平价并给出 安全 / 注意 / 危险 提示 |
| 价格预警 | 自定义上下限，突破后页内 Toast + 浏览器 Notification + 声音 |
| 快捷开关 | 声音警报、浏览器通知、钉钉/微信推送（Webhook 预留） |
| **山寨短线雷达** | 每分钟扫描全市场，成交额>5000万 / 相对 BTC 强弱 / \|费率\|≤0.03% 取 Top 3；禁多时暂停推荐 |
| **宏观风控** | BTC 1h 涨跌+波动率；暴跌触发「全局禁多令」 |
| **1x-2x 安全计算器** | 保证金+1x/2x → 止损线/强平线，并画入主图 |
| **全网交易所聚合** | Binance / OKX / Bybit 永续价格对比，算全网均价与跨所价差（bps），标注最高/最低所 |
| 断线重连 | 后端↔币安（现货+合约多端点）、前端↔后端均自动重连（指数退避） |

### 山寨短线雷达算法

每 **60 秒**执行一次：

1. 拉取合约 `/fapi/v1/ticker/24hr` + `/fapi/v1/premiumIndex`（不可达时回退现货 `data-api.binance.vision`）
2. **流动性**：USDT 成交额 `quoteVolume > 5000 万`，剔除 BTC/ETH/稳定币
3. **费率**：`|lastFundingRate| ≤ 0.03%`（现货代理模式下跳过，界面费率显示 `—`）
4. **相对强弱**：近 1 小时涨幅（1h K 线）**强于 BTC**
5. 按相对 BTC 超额涨幅排序，取 Top **3**；大盘禁多时清空推荐
6. 同步推送 `market_safety`（BTC 1h / 波动率 / 安全可做多|大盘风险避险中）
7. 自动订阅入选币种 `aggTrade`；前端点击发送 `watch` 切换主图

> 当前网络若无法访问 `fapi`，雷达会标注数据源为 **现货代理**（成交额/1h/价格为真实数据，费率字段暂缺）。

### 全网多交易所聚合（价差 / 套利观察）

后端为 **Binance / OKX / Bybit** 三家永续行情各起一个连接器，采用三级降级策略：

| 级别 | 方式 | 说明 |
|------|------|------|
| 1 | **WebSocket** | Binance `aggTrade`/`markPrice`、OKX `tickers`、Bybit `tickers`，毫秒级 |
| 2 | **REST 轮询** | WS 不可达时每 3s 轮询各所 ticker 接口 |
| 3 | **模拟** | 该所完全不可达时生成标注 `模拟` 的报价，保证对比表不空白 |

聚合服务每秒计算并推送 `type: "multi_exchange"`：

- **全网均价** = 各所有效报价均值
- **对均价偏离** = `(该所价 - 均价) / 均价 × 10000` bps（红=偏高可卖，绿=偏低可买）
- **跨所价差** = `(最高所 - 最低所) / 均价 × 10000` bps，≥ 8 bps 标记「套利机会」

各所连接方式在界面「状态」列实时显示（实时 / REST / 模拟 / 离线）。

```bash
# 关闭模拟报价（不可达的交易所显示离线）
DEMO_EXCHANGES=off uvicorn main:app --port 8000
```

> ⚠️ 价差仅为**行情观察**，未扣除手续费、资金费率、提币与滑点成本，不构成套利收益。

### 关于合约数据（资金费率 / 多空比 / 爆仓）

这些属于**币安合约接口**（`fstream.binance.com` / `fapi.binance.com`）的数据。若你的网络无法访问（如返回 `451` 或握手超时），后端会**自动切换为「模拟数据」**保证面板不空白，前端会在对应位置显示醒目的 `模拟` 标记。恢复真实合约访问（如使用可访问币安合约的网络）后会自动切回真实数据。

可通过环境变量 `DEMO_FUTURES` 控制：

| 值 | 行为 |
|----|------|
| `auto`（默认） | 真实合约流不可达时自动使用模拟数据 |
| `on` | 始终使用模拟数据（演示 / 离线开发） |
| `off` | 从不使用模拟数据（合约不可达时相关字段显示 `—`） |

```bash
# 例如强制关闭模拟：
DEMO_FUTURES=off uvicorn main:app --host 0.0.0.0 --port 8000
```

> ⚠️ 模拟数据仅用于界面演示，**不代表真实行情**，请勿据此交易。

## 环境要求

- Python **3.10+**
- 可访问公网（连接 `stream.binance.com`）
- 现代浏览器（Chrome / Edge / Firefox / Safari）

## 安装与启动

### 1. 安装后端依赖

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 启动后端

在 `backend/` 目录下执行：

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

或：

```bash
python main.py
```

看到日志中出现类似 `Connected to Binance WebSocket` 即表示已接到币安行情。

健康检查：打开 [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)

### 3. 打开前端大屏

**推荐方式（由后端托管）：**

浏览器访问 → [http://127.0.0.1:8000/](http://127.0.0.1:8000/)

**独立打开 HTML：**

直接用浏览器打开 `frontend/index.html`（需保证后端已在 `8000` 端口运行；CDN 资源需要网络）。

## 使用说明

1. 打开大屏后，顶部指示灯应变为「已连接」，价格卡片开始跳动。
2. 在 **PRICE ALERTS** 面板为 BTC/ETH 填写上限/下限并勾选「启用」。
3. 点击「开启浏览器通知」授权后，价格突破阈值时会弹出系统通知。
4. 右侧 **EVENT LOG** 可持续观察成交推送与重连事件。

## 技术说明

### 数据流

```
现货  Binance aggTrade / 24hrTicker ─┐
合约  markPrice / forceOrder (ws)   ─┤──►  FastAPI (main.py) ──广播 /ws──►  浏览器大屏 (Vue)
合约  多空比 globalLongShortAccountRatio (REST 轮询) ─┘        ▲
                                                     合约不可达时 → 模拟数据（标注 simulated）
```

- **aggTrade**：毫秒级成交价，驱动价格闪烁与折线图
- **24hrTicker**：补充 24h 最高/最低/涨跌幅
- **markPrice**：资金费率 + 下次结算时间
- **forceOrder**：强平/爆仓事件（驱动爆仓日志）
- **globalLongShortAccountRatio**：多空持仓人数比（每 30s 轮询）

前后端消息类型：`snapshot` / `trade` / `ticker` / `funding` / `ratio` / `liquidation` / `system` / `pong`。

### 端口与地址

| 用途 | 地址 |
|------|------|
| 大屏页面 | `http://127.0.0.1:8000/` |
| 前端 WebSocket | `ws://127.0.0.1:8000/ws` |
| 健康检查 | `http://127.0.0.1:8000/health` |

若部署到其他主机，请确保前端页面与后端同源，或自行修改 `index.html` 中的 `WS_URL`。

## 常见问题

**Q: 页面显示「重连中 / 已断开」？**  
确认后端进程在跑，且本机防火墙未拦截 8000 端口。

**Q: 交易所显示离线？**  
检查本机网络是否可访问币安 WebSocket；后端会自动指数退避重连（最长间隔 60s）。

**Q: 浏览器通知不弹出？**  
需在 HTTPS 或 `localhost` 下授权 Notification；`file://` 打开时部分浏览器会限制通知权限，建议通过 `http://127.0.0.1:8000/` 访问。

## License

MIT — 仅供学习与个人监控使用，行情数据来自币安公开接口。
