# 009 — MED 档新增方向强度下限（修复"持续噪音"误判为中等信号）

- **日期**：2026-07-03
- **状态**：✅ 已实现，待 paper 盘验证
- **涉及文件**：`config.py`、`main.py`
- **前置**：[008](008-2026-07-03-tiered-noise-skip-reversal-guard.md) 引入的三档仓位 + 反转哨兵

---

## 背景：一笔 dir_score=1.9 的"零信号"单，靠一致性拿到了 50% 仓位

线上单 `PAPER-25393563`：

```
12:32:06 SIGNAL ^ YES @ 0.530 x1.89 score=48.2 [dir=2 | odd=92 | tim=81 | spr=85 | cha=100]
         consist=64% tier=MED $5.00 t=127s/173s
12:32:06 OPEN ^ YES @ 0.550 qty=9.09 $5.00 ... PAPER-25393563
12:34:30 EXIT FORCE_EXIT YES 0.550->0.080 pnl=-4.27 hold=143s PAPER-25393563
```

同一 5 分钟窗口的漏斗统计：

```
方向漏斗(近5分钟): NEUTRAL=68 噪声跳过=2 反转拦截=0 | 成交 LOW=0 MED=1 HIGH=0 (共1笔)
```

`dir_score=1.9` 是几乎为零的信号（对应原始 raw ≈ 21.5，只是刚好越过 `DIRECTION_MIN_SCORE=20` 的 NEUTRAL 线），却拿到了和"真正中等强度信号"同样的 **50% 仓位（MED 档）**。开仓后 143 秒里没有任何止损/中途监控（`STOP_LOSS_ENABLED=false`，反转哨兵只看下单前 12 秒），YES 价格从 0.55 崩到 0.08，到期前 30 秒强平，亏损 -4.27（占 $5 本金的 85%）。

## 根因：一致性只测"符号持续性"，不测"强度"

008 的三档仓位逻辑（`main.py`）：

```python
elif consistency >= DIRECTION_CONSISTENCY_LOW or strong_signal:
    trade_size = TRADE_SIZE_USD * 0.5   # 50%
    tier = "MED"
```

`consistency≥50%` 是这笔单进 MED 档的唯一原因——`dir_result.score=1.9` 远够不到 `strong_signal`(≥25) 的门槛。问题是：**一致性只回答"过去 60 秒方向读数的符号是否一致"，不回答"这个信号本身有没有强度"**。一个强度几乎为零、纯粹因为运气符号没翻转的信号，和一个真正持续 60 秒、强度中等的信号，在当前逻辑下被同等对待，都能拿 50% 仓位。

这与 008 记录里已经预见的风险是同一类问题，但落在了**风险敞口更大的 MED 档**（008 当时只标注了 LOW 档"胜率未知"的风险，未注意到 MED 档的准入条件同样没有强度约束）。

## 附带发现：NEUTRAL 才是真正的频率瓶颈（本次不处理，仅记录）

同一份漏斗日志显示：**NEUTRAL:噪声跳过:成交 ≈ 68:2:1**——绝大多数评估在 `direction.check()` 最前面的 `|raw|<20` 硬门槛就被拦下，远超一致性/分级这一层的影响。这印证了 008 自己提出但当时没有数据支撑的猜测。这是否要下调 `DIRECTION_MIN_SCORE(20)` 需要更长时间的漏斗数据积累后再评估，本次不动，只记录在案。

---

## 改动：MED 档新增方向强度下限

### `config.py`

```python
DIRECTION_MED_MIN_SCORE: float = 10.0   # filter_score floor for the MED tier
```

10 分（约对应 raw≈28）是一个很低的下限，明显低于 `strong_signal` 的 25 分——目的只是把"强度几乎为零但符号凑巧一致"的情况排除出 MED，而不是收紧真正中等强度的信号。

### `main.py`

```python
elif (consistency >= DIRECTION_CONSISTENCY_LOW or strong_signal) \
        and dir_result.score >= DIRECTION_MED_MIN_SCORE:
    trade_size = TRADE_SIZE_USD * 0.5   # 50%
    tier = "MED"
else:
    trade_size = TRADE_SIZE_USD * TRADE_SIZE_LOW_TIER_RATIO  # 25%
    tier = "LOW"
```

不达标的信号**不再被跳过**，而是降级到 **LOW（25% 仓位）**——保持 008"只对真噪音硬跳过、其余靠仓位分级"的整体设计哲学不变，只是把"持续的零强度信号"从 50% 挪到 25%。

`strong_signal` 路径不受影响：其门槛（dir_score≥25）本身就高于新下限（10），不会被误伤。

---

## 验证

1. `python3 -m py_compile config.py main.py` ✅ 通过
2. 用本次亏损单的真实参数回放（`dir_score=1.9, consistency=0.64, strong_signal=False`）：
   - 旧逻辑 → MED，$5.00
   - 新逻辑 → **LOW，$2.50**（同样的信号，仓位减半）
3. 回归检查真正中等/强信号不受影响：
   - `dir_score=15, consistency=0.55` → 仍为 MED
   - `dir_score=26`（strong_signal）`, consistency=0.40` → 仍为 MED
4. **需重启机器人生效**。重启后观察 MED 档新样本的方向强度分布，以及 LOW 档因此新增的样本胜率。
