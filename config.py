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


# ──────────────────────────────────────────────
# MYSQL — position storage
# ──────────────────────────────────────────────
MYSQL_HOST: str = os.getenv("MYSQL_HOST", "192.168.0.103")
MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER: str = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD: str = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE: str = os.getenv("MYSQL_DATABASE", "polymarket_bot")

# BTC spot price feed source. Binance WS can return HTTP 451 (geo-blocked)
# depending on the server's region — switch source via env var, no code change.
#   "binance" (default) | "okx"
BTC_PRICE_SOURCE: str = os.getenv("BTC_PRICE_SOURCE", "binance").lower()

# Master proxy switch. When True (default), feeds use the system HTTP_PROXY/
# HTTPS_PROXY env vars if set (needed when Polymarket's CLOB returns HTTP 403
# for this server's IP). When False, ALL feeds connect directly and ignore
# HTTP_PROXY/HTTPS_PROXY entirely, even if configured in the environment.
PROXY_ENABLED: bool = os.getenv("PROXY_ENABLED", "true").lower() == "true"

# Local file backup of trade/position records (MySQL is the source of truth
# for stats/PnL; these files are kept only for manual inspection/debugging).
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
ODDS_MAX_PRICE: float = 0.72   # hard ceiling: never buy above this, even on max conviction
ODDS_PREFERRED_MIN: float = 0.30
ODDS_PREFERRED_MAX: float = 0.70

# Direction-adaptive price ceiling.
# The effective max acceptable price scales with direction strength [0,100]:
#   weak direction   → only cheap prices allowed (ODDS_DYN_MAX_WEAK)
#   strong direction → up to the hard ceiling   (ODDS_MAX_PRICE)
# This stops mid-conviction trades from chasing expensive prices (low payout)
# while still letting genuinely strong signals pay up.
ODDS_DYN_MAX_WEAK: float = 0.60   # acceptable ceiling when direction strength = 0


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
DIRECTION_MIN_SCORE: float = 20.0    # minimum abs momentum score to trade (reverted 16→20: 005's loosening admitted near-noise dir=15-25 signals that lost 4/4)
DIRECTION_LOOKBACK_SECONDS: int = 180  # how far back to measure BTC momentum
DIRECTION_FAST_WINDOW: int = 45        # short-term window (seconds) for fast MA
DIRECTION_SLOW_WINDOW: int = 120       # long-term window (seconds) for slow MA

# Volatility-adaptive momentum normalisation.
# The raw momentum (a % move) is divided by the recent realised volatility
# instead of a fixed % threshold, so the signal stays meaningful in both
# calm and violent regimes. full_scale = SIGMA_FULL * realised_horizon_vol.
DIRECTION_VOL_SIGMA_FULL: float = 2.5  # combined move of this many horizon-sigmas → ±100
DIRECTION_VOL_FLOOR_PCT: float = 0.02  # min volatility scale (%) — guards flat-tape divide-by-zero

# Direction smoothing: use average of recent scores over this window
DIRECTION_SMOOTH_WINDOW: float = 20.0  # seconds to average direction scores over

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

TIMING_MIN_ELAPSED: int = 70          # must have at least 70s of market history (was 40 — raised so direction signal has more time to mature before entry)
TIMING_MAX_ELAPSED: int = 210         # never enter after 3m30s elapsed
TIMING_MIN_REMAINING: int = 90        # must have ≥90s left when we enter
TIMING_MAX_REMAINING: int = 250       # don't enter in the first 40s (< this = too early)

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
DEPTH_MIN_SHARES: float = 30.0        # minimum shares at best ask for our side
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
MAX_CONCURRENT_POSITIONS: int = 2      # only 1 open position at a time
MAX_DAILY_LOSS_USD: float = 20.0       # halt if daily P&L < -$10


# ──────────────────────────────────────────────
# ORDER EXECUTION
# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
# STOP-LOSS / TAKE-PROFIT (mid-market exit)
# ──────────────────────────────────────────────
# Monitor token best_bid during hold period.
# Exit early if price moves against us (stop-loss) or in our favour (take-profit).
# Stop-loss uses a ratio of entry price (dynamic):
#   entry 0.45 → delta=0.09, stop at 0.36
#   entry 0.55 → delta=0.11, stop at 0.44
#   entry 0.65 → delta=0.13, stop at 0.52
STOP_LOSS_RATIO: float = 0.20         # sell if best_bid <= entry_price * (1 - ratio)
TAKE_PROFIT_DELTA: float = 0.15       # sell if best_bid >= entry_price + 0.15
FORCE_EXIT_BEFORE_EXPIRY: int = 30    # force exit when ≤30s remain before market settlement


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
    "odds":      0.20,
    "direction": 0.35,
    "timing":    0.25,
    "spread":    0.10,
    "chase":     0.10,
}
MIN_COMBINED_SCORE: float = 55.0      # minimum composite score to place trade (reverted 53→55: undo 005's loosening after 4/4 marginal-score losers)
MIN_DIRECTION_FILTER_SCORE: float = 30.0  # hard floor: direction score < 30 → skip regardless of combined (raised 15→30: dir<30 signals were pure noise, lost 4/4)
MIN_TIMING_FILTER_SCORE: float = 20.0     # hard floor: timing score < 20 → skip regardless of combined

# Strategy-layer gate: the "should we buy" filters (direction + odds + timing),
# renormalised to [0,100], must clear this on their own. spread/chase are
# execution-quality ("can we fill it") and are near-constant (85/100); they
# stay as pass/fail vetoes but must NOT be able to lift a weak-strategy trade
# over MIN_COMBINED_SCORE. The 4/4 losers all had strategy-only scores <55.
STRATEGY_SCORE_KEYS: tuple = ("direction", "odds", "timing")
MIN_STRATEGY_SCORE: float = 55.0


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
    market_end_ts: float = 0.0  # market resolution time (unix s), for settlement
    skip_reason: Optional[str] = None

    def __str__(self) -> str:
        if self.should_trade:
            return (
                f"TRADE {self.side} @ {self.entry_price:.3f} "
                f"score={self.combined_score:.1f}"
            )
        return f"SKIP — {self.skip_reason}"
