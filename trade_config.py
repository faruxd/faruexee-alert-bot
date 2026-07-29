# =============================================================
#   FARUEXEE TRADE BOT  —  CONFIGURATION
# =============================================================
#
#   Everything tunable lives here. Secrets never do — those come
#   from environment variables (see .env.example).
#
#   The defaults below match the risk profile you chose:
#     • live trading, small size
#     • 3 concurrent positions max, 2% risk each → 6% portfolio risk
#     • resting limit orders at the zone edge
#     • BTC / ETH / SOL / BNB / XRP on 1H and 4H
# =============================================================

import os

# Load a local .env when python-dotenv is installed. Optional — the bot
# works fine with plain environment variables.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def _b(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# =============================================================
#   CREDENTIALS  (environment only — never hardcode)
# =============================================================

BITGET_API_KEY     = os.environ.get("BITGET_API_KEY", "")
BITGET_API_SECRET  = os.environ.get("BITGET_API_SECRET", "")
BITGET_PASSPHRASE  = os.environ.get("BITGET_PASSPHRASE", "")
DISCORD_WEBHOOK    = os.environ.get("DISCORD_WEBHOOK", "")


# =============================================================
#   MASTER SWITCHES
# =============================================================

# Nothing is sent to the exchange unless this is explicitly true.
# Default false so an accidental run cannot open a position.
LIVE_TRADING       = _b("LIVE_TRADING", False)

# Dry run still does everything except place/cancel/modify orders.
# Useful for watching the bot's decisions against a live market.
DRY_RUN            = _b("DRY_RUN", not LIVE_TRADING)

PRODUCT_TYPE       = os.environ.get("BITGET_PRODUCT_TYPE", "USDT-FUTURES")
MARGIN_COIN        = os.environ.get("BITGET_MARGIN_COIN", "USDT")


# =============================================================
#   TRADING SCOPE
# =============================================================

SYMBOLS = [s.strip().upper() for s in os.environ.get(
    "TRADE_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT"
).split(",") if s.strip()]

# 4H first so the higher timeframe wins when both fire on one symbol.
TIMEFRAMES = [t.strip() for t in os.environ.get(
    "TRADE_TIMEFRAMES", "4H,1H"
).split(",") if t.strip()]

HTF_MAP = {"30m": "1H", "1H": "4H", "4H": "1D", "1D": None}

CHECK_INTERVAL     = _i("CHECK_INTERVAL", 120)     # seconds between scans
CANDLE_LIMIT       = _i("CANDLE_LIMIT", 300)


# =============================================================
#   RISK  —  the numbers that decide whether you survive
# =============================================================

RISK_PER_TRADE     = _f("RISK_PER_TRADE", 0.02)    # 2% of equity per trade
MAX_CONCURRENT     = _i("MAX_CONCURRENT", 3)       # open positions + resting orders
MAX_PORTFOLIO_RISK = _f("MAX_PORTFOLIO_RISK", 0.06)  # hard ceiling on summed risk

# Stop trading for the rest of the UTC day after this drawdown.
MAX_DAILY_LOSS     = _f("MAX_DAILY_LOSS", 0.06)

# Refuse to trade at all if equity falls below this fraction of the
# equity recorded when the bot first started. A circuit breaker for
# "something is badly wrong".
MIN_EQUITY_FRACTION = _f("MIN_EQUITY_FRACTION", 0.70)

LEVERAGE           = _i("LEVERAGE", 10)
MARGIN_MODE        = os.environ.get("MARGIN_MODE", "isolated")

# Position notional may not exceed this multiple of account equity.
# Protects against a very tight stop producing an enormous position.
MAX_NOTIONAL_X_EQUITY = _f("MAX_NOTIONAL_X_EQUITY", 3.0)

# Never commit more than this fraction of free margin to one trade.
MAX_MARGIN_FRACTION = _f("MAX_MARGIN_FRACTION", 0.30)

# Skip a setup whose entry is further than this from current price —
# a limit order 20% away is not a trade, it is a wish.
MAX_ENTRY_DISTANCE = _f("MAX_ENTRY_DISTANCE", 0.05)   # 5%

# Minimum account equity required to operate at all.
MIN_EQUITY_USDT    = _f("MIN_EQUITY_USDT", 50.0)

# Simulated equity used when dry-running without API credentials, so the
# whole bot can be exercised end to end before any key exists.
PAPER_EQUITY       = _f("PAPER_EQUITY", 1000.0)


# =============================================================
#   ORDER LIFECYCLE
# =============================================================

ORDER_FORCE        = os.environ.get("ORDER_FORCE", "gtc")   # gtc | post_only

# Cancel a resting entry that has not filled within this many hours.
ORDER_TTL_HOURS    = _f("ORDER_TTL_HOURS", 48.0)

# Take-profit ladder as fractions of position size. Must sum to 1.0.
TP_SPLIT           = (
    _f("TP1_SPLIT", 0.50),
    _f("TP2_SPLIT", 0.30),
    _f("TP3_SPLIT", 0.20),
)

# Move the stop to break-even once TP1 fills.
MOVE_SL_TO_BE      = _b("MOVE_SL_TO_BE", True)

# Nudge break-even slightly into profit to cover fees (fraction of the
# original stop distance). 0.05 = 5% of the risk distance.
BE_OFFSET_R        = _f("BE_OFFSET_R", 0.05)

TRIGGER_TYPE       = os.environ.get("TRIGGER_TYPE", "mark_price")  # mark_price | fill_price


# =============================================================
#   STRATEGY  (passed into faruexee_engine.EngineConfig)
# =============================================================

LOOKBACK           = _i("LOOKBACK", 20)
IMPULSE_STRENGTH   = _f("IMPULSE_STRENGTH", 1.5)
TREND_STABILITY    = _i("TREND_STABILITY", 2)
VOLUME_MULT        = _f("VOLUME_MULT", 1.5)
REQUIRE_VOLUME     = _b("REQUIRE_VOLUME", True)
USE_BASE_CANDLE    = _b("USE_BASE_CANDLE", True)
USE_ATR_SL         = _b("USE_ATR_SL", True)
ATR_LEN            = _i("ATR_LEN", 14)
ATR_MULT           = _f("ATR_MULT", 0.5)
SL_BUFFER          = _f("SL_BUFFER", 0.25)
MIN_RR             = _f("MIN_RR", 1.5)
TP_MULTI           = _f("TP_MULTI", 2.0)
ZONE_MAX_AGE       = _i("ZONE_MAX_AGE", 300)
BREACH_BUF_MULT    = _f("BREACH_BUF_MULT", 0.1)
FVG_RECENT_BARS    = _i("FVG_RECENT_BARS", 100)
REQUIRE_HTF        = _b("REQUIRE_HTF", True)

# HTF bias: "ema" or "structure".
#
# Keep this on "ema". Bitget serves at most 90 daily candles, and the
# 20/20 pivot structure trend needs two confirmed pivots (41 bars each)
# before it leaves zero — so on the 1D it is permanently undetermined and
# blocks every 4H setup. Verified against live data on all five majors.
HTF_BIAS_MODE      = os.environ.get("HTF_BIAS_MODE", "ema").lower()
HTF_EMA_LEN        = _i("HTF_EMA_LEN", 50)

# Should an undetermined HTF bias block entries? Off by default so a
# timeframe that cannot produce a reading does not veto everything.
HTF_FLAT_BLOCKS    = _b("HTF_FLAT_BLOCKS", False)


# =============================================================
#   FILES
# =============================================================
#
#   On Render, set DATA_DIR to a mounted persistent disk (e.g. /var/data).
#   Without one the filesystem is wiped on every restart and redeploy,
#   which resets the daily-loss baseline, clears the halt flag, and
#   destroys the trade journal.

DATA_DIR = os.environ.get("DATA_DIR", "").strip()

if DATA_DIR:
    os.makedirs(DATA_DIR, exist_ok=True)

def _data_path(env_name, filename):
    explicit = os.environ.get(env_name)
    if explicit:
        return explicit
    return os.path.join(DATA_DIR, filename) if DATA_DIR else filename

STATE_FILE   = _data_path("TRADE_STATE_FILE", "trade_state.json")
JOURNAL_FILE = _data_path("TRADE_JOURNAL_FILE", "trade_journal.csv")

# Set false only if you have deliberately accepted ephemeral storage.
WARN_EPHEMERAL = _b("WARN_EPHEMERAL", True)


# =============================================================
#   WEB / HEALTH SERVER  (Render web services need a bound port)
# =============================================================

# Render injects PORT. Set ENABLE_WEB=true when deploying as a Web
# Service; leave it off for a Background Worker, which needs no port.
ENABLE_WEB = _b("ENABLE_WEB", bool(os.environ.get("PORT")))
WEB_PORT   = _i("PORT", 8080)


# =============================================================
#   VALIDATION
# =============================================================

def validate():
    """Return a list of fatal configuration problems."""
    errs = []

    if LIVE_TRADING and not DRY_RUN:
        if not BITGET_API_KEY:
            errs.append("BITGET_API_KEY is not set")
        if not BITGET_API_SECRET:
            errs.append("BITGET_API_SECRET is not set")
        if not BITGET_PASSPHRASE:
            errs.append("BITGET_PASSPHRASE is not set")

    if not 0 < RISK_PER_TRADE <= 0.10:
        errs.append(f"RISK_PER_TRADE {RISK_PER_TRADE} outside sane range (0, 0.10]")

    if MAX_CONCURRENT < 1:
        errs.append("MAX_CONCURRENT must be at least 1")

    implied = RISK_PER_TRADE * MAX_CONCURRENT
    if implied > MAX_PORTFOLIO_RISK + 1e-9:
        errs.append(
            f"RISK_PER_TRADE x MAX_CONCURRENT = {implied:.1%} exceeds "
            f"MAX_PORTFOLIO_RISK {MAX_PORTFOLIO_RISK:.1%}"
        )

    if abs(sum(TP_SPLIT) - 1.0) > 1e-6:
        errs.append(f"TP_SPLIT must sum to 1.0, got {sum(TP_SPLIT)}")

    if not SYMBOLS:
        errs.append("TRADE_SYMBOLS is empty")

    if not TIMEFRAMES:
        errs.append("TRADE_TIMEFRAMES is empty")

    if ORDER_FORCE not in ("gtc", "post_only"):
        errs.append(f"ORDER_FORCE must be gtc or post_only, got {ORDER_FORCE}")

    if HTF_BIAS_MODE not in ("ema", "structure"):
        errs.append(f"HTF_BIAS_MODE must be ema or structure, got {HTF_BIAS_MODE}")

    if HTF_BIAS_MODE == "structure" and "4H" in TIMEFRAMES:
        errs.append(
            "HTF_BIAS_MODE=structure with a 4H timeframe blocks every trade — "
            "the 1D structure trend cannot resolve on Bitget's 90-candle daily "
            "history. Use ema, or set REQUIRE_HTF=false."
        )

    return errs


def summary():
    mode = "LIVE" if (LIVE_TRADING and not DRY_RUN) else "DRY RUN (no orders sent)"
    return "\n".join([
        f"  Mode            : {mode}",
        f"  Symbols         : {', '.join(SYMBOLS)}",
        f"  Timeframes      : {', '.join(TIMEFRAMES)}",
        f"  Risk per trade  : {RISK_PER_TRADE:.1%}",
        f"  Max concurrent  : {MAX_CONCURRENT}  (portfolio cap {MAX_PORTFOLIO_RISK:.1%})",
        f"  Daily loss stop : {MAX_DAILY_LOSS:.1%}",
        f"  Leverage        : {LEVERAGE}x {MARGIN_MODE}",
        f"  Entry           : resting limit at zone edge, {ORDER_FORCE}, "
        f"TTL {ORDER_TTL_HOURS:.0f}h",
        f"  TP ladder       : {TP_SPLIT[0]:.0%} / {TP_SPLIT[1]:.0%} / {TP_SPLIT[2]:.0%}"
        f"{'  then SL to BE' if MOVE_SL_TO_BE else ''}",
        f"  Min R:R         : {MIN_RR}",
        f"  HTF filter      : {'on' if REQUIRE_HTF else 'off'}"
        f" ({HTF_BIAS_MODE}"
        f"{f', ema{HTF_EMA_LEN}' if HTF_BIAS_MODE == 'ema' else ''})",
        f"  Scan interval   : {CHECK_INTERVAL}s",
    ])
