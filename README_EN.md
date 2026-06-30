# Polymarket BTC 5-Minute Trading Bot

A bot that trades BTC 5-minute binary markets on Polymarket. It only places orders when odds, direction, timing, and order book conditions are simultaneously satisfied. The core philosophy is filtering, not frequent trading.

---

## Table of Contents

1. [Project Structure](#project-structure)
2. [Quick Start](#quick-start)
3. [How to Obtain Credentials](#how-to-obtain-credentials)
4. [5 Filters Explained](#5-filters-explained)
5. [Combined Scoring](#combined-scoring)
6. [Console Log Reference](#console-log-reference)
7. [Trade Record File](#trade-record-file)
8. [Risk Management Rules](#risk-management-rules)
9. [Parameter Tuning](#parameter-tuning)

---

## Project Structure

```
polymarket-btc-5m-bot/
├── main.py                   # Main loop — evaluates all active markets every 2 seconds
├── config.py                 # Single source of truth for all thresholds
├── get_credentials.py        # Run once to auto-generate .env credentials
├── .env.example              # Environment variable template
├── .gitignore                # Excludes .env and log files
├── requirements.txt
│
├── feeds/
│   ├── btc_price.py          # Binance WebSocket — maintains a 10-minute rolling BTC price history
│   └── polymarket_clob.py    # Gamma API market scanner + CLOB order book fetcher
│
├── filters/
│   ├── direction.py          # Directional strength (BTC momentum 65% + book imbalance 35%)
│   ├── odds.py               # Odds quality (price range check)
│   ├── timing.py             # Entry timing window
│   ├── spread.py             # Bid-ask spread + depth
│   └── chase.py              # Fill headroom for chasing
│
├── execution/
│   ├── order_manager.py      # Order execution (paper / live dual mode)
│   └── risk.py               # Daily loss cap, position sizing
│
└── utils/
    └── logger.py             # Colored console logs + trades.jsonl recorder
```

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Obtain credentials (see section below)
python get_credentials.py

# 3. Start in paper trading mode (default — no real money)
python main.py

# 4. Once satisfied with the logic, edit .env to enable live trading
#    PAPER_TRADING=false
python main.py
```

---

## How to Obtain Credentials

Polymarket CLOB uses a two-layer authentication system.

### L1 — Wallet Private Key (`PRIVATE_KEY`)

**Purpose**: Signs every on-chain order to guarantee authenticity.

**How to get it**:
- MetaMask: top-right menu → Account Details → Export Private Key
- Coinbase Wallet, OKX Wallet, or any other EVM wallet: same procedure
- Format: 66-character hex string starting with `0x`

> **Security warning**: Your private key is the sole control over your assets.
> Use it only on trusted machines. Never screenshot it, share it, or commit it to git
> (`.env` is already excluded via `.gitignore`).

### L2 — REST API Key (`API_KEY` / `SECRET` / `PASSPHRASE`)

**Purpose**: Signs every HTTP request so Polymarket's servers can verify the caller's identity.

**How to get it**: These three values **do not require manual registration**. They are derived automatically from your private key by `py-clob-client`:

```
Your private key  →  Signs a standard message  →  Sent to CLOB /auth/api-key  →  Server returns L2 credentials
```

This is deterministic derivation: **the same private key always produces the same credentials** — no duplicate accounts are created.

**One-command generation**:

```bash
python get_credentials.py
```

The script prompts for your private key and writes the credentials directly to `.env`.

**Prerequisite**: The wallet address must have connected to [polymarket.com](https://polymarket.com) at least once (no deposit needed — just a wallet connection). Otherwise the CLOB server does not recognize the address and returns a `401` error.

### .env File Example

```env
PRIVATE_KEY=0xabcdef...           # 64-character hex
CLOB_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
CLOB_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
CLOB_API_PASSPHRASE=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

PAPER_TRADING=true       # true = paper mode  |  false = live orders
TRADE_SIZE_USD=1.0       # USD amount per trade
LOG_LEVEL=INFO
```

---

## 5 Filters Explained

Every market is evaluated every 2 seconds. **If any single filter fails, the market is skipped immediately — subsequent filters are not checked.**

### Filter 1 — Directional Strength (`direction`)

**Purpose**: Determine whether BTC currently has a clear directional momentum and what that direction is.

**Two signals combined**:

| Signal | Weight | Calculation |
|--------|--------|-------------|
| BTC spot momentum | 65% | Deviation of Fast MA (30-second average) vs Slow MA (90-second average), plus total price change over 120 seconds |
| Polymarket book imbalance | 35% | Bid/ask volume ratio for YES and NO tokens — reflects the binary market's own directional pressure |

**Score**: combined score ∈ [-100, +100]

| Score range | Action |
|-------------|--------|
| ≥ +35 | Bullish → buy YES token |
| ≤ -35 | Bearish → buy NO token |
| (-35, +35) | Direction unclear → **skip** |

**Default threshold**: `DIRECTION_MIN_SCORE = 35` (edit in `config.py`)

---

### Filter 2 — Odds Quality (`odds`)

**Purpose**: Exclude markets where the price is too low (lottery-like) or too high (near-certain, poor return).

On Polymarket binary markets, token price = implied probability of that outcome = your cost per share.

| Price range | Meaning | Action |
|-------------|---------|--------|
| < 0.20 | Implied probability < 20% — lottery-like, highly unstable expected value | **Skip** |
| > 0.80 | Near-certain — payout < 1.25×, insufficient return | **Skip** |
| 0.20–0.30 / 0.70–0.80 | Acceptable but not ideal | Pass, low score |
| 0.30–0.70 | Ideal range — balanced odds vs. probability | Pass, high score |
| Near 0.50 | Maximum uncertainty = highest payout leverage | Score 100 |

**Examples**:
- Price 0.45 → payout 2.22× → score ≈ 95
- Price 0.25 → payout 4.00× → score ≈ 25 (low score)
- Price 0.82 → **skip**

---

### Filter 3 — Entry Timing (`timing`)

**Purpose**: Only enter during the effective window of a 5-minute market. Too early and direction hasn't formed; too late and there's no room to profit.

Market duration: 300 seconds (5 minutes)

```
|--45s--|------------ Entry window (165 seconds) ------------|--90s--|
0s     45s                                                  210s   300s
(open) (earliest entry)                           (latest entry) (expiry)
```

| Condition | Threshold | Skip reason |
|-----------|-----------|-------------|
| Elapsed < 45s | `TIMING_MIN_ELAPSED = 45` | Too early — direction not yet formed |
| Elapsed > 210s | `TIMING_MAX_ELAPSED = 210` | Too late — price already near settlement |
| Remaining < 90s | `TIMING_MIN_REMAINING = 90` | Not enough time to realize directional edge |

**Score**: Peak score at the midpoint of the window (~127s elapsed); decreases linearly toward either boundary.

---

### Filter 4 — Spread & Depth (`spread`)

**Purpose**: Avoid trading in markets with excessively wide spreads (high market-maker cost), and ensure sufficient liquidity at the best price level.

| Check | Threshold | Skip reason |
|-------|-----------|-------------|
| Best bid < 0.03 | `MIN_BID = 0.03` | Market is nearly dead, no liquidity |
| Best ask < 0.03 | `MIN_ASK = 0.03` | Same |
| Spread > 0.04 | `SPREAD_MAX_ALLOWED = 0.04` | Bid-ask spread over 4 cents — too costly |
| Ask size < 50 shares | `DEPTH_MIN_SHARES = 50` | Insufficient depth at best ask to fill |

**Scoring**:
- Spread score (60% weight): higher as spread narrows; 100 when spread = 0
- Depth score (40% weight): higher as ask size grows; 100 when ≥ 3× minimum
- Spreads > 0.02 are flagged with a `[wide]` warning in the log

---

### Filter 5 — Fill Headroom (`chase`)

**Purpose**: Confirm there is enough liquidity within the chase window around the order price (best ask + slippage) to fill the order — avoids being trapped by shallow books.

**Logic**:

```
Chase window = [best_ask, best_ask + 0.03]  (maximum 3-cent chase)
```

Cumulative order volume within this window must satisfy:
- Total ≥ 30 shares (`CHASE_MIN_FILL_SHARES`)
- Window volume ≥ shares needed for `TRADE_SIZE_USD / best_ask`

**Scoring**:
- Fill coverage (60%): available volume in window ÷ required volume; 100% = perfect score
- Headroom buffer (40%): available volume ÷ (2 × minimum); higher is better

---

## Combined Scoring

After **all 5 filters pass**, scores are weighted to produce a combined score:

| Filter | Weight |
|--------|--------|
| Directional strength | 35% |
| Odds quality | 25% |
| Entry timing | 15% |
| Spread & depth | 15% |
| Fill headroom | 10% |

**An order is placed only when combined score ≥ 55** (`MIN_COMBINED_SCORE = 55`)

Even if all filters pass, the market is skipped if the combined score is too low. The log shows `SKIP score_gate`.

---

## Console Log Reference

Every market evaluation is printed — both skips and trades.

### Skip Log (`SKIP`)

```
10:23:45 INFO  [abc1234567] SKIP direction     Direction too weak — combined=12.3, need |score| ≥ 35   elapsed=67s remaining=233s
10:23:47 INFO  [abc1234567] SKIP timing        Too early — only 30s elapsed, need ≥45s   dir=✓82  odds=✓71
10:23:49 INFO  [abc1234567] SKIP spread        Spread 0.0520 > max allowed 0.04   dir=✓82  odds=✓71  time=✓88  sprd=✗52
10:23:51 INFO  [abc1234567] SKIP score_gate    Score too low: 52.3 < 55   dir=✓71  odds=✓65  time=✓88  sprd=✓60  chs=✓70
```

**Format notes**:
- `[abc1234567]` — first 10 characters of the market ID
- `SKIP xxx` — which filter blocked it: `direction` / `odds` / `timing` / `spread` / `chase` / `score_gate` / `book_fetch`
- Following text explains exactly why it was skipped
- `dir=✓82` means the direction filter passed with score 82; `sprd=✗52` means spread filter failed
- **Dedup**: identical skip reasons for the same market are printed only once; reprinted when the reason changes

### Trade Log (`TRADE`)

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

### Status Summary (every 60 seconds)

```
10:30:00 INFO  Status: 3 active markets | Day 2026-06-29 | trades=5 W/L=3/2 (60%) | PnL=+0.4200 USD
```

---

## Trade Record File

All trades (paper and live) are appended to `trades.jsonl` — one JSON object per line.

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

**Analyze with pandas**:

```python
import pandas as pd
df = pd.read_json("trades.jsonl", lines=True)
df["entry_price"].hist()
df.groupby("side")["combined_score"].mean()
```

---

## Risk Management Rules

| Rule | Value | Description |
|------|-------|-------------|
| Max concurrent positions | 1 | Only one market position held at a time |
| Order size | `TRADE_SIZE_USD` (default $1) | Configured in `.env` |
| Daily loss cap | $10 | Bot stops placing orders if cumulative daily loss exceeds $10 |
| Order timeout | 8 seconds | Limit orders automatically cancelled after 8 seconds if unfilled |
| Chase limit | 3 cents | No order chasing beyond 3 cents above best ask |
| One trade per market | — | At most one order per market over its lifetime |

---

## Parameter Tuning

All tunable parameters are centralized in `config.py`. Changes take effect on the next bot restart.

| Parameter | Default | Effect |
|-----------|---------|--------|
| `ODDS_MIN_PRICE` | 0.20 | Minimum tradable price (implied probability floor) |
| `ODDS_MAX_PRICE` | 0.80 | Maximum tradable price (implied probability ceiling) |
| `DIRECTION_MIN_SCORE` | 35 | Minimum direction score — raise for stronger trends only |
| `TIMING_MIN_ELAPSED` | 45s | Earliest entry — raise to wait longer before entering |
| `TIMING_MIN_REMAINING` | 90s | Minimum time remaining — raise to cut off entry earlier |
| `SPREAD_MAX_ALLOWED` | 0.04 | Maximum spread — lower to trade only liquid markets |
| `CHASE_BUDGET` | 0.03 | Chase budget — lower to reduce slippage |
| `MIN_COMBINED_SCORE` | 55 | Combined score threshold — raise for fewer, higher-quality trades |
| `TRADE_SIZE_USD` | 1.0 | USD amount per trade |
| `MAX_DAILY_LOSS_USD` | 10.0 | Daily loss cap in USD |
