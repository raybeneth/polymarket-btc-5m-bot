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
# PRIVATE_KEY is the only credential needed — execution/order_manager.py's
# AsyncSecureClient (polymarket-client) derives its own L2 API auth from it,
# and auto-detects the wallet type (EOA / proxy / Gnosis Safe / deposit
# wallet) on-chain. No separate CLOB_API_KEY/SECRET/PASSPHRASE or
# SIGNATURE_TYPE config is needed or read anywhere in this codebase.
PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
POLYMARKET_HOST: str = "https://clob.polymarket.com"
GAMMA_HOST: str = "https://gamma-api.polymarket.com"
# Powers polymarket.com's own Profit/Loss widget — the dashboard reads its
# cumulative P&L series from here (live mode) so the curve/number match the
# site exactly instead of drifting from a locally-recomputed figure.
USER_PNL_HOST: str = "https://user-pnl-api.polymarket.com"
CHAIN_ID: int = 137  # Polygon mainnet

# Polymarket wraps most accounts (browser-wallet connect, email/Magic login,
# deposit wallets) in a proxy contract that actually custodies funds —
# PRIVATE_KEY only signs on behalf of that proxy, it doesn't hold the pUSD
# itself. If your Polymarket UI wallet address differs from the address
# derived from PRIVATE_KEY, set FUNDER_ADDRESS to that UI address so balance
# checks and orders target the right account.
FUNDER_ADDRESS: str = os.getenv("FUNDER_ADDRESS", "")

# Dashboard balance (live mode only) is read directly from FUNDER_ADDRESS's
# on-chain pUSD balance via a Polygon RPC call, rather than through the CLOB
# API — this always matches what polymarket.com's UI shows regardless of
# how the CLOB backend resolves the account internally.
POLYGON_RPC_URL: str = os.getenv("POLYGON_RPC_URL", "https://polygon-bor.publicnode.com")
# Polymarket's April 28 2026 "CLOB V2" upgrade replaced USDC.e with pUSD as the
# collateral token traders actually hold — FUNDER_ADDRESS's pUSD balance is
# what polymarket.com's UI shows now, not its USDC.e balance.
PUSD_CONTRACT_ADDRESS: str = os.getenv(
    "PUSD_CONTRACT_ADDRESS", "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
)  # pUSD — Polymarket's collateral token since the April 2026 CLOB V2 upgrade


# ──────────────────────────────────────────────
# OPERATIONAL MODE
# ──────────────────────────────────────────────
PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# Dashboard "pause/resume trading" button (web/server.py's /api/trading/toggle)
# is gated on this fixed password — new trade entries stop while paused, but
# existing open positions still get managed (stop-loss/force-exit/reconcile).
# Left blank by default so the endpoint always rejects until explicitly set.
TRADING_CONTROL_PASSWORD: str = os.getenv("TRADING_CONTROL_PASSWORD", "")

# Total USD ever deposited into the trading wallet — shown on the dashboard.
# No Polymarket API reports this (deposits are plain on-chain transfers, not
# an "activity" type their API tracks), so it's maintained by hand: bump this
# in .env whenever a real deposit is made. Display only — not used in sizing/
# risk logic.
TOTAL_DEPOSITS_USD: float = float(os.getenv("TOTAL_DEPOSITS_USD", "0.0"))


# ──────────────────────────────────────────────
# MYSQL — position storage
# ──────────────────────────────────────────────
MYSQL_HOST: str = os.getenv("MYSQL_HOST", "156.233.234.116")
MYSQL_PORT: int = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER: str = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD: str = os.getenv("MYSQL_PASSWORD", "mysql_jpdBm6")
MYSQL_DATABASE: str = os.getenv("MYSQL_DATABASE", "polymarket_bot_test")

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

# Raw-move floor for momentum normalisation. In a genuinely flat market,
# realised vol collapses toward DIRECTION_VOL_FLOOR_PCT (0.02%), making
# full_scale_pct tiny (2.5*0.02%=0.05%) — a ~$32-50 noise wobble on $64k BTC
# then normalises to a FULL ±100 momentum score. Below this floor we skip
# vol-adaptive scaling entirely and report momentum_score=0.0 instead of
# letting the floored denominator amplify noise. Set below the vol-floor's
# own full_scale (0.05%) so it never fires for markets with real measured
# vol above the floor — it only catches the exact "vol clamped to floor" regime.
DIRECTION_MIN_RAW_MOVE_PCT: float = 0.03

# Direction smoothing: use average of recent scores over this window
DIRECTION_SMOOTH_WINDOW: float = 20.0  # seconds to average direction scores over

# Orderbook imbalance weight in combined direction score
DIRECTION_OB_WEIGHT: float = 0.35     # 35% from orderbook imbalance
DIRECTION_MOMENTUM_WEIGHT: float = 0.65  # 65% from price momentum

# OB imbalance (35% weight, can reach ±100 alone) must not be able to
# manufacture a trade signal on a flat tape. Require momentum_score itself
# to clear this floor before OB is allowed to contribute at all — OB can only
# amplify/confirm a real momentum read, never create one solo. 20/0.65≈30.8
# is the momentum_score that alone clears DIRECTION_MIN_SCORE; 15 is set
# below that deliberately — it demands "non-trivial" momentum, not "solo
# bar-clearing" momentum, since OB is a legitimate confirming signal once
# momentum is real.
DIRECTION_MIN_MOMENTUM_ABS: float = 15.0

# Direction consistency — measures how persistently readings agree on direction.
# High consistency = genuine trend; low consistency = choppy/noisy.
DIRECTION_CONSISTENCY_WINDOW: float = 100.0    # seconds to look back for consistency
DIRECTION_CONSISTENCY_HIGH: float = 0.80      # ≥80% agreement = trending → lower dir threshold
DIRECTION_CONSISTENCY_LOW: float = 0.50       # ≥50% = MED tier boundary (no longer a hard skip)
DIRECTION_CONSISTENCY_FLOOR: float = 0.30     # <30% = pure noise → hard skip (only real cutoff left)

# MED-tier magnitude floor (see docs/optimizations/009). Consistency alone
# measures whether the SIGN has persisted, not whether the signal has any
# real strength — a filter_score sitting near 0 can still read consistency
# ≥50% by chance and would otherwise get the same 50% sizing as a genuinely
# moderate signal. PAPER-25393563 (dir_score=1.9, consistency=64% → MED $5,
# lost -4.27) is exactly this case. Requiring a small non-zero floor keeps
# "persistent near-noise" out of MED — it falls through to LOW (25%) instead.
DIRECTION_MED_MIN_SCORE: float = 10.0         # filter_score floor for the MED tier

# HIGH tier requires direction to be genuinely strong — not just technically
# above the minimum. Without this floor, timing(99)+odds(90) can push
# combined_score above 60 while direction sits at 21 (barely passing), which
# still qualifies for full $1000 sizing. 14 consecutive stops showed this
# exact pattern: combined≥60 carried by timing+odds, direction 12-25.
# Setting this to 40 means raw direction score must be ≥52 (out of 100)
# before a $1000 bet is placed. Falls through to MED ($500) otherwise.
DIRECTION_HIGH_MIN_SCORE: float = 40.0        # min filter_score for HIGH tier (was implicitly 20)

# Reversal guard — last-look freshness check performed immediately before
# order execution (not a duplicate of the 60s consistency window above).
# Looks at BTC price movement over this short horizon; if it has already
# reversed hard against our intended side, we abandon this attempt instead
# of firing into a stale signal. Extended from 12s to 30s: the 14-loss
# analysis showed entries at local extremes with reversals happening within
# 7-25s — a 12s window was too narrow to catch trend exhaustion.
DIRECTION_REVERSAL_GUARD_WINDOW: float = 30.0  # seconds (was 12.0)


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
TRADE_SIZE_USD: float = float(os.getenv("TRADE_SIZE_USD", "1000.0"))  # per trade (HIGH tier = 100%)
TRADE_SIZE_LOW_TIER_RATIO: float = 0.25  # LOW confidence tier sizing (MED tier stays 0.5, hardcoded in main.py)

# Weak entry-timing band → half size. Measured over 80 settled live positions
# (2026-07-09..07-17) by replaying each market's full trade tape: bucketing by
# seconds-to-expiry AT FILL, every band clears its own breakeven win rate except
# 180-200s, which settles at 52.0% against a 56.2% breakeven (-4.2pp edge):
#
#     band        n    breakeven    settled-win    edge
#     0-150s      15      56.6%         80.0%     +23.4pp
#     150-180s    21      61.1%         71.4%     +10.3pp
#     180-200s    25      56.2%         52.0%      -4.2pp   <- this one
#     200s+       19      59.4%         68.4%      +9.0pp
#
# This band was the worst P&L bucket as traded (-$59.82) but that was mostly the
# 20% stop-loss: the longer the runway to expiry, the more chance to dip 20% and
# get stopped. With the stop off it is only mildly negative, so it is halved
# rather than skipped — dropping the band entirely tested WORSE on the same data
# (+$146.48 vs +$156.33), because its post-stop contribution is slightly positive.
TRADE_SIZE_WEAK_TIMING_BAND: tuple = (180, 200)   # seconds-to-expiry at fill
TRADE_SIZE_WEAK_TIMING_RATIO: float = 0.5         # half size inside that band
TRADE_SIZE_WEAK_TIMING_ENABLED: bool = (
    os.getenv("TRADE_SIZE_WEAK_TIMING_ENABLED", "true").lower() == "true"
)
MAX_CONCURRENT_POSITIONS: int = 2      # only 1 open position at a time
MAX_DAILY_LOSS_USD: float = float(os.getenv("MAX_DAILY_LOSS_USD", "10000.0"))  # halt if daily P&L drops below -this

# Starting account balance for the dashboard's Profit/Loss widget — displayed
# balance/curve is this plus cumulative P&L, not raw P&L from zero. Purely a
# display baseline; doesn't affect trade sizing, risk gates, or PnL math.
ACCOUNT_BALANCE_USD: float = float(os.getenv("ACCOUNT_BALANCE_USD", "10000.0"))


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
STOP_LOSS_ENABLED: bool = os.getenv("STOP_LOSS_ENABLED", "false").lower() == "true"
TAKE_PROFIT_ENABLED: bool = os.getenv("TAKE_PROFIT_ENABLED", "false").lower() == "true"
STOP_LOSS_RATIO: float = 0.20         # sell if best_bid <= entry_price * (1 - ratio)
TAKE_PROFIT_DELTA: float = 0.15       # sell if best_bid >= entry_price + 0.15
FORCE_EXIT_BEFORE_EXPIRY: int = 30    # force exit when ≤30s remain before market settlement


# ──────────────────────────────────────────────
# ENTRY CONFIRMATION — BTC momentum deceleration (Strategy 1)
# ──────────────────────────────────────────────
# After the filter pipeline passes, hold off entry until BTC momentum has
# peaked and is visibly decelerating — or until ENTRY_MAX_WAIT_SECONDS elapses.
# Every 2s the bot checks whether EITHER condition below is satisfied:
#
#   Condition A (flat): |BTC % change in last 20s| < ENTRY_BTC_MAX_VELOCITY
#     → price has stabilised; entering is safe.
#
#   Condition B (decel): per-second rate over 20s < ENTRY_BTC_DECEL_RATIO × per-second rate over 60s
#     → the initial thrust has peaked; momentum is fading.
#
# Unlike a fixed 45s delay, this enters EARLY on stable/decelerating markets
# (good signals get full payout) and WAITS on fast-moving ones (eliminates
# the "enter at peak then immediately reversed" pattern that caused 7-27s stops).
# Hard ceiling ENTRY_MAX_WAIT_SECONDS prevents the market window from expiring.
#
# In parallel, a token-price adverse-move guard cancels the pending entry the
# moment the prediction-market price drops >ENTRY_CONFIRM_MAX_ADVERSE_PCT from
# the signal price — irrespective of BTC velocity.
#
# Both ENTRY_BTC_MAX_VELOCITY and ENTRY_FLAT_CONFIRM_TICKS were recalibrated
# after a review of 55 closed positions found 12/33 stop-losses (36%, -$37.77)
# closed within 20s of entry — every single one confirmed via Condition A on
# the very first ~2s check. Historical vel_20s at confirmation ranged 0-0.053%
# (p90=0.029%), meaning the old 0.1% bar never once actually filtered anything
# — it passed on tick 1 regardless of whether BTC had genuinely settled or was
# just pausing mid-trend before reversing. Tightened the bar AND require it to
# hold across several consecutive checks, not a single lucky snapshot.
ENTRY_BTC_MAX_VELOCITY: float = 0.0005       # 0.05% (was 0.1%) — demand calmer than ~90% of historical readings
ENTRY_BTC_DECEL_RATIO: float = 0.40          # 20s rate / 60s rate < 40% → momentum peaked
ENTRY_FLAT_CONFIRM_TICKS: int = 3            # condition A/B must hold on this many CONSECUTIVE ~2s checks before firing (resets on any failed check)
ENTRY_MAX_WAIT_SECONDS: float = 60.0         # hard ceiling: enter regardless after this many s
ENTRY_CONFIRM_MAX_ADVERSE_PCT: float = 0.015  # cancel if token price fell >1.5% from signal

# NOTE: the consecutive-loss circuit breaker (docs/optimizations/012) was removed
# on 2026-07-25. It never fired once in production: entries average ~138 minutes
# apart and all 35 consecutive-loss pairs in the live record were >300s apart, so
# the 300s pause had always expired before the next signal arrived. The loss
# clusters it targeted were an artifact of the 20% stop-loss — replaying the same
# 80 positions with the stop off drops the worst loss streak from 10 to 4 and max
# drawdown from -$64.86 to -$49.27 (both measured against the actual recorded
# P&L series). MAX_DAILY_LOSS_USD is the real remaining kill switch.

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
    "direction": 0.45,
    "timing":    0.30,
    "spread":    0.00,     # pass/fail gate only — no score contribution (near-constant ~85 inflated combined)
    "chase":     0.00,     # pass/fail gate only — no score contribution (near-constant ~100 inflated combined)
}
MIN_COMBINED_SCORE: float = 45.0      # minimum composite score (now only direction+odds+timing); lowered 55→45 since tiered sizing (LOW/MED/HIGH) now absorbs weaker signals instead of a hard skip
MIN_SPREAD_FILTER_SCORE: float = 65.0     # hard floor: spread filter score < 65 → skip (Strategy 3: 3 losses w/ spread<65, 0 wins below 65)
MIN_TIMING_FILTER_SCORE: float = 35.0     # hard floor: timing score < 35 → skip regardless of combined (raised from 20; data shows no wins below 46.4, losses at 14.5/20.0)
# Strategy 2: raised threshold to 35 (was 20) and enabled by default — 2 losses
# filtered (#127 timing=20, #136 timing=14.5), 0 wins filtered (min win=46.4).
MIN_TIMING_FILTER_SCORE_ENABLED: bool = os.getenv("MIN_TIMING_FILTER_SCORE_ENABLED", "true").lower() == "true"


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
    trade_size_usd: float = 0.0  # dynamic size based on confidence tier (0 = use default TRADE_SIZE_USD)
    skip_reason: Optional[str] = None
    # Real market metadata carried from the order book response (see feeds/polymarket_clob.py
    # OrderBook), used by live execution to round to a valid tick and skip unsupported markets.
    tick_size: Optional[str] = None
    min_order_size: float = 0.0
    neg_risk: bool = False

    def __str__(self) -> str:
        if self.should_trade:
            return (
                f"TRADE {self.side} @ {self.entry_price:.3f} "
                f"score={self.combined_score:.1f}"
            )
        return f"SKIP — {self.skip_reason}"
