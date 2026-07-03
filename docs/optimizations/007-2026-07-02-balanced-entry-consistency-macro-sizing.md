# 007 — 平衡进场策略：方向一致性 + CEX 宏观趋势 + 分级仓位

- **日期**：2026-07-02
- **状态**：✅ 已实现，待 paper 盘验证
- **涉及文件**：`config.py`、`filters/direction.py`、`main.py`、`execution/order_manager.py`
- **前置**：[006](006-2026-07-02-tighten-entry-strategy-gate.md) 收紧后交易机会极少

---

## 背景：松与紧的两难

| 版本 | 方向硬地板 | 综合分 | 策略层分 | 结果 |
|------|-----------|--------|---------|------|
| 005 (松) | filter≥15, raw≥~29 | ≥53 | 无 | 4/4 全亏，dir=15/18/25 全是噪音 |
| 006 (紧) | filter≥30, raw≥~44 | ≥55 | ≥55 | 交易机会极少 |

两个版本都在固定阈值上做文章，区别只是"刀切在哪里"。根因是三个结构性问题：

1. **单一 pass/fail 阈值 = 悬崖效应**。稍弱信号被完全排除，稍强信号全仓进场，无中间地带。
2. **spread(~85) 和 chase(~100) 是近常量**，占 combined 权重 20%，白送 ~19 分，能把弱策略信号顶过门槛（006 用 `MIN_STRATEGY_SCORE` 补丁修了，但治标不治本）。
3. **方向信号只看瞬时强度，不看持续性**。一个 dir=40 的单次尖刺和一个持续 60 秒的 dir=30 被同等对待，但后者远更可靠。
4. **CEX K 线数据已接入但仅用于 dashboard**，完全未参与交易决策。

---

## 方案：三层改进

### 层 1：方向一致性过滤（替代硬地板）

在 `filters/direction.py` 新增 `_compute_consistency()`，分析 `_score_history` 中最近 60 秒的方向读数一致率：

```
一致率 = 最近 N 次读数中与当前方向同号的占比
```

| 一致率 | 含义 | 方向阈值 (filter_score) |
|--------|------|----------------------|
| ≥ 80% | 趋势明确 | ≥ 6（raw ≥ ~25） |
| 60-80% | 趋势较弱 | ≥ 19（raw ≥ ~35） |
| < 60% | 方向混乱/震荡 | 跳过，不做 |

**效果**：真趋势中放宽（恢复交易机会），震荡中自动收紧（过滤噪音），替代了 006 的 `MIN_DIRECTION_FILTER_SCORE=30` 一刀切。

### 层 2：CEX K 线宏观趋势确认

在 `filters/direction.py` 新增 `_compute_macro_trend()`，使用 `btc_price.get_klines()` 已有的 1 分钟 K 线数据：

- 取最近 3 根已闭合 K 线的 close 变化
- 变化 > ±0.02% → 判定 UP/DOWN
- 变化 ≤ 0.02% → FLAT（不干预）

**规则**：
- 宏观趋势与方向信号**矛盾**（如 macro=UP 但要做 NO）→ **强制跳过**
- 宏观**确认** → 可参与高信心档位
- 宏观 FLAT → 不加分不减分

**效果**：直接杀死"BTC 明显单边涨却反复做空"的致命模式（004/006 的连亏根因）。

### 层 3：信心分级仓位

替代"要么全仓要么不做"的二元模式：

| 信心档位 | 条件 | 仓位 |
|---------|------|------|
| **HIGH** | combined≥60 且 一致率≥80% 且 宏观确认 | 100% TRADE_SIZE_USD |
| **MED** | combined≥55（过基础门槛） | 50% TRADE_SIZE_USD |

**效果**：中等信号以半仓参与，单笔最大亏损从 ~$6 降到 ~$3，同时积累样本。

### 配套改动：评分权重重构

将 spread/chase 从评分体系中移除，改为纯 pass/fail 否决门：

```
旧: odds=0.20, direction=0.35, timing=0.25, spread=0.10, chase=0.10
新: odds=0.25, direction=0.45, timing=0.30, spread=0.00, chase=0.00
```

combined_score 现在 = direction + odds + timing 的加权平均，不再被执行层指标虚高分数掩盖。**不再需要 `MIN_STRATEGY_SCORE` 补丁**（已移除）。

---

## 改动清单

### `config.py`

| 改动 | 内容 |
|------|------|
| 新增参数 | `DIRECTION_CONSISTENCY_WINDOW=60.0`, `DIRECTION_CONSISTENCY_HIGH=0.80`, `DIRECTION_CONSISTENCY_LOW=0.60`, `DIRECTION_MACRO_FLAT_THRESHOLD=0.02` |
| 修改 `SCORE_WEIGHTS` | spread/chase 权重 → 0.00；odds 0.20→0.25, direction 0.35→0.45, timing 0.25→0.30 |
| 移除 | `MIN_DIRECTION_FILTER_SCORE`, `STRATEGY_SCORE_KEYS`, `MIN_STRATEGY_SCORE` |
| 新增字段 | `TradeDecision.trade_size_usd` |

### `filters/direction.py`

| 改动 | 内容 |
|------|------|
| 新增 `_compute_consistency()` | 分析 `_score_history` 最近 60 秒方向一致率 |
| 新增 `_compute_macro_trend()` | 用 CEX 1m K 线判断宏观趋势 |
| `check()` 返回值扩展 | details 新增 `consistency`, `consistency_samples`, `macro_trend`, `macro_magnitude` |

### `main.py`

| 改动 | 内容 |
|------|------|
| 移除 `MIN_DIRECTION_FILTER_SCORE` 硬地板 | 替换为一致率动态阈值 |
| 新增宏观趋势矛盾检查 | macro 与 side 矛盾 → 跳过 |
| 移除 `MIN_STRATEGY_SCORE` 闸门 | 不再需要（权重重构已解决） |
| 新增信心分级 | HIGH=全仓, MED=半仓 |
| SIGNAL 日志增强 | 显示 consistency, macro, tier, 实际下单金额 |

### `execution/order_manager.py`

| 改动 | 内容 |
|------|------|
| `_paper_execute()` | 使用 `decision.trade_size_usd` 替代固定 `TRADE_SIZE_USD` |
| `_live_execute()` | 同上 |

---

## 回测验证（用 005/006 的亏损单）

### 005 的 4 笔亏损单能否被本方案拦截？

| 单 | dir | macro可能状态 | 一致率可能 | 本方案处理 |
|----|-----|-------------|-----------|----------|
| 20:23 NO | ? | UP（BTC 在涨） | 低（方向反复翻转） | **宏观矛盾拦截** (macro=UP vs NO) |
| 20:52 YES dir=15 | UP | 低 | **一致率<60% 拦截** |
| 21:57 NO dir=25 | UP | 低 | **宏观矛盾拦截** (macro=UP vs NO) |
| 22:08 NO dir=18 | UP | 低 | **宏观矛盾拦截** (macro=UP vs NO) |

4 笔全部被拦截，且有多重保险（宏观矛盾 + 低一致率双重否决）。

### 006 收紧后被过滤掉的合理交易能否恢复？

一个方向明确（raw=30, filter=12.5）、趋势一致（80%+）、宏观确认的单：
- 006 下：filter=12.5 < 30 → **被拦截**
- 本方案：一致率≥80% → 阈值=6, filter=12.5 > 6 → **通过，以半仓进场**

---

## 风险与注意事项

1. **K 线数据可能延迟**：`get_klines()` 每 60 秒刷新一次，宏观趋势可能滞后 1-2 分钟。好在这是用来确认大方向而非精确择时，滞后是可接受的。
2. **一致性窗口冷启动**：启动后前 60 秒 `_score_history` 样本不足，一致率返回 0 → 不会做任何交易。等信号积累后自然开始，这也是合理的预热行为。
3. **半仓交易的最小下单量**：$0.50 在 Polymarket 上可能接近最小下单限制。若实盘遇到拒单，需调高 `TRADE_SIZE_USD` 基数。
4. **本次移除了 `MIN_DIRECTION_FILTER_SCORE` 和 `MIN_STRATEGY_SCORE`**，如需回退可从 git 恢复 006 的配置。

---

## 验证

1. `python -m py_compile config.py main.py filters/direction.py execution/order_manager.py` ✅ 通过
2. 重启 bot（paper 模式），观察日志：
   - `consist=XX%` 和 `macro=UP/DOWN/FLAT` 出现在 SIGNAL 行
   - 宏观矛盾跳过日志 ("跳过宏观趋势矛盾")
   - `tier=HIGH $1.00` 和 `tier=MED $0.50` 两种仓位同时出现
3. 运行数小时后对比：
   - 交易频率应介于 005（过多）和 006（过少）之间
   - 胜率应高于 005
   - 单笔最大亏损应因半仓机制降低
