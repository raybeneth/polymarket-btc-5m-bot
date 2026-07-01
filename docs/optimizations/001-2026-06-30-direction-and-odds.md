# 001 — 方向动量波动率自适应 + odds 动态价格上限

- **日期**：2026-06-30
- **状态**：✅ 已实现并验证
- **涉及文件**：`config.py`、`filters/direction.py`、`filters/odds.py`、`main.py`

---

## 背景：起因于一笔可疑的边缘交易

排查 `positions.log` 最近一笔开仓 `PAPER-e3f11617`，从 `trades.jsonl` 还原出它的分项得分：

| 过滤器 | 得分 | 权重 | 贡献 |
|--------|------|------|------|
| direction | 50.7 | 35% | 17.7 |
| **odds** | **20.0** | 25% | 5.0 |
| timing | 48.4 | 15% | 7.3 |
| spread | 85.0 | 15% | 12.8 |
| chase | 100.0 | 10% | 10.0 |
| **合计** | | | **≈52.8**（门槛 50） |

问题很清楚：这笔交易在 **价格 0.78**（隐含概率 78%、赔付仅 1.28x）进场，odds 已给出接近垫底的 20 分警告，方向也只是中等的 50.7。真正把综合分撑过 50 门槛的是 spread+chase 这种"能不能买得进"的执行层指标（27.8 分），而"该不该买"的策略层（direction+odds+timing）只有 30 分。这是一笔 edge 很薄、赢少亏多的交易。

由此暴露出两个更深的设计缺陷，分别对应本次两项改动。

---

## 问题一：方向动量信号长期接近于零

### 病根

`filters/direction.py` 原来的动量归一化用固定 **±3%** 当满分边界：

```python
combined_raw = 0.6 * crossover_pct + 0.4 * total_move_pct
score = max(-100.0, min(100.0, combined_raw / 3.0 * 100.0))
```

但 3% 是 BTC 在 120 秒内的涨跌幅——这是极端行情，几乎不会出现。典型情况下 BTC 两分钟只波动 0.05%~0.3%：

- BTC 两分钟涨 0.1% → `combined_raw ≈ 0.07` → **momentum_score ≈ 2.3**

动量占 65% 权重，却长期只贡献 2~3 分。结果方向分**几乎完全由 35% 权重的盘口失衡决定**——名义上"动量 65% + 盘口 35%"，实际是"动量约等于 0 + 盘口扛全部"。这是整个系统最大的设计缺陷，在它之上调任何方向阈值都是在调一个坏掉的信号。

### 改动：波动率自适应归一化

不再用固定 3%，而是用近期已实现波动率做分母，把动量变成"几个 sigma 的移动"：

- 新增 `_realised_vol_pct()`：取 tick-to-tick 对数收益率的标准差，按 `sqrt(N)` 聚合到整个 lookback 时间尺度，得到该尺度的波动率（%）。
- 满分边界 = `DIRECTION_VOL_SIGMA_FULL × 波动率`，而非固定 3%。
- 加波动率下限 `DIRECTION_VOL_FLOOR_PCT` 防平盘除零。

```python
combined_move_pct = 0.6 * crossover_pct + 0.4 * total_move_pct
vol_pct = _realised_vol_pct(prices_raw)
full_scale_pct = DIRECTION_VOL_SIGMA_FULL * vol_pct
score = max(-100.0, min(100.0, combined_move_pct / full_scale_pct * 100.0))
```

新增配置（`config.py`）：

| 参数 | 默认值 | 作用 |
|------|--------|------|
| `DIRECTION_VOL_SIGMA_FULL` | 2.5 | 几个 horizon-sigma 的移动算满分；调小=更易给高分=交易更多 |
| `DIRECTION_VOL_FLOOR_PCT` | 0.02 | 波动率下限（%），防平盘除零 |

### 验证（合成 tape）

| 场景 | 旧 score | 新 score | 说明 |
|------|---------|---------|------|
| calm + 0.1% drift | ≈1.6 | **20.7** | 平静行情里的小趋势终于有意义了 |
| calm + 0.3% drift | ≈5 | **67.9** | 同行情更强趋势，分数随之上去 |
| volatile + 0.3% drift | ≈5 | **1.9** | 关键点：同样 0.3% 移动但市场剧烈震荡，相对自身波动率无意义 → 正确地几乎不给分 |
| flat（无趋势） | 0 | **-1.3** | 接近 0，下限护着不爆 |

---

## 问题二：贵价格无条件放行，与信心强度脱钩

### 病根

odds 过滤器原来用一刀切的 `ODDS_MAX_PRICE = 0.80` 当唯一上限。无论方向信号强弱，只要价格 ≤ 0.80 就放行。这导致中等方向（50.7）配很贵价格（0.78）的组合也能成立——正是 `PAPER-e3f11617` 的病根。价格该不该接受，本应取决于你有多大把握。

### 改动：硬护栏 + 方向自适应软门槛（两层）

**第一层——硬护栏（钝器）**：`ODDS_MAX_PRICE` 从 0.80 → **0.78**。绝对天花板，方向满分 100 也不破。

**第二层——动态上限（决策函数）**：新增 `_dynamic_max_price(direction_strength)`，可接受价格上限在 `[ODDS_DYN_MAX_WEAK, ODDS_MAX_PRICE]` 之间随方向强度 [0,100] 线性浮动：

```python
def _dynamic_max_price(direction_strength):
    s = max(0.0, min(100.0, direction_strength))
    dyn = ODDS_DYN_MAX_WEAK + (ODDS_MAX_PRICE - ODDS_DYN_MAX_WEAK) * (s / 100.0)
    return min(dyn, ODDS_MAX_PRICE)
```

- 方向=0 → 上限 0.60（弱信号只能买便宜价）
- 方向=50 → 上限约 0.69
- 方向=100 → 上限 0.78（到硬顶）

数据流：`main.py` 中 direction 先于 odds 运行且失败短路，因此把 `dir_result.score` 传入 `odds.check(entry_price, direction_strength=dir_result.score)`。`direction_strength` 默认 100，保持向后兼容。

新增/修改配置（`config.py`）：

| 参数 | 旧值 | 新值 | 作用 |
|------|------|------|------|
| `ODDS_MAX_PRICE` | 0.80 | **0.78** | 硬上限，任何情况不破 |
| `ODDS_DYN_MAX_WEAK` | — | **0.60** | 方向强度=0 时的可接受上限 |

### 关键验证：那笔边缘交易被拦下

```
price=0.78 dir=50.7  →  [SKIP] 价格 0.780 高于动态上限 0.691 — 方向强度 51 不足以支撑此价格
```

`PAPER-e3f11617` 在新规则下被 odds 第二关直接拦截：0.78 需要方向强度 ≥95 才放行，而它只有 50.7。其余验证：

| 场景 | 结果 | 说明 |
|------|------|------|
| 0.78 @ dir=100 | PASS | 满信心可付到硬顶 |
| 0.78 @ dir=95 | SKIP | 动态上限 0.771，差一点 |
| 0.65 @ dir=0 | SKIP | 弱方向上限 0.60 |
| 0.65 @ dir=50 | PASS | 动态上限 0.69 |
| 0.79 @ dir=100 | SKIP | 触硬上限，永不放行 |

---

## 设计原则：钝器做护栏，函数做决策

- **硬护栏**（收紧的 `ODDS_MAX_PRICE`）：绝对不碰的区间，风控底线。
- **软调节**（动态上限 + 波动率自适应）：护栏之内，用信号强度动态决策。

两者不冲突，分属不同层面。**分数与门槛也是两套机制**：动态上限只决定"能不能过"，过了之后 odds 打分仍按离 0.50 的距离算——所以即便强方向放行 0.78，它在综合分里的 odds 贡献依然很低，"贵就是贵"。

---

## 待办（本次未做）

- README.md / README_EN.md 与 `config.py` 已漂移（README 仍写 `DIRECTION_MIN_SCORE=35`、`TIMING_MIN_ELAPSED=45`、`MIN_COMBINED_SCORE=55`、`TRADE_SIZE_USD=1.0`、`ODDS_MAX_PRICE=0.80`）。本次新增的动态上限与波动率自适应机制也未写入文档。
- **B 方案（未实现）**：方向盘口失衡用了 `yes_imb - no_imb`，但二元市场 YES/NO 盘口互补、高度相关，可能重复计同一信息。需先核对 `feeds/polymarket_clob.py` 的 `imbalance()` 实现，确认两本盘口是否真正独立。
