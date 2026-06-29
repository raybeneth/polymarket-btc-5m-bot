"""
Central configuration for the BTC 5-min Polymarket bot.
All thresholds live here — nothing is hardcoded in logic modules.
"""
from dataclasses import dataclass, field
from typing import Optional
import os
from dotenv import load_dotenv

load_dotenv()


# ──────────────────────────────────────────────
# CREDENTIALS
# ──────────────────────────────────────────────
PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
API_KEY: str = os.getenv("CLOB_API_KEY", "")
API_SECRET: str = os.getenv("CLOB_API_SECRET", "")
API_PASSPHRASE: str = os.getenv("CLOB_API_PASSPHRASE", "")
POLYMARKET_HOST: str = "https://clob.polymarket.com"
GAMMA_HOST: str = "https://gamma-api.polymarket.com"
CHAIN_ID: int = 137  # Polygon mainnet


# ──────────────────────────────────────────────
# OPERATIONAL MODE
# ──────────────────────────────────────────────
PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
TRADE_LOG_FILE: str = "trades.jsonl"


# ──────────────────────────────────────────────
# FILTER 1 — ODDS / PRICE QUALITY
# ──────────────────────────────────────────────
# Price here is the cost of 1 YES/NO share (= implied probability).
# We buy YES when we think BTC goes up, NO when we think it goes down.
#
#   < MIN_PRICE  →  lottery ticket, skip
#   > MAX_PRICE  →  nearly certain outcome, risk/reward terrible, skip
#   PREFERRED range gets a score bonus
#
ODDS_MIN_PRICE: float = 0.20   # below this: skip (implied prob <20% = lotto)
ODDS_MAX_PRICE: float = 0.80   # above this: skip (payout too small)
ODDS_PREFERRED_MIN: float = 0.30
ODDS_PREFERRED_MAX: float = 0.70


# ──────────────────────────────────────────────
# FILTER 2 — DIRECTION STRENGTH
# ──────────────────────────────────────────────
# Measures BTC spot momentum to decide which side to take.
# Score is normalised to [-100, +100].
#
#   |score| < MIN_DIRECTION_SCORE  →  market is ambiguous, skip
#   score   ≥ MIN_DIRECTION_SCORE  →  bullish  → buy YES
#   score   ≤ -MIN_DIRECTION_SCORE →  bearish  → buy NO
#
DIRECTION_MIN_SCORE: float = 35.0    # minimum abs momentum score to trade
DIRECTION_LOOKBACK_SECONDS: int = 120  # how far back to measure BTC momentum
DIRECTION_FAST_WINDOW: int = 30        # short-term window (seconds) for fast MA
DIRECTION_SLOW_WINDOW: int = 90        # long-term window (seconds) for slow MA

# Orderbook imbalance weight in combined direction score
DIRECTION_OB_WEIGHT: float = 0.35     # 35% from orderbook imbalance
DIRECTION_MOMENTUM_WEIGHT: float = 0.65  # 65% from price momentum


# ──────────────────────────────────────────────
# FILTER 3 — ENTRY TIMING (remaining time in market)
# ──────────────────────────────────────────────
# 5-min market = 300 seconds total.
# We only enter during a specific window where:
#   - enough has happened to read direction (>= MIN_ELAPSED)
#   - enough time remains to benefit from being right (>= MIN_REMAINING)
#
MARKET_DURATION_SECONDS: int = 300    # total market length

TIMING_MIN_ELAPSED: int = 45          # must have at least 45s of market history
TIMING_MAX_ELAPSED: int = 210         # never enter after 3m30s elapsed
TIMING_MIN_REMAINING: int = 90        # must have ≥90s left when we enter
TIMING_MAX_REMAINING: int = 255       # don't enter in the first 45s (< this = too early)

# Derived helpers (do not change)
TIMING_ENTRY_OPEN_AT: int = MARKET_DURATION_SECONDS - TIMING_MAX_REMAINING   # 45s
TIMING_ENTRY_CLOSE_AT: int = MARKET_DURATION_SECONDS - TIMING_MIN_REMAINING  # 210s


# ──────────────────────────────────────────────
# FILTER 4 — ORDER BOOK SPREAD & DEPTH
# ──────────────────────────────────────────────
# Spread = ask - bid (in dollars/cents).
# Depth = total shares available within a price band.
#
SPREAD_MAX_ALLOWED: float = 0.04      # skip if spread > 4 cents
SPREAD_WARN_THRESHOLD: float = 0.02   # above 2 cents → reduce score
DEPTH_MIN_SHARES: float = 50.0        # minimum shares at best ask for our side
DEPTH_LEVELS_TO_CHECK: int = 5        # how many book levels to aggregate

# Minimum bid/ask to even consider — avoids dead markets
MIN_BID: float = 0.03
MIN_ASK: float = 0.03


# ──────────────────────────────────────────────
# FILTER 5 — CHASE SPACE (slippage tolerance)
# ──────────────────────────────────────────────
# How far above best ask are we willing to chase to fill our order?
# Chase budget = absolute max additional cost per share.
# Available volume = shares available within best_ask + CHASE_BUDGET.
#
CHASE_BUDGET: float = 0.03            # max 3 cents above best ask to chase
CHASE_MIN_FILL_SHARES: float = 30.0   # must have ≥30 shares within chase budget


# ──────────────────────────────────────────────
# TRADE SIZING
# ──────────────────────────────────────────────
TRADE_SIZE_USD: float = float(os.getenv("TRADE_SIZE_USD", "1.0"))  # per trade
MAX_CONCURRENT_POSITIONS: int = 1      # only 1 open position at a time
MAX_DAILY_LOSS_USD: float = 10.0       # halt if daily P&L < -$10


# ──────────────────────────────────────────────
# ORDER EXECUTION
# ──────────────────────────────────────────────
ORDER_TYPE: str = "GTC"               # Good Till Cancel (we cancel manually if stale)
ORDER_FILL_TIMEOUT_SECONDS: int = 8   # cancel unfilled order after 8s
ORDER_PRICE_SLIPPAGE: float = 0.02    # place limit at best_ask + 2 cents


# ──────────────────────────────────────────────
# MARKET SCANNER
# ──────────────────────────────────────────────
SCANNER_POLL_INTERVAL: int = 15       # re-scan for new markets every 15s
MARKET_KEYWORDS: list = ["BTC", "Bitcoin", "5 minute", "5-minute", "5min"]
MARKET_MIN_VOLUME_USD: float = 100.0  # skip markets with too little volume


# ──────────────────────────────────────────────
# SCORING — combined gate before any trade
# ──────────────────────────────────────────────
# Each filter returns a sub-score [0, 100].
# Final score = weighted average of all sub-scores.
# Trade only fires if final score >= MIN_COMBINED_SCORE.
#
SCORE_WEIGHTS: dict = {
    "odds":      0.25,
    "direction": 0.35,
    "timing":    0.15,
    "spread":    0.15,
    "chase":     0.10,
}
MIN_COMBINED_SCORE: float = 55.0      # minimum composite score to place trade


@dataclass
class FilterResult:
    """Returned by every filter. Carries pass/fail, score, and human reason."""
    passed: bool
    score: float          # 0-100, only meaningful when passed=True
    reason: str           # human-readable explanation (used in logs)
    details: dict = field(default_factory=dict)  # extra data for debugging

    def __str__(self) -> str:
        status = "PASS" if self.passed else "SKIP"
        return f"[{status}] {self.reason} (score={self.score:.1f})"


@dataclass
class TradeDecision:
    """Final verdict from the filter pipeline."""
    should_trade: bool
    side: str             # "YES" or "NO"
    market_id: str
    token_id: str
    entry_price: float    # best ask for our side
    combined_score: float
    filter_results: dict  # name → FilterResult
    skip_reason: Optional[str] = None

    def __str__(self) -> str:
        if self.should_trade:
            return (
                f"TRADE {self.side} @ {self.entry_price:.3f} "
                f"score={self.combined_score:.1f}"
            )
        return f"SKIP — {self.skip_reason}"
