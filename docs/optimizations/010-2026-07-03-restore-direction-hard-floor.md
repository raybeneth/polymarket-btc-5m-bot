# 010 — 恢复 direction 硬性门槛，堵住"无关分项凑综合分"的漏洞

- **日期**：2026-07-03
- **状态**：✅ 已实现，待 paper 盘验证
- **涉及文件**：`main.py`、`filters/direction.py`
- **前置**：[003](003-2026-07-01-min-scores-force-exit-odds.md) 首次引入并 [008](008-2026-07-03-tiered-noise-skip-reversal-guard.md) 移除的方向硬门槛；[009](009-2026-07-03-med-tier-magnitude-floor.md) 引入的 `DIRECTION_MED_MIN_SCORE`

---

## 背景：PAPER-93332164，direction=0.5 靠 odds+timing 凑到 50.48 分开仓，亏损 -$154.26

```
14:07:16 SIGNAL ^ YES @ 0.450 x2.22 score=50.5 [dir=0 | odd=88 | tim=95 | spr=85 | cha=85]
         consist=84% tier=LOW $250.00 t=136s/164s
14:07:16 OPEN ^ YES @ 0.470 qty=531.91 $250.00 ... PAPER-93332164
14:09:30 EXIT FORCE_EXIT YES 0.470->0.180 pnl=-154.26 hold=133s PAPER-93332164
```

`filter_scores = {"odds": 87.5, "chase": 85.4, "spread": 85.0, "timing": 94.6, "direction": 0.5}`，`combined_score = 0.25×87.5 + 0.45×0.5 + 0.30×94.6 = 50.48`。

`direction=0.5` 对应的原始平滑分只是刚好压线超过 `DIRECTION_MIN_SCORE=20`（filters/direction.py 的归一化：`filter_score = 100×(abs_score-20)/(100-20)`），本质上和噪声没有区别。`consistency=84%` 很高，但 [009](009-2026-07-03-med-tier-magnitude-floor.md) 加的 `DIRECTION_MED_MIN_SCORE=10` 把它从 MED 挤到了 LOW 档（25%仓位，$250），而不是直接跳过。持仓期间 `STOP_LOSS_ENABLED=false`，价格从 0.47 崩到 0.18 没有任何中途保护，到期前 30 秒强平，亏损占本金的 62%。

这与 009 记录的 `PAPER-25393563`（dir=1.9, consistency=64%, 亏损占本金 85%）是同一类问题——**近零强度的方向信号，靠一致性或靠 odds/timing 补分，仍然能开仓，只是仓位小一点**。仓位分档能缩小亏损的绝对金额，但改变不了亏损占本金的比例，因为止损是关闭的，输了就是接近全输。

## 根因：综合分门槛可以被 odds+timing 单独凑够，不需要 direction 参与

`SCORE_WEIGHTS = {odds: 0.25, direction: 0.45, timing: 0.30, spread: 0, chase: 0}`，`MIN_COMBINED_SCORE = 45`。

`odds` 和 `timing` 权重之和是 0.55，也就是说 **即使 `direction` 贡献恰好是 0，`odds=100 × 0.25 + timing=100 × 0.30 = 55`，已经超过 45 的门槛**。`direction` 名义上权重最高（45%），但在综合分这道关卡上并没有真正意义上的否决权，可以被两个和"该买 YES 还是 NO"完全无关的分项（价格贵不贵、进场时机对不对）稀释。

008 把 `MIN_COMBINED_SCORE` 从 55 降到 45、同时移除了方向硬门槛，理由是"三档仓位已经在吸收弱信号，不需要重复的硬跳过"。这次的复盘证明：仓位分档只降低了亏损的绝对值，没有降低亏损发生的概率或亏损占比——对于 direction 近乎为零的交易，本质上是纯赌 BTC 5 分钟内随机涨跌，25% 仓位一样能亏掉本金的六成以上。

## 改动：恢复硬跳过，复用已有的 `DIRECTION_MED_MIN_SCORE`

不新增配置项——`DIRECTION_MED_MIN_SCORE=10` 已经是 009 验证过的"近零强度"分界线，直接复用同一个常量，避免两条判定基准各自漂移。

### `main.py`

在噪声跳过（`consistency < DIRECTION_CONSISTENCY_FLOOR`）之后新增：

```python
if dir_result.score < DIRECTION_MED_MIN_SCORE:
    direction.record_skip("weak_skip")
    log.info(
        f"[{mid}] 跳过方向弱信号: filter_score={dir_result.score:.1f} < {DIRECTION_MED_MIN_SCORE} "
        f"(raw={raw_dir:.0f}, consistency={consistency:.0%})"
    )
    return
```

`weak_skip` 在硬跳过之前发生，后面的仓位分档逻辑因此保证 `dir_result.score >= DIRECTION_MED_MIN_SCORE`，MED 档判断里原本冗余的 `and dir_result.score >= DIRECTION_MED_MIN_SCORE` 一并删除：

```python
elif consistency >= DIRECTION_CONSISTENCY_LOW or strong_signal:
    trade_size = TRADE_SIZE_USD * 0.5
    tier = "MED"
```

`HIGH` 档的 `dir_result.score >= 20` 不受影响（20 > 10，不是冗余条件）。

### `filters/direction.py`

`_skip_stats` 新增 `"weak_skip"` 键，与 `neutral`/`noise_skip`/`low`/`med`/`high`/`reversal_guard` 并列，方便在 5 分钟一次的方向漏斗日志里观察这条门槛的拦截频率。

---

## 回放验证

用本次亏损单的真实参数：`dir_result.score=0.5, consistency=0.84`

- 旧逻辑 → 通过噪声跳过（84%≥30%）→ combined_score=50.48≥45 → LOW 档 $250 → 最终亏损 -$154.26
- 新逻辑 → `0.5 < DIRECTION_MED_MIN_SCORE(10)` → **在 combined_score 计算之前直接跳过**

回归检查（确认不误伤真正有强度的信号）：

| 场景 | dir_result.score | consistency | 结果 |
|------|------------------|-------------|------|
| 边界值刚好放行 | 10 | 55% | 通过硬跳过 → MED 档 |
| strong_signal 路径 | 26 | 40% | 通过硬跳过 → MED 档 |
| 高质量信号 | 45 | 85% | 通过硬跳过 → HIGH 档（combined≥60 时） |

## 对 LOW 档位构成的影响

硬跳过之后，LOW 档只会来自"`consistency` 在 30%~50% 之间但 `direction` 强度≥10"的信号——也就是真正有一定方向强度、只是持续性还不够的情况，不再包含"强度几乎为零、纯粹靠一致性凑巧混进来"的信号。这比 009 之前"LOW 档什么都能进"的构成更干净。

## 风险与注意事项

1. 这是继 003→008→009 之后第三次在"硬门槛 vs 仓位分档"之间调整，说明这个权衡本身没有一次性答案——固定阈值本身也可能需要随样本积累再调整。如果观察到弱信号跳过频率过高、交易频次明显下降，可考虑把 `DIRECTION_MED_MIN_SCORE` 拆分成两个独立常量（跳过用一个更低的值，MED 档准入用现在的 10），而不是简单调高/调低共用的 10。
2. 该门槛不解决"止损关闭导致亏损无下限"这个独立问题（同一份根因分析里另外指出的 `STOP_LOSS_ENABLED=false`），两者是互补的两道防线，不是替代关系。
3. **需重启机器人生效。**
