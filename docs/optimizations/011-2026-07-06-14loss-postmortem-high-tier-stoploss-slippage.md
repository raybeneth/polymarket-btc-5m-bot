# 优化 011 — 14连亏事后分析：HIGH tier 方向门槛、止损滑点、反转守卫

**日期**: 2026-07-06  
**触发**: 14连续亏损，总亏损 $2,170.40（Trade #106–119）

---

## 根因分析

### 问题 1：HIGH tier 被弱方向信号解锁（最严重）

**代码位置**: `main.py`，HIGH tier 判断条件  
**旧逻辑**: `dir_result.score >= 20`（direction 的最低通过门槛）  

Timing(0.30) + Odds(0.25) 权重合计 0.55，在两者都高分时可以把 `combined_score` 推过 60，
同时 direction 只需满足 ≥ 20 即可触发 HIGH tier（$1000 全额下注）。

**典型案例 — Trade 119**:
```
odds=90.0, timing=99.8, direction=23.7
combined = 90×0.25 + 23.7×0.45 + 99.8×0.30 = 22.5 + 10.7 + 29.9 = 63.1 ✓
direction ≥ 20 ✓  →  HIGH tier $1000
实际亏损: -$392.86
```

**修复**: 新增 `DIRECTION_HIGH_MIN_SCORE = 40.0`，HIGH tier 要求 `dir_result.score >= DIRECTION_HIGH_MIN_SCORE`。  
对应原始方向得分 ≥ 52/100，确保 HIGH 只在方向信号真实强劲时触发。  
方向分 20–39 的情形降级为 MED tier（$500），不再以全仓押注。

---

### 问题 2：止损触发价与实际出场价不一致（系统性额外亏损）

**代码位置**: `execution/order_manager.py`，`check_early_exits`  
**旧逻辑**:
```python
trigger_price = book.mid   # 用 mid 判断是否触发
exit_price    = best_bid   # 用 best_bid 实际出场
```

在快速运动的市场中，mid 和 best_bid 之间可以相差 10+ 美分：
- mid 触及止损线 → 触发
- 但实际用 best_bid 成交 → 远低于预期止损价

**Trade 119 的实际路径**:
```
entry    = 0.56
stop_line = 0.56 × 0.80 = 0.448  (mid 触发)
exit_fill = 0.34  (best_bid 成交)
额外损失 ≈ (0.448-0.34) × 1785 ≈ $193 超出预期止损
```

**修复**: 触发判断也改用 `best_bid`，与出场价保持一致。  
副作用：止损触发时间稍早（best_bid < mid），但出场价也是同一个价格，不存在滑点缺口。

---

### 问题 3：Paper 模式填单价虚增 +$0.02

**代码位置**: `execution/order_manager.py`，`_paper_execute`  
**旧逻辑**: `simulated_fill_price = decision.entry_price + ORDER_PRICE_SLIPPAGE`  

Paper 模式没有真实订单撮合，加上 $0.02 没有意义，只会：
1. 抬高模拟入场成本
2. 抬高止损触发线（entry 越高，stop = entry × 0.80 也越高）
3. 让 paper 模式的止损表现比真实情况更悲观，掩盖真实信号质量

**修复**: Paper 模式直接用 `decision.entry_price`（= best_ask）作为填单价，不加滑点。  
实盘模式的 `best_ask + slippage` 限价单逻辑保持不变。

---

### 问题 4：反转守卫窗口过短

**代码位置**: `config.py`，`DIRECTION_REVERSAL_GUARD_WINDOW`  
**旧值**: `12.0` 秒  

14连亏中有多笔在 7–25 秒内触发止损，说明动量信号是在趋势耗尽点产生的。
12 秒窗口根本无法识别这类 "MA 滞后买在顶部" 的情形。

**修复**: 延长至 `30.0` 秒，覆盖更完整的短期回撤范围。

---

## 改动汇总

| 文件 | 改动 |
|------|------|
| `config.py` | 新增 `DIRECTION_HIGH_MIN_SCORE = 40.0` |
| `config.py` | `DIRECTION_REVERSAL_GUARD_WINDOW`: 12.0 → 30.0 |
| `main.py` | HIGH tier: `dir_result.score >= 20` → `>= DIRECTION_HIGH_MIN_SCORE` |
| `execution/order_manager.py` | 止损触发: `book.mid` → `best_bid` |
| `execution/order_manager.py` | Paper 填单: `entry + 0.02` → `entry`（去掉虚增滑点） |

---

## 预期效果

- **Trade 106–118**（direction 12–32）：降级为 MED tier（$500→维持）或 LOW tier（$250）
- **Trade 119**（direction 23.7）：HIGH 条件 `score ≥ 40` 不满足 → 降级为 MED tier（$500）
- 止损出场时，不再因 mid/best_bid 差价产生额外亏损
- Paper 测试数据更真实，信号评估更准确
