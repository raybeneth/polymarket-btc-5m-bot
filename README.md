# Polymarket BTC 5-min Trading Bot

在 Polymarket 的 BTC 5 分钟涨跌二元市场中，只在赔率、方向、时机、盘口同时满足条件时出手。核心逻辑是过滤，不是频繁交易。

---

## 目录

1. [项目结构](#项目结构)
2. [快速启动](#快速启动)
3. [凭证获取方法](#凭证获取方法)
4. [5 个过滤器详解](#5-个过滤器详解)
5. [综合评分机制](#综合评分机制)
6. [控制台日志说明](#控制台日志说明)
7. [交易记录文件](#交易记录文件)
8. [风控规则](#风控规则)
9. [参数调整](#参数调整)

---

## 项目结构

```
polymarket-btc-5m-bot/
├── main.py                   # 主循环，每 2 秒评估一次所有活跃市场
├── config.py                 # 所有阈值的唯一来源，改参数只动这一个文件
├── get_credentials.py        # 一次性运行，自动生成 .env 凭证
├── .env.example              # 环境变量模板
├── .gitignore                # 排除 .env 和日志文件
├── requirements.txt
│
├── feeds/
│   ├── btc_price.py          # Binance WebSocket，维护 BTC 价格滚动历史（10 分钟）
│   └── polymarket_clob.py    # Gamma API 市场扫描 + CLOB 盘口拉取
│
├── filters/
│   ├── direction.py          # 方向强度（BTC 动量 65% + 盘口失衡 35%）
│   ├── odds.py               # 赔率质量（价格区间判断）
│   ├── timing.py             # 入场时间窗口
│   ├── spread.py             # 买卖价差 + 深度
│   └── chase.py              # 追价空间
│
├── execution/
│   ├── order_manager.py      # 下单执行（paper / live 双模式）
│   └── risk.py               # 日亏损上限、仓位控制
│
└── utils/
    └── logger.py             # 彩色控制台日志 + trades.jsonl 记录
```

---

## 快速启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 获取凭证（见下方说明）
python get_credentials.py

# 3. 模拟模式启动（默认，不花真钱）
python main.py

# 4. 确认逻辑没问题后，编辑 .env 改为真实模式
#    PAPER_TRADING=false
python main.py
```

---

## 凭证获取方法

Polymarket CLOB 使用两层认证体系：

### L1 — 钱包私钥（PRIVATE_KEY）

**用途**：给每一笔链上订单签名，保证不可伪造。

**获取方式**：
- MetaMask：右上角菜单 → 账户详情 → 导出私钥
- Coinbase Wallet / OKX Wallet 等任何 EVM 钱包操作相同
- 格式：`0x` 开头的 64 位十六进制字符串，共 66 个字符

> **安全提示**：私钥是你资产的唯一控制权。只在可信机器上使用，
> 不要截图，不要发给任何人，不要提交到 git（`.gitignore` 已排除 `.env`）。

### L2 — REST API 密钥（API_KEY / SECRET / PASSPHRASE）

**用途**：给每个 HTTP 请求签名，Polymarket 服务器用来验证调用者身份。

**获取方式**：这三个值**不需要手动申请**，由 `py-clob-client` 用你的私钥自动派生：

```
你的私钥  →  签名一条标准消息  →  发送给 CLOB /auth/api-key  →  服务器返回 L2 凭证
```

这是确定性派生：**同一私钥每次得到相同的凭证**，不会重复创建。

**一键生成**：

```bash
python get_credentials.py
```

脚本会提示输入私钥，完成后自动写入 `.env` 文件。

**前提条件**：这个钱包地址必须至少在 [polymarket.com](https://polymarket.com) 连接登录过一次（不需要充值，只需连接钱包），否则 CLOB 服务器不认识这个地址，会返回 401 错误。

### .env 文件示例

```env
PRIVATE_KEY=0xabcdef...（64位十六进制）
CLOB_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
CLOB_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
CLOB_API_PASSPHRASE=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

PAPER_TRADING=true       # true=模拟  false=真实下单
TRADE_SIZE_USD=1.0       # 每笔交易金额（美元）
LOG_LEVEL=INFO
```

---

## 5 个过滤器详解

每个市场每 2 秒评估一次，**任意一个过滤器不通过，立即跳过，不继续后续判断**。

### 过滤器 1 — 方向强度（Direction）

**目的**：判断 BTC 当前是否有明确的方向动能，以及方向是什么。

**两个信号合并**：

| 信号 | 权重 | 计算方式 |
|------|------|---------|
| BTC 现货动量 | 65% | Fast MA（近 30 秒均价）vs Slow MA（近 90 秒均价）的偏离幅度，叠加 120 秒内的总涨跌幅 |
| Polymarket 盘口失衡 | 35% | YES token 和 NO token 各自的买卖挂单量比，反映二元市场本身的方向压力 |

**评分**：合并分 ∈ [-100, +100]

| 分值区间 | 行动 |
|---------|------|
| ≥ +35 | 看涨 → 买 YES token |
| ≤ -35 | 看跌 → 买 NO token |
| (-35, +35) | 方向不明 → **跳过** |

**默认阈值**：`DIRECTION_MIN_SCORE = 35`（在 `config.py` 中修改）

---

### 过滤器 2 — 赔率质量（Odds）

**目的**：排除价格过低（像彩票）和价格过高（几乎必然，回报太小）的市场。

Polymarket 二元市场的 token 价格 = 该结果的隐含概率，也就是你的成本。

| 价格区间 | 含义 | 行动 |
|---------|------|------|
| < 0.20 | 隐含概率 < 20%，赔率像彩票，期望值极不稳定 | **跳过** |
| > 0.80 | 几乎确定，赔率 < 1.25x，没有足够回报空间 | **跳过** |
| 0.20 ~ 0.30 / 0.70 ~ 0.80 | 可接受但不理想 | 通过，低分 |
| 0.30 ~ 0.70 | 理想区间，赔率与概率最均衡 | 通过，高分 |
| 最优：接近 0.50 | 最高不确定性 = 最高赔率杠杆 | 满分 100 |

**计算示例**：
- 价格 0.45 → 赔率 2.22x → 分数约 95
- 价格 0.25 → 赔率 4.00x → 分数约 25（低分）
- 价格 0.82 → **跳过**

---

### 过滤器 3 — 入场时机（Timing）

**目的**：只在 5 分钟市场的有效窗口内入场，太早方向未形成，太晚没有获利空间。

市场总时长：300 秒（5 分钟）

```
|--45s--|------------ 入场窗口（165秒）------------|--90s--|
0s     45s                                      210s   300s
(开盘)  (最早入场)                             (最晚入场) (到期)
```

| 条件 | 阈值 | 跳过原因 |
|------|------|---------|
| 已过时间 < 45s | `TIMING_MIN_ELAPSED = 45` | 太早，方向未形成 |
| 已过时间 > 210s | `TIMING_MAX_ELAPSED = 210` | 太晚，价格已接近结算值 |
| 剩余时间 < 90s | `TIMING_MIN_REMAINING = 90` | 没有足够时间兑现方向优势 |

**评分**：窗口中间（约第 127 秒时）得满分，向两端线性递减。

---

### 过滤器 4 — 盘口价差与深度（Spread）

**目的**：避免在买卖价差过宽的市场交易（做市商成本太高），同时确保最优价位有足够挂单量。

**检查项目**：

| 检查项 | 阈值 | 跳过原因 |
|-------|------|---------|
| 买一价 < 0.03 | `MIN_BID = 0.03` | 市场几乎死亡，无流动性 |
| 卖一价 < 0.03 | `MIN_ASK = 0.03` | 同上 |
| 价差 > 0.04 | `SPREAD_MAX_ALLOWED = 0.04` | 买卖价差超过 4 分，成本太高 |
| 卖一档数量 < 50 shares | `DEPTH_MIN_SHARES = 50` | 最优价格处挂单不足，无法成交 |

**评分**：
- 价差分（60%权重）：价差越窄越高，0 时满分
- 深度分（40%权重）：卖一数量越多越高，超过最小值 3 倍时满分
- 价差 > 0.02 时日志会标注 `[wide]` 警告

---

### 过滤器 5 — 追价空间（Chase）

**目的**：确认在下单价格（卖一价 + 滑点）附近有足够的流动性完成成交，避免追高被深度坑。

**逻辑**：

```
追价窗口 = [best_ask, best_ask + 0.03]（最多追 3 分钱）
```

在这个价格窗口内的累计挂单量必须满足：
- 总量 ≥ 30 shares（`CHASE_MIN_FILL_SHARES`）
- 窗口内足够覆盖我们按 `TRADE_SIZE_USD / best_ask` 算出的所需股数

**评分**：
- 成交覆盖率（60%）：窗口内可成交量 / 所需量，100% = 满分
- 余量空间（40%）：窗口内量 / (2 × 最小值)，越宽裕越高

---

## 综合评分机制

5 个过滤器**全部通过**后，按权重加权计算综合分：

| 过滤器 | 权重 |
|--------|------|
| 方向强度 | 35% |
| 赔率质量 | 25% |
| 入场时机 | 15% |
| 盘口价差 | 15% |
| 追价空间 | 10% |

**综合分 ≥ 55 才下单**（`MIN_COMBINED_SCORE = 55`）

即使所有过滤器都通过，如果综合分不够，依然跳过，日志显示 `SKIP score_gate`。

---

## 控制台日志说明

所有市场评估都会打印，包括跳过的和成交的。

### 跳过日志（SKIP）

```
10:23:45 INFO  [abc1234567] SKIP direction     Direction too weak — combined=12.3, need |score| ≥ 35   elapsed=67s remaining=233s
10:23:47 INFO  [abc1234567] SKIP timing        Too early — only 30s elapsed, need ≥45s   dir=✓82  odds=✓71
10:23:49 INFO  [abc1234567] SKIP spread        Spread 0.0520 > max allowed 0.04   dir=✓82  odds=✓71  time=✓88  sprd=✗52
10:23:51 INFO  [abc1234567] SKIP score_gate    Score too low: 52.3 < 55   dir=✓71  odds=✓65  time=✓88  sprd=✓60  chs=✓70
```

格式说明：
- `[abc1234567]`：市场 ID 前 10 位
- `SKIP xxx`：哪个过滤器拦截了（`direction` / `odds` / `timing` / `spread` / `chase` / `score_gate` / `book_fetch`）
- 后面的原因文字说明具体为什么跳过
- `dir=✓82` 表示方向过滤器通过，得分 82；`sprd=✗52` 表示盘口过滤器未通过
- **防刷屏**：同一市场相同原因连续出现只打印一次，原因变化时才重新打印

### 交易日志（TRADE）

```
10:24:01 INFO  ──────────────────────────────────────────────────────────────────────
10:24:01 INFO  [abc1234567] ★ TRADE  YES @ 0.445  score=67.3  elapsed=95s remaining=205s
10:24:01 INFO    question : Will BTC be higher in 5 minutes?
10:24:01 INFO    direction: Direction YES — combined_score=61.2
10:24:01 INFO    odds     : Price 0.445 OK — implied_prob=44.5%, payout=2.25x
10:24:01 INFO    timing   : Timing OK — 95s elapsed, 205s remaining
10:24:01 INFO    spread   : Spread 0.0180, depth_at_ask=120 shares
10:24:01 INFO    chase    : Chase OK — 230 shares up to 0.475 (100% fill coverage)
10:24:01 INFO  ──────────────────────────────────────────────────────────────────────
```

### 状态摘要（每 60 秒）

```
10:30:00 INFO  Status: 3 active markets | Day 2026-06-29 | trades=5 W/L=3/2 (60%) | PnL=+0.4200 USD
```

---

## 交易记录文件

所有交易（模拟和真实）记录到 `trades.jsonl`，每行一个 JSON 对象。

```json
{
  "mode": "paper",
  "order_id": "PAPER-a1b2c3d4",
  "market_id": "0xabc...",
  "side": "YES",
  "token_id": "0xdef...",
  "entry_price": 0.4620,
  "shares": 2.16,
  "usd_size": 1.0,
  "combined_score": 67.3,
  "filter_scores": {
    "direction": 74.2,
    "odds": 91.0,
    "timing": 88.5,
    "spread": 78.0,
    "chase": 95.0
  },
  "timestamp": 1719648241.23,
  "logged_at": 1719648241.24
}
```

**用 pandas 分析**：

```python
import pandas as pd
df = pd.read_json("trades.jsonl", lines=True)
df["entry_price"].hist()
df.groupby("side")["combined_score"].mean()
```

---

## 风控规则

| 规则 | 值 | 说明 |
|------|-----|------|
| 最大同时持仓 | 1 | 同一时间只持有一个市场的仓位 |
| 每笔金额 | `TRADE_SIZE_USD`（默认 $1） | 在 `.env` 中配置 |
| 每日亏损上限 | $10 | 当日累计亏损超过 $10 自动停止下单 |
| 订单超时 | 8 秒 | 限价单 8 秒未成交自动撤单 |
| 追价上限 | 3 分钱 | 超出卖一价 3 分钱不再追单 |
| 每个市场只交易一次 | — | 同一个市场生命周期内最多下一次单 |

---

## 参数调整

所有可调参数集中在 `config.py`，修改后重启 bot 即生效。

| 参数 | 默认值 | 作用 |
|------|--------|------|
| `ODDS_MIN_PRICE` | 0.20 | 最低可交易价格（隐含概率下限） |
| `ODDS_MAX_PRICE` | 0.80 | 最高可交易价格（隐含概率上限） |
| `DIRECTION_MIN_SCORE` | 35 | 方向分最小值，调高 = 只做强趋势 |
| `TIMING_MIN_ELAPSED` | 45s | 最早入场时间，调高 = 等更长再入场 |
| `TIMING_MIN_REMAINING` | 90s | 最少剩余时间，调高 = 更早截止入场 |
| `SPREAD_MAX_ALLOWED` | 0.04 | 最大允许价差，调低 = 只做流动性好的市场 |
| `CHASE_BUDGET` | 0.03 | 追价预算，调低 = 减少滑点 |
| `MIN_COMBINED_SCORE` | 55 | 综合门槛，调高 = 更严格，交易更少 |
| `TRADE_SIZE_USD` | 1.0 | 每笔金额（美元） |
| `MAX_DAILY_LOSS_USD` | 10.0 | 日亏损上限 |
