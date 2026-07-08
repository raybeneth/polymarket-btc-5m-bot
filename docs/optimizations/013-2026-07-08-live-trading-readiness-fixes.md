# 优化 013 — 实盘交易就绪修复：余额/成交确认/对账/小额仓位试跑

**日期**: 2026-07-08
**触发**: 计划将 `PAPER_TRADING` 切换为 `false` 开始真实下单前的功能自查，发现实盘执行路径 (`execution/order_manager.py`) 存在多处从未被模拟盘验证过的缺陷。

---

## 根因分析

`_live_execute` / `check_early_exits` / `resolve_expired_positions` 中的实盘分支自项目初期写完后从未真正跑过实盘订单，存在以下问题：

1. **下单价格未按市场真实 tick size 取整**：不同市场的最小价格增量不同（如 `0.01` / `0.001`），原代码统一 `round(price, 4)`，实盘会被交易所拒单。
2. **未做 negRisk 市场识别**：negRisk 市场走不同的交易所合约，实盘下单路径从未针对该类型验证过。
3. **成交状态判断大小写错误**：`_wait_for_fill` 用 `status.get("status") == "MATCHED"`（大写）比较，而 CLOB 接口真实返回的是小写（`matched`/`live`/`delayed`/`unmatched`/`cancelled`），导致成交检测**恒为假**——每笔实盘订单无论是否真的成交，都会被当作超时撤单处理。
4. **不支持部分成交**：`_wait_for_fill` 只返回"成交价或 None"，未读取 `size_matched`，部分成交会被当成完全未成交或完全成交，仓位记录的份额与实际持仓不符。
5. **下单前风控用的是模拟余额**：`_live_execute` 走的是 `state.can_trade(actual_size)`，检查的是 paper 模式的虚拟账本 `state.balance`，跟钱包里真实的 USDC 余额完全无关。
6. **卖出平仓"先斩后奏"**：止损/止盈/到期强平在实盘下单提交 SELL 单后立即调用 `record_close` + `db.close_position`，不管这笔卖单是否真的成交——如果卖单没成交（滑点、流动性不足等），本地已经认为仓位平了，但链上仍持有真实份额，造成账本与真实持仓不同步且无法察觉。
7. **没有任何对账机制**：本地 DB/state 与 Polymarket 链上真实持仓之间从无交叉校验，一旦出现 5/6 这类问题或程序崩溃在下单和落库之间，desync 会一直无声存在。

---

## 修改内容

### 1. Tick size / 最小下单量 / negRisk 元数据接入真实数据

Polymarket CLOB 的 `/book` 接口本身就返回 `tick_size`、`min_order_size`、`neg_risk`，无需额外请求：

- `feeds/polymarket_clob.py`：`OrderBook` 新增 `tick_size`、`min_order_size`、`neg_risk` 字段，`fetch_order_book` 从 `/book` 响应中解析并填充。
- `config.py`：`TradeDecision` 新增同名字段，作为过滤流水线到执行层的元数据载体。
- `main.py`：两处 `TradeDecision` 构造点（信号生成 + pending 信号确认前刷新）都带上这些字段。

### 2. `_round_to_tick` 辅助函数

`execution/order_manager.py` 新增 `_round_to_tick(price, tick_size)`：按市场真实 tick size 取整并夹在 `[tick, 1-tick]` 区间内，替代原来不区分市场的 `round(x, 4)`。

### 3. `_live_execute` 重写

- negRisk 市场直接跳过（`decision.neg_risk` 为真时拒绝下单，安全兜底而非假设可用）。
- 风控改为 `state.can_trade(0)`（跳过内部模拟余额检查，仍保留并发仓位数/日亏损上限/熔断闸门），随后单独调用 `get_wallet_balance_usdc()` 查询真实 USDC 余额并与所需金额比较，余额不足或查询失败一律跳过下单。
- 限价用 `_round_to_tick(entry_price + slippage, decision.tick_size)` 计算。
- 下单量低于 `decision.min_order_size` 时跳过。
- 用重写后的 `_wait_for_fill` 返回的**实际成交价与实际成交份额**建仓，而不是用下单时的请求价/请求量——部分成交只按实际成交部分建仓，剩余部分撤单。

### 4. `_wait_for_fill` 重写

- 状态比较统一转小写（`str(status.get("status","")).lower()`），修复大小写导致成交检测恒假的问题。
- 读取 `size_matched` 支持部分成交，返回 `(fill_price, filled_size)` 而不是 `Optional[float]`。
- 未成交/取消提前返回；超时仍在 book 上则返回已匹配部分，调用方负责撤销剩余。

### 5. 实盘平仓改为"提交卖单 → 轮询确认成交 → 才落库平仓"

新增模块级 `_pending_exits` 字典跟踪在途卖单：

- `_submit_exit()`：提交 SELL 单后只记录到 `_pending_exits`，不立即调用 `record_close`。
- `_poll_pending_exits()`：每轮 `check_early_exits` 开头轮询在途卖单状态，确认 `matched` 才 `record_close` + `db.close_position`；`unmatched`/`cancelled` 则放弃本次尝试，仓位保留、下轮重新判断是否需要平仓；长时间挂单（> 3× 下单超时）主动撤单重试。
- `check_early_exits` 主循环里，已有在途卖单的仓位这一轮跳过重复触发。

### 6. 新增 `reconcile_positions()` 对账

用 Polymarket 公开、无需鉴权的 Data API（`https://data-api.polymarket.com/positions?user=<wallet>`）查询钱包真实链上持仓，与本地 `state.open_positions` 比对：

- 链上有但本地无记录 → 告警（可能是漏记的仓位）。
- 本地有但链上无 → 告警（可能已被平仓但本地未同步，或订单从未真正成交）。
- 两边都有但份额不一致 → 告警。

`main.py` 中在启动时（session 建立后、进入主循环前）调用一次，并在主循环中每 30 轮（约 60 秒）调用一次，仅实盘模式生效。

### 7. 小额实盘试跑参数

`.env` 中调整（首次实盘只充值 $10，单笔仓位上限 $1.5）：

```
TRADE_SIZE_USD=1.5          # 原 3.0，HIGH档满仓金额
ACCOUNT_BALANCE_USD=10.0    # 新增，仅用于仪表盘展示基准，不影响下单逻辑
MAX_DAILY_LOSS_USD=5.0      # 新增，此前硬编码 10000 且不支持环境变量覆盖
```

`config.py` 同步把 `MAX_DAILY_LOSS_USD` 改为可通过环境变量覆盖（此前是硬编码常量，跟同级的其他风控阈值不一致）。

**确认**：实盘模式下账户余额展示（仪表盘 `account_balance` 字段）与下单前的余额检查，用的都是 `get_wallet_balance_usdc()` 查询到的真实 USDC 余额，`ACCOUNT_BALANCE_USD` 只是给内部模拟账本 `state.balance` 做初始种子值，在实盘模式下既不参与风控判断也不落库展示。

---

## 改动汇总

| 文件 | 改动 |
|------|------|
| `feeds/polymarket_clob.py` | `OrderBook` 新增 `tick_size`/`min_order_size`/`neg_risk`，`fetch_order_book` 解析填充 |
| `config.py` | `TradeDecision` 新增同名三字段；`MAX_DAILY_LOSS_USD` 改为环境变量可覆盖 |
| `main.py` | `TradeDecision` 构造点补充元数据；启动时 + 每30轮调用 `reconcile_positions()` |
| `execution/order_manager.py` | 新增 `_round_to_tick`；重写 `_live_execute`（negRisk跳过/真实余额检查/tick取整/最小下单量校验/按实际成交量建仓）；重写 `_wait_for_fill`（小写状态匹配、部分成交支持）；新增 `_pending_exits`/`_submit_exit`/`_poll_pending_exits`（卖单成交确认后才平仓）；新增 `reconcile_positions()`（链上对账） |
| `.env` | `TRADE_SIZE_USD=1.5`，新增 `ACCOUNT_BALANCE_USD=10.0`、`MAX_DAILY_LOSS_USD=5.0` |

---

## 仍待用户手动处理

- **CLOB/钱包授权（allowance）**：需在 Polymarket 网站手动完成，`get_credentials.py` 不包含此步骤。
- **`docker-compose.yml` 明文密钥**：`PRIVATE_KEY`/`CLOB_API_KEY`/`CLOB_API_SECRET`/`CLOB_API_PASSPHRASE` 硬编码在已提交并推送到 GitHub 的文件中，且其 `environment:` 会覆盖 `.env` 里刚调整的 `TRADE_SIZE_USD`/`ACCOUNT_BALANCE_USD`/`MAX_DAILY_LOSS_USD`/`PAPER_TRADING`。该问题已提出，尚未处理，需要用户确认后再动（涉及可能的密钥吊销/git 历史清理，属破坏性操作）。
- **切换实盘开关**：`.env` 中 `PAPER_TRADING` 目前仍为 `true`，需用户在完成授权与确认风险后手动改为 `false`。

> ⚠️ 以上修复均基于对 `py_clob_client` 源码和 Polymarket 公开 REST 接口行为的静态分析，尚未在真实小额资金下实测验证，首次实盘务必用 $10/$1.5 的最小规模观察至少若干笔完整开平仓后再考虑加大仓位。
