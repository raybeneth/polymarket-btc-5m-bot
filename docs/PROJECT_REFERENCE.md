# Polymarket BTC 5min Bot — 技术参考文档

> 整理目的：作为重建"15分钟周期版本"新项目的参考底稿。本文档覆盖技术栈、项目结构、
> 外部接口、CLOB SDK 用法、交易决策链路、数据库结构、Web 面板协议、部署方式，
> 以及移植到 15 分钟市场时需要改动的具体位置。

---

## 1. 技术栈

| 类别 | 选型 | 说明 |
|---|---|---|
| 语言/运行时 | Python 3.11 (`python:3.11-slim`) | 全异步（asyncio），单进程单事件循环 |
| HTTP 客户端 | `aiohttp>=3.9.0` | 所有 REST 请求（Gamma/CLOB/Binance/OKX/Polygon RPC）+ Web 面板服务端 |
| WebSocket 客户端 | `websockets>=12.0` | BTC 现货价格流（Binance/OKX） |
| 代理支持 | `aiohttp_socks`, `websockets_proxy` | 部分服务器所在地区被 Polymarket/交易所墙，需走代理；`PROXY_ENABLED` 总开关 |
| 交易所 SDK | `polymarket-client>=0.1.0b16`（`AsyncSecureClient`） | Polymarket 官方新统一 SDK，替代已废弃且有 bug 的 `py-clob-client-v2` |
| 数据库 | MySQL 8.4（`pymysql` + `dbutils.pooled_db.PooledDB` 连接池） | 唯一的持久化存储，仓位/日志/账户余额/策略导航 全部落库 |
| 配置管理 | `python-dotenv`（`.env`） | 所有密钥/开关走环境变量，`config.py` 集中读取 |
| 日志 | `colorlog`（终端彩色） + 标准 `logging.FileHandler`（`bot.log`） | 时间戳强制 UTC+8，与系统时区无关 |
| 终端仪表盘（可选） | `rich` | `utils/dashboard.py`，非必需，Web 面板是主要监控方式 |
| Web 面板 | `aiohttp.web` 内置 HTTP+WS 服务，前端是**纯静态 HTML/CSS/JS**（无框架） | 图表库用 `lightweight-charts`（CDN），Markdown 渲染用 `marked`（CDN） |
| 容器化 | `Dockerfile`（单阶段） + `docker-compose.yml`（bot + mysql 两个服务） | |

依赖清单（`requirements.txt`）：
```
polymarket-client>=0.1.0b16
aiohttp>=3.9.0
websockets>=12.0
python-dotenv>=1.0.0
colorlog>=6.8.0
rich>=13.0.0
websockets_proxy
aiohttp_socks
pymysql>=1.1.0
dbutils>=3.0.0
```

---

## 2. 项目结构

```
polymarket-btc-5m-bot/
├── main.py                  # 主循环：市场扫描→过滤器管道→下单→风控→仪表盘状态
├── config.py                 # 全部配置/阈值集中于此，无硬编码散落在业务代码里
├── web_only.py                # 独立仪表盘模式：不交易，只读 MySQL 历史数据展示
├── test_order.py                # 手工验证下单链路的最小金额测试脚本（真实网络）
├── print_sigtypes.py         # 打印旧 SDK(py-clob-client-v2) 签名类型枚举，调试用
│
├── feeds/                    # 数据源层
│   ├── btc_price.py           # BTC 现货 WS 价格流(Binance/OKX 可切换) + REST K线拉取
│   └── polymarket_clob.py     # Gamma API 市场扫描 + CLOB REST 订单簿 + user-pnl 拉取
│
├── filters/                  # 5 个独立打分/过滤器（每个返回 FilterResult）
│   ├── direction.py            # 方向强度：动量(65%)+订单簿失衡(35%)，含波动率自适应
│   ├── odds.py                  # 价格/赔率质量（隐含概率是否在合理区间）
│   ├── timing.py                 # 入场时机（市场生命周期内的窗口）
│   ├── spread.py                 # 价差与深度（流动性）
│   └── chase.py                   # 追单空间（滑点容忍度内是否够量成交）
│
├── signals/                  # 目前为空占位包（预留给未来独立信号模块）
│
├── execution/                 # 执行与风控
│   ├── order_manager.py         # 下单/撤单/止损止盈/强制平仓/链上对账（paper+live 两套路径）
│   └── risk.py                    # 仓位状态机：日亏损上限/最大持仓数/熔断/余额记账
│
├── storage/                   # 持久化
│   ├── db.py                     # 所有 MySQL 读写（连接池，同步 pymysql，异步层用 run_in_executor 包裹）
│   └── schema.sql                # 表结构（positions / logs / strategy_links / account_state）
│
├── utils/
│   ├── logger.py                  # 日志初始化 + JSONL/文本备份文件写入
│   └── dashboard.py                # 终端富文本仪表盘（可选，非核心路径）
│
├── web/
│   ├── server.py                    # aiohttp 路由 + WebSocket 广播 + 日志转发到 MySQL
│   └── static/index.html             # 单文件前端（HTML+CSS+JS 全内联，无构建步骤）
│
├── docs/optimizations/            # 每次策略调优的复盘记录（编号+日期+摘要命名）
├── Dockerfile / docker-compose.yml / .env.example
├── requirements.txt
├── bot.log / positions.log / trades.jsonl   # 运行期产物（MySQL 是权威数据源，这些是本地备份）
```

**关键设计原则**（对新项目同样适用）：
- 所有阈值/开关集中在 `config.py`，业务代码里不出现魔法数字。
- 每个 filter 独立返回 `FilterResult(passed, score, reason, details)`，互不感知，`main.py` 里组装。
- paper/live 两条执行路径共享同一套过滤器和风控逻辑，只在 `order_manager.py` 内部分叉（模拟成交 vs 真实下单）。
- MySQL 是唯一权威数据源；本地文件（`bot.log`/`positions.log`/`trades.jsonl`）只是调试备份，重启/迁移不依赖它们。
- Web 面板与交易逻辑同进程同事件循环，用共享内存字典（`web/server.py` 的 `_state`）传递状态，无需额外消息队列。

---

## 3. 外部接口清单

### 3.1 Polymarket Gamma API（市场发现）
- **Host**: `https://gamma-api.polymarket.com`
- **GET `/markets?slug={slug}`** — 按 slug 查询市场。
  - 5分钟市场 slug 规则：`btc-updown-5m-{window_start_unix}`，`window_start_unix` 是 5 分钟窗口起点的 Unix 秒（一定是 300 的整数倍）。`endDate = window_start + 300`。
  - 响应字段：`closed`, `active`, `volume`, `clobTokenIds`（JSON 字符串数组，`["Up的tokenId","Down的tokenId"]`）, `conditionId`（market_id）, `question`。
  - 扫描策略：以当前 5 分钟边界为基准，检查 `[-1, 0, +1, +2]` 共4个窗口，捕获刚进入/即将进入可交易窗口的市场（见 `feeds/polymarket_clob.py:_fetch_active_markets`）。
  - 轮询间隔：`SCANNER_POLL_INTERVAL=15s`。

### 3.2 Polymarket CLOB REST API（订单簿+下单）
- **Host**: `https://clob.polymarket.com`
- **GET `/book?token_id={token_id}`** — 订单簿快照。响应 `{bids:[{price,size}], asks:[...], tick_size, min_order_size, neg_risk}`。
  - 带重试：403（Cloudflare WAF）和 5xx 重试最多3次（0.4s/0.8s 指数退避），其他4xx不重试。
  - 深度截取到 `DEPTH_LEVELS_TO_CHECK`（=5）档。
- 下单/撤单/查单不是直连 REST，而是通过 **CLOB SDK**（见第4节）调用。
- **GET `/tick-size?token_id=...`**、**GET `/neg-risk?token_id=...`** — SDK 内部在下单前自动调用，用于价格精度和合约类型判断（日志里能看到这两条 HTTP 请求）。

### 3.3 Polymarket Data API（链上持仓对账，仅 live 模式）
- **Host**: `https://data-api.polymarket.com`
- **GET `/positions?user={wallet}`** — 查询钱包实际链上持仓，用于定期核对本地记录与链上是否一致（`execution/order_manager.py:reconcile_positions`，每 30 个主循环周期 ≈ 60s 跑一次）。

### 3.4 Polymarket User P&L API（仪表盘用，仅 live 模式）
- **Host**: `https://user-pnl-api.polymarket.com`
- **GET `/user-pnl?user_address={addr}&interval=all&fidelity=1h`** — Polymarket 官方账户累计盈亏曲线（和官网 Profit/Loss 组件数据源一致），用于 Web 面板的资金曲线，避免和本地统计口径产生偏差。约每 30 个循环刷新一次。

### 3.5 Polygon RPC（链上余额查询，仅 live 模式）
- **URL**: 环境变量 `POLYGON_RPC_URL`（默认 `https://polygon-bor.publicnode.com`）
- **POST** JSON-RPC `eth_call`，手工 ABI 编码 `balanceOf(address)`（selector `0x70a08231`）查询 `FUNDER_ADDRESS` 的 pUSD（`PUSD_CONTRACT_ADDRESS`，Polymarket 2026-04 CLOB V2 升级后的抵押代币，默认地址 `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`）余额。
  - 这是 live 模式下单前"余额是否足够"判断的权威来源，独立于 CLOB SDK 的 `get_balance_allowance`（后者作为 fallback）。

### 3.6 BTC 现货数据源（可切换，二选一）
| | Binance | OKX |
|---|---|---|
| WS（逐笔成交） | `wss://stream.binance.com:9443/ws/btcusdt@aggTrade` | `wss://ws.okx.com:8443/ws/v5/public`（需发送订阅帧 `{"op":"subscribe","args":[{"channel":"trades","instId":"BTC-USDT"}]}`） |
| REST K线 | `GET https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=180` | `GET https://www.okx.com/api/v5/market/candles?instId=BTC-USDT&bar=5m&limit=300` |
| 选择方式 | `BTC_PRICE_SOURCE=binance`（默认，可能因服务器所在地区返回 451 被墙） | `BTC_PRICE_SOURCE=okx` |

价格流维护一个内存滚动窗口（`_MAX_HISTORY_SECONDS=600`，即10分钟），供方向过滤器做动量计算；K线用于仪表盘蜡烛图，与 WS 现价共同拼出"当前进行中5分钟K线实时跳动"的效果。**改造15分钟版本时，K线周期需要从 `5m` 改成 `15m`，历史缓冲窗口也要相应放大**（见第8节）。

---

## 4. CLOB SDK 用法（`polymarket-client` / `AsyncSecureClient`）

```python
from polymarket import AsyncSecureClient

client = await AsyncSecureClient.create(
    private_key=PRIVATE_KEY,        # 唯一必需凭证
    wallet=FUNDER_ADDRESS or None,  # 代理钱包地址，留空则自动检测
)
```

**为什么不用 `py-clob-client-v2`**：该 SDK 存在未修复的上游 bug——L1 认证始终把 API key 绑定到签名者 EOA，而不管 `signature_type`/`funder` 参数，导致所有代理钱包/存款钱包账户的订单都被拒（`maker address not allowed`）。`polymarket-client` 是官方新统一 SDK，正确处理了 funder/wallet 解析。**新项目应直接用 `polymarket-client`，不要引入 `py-clob-client-v2`**。

调用点（均为 `async`）：

| 方法 | 用途 | 代码位置 |
|---|---|---|
| `client.place_limit_order(token_id, price, size, side)` | 下限价单（`side="BUY"`开仓 / `"SELL"`平仓） | `order_manager.py:_live_execute` / `_submit_exit` |
| `client.get_order(order_id)` | 轮询订单状态，返回 `.status`（小写字符串：`matched`/`live`/`delayed`/`unmatched`/`cancelled`）、`.size_matched`、`.price` | `order_manager.py:_wait_for_fill` / `_poll_pending_exits` |
| `client.cancel_order(order_id)` | 撤单 | 超时未成交 / 部分成交后撤剩余 |
| `client.get_balance_allowance(asset_type="COLLATERAL")` | 查询抵押代币余额/额度（USDC=6位小数） | 链上RPC查询失败时的 fallback |
| `client.get_last_trade_price(token_id)` | 结算时查最后成交价作为近似结算价（仅 live 模式，市场到期后） | `resolve_expired_positions` |
| `client.wallet` | 实际持仓的资金地址（代理钱包），不是 `client.signer`（EOA） | 链上对账用它查询 Data API |

**易错点（新项目要注意）**：
1. `status_val` 比较必须转小写——之前误用大写 `"MATCHED"` 导致每笔成交都被误判为超时进而撤单。
2. 价格必须按市场实际 `tick_size` 取整（`_round_to_tick`），不能盲目 `round(x, 4)`——不同市场 tick 不同，超出精度网格会被直接拒单。
3. 下单前判断 `neg_risk` 字段——negRisk 市场用不同的交易所合约，本项目从未针对它验证过实盘路径，直接拒绝而非假设可行。
4. 卖出（平仓）在 live 模式下不是"提交即视为已平"——要等 `_poll_pending_exits` 确认真正 `matched` 才 `record_close`，否则和交易所实际持仓会错位。

---

## 5. 交易决策链路（`main.py:evaluate_market`）

```
市场扫描(Gamma) → 拉双边订单簿(YES/NO) → 5个过滤器顺序短路判断 → 综合评分门槛
  → 分级仓位(LOW/MED/HIGH) → 动量确认延迟入场 → 反转哨兵最后一次核查 → 下单
```

### 5.1 五个过滤器（`filters/`）

| 过滤器 | 权重（综合分） | 核心逻辑 | 硬性跳过条件 |
|---|---|---|---|
| **Direction**（方向） | 45% | 45s/120s快慢均线交叉(65%) + 订单簿失衡(35%)，180s回看窗口；结果按已实现波动率自适应归一化到[-100,100]；20s滑动平均去噪 | `\|score\|<20` 视为NEUTRAL跳过；`consistency<30%`噪声跳过；`filter_score<10`弱信号跳过 |
| **Odds**（赔率） | 25% | 价格(=隐含概率)离0.5越近分越高；价格上限随方向强度动态调整(弱信号只能买便宜的) | `<0.20`(彩票) 或 `>0.72`(硬顶) 跳过 |
| **Timing**（时机） | 30% | 市场生命周期内的窗口打分，中点分最高 | 已过时间`<45s`或剩余`<90s`或已过`>210s` 跳过 |
| **Spread**（价差） | 0%（仅硬门禁） | 价差+深度打分，但权重设0避免"近乎常数拉高综合分" | 价差`>0.04` 或 最佳卖单深度`<30股` 或 死盘 跳过；`score<65`额外软跳过 |
| **Chase**（追单空间） | 0%（仅硬门禁） | 追单预算内(`+0.03`)可用量是否够 | 可用量`<30股` 跳过 |

综合分 = `Σ weight[i] * score[i]`（只累加权重>0的项），门槛 `MIN_COMBINED_SCORE=45`。

### 5.2 仓位分级（main.py 内联逻辑，非独立filter）

```
combined≥60 且 direction_filter_score≥40 且 consistency≥80%  → HIGH  (100% 仓位)
consistency≥50% 或 direction_filter_score≥25(strong_signal)  → MED   (50% 仓位)
其余（已通过硬性门槛的）                                        → LOW   (25% 仓位)
```

### 5.3 动量确认延迟入场（main.py:_btc_entry_ready + evaluate_market，"Strategy 1"）

过滤器通过后**不立即下单**，而是挂起等待，每 ~2s 复查一次，满足以下任一条件才真正下单：
- **条件A（走平）**：近20s BTC变动 `< ENTRY_BTC_MAX_VELOCITY`
- **条件B（衰减）**：近20s变化率 `< ENTRY_BTC_DECEL_RATIO(40%) ×` 近60s变化率
- **硬顶**：等待超过 `ENTRY_MAX_WAIT_SECONDS(60s)` 强制入场

> ⚠️ **已知设计缺陷（本项目实测踩过的坑，新项目要注意）**：`ENTRY_BTC_MAX_VELOCITY` 原值0.1%过于宽松，导致条件A几乎总在第一次2s检查就通过，"等待衰减"机制形同虚设，历史数据显示77次入场100%都是等2秒就确认。修复方式：收紧阈值 + 要求连续多次(`ENTRY_FLAT_CONFIRM_TICKS`)检查都满足才算确认，而不是单次达标就放行。**移植到15分钟市场时，这套阈值需要重新用实际数据校准**，不能直接照抄5分钟版本的数值（15分钟窗口内BTC自然波动幅度会更大）。

并行还有一个**逆向取消**保护：等待期间若代币价格已经比信号时下跌超过 `ENTRY_CONFIRM_MAX_ADVERSE_PCT(1.5%)`，立即放弃这次信号。

### 5.4 反转哨兵（direction.py:check_reversal）

下单前最后一次检查：近 `DIRECTION_REVERSAL_GUARD_WINDOW(30s)` 内价格是否已经反向移动超过1个短周期波动率——是则放弃本次入场。

---

## 6. 执行与风控

### 6.1 `execution/risk.py`（RiskState，进程内单例）
- 同时最多 `MAX_CONCURRENT_POSITIONS(2)` 个仓位（当前代码逐笔顺序，实质更接近1个）。
- 日亏损硬顶 `MAX_DAILY_LOSS_USD`，按 UTC 日切换自动重置。
- **熔断**：连续亏损达到 `CIRCUIT_BREAKER_CONSECUTIVE_LOSSES(3)` 笔，暂停新入场 `CIRCUIT_BREAKER_PAUSE_SECONDS(300s)`。
- paper模式虚拟余额记账（`balance -= usd_size` 开仓 / `balance += usd_size+pnl` 平仓），持久化到 MySQL `account_state` 表，重启不丢失。

### 6.2 `execution/order_manager.py`
- **paper**：直接按 `entry_price`(best_ask，无额外滑点) 模拟成交。
- **live**：
  1. 查真实钱包余额（链上RPC优先，SDK余额接口 fallback）确认够用；
  2. 限价 = `best_ask + ORDER_PRICE_SLIPPAGE(0.02)`，按市场 tick_size 取整；
  3. 下单后轮询 `ORDER_FILL_TIMEOUT_SECONDS(8s)`，超时/未满仓撤单，按实际成交量建仓（绝不用请求量记账）；
  4. 平仓（止损/止盈/强制离场）用同样的"提交卖单→轮询确认成交→才真正平仓"流程（`_pending_exits`），避免和交易所真实持仓错位；
  5. 定期链上对账（`reconcile_positions`，比对 Data API 持仓 vs 本地记录，发现不一致只报警不自动纠正）。
- 止损/止盈判断用订单簿 `best_bid`（不是mid价），触发价和实际卖出价用同一个数字，避免"用mid触发、按best_bid成交"造成的滑点缺口。
- 到期强制离场：`FORCE_EXIT_BEFORE_EXPIRY(30s)` 前不管盈亏直接平仓，防止裸持到结算。
- 结算（`resolve_expired_positions`）：paper模式按市场自己的开盘/收盘窗口（`market_end_ts-300` ~ `market_end_ts`）比较BTC涨跌自行判定胜负；live模式查CLOB最后成交价近似结算价。

---

## 7. 数据库结构（MySQL, `storage/schema.sql`）

| 表 | 用途 | 关键字段 |
|---|---|---|
| `positions` | 唯一权威交易记录 | `order_id`(唯一,VARCHAR(80)—早期用VARCHAR(64)导致真实订单ID(0x+64hex=66字符)插入失败，新项目直接用80) / `side` ENUM('YES','NO') / `mode` ENUM('paper','live') / `status` ENUM('OPEN','CLOSED') / `filter_scores` JSON / `pnl`和`result`是**SQL生成列**(STORED)，不在Python里手算 |
| `logs` | Web面板"Live Log"持久化，重启后仪表盘历史不丢 | `ts`, `line` |
| `strategy_links` | 仪表盘导航栏，指向其它同时运行的策略实例 | 手工维护，无管理界面 |
| `account_state` | paper模式虚拟账户余额（单行 id=1） | `balance` |

连接方式：`PooledDB`(dbutils) 包 `pymysql`，`maxconnections=8`，同步调用，异步层统一用 `loop.run_in_executor(None, fn)` 包裹避免阻塞事件循环。

---

## 8. Web 仪表盘

### 8.1 后端路由（`web/server.py`，aiohttp）

| 路由 | 方法 | 说明 |
|---|---|---|
| `/` | GET | 返回 `web/static/index.html` |
| `/ws` | WS | 连接后先推一次全量快照，之后主循环每 ~2s `broadcast()` 一次全量状态（无增量协议） |
| `/api/state` | GET | 一次性获取当前完整状态（等价于WS快照，供无WS环境用） |
| `/api/klines` | GET | K线数组（低频，前端每60s拉一次，不走WS省流量） |
| `/api/price` | GET | 仅最新现价（前端700ms轮询，用于蜡烛图丝滑过渡，WS的2s粒度太粗） |
| `/api/optimizations` | GET | 读取 `docs/optimizations/*.md` 供前端弹窗展示 |
| `/api/strategy_links` | GET | 查 `strategy_links` 表 |
| `/api/trading/toggle` | POST | 密码校验(`TRADING_CONTROL_PASSWORD`，`hmac.compare_digest`防时序攻击)后翻转暂停开关，只影响新开仓，不影响已有仓位管理 |

WS推送的状态JSON核心字段：`btc_price/btc_connected/btc_history/btc_target/paper_trading/trading_paused/account_balance/today{}/all_time{}/positions[]/pnl_series[]/history[]/logs[]/uptime`。

### 8.2 前端

单文件 `web/static/index.html`，无构建步骤，CDN引入：
- `lightweight-charts@4.1.0` — BTC蜡烛图 + P&L曲线图
- `marked@12` — 渲染优化记录的Markdown

移动端做了断点适配（`@media max-width:768px`），近期还加了：P&L图表悬浮/触摸的"翻牌器"数字滚动效果、切换1D/1W/1M等区间时的"幕布左滑揭示"过渡动画。这部分是纯展示逻辑，与交易核心无关，迁移新项目时可以直接复用整份 `index.html`，只需要把文案里"5分钟"相关的地方（如倒计时逻辑、K线周期标签）改成15分钟。

---

## 9. 部署

- **Dockerfile**：`python:3.11-slim`，装 `gcc`+`tzdata`，时区写死 `Asia/Shanghai`，工作目录 `/data`（运行期产物），代码在 `/app`（`PYTHONPATH=/app`），入口 `python -u /app/main.py`。
- **docker-compose.yml**：两个服务
  - `mysql`（8.4.10，数据卷 `/home/mysql-data`）
  - `bot`（build本地Dockerfile，暴露 `8080`，依赖mysql健康检查通过才启动）
  - 两者共享外部网络 `1panel-network`（宿主机上已存在的Docker网络，不是compose自建的）。
- **环境变量**（`.env`，完整清单见 `.env.example`）：
  - 凭证：`PRIVATE_KEY`（必需）、`FUNDER_ADDRESS`（代理钱包时需要）
  - 模式：`PAPER_TRADING`、`TRADE_SIZE_USD`、`MAX_DAILY_LOSS_USD`、`ACCOUNT_BALANCE_USD`
  - 开关：`STOP_LOSS_ENABLED`、`TAKE_PROFIT_ENABLED`、`PROXY_ENABLED`、`MIN_TIMING_FILTER_SCORE_ENABLED`
  - 数据源：`BTC_PRICE_SOURCE`
  - MySQL：`MYSQL_HOST/PORT/USER/PASSWORD/DATABASE`(+`MYSQL_ROOT_PASSWORD`供mysql容器自身用)
  - 面板：`TRADING_CONTROL_PASSWORD`、`TOTAL_DEPOSITS_USD`、`WEB_PORT`(默认8080，代码里`os.getenv`读取，`.env.example`未列出但存在)

---

## 10. 移植到「15分钟市场」需要改动的具体位置

这是本文档专门为你下一步任务准备的清单，按影响面从大到小排列：

1. **市场发现的 slug 规则与窗口大小**（`feeds/polymarket_clob.py:_fetch_active_markets`）
   - slug 从 `btc-updown-5m-{window_start}` 改成对应15分钟市场的实际 slug 格式（需要先在 Polymarket 上确认真实命名规则，5m/15m可能不是简单替换数字）。
   - `window_ts = int(now // 300) * 300` 的 `300` → `900`（15分钟=900秒）。
   - `end_ts = window_ts + 300` → `+900`。
   - 候选窗口 `(-1,0,1,2)` 的扫描范围可能也要按比例调整。

2. **`config.py` 里所有跟"300秒/5分钟"绑定的常量**（`MARKET_DURATION_SECONDS=300` 是核心，其余多个 timing/entry 参数是它的派生比例，不能等比例硬套，需要用15分钟市场的真实行情重新统计校准）：
   - `MARKET_DURATION_SECONDS`
   - `TIMING_MIN_ELAPSED/MAX_ELAPSED/MIN_REMAINING/MAX_REMAINING`（入场时间窗口）
   - `ENTRY_MAX_WAIT_SECONDS`、`ENTRY_CONFIRM_MAX_ADVERSE_PCT`（15分钟窗口给了更长决策时间，等待策略可以更从容）
   - `DIRECTION_LOOKBACK_SECONDS/FAST_WINDOW/SLOW_WINDOW/SMOOTH_WINDOW`（动量回看窗口，15分钟市场的"趋势"尺度和5分钟不同，均线窗口大概率要放大）
   - `FORCE_EXIT_BEFORE_EXPIRY`

3. **BTC K线周期**（`feeds/btc_price.py`）
   - Binance `interval=5m` → `interval=15m`；OKX `bar=5m` → `bar=15m`。
   - `_MAX_HISTORY_SECONDS=600`（10分钟价格缓冲）需要放大到覆盖新的 `DIRECTION_LOOKBACK_SECONDS`。

4. **仪表盘前端里硬编码的"5分钟/5MIN"文案与倒计时逻辑**（`web/static/index.html`）
   - 标题 `POLYMARKET BTC 5MIN`、图表标题 `BTC/USDT · 5minute`。
   - 倒计时 `_updateBtcCountdownText()` 里的 `300`（5分钟窗口秒数）→ `900`。
   - K线时间桶 `Math.floor(Date.now()/1000/300)*300` 等多处 `300` 常量。

5. **数据库**：`schema.sql` 结构本身不需要改（表结构与周期无关），但如果要和现有5分钟数据区分，建议用独立的 `MYSQL_DATABASE`（比如 `polymarket_bot_15m`），而不是共用同一个库。

6. **止损/止盈/仓位分级等策略参数**：这些是基于5分钟市场历史数据统计校准出来的（参见 `docs/optimizations/` 下的历次复盘记录），不能直接套到15分钟市场——15分钟窗口内的价格波动幅度、噪声特性都不同，需要先跑一段paper模式积累数据后重新校准，不要用5分钟版本调好的数字直接上15分钟实盘。

7. **CLOB SDK / 下单执行逻辑（`execution/order_manager.py`）不需要改**——下单、轮询、对账这套机制与市场周期无关，可以整份复用。风控（`execution/risk.py`）同理。
