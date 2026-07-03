# 008 — 提高交易频率：噪声门槛收窄 + 三档仓位 + 入场前反转哨兵

- **日期**：2026-07-03
- **状态**：✅ 已实现，待 paper 盘验证
- **涉及文件**：`config.py`、`filters/direction.py`、`main.py`
- **前置**：[007](007-2026-07-02-balanced-entry-consistency-macro-sizing.md) 引入的方向一致性 + CEX 宏观趋势 + 分级仓位；宏观趋势层已应用户要求于本次之前完全移除（5 分钟 K 线策略不需要大盘确认）

---

## 背景：7 笔/天，近一半亏损，且分数越高不代表越会赢

诊断出两个根本问题，而不是"阈值调错了"：

1. **频率瓶颈藏在看不见的地方。** 过去 7.5 小时 625 条日志里能看到的方向层拒绝原因只有 `consistency<50% → 62次`、`score<min_dir_score → 71次`、`SIGNAL 触发 → 7次`。但方向层最早的一道关卡——NEUTRAL 判定（`|raw|<20` 直接跳过）——用的是 `log.debug`，在 `LOG_LEVEL=INFO` 下从未被记录（见 `utils/logger.py`：根 logger 的 level 在所有 handler 之前就过滤掉了 DEBUG 消息）。也就是说，一直在对着一个看不见规模的漏斗调参数。

2. **方向分数和输赢没有正相关，甚至反着来。** 抽样最近 10 笔仓位：combined_score 最高的一笔(69.05分)和 direction 分最高的两笔(46.7/46.9分)全部是亏损，且都在入场后 18~64 秒内反转。继续抬高阈值只会削减交易量，不会带来对应的胜率收益。

## 问题拆解：现有门槛互相打架在哪

`main.py:evaluate_market()` 此前对方向信号做了 **4 层独立判断**，全部在回答同一个问题"方向信号够不够好"：

1. `direction.check()` 内部：`|raw|<20` → NEUTRAL，硬跳过（不可见）
2. `strong_signal(filter_score≥25)` 绕过下面两条（唯一的软化机制）
3. 非强信号时：`consistency<50%` → 硬跳过
4. 非强信号时：`dir_result.score < min_dir_score`（0 或 12，取决于一致性档位）→ 硬跳过
5. 事后 `combined_score<55` 第 4 次硬跳过

第 3/4/5 步本质上是对同一件事的重复检验，制造了悬崖效应而非提供额外信息量：一致性52%+方向分13分能通过，一致性49%+方向分90分却被砍。

---

## 方案：三点改动

### 1. 跳过 → 缩小仓位：新增 LOW 档位，缩小真正的硬跳过区

只在**真正是噪声**时才硬跳过（`consistency < DIRECTION_CONSISTENCY_FLOOR(0.30)`），其余全部放行，只是仓位不同：

| 档位 | 条件 | 仓位 |
|------|------|------|
| **HIGH** | combined≥60 且 dir_result.score≥20 且 consistency≥80% | 100% `TRADE_SIZE_USD` |
| **MED** | consistency≥50% 或 strong_signal(dir_result.score≥25) | 50% |
| **LOW**（新增） | 其余所有非噪声情况（已通过 combined_score≥45 卫生线） | 25%（`TRADE_SIZE_LOW_TIER_RATIO`） |

`DIRECTION_CONSISTENCY_LOW(0.50)` 从"硬跳过线"变成"MED档位线"，语义不变，只是不再直接淘汰。`MIN_COMBINED_SCORE` 从 55 降到 45——它现在只需充当"赔率+方向+时机三者别一起烂"的卫生线，不再重复方向层已经做过的把关（那部分交给分档处理）。

**效果**：以前 consistency 49%、方向分 13 这种"差一点点"的信号会直接被砍掉，现在以 25% 仓位成交——频率上升，但单笔风险敞口只有满仓的 1/4，不会让整体盈亏结构恶化。

### 2. 新增入场前反转哨兵（`filters/direction.py::check_reversal`）

止损已关闭（`STOP_LOSS_ENABLED=false`），意味着一旦入场方向错了会直接扛到到期结算（0 或 1），而不是像以前止损减仓到 -20%~-30%。历史亏损样本显示，方向分 46+ 的"强信号"仍会在 18~64 秒内反转——现在这种"信号算完到下单之间行情已变"的情况，代价从"小亏"变成"全损"。

新增的检查在真正调用 `order_manager.execute()` 之前触发，用比一致性窗口（60秒）短得多的近端窗口（`DIRECTION_REVERSAL_GUARD_WINDOW=12秒`）看最新几笔 BTC 价格变动：若这 12 秒内价格已朝交易方向反方向移动超过 1 个短程 sigma（复用已有的 `_realised_vol_pct()` 做自适应噪声基准，而非固定百分比——固定阈值在不同波动率环境下会一会儿太松一会儿太紧），则放弃这次入场（不降档，直接跳过，2 秒后下一轮重新评估）。

这不是重复 consistency 检查（一致性看过去 60 秒的历史平均），而是"下单前最后一次确认信号是否还站得住脚"的新鲜度检查。

### 3. 漏斗可见性（`filters/direction.py::get_and_reset_skip_stats`）

新增进程内计数器 `_skip_stats`（neutral / noise_skip / low / med / high / reversal_guard），`main_loop()` 每 5 分钟汇总打印一条 INFO 级摘要日志（不是每次都记，避免刷屏）。跑一段时间后即可看到 NEUTRAL 到底占多大比例——如果它确实是最大瓶颈，下一轮再基于真实数据决定是否下调 `DIRECTION_MIN_SCORE(20)`，而不是再猜一次。

---

## 改动清单

### `config.py`

| 改动 | 内容 |
|------|------|
| 新增参数 | `DIRECTION_CONSISTENCY_FLOOR=0.30`、`TRADE_SIZE_LOW_TIER_RATIO=0.25`、`DIRECTION_REVERSAL_GUARD_WINDOW=12.0` |
| 修改 | `MIN_COMBINED_SCORE` 55→45 |
| 语义变化（值不变） | `DIRECTION_CONSISTENCY_LOW(0.50)`：硬跳过线 → MED 档位线 |

### `filters/direction.py`

| 改动 | 内容 |
|------|------|
| 新增 `check_reversal(side, now)` | 12 秒短窗口反转检测，复用 `_realised_vol_pct()` 做自适应阈值 |
| 新增 `_skip_stats` + `record_skip()` + `get_and_reset_skip_stats()` | 方向漏斗可见性计数器 |
| `check()` 内 NEUTRAL 分支 | 新增 `record_skip("neutral")` |

### `main.py`

| 改动 | 内容 |
|------|------|
| 移除旧的 4 层级联判断 | 替换为"仅噪声硬跳过 + 三档定仓位" |
| 新增噪声跳过 | `consistency < DIRECTION_CONSISTENCY_FLOOR` → 跳过（唯一剩下的硬性方向否决） |
| 新增 LOW 档位 | 25% 仓位，覆盖之前被硬跳过的边缘信号 |
| 新增反转哨兵调用 | 在 `combined_score` 通过、下单前插入 `direction.check_reversal()` |
| 新增周期性统计日志 | `main_loop()` 每 5 分钟打印一次方向漏斗摘要 |

---

## 验证

1. `python -m py_compile config.py main.py filters/direction.py` ✅ 通过
2. 纸面模式运行，观察日志：
   - 每 5 分钟一条"方向漏斗"摘要，确认 NEUTRAL 占比、噪声跳过占比、LOW/MED/HIGH 成交占比
   - SIGNAL 数量是否明显高于之前的 7 笔/天
   - 偶尔出现"反转哨兵拦截"日志
3. 运行数小时到数天后对比：
   - 交易频率是否提升
   - LOW 档位胜率 vs MED/HIGH 档位胜率（积累足够样本后）
   - 单笔最大亏损是否因 LOW 档位 25% 仓位而降低

## 风险与注意事项

1. **LOW 档位样本目前为 0**，其真实胜率未知——这正是本次改动的目的：先产生样本，而非提前假设它会盈利或亏损。
2. **反转哨兵的短窗口（12秒）在启动初期可能样本不足**（`get_prices_since()` 返回 < 3 个点时直接放行，不拦截），属于合理的预热行为。
3. **`MIN_COMBINED_SCORE` 55→45 是本次改动里唯一直接放宽的硬阈值**，如果观察到 LOW/MED 档位胜率显著低于 HIGH，应优先复查是否是这一步放得太松，而不是急于再调方向层参数。
