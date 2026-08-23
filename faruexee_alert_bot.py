# =============================================================
#   FARUEXEE ALERT BOT  —  v2
#   Strategy: FARUEXEE [5m-4H]
#   Exchange Data: Bitget Futures (Public API — no keys needed)
#   Alerts: Discord Webhook
# =============================================================
#
#   WHAT THIS BOT DOES:
#   ─────────────────────────────────────────────────────────
#   1. Fetches live candles from Bitget public API (no API key)
#   2. Runs FARUEXEE indicator to find Potential Entry Zones
#   3. Sends a Discord alert ONCE per new zone with:
#      → Direction (Long / Short)
#      → Entry price
#      → Stop Loss
#      → TP1, TP2, TP3 (all available TPs)
#      → Risk/Reward ratio
#   4. Tracks alerted zones in a state file — no duplicate alerts
#   5. Repeats every CHECK_INTERVAL seconds
#
#   HOW TO SET UP DISCORD WEBHOOK:
#   ─────────────────────────────────────────────────────────
#   1. Open your Discord server
#   2. Go to the channel you want alerts in
#   3. Click the gear icon (Edit Channel) → Integrations → Webhooks
#   4. Click "New Webhook" → Copy Webhook URL
#   5. Paste it below as DISCORD_WEBHOOK
# =============================================================

import requests
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from keep_alive import keep_alive


# =============================================================
#   🔔  DISCORD WEBHOOK — PASTE YOUR URL HERE
# =============================================================

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")


# =============================================================
#   ⚙️  BOT SETTINGS — CUSTOMIZE THESE
# =============================================================

BASE_URL       = "https://api.bitget.com"

SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT",
    "ADAUSDT","AVAXUSDT","LINKUSDT","LTCUSDT","DOTUSDT",
    "DOGEUSDT","POLUSDT","SUIUSDT","TRXUSDT","UNIUSDT",
    "XAUUSDT","XAGUSDT","XPTUSDT","CLUSDT"
]

TIMEFRAMES = ["30m", "1H", "4H", "1D"]   # Timeframes to scan

CHECK_INTERVAL = 60    # Seconds between each bot run

# ── Indicator Settings ──
LOOKBACK          = 20     # Swing Lookback bars
IMPULSE_STRENGTH  = 1.5    # Impulse Strength × avg body

TREND_STABILITY   = 2      # Fix H — opposing pivots needed before trend flips
VOLUME_MULT       = 1.5    # Fix J — impulse volume must be ≥ N × 20-bar avg volume
USE_BASE_CANDLE   = True   # Fix I — use origin block candle as zone (not impulse bar)
USE_ATR_SL        = True   # Fix E — ATR-based SL (adapts to volatility)
ATR_LEN           = 14     # Fix E — ATR period
ATR_MULT          = 0.5    # Fix E — ATR SL multiplier
SL_BUFFER         = 0.1    # Fallback SL buffer × zone height (matches Pine default)
MIN_RR            = 1.5    # Fix D — reject zones where TP1 < N × SL distance
FIRE_ON_2ND_TEST  = False  # Fire immediately on the first tap (not 2nd test)
REQUIRE_REJECTION = False  # Any wick into the zone counts — no waiting for close back outside
USE_HTF_FILTER    = True   # Fix F — only alert when HTF trend agrees
USE_FVG_FILTER    = False  # Pine default — showImb=false; FVG filter off

HTF_MAP = {                # Fix F — which HTF to check per scanning timeframe
    "30m": "1H",
    "1H":  "4H",
    "4H":  "1D",
    "1D":  None,           # No HTF for Daily — no filter applied
}

TP_MULTI          = 2.0    # TP Fallback multiplier (when no opposing zone found)

# ── SMC CHoCH Confluence Filter ──
# Disabled by default — bot mirrors the Pine indicator exactly:
# new zone alerts + tap alerts on Pine's LONG/SHORT ENTRY conditions only.
USE_SMC_CHOCH_FILTER = False
SMC_CHOCH_TIMEFRAME  = "4H"    # Timeframe to compute CHoCH state on
SMC_STRUCT_LOOKBACK  = 10      # Pivot lookback bars (matches Pine Script default)
SMC_BODY_BREAK       = True    # Use close price for structure break (body candle)

# State file — tracks which zones have already been alerted
STATE_FILE = "alert_state.json"


# =============================================================
#   📡  BITGET PUBLIC API — CANDLE FETCH (NO AUTH NEEDED)
# =============================================================

def get_candles(symbol, granularity="1H", limit=300):
    """
    Fetch OHLCV candles from Bitget Futures public API.
    No API key required.
    Returns list oldest → newest: [timestamp, open, high, low, close, vol]
    """
    path = (
        f"/api/v2/mix/market/candles"
        f"?symbol={symbol}&productType=USDT-FUTURES"
        f"&granularity={granularity}&limit={limit}"
    )
    try:
        resp   = requests.get(BASE_URL + path, timeout=10)
        result = resp.json()
        data   = result.get("data") or []
        return data
    except Exception as e:
        print(f"  [ERROR] Candle fetch failed for {symbol} {granularity}: {e}")
        return []


# =============================================================
#   📊  FARUEXEE INDICATOR — UPGRADED (Fixes A–J applied)
#
#   Only detects Potential Entry Zones:
#   ★ Demand: bullImpulse + trend==1  + bullGap  → Long alert
#   ★ Supply: bearImpulse + trend==-1 + bearGap  → Short alert
#
#   Upgrades vs original:
#   Fix C — avgBody uses prior-bar average (not current bar)
#   Fix D — minimum R:R filter on TP1
#   Fix E — ATR-based SL
#   Fix F — HTF trend agreement filter
#   Fix G — fire alert on 2nd zone tap, not 1st
#   Fix H — trend stability gate (N opposing pivots to flip)
#   Fix I — origin block (base candle) as zone, not impulse bar
#   Fix J — volume confirmation on impulse candle
# =============================================================

def find_pivot_highs(highs, lookback):
    """Exact match of Pine Script: ta.pivothigh(high, lookback, lookback)"""
    n    = len(highs)
    fire = [None] * n
    for i in range(lookback * 2, n):
        center_idx = i - lookback
        center_val = highs[center_idx]
        window     = highs[i - 2*lookback : i + 1]
        if all(center_val > window[j] for j in range(len(window)) if j != lookback):
            fire[i] = center_val
    return fire


def find_pivot_lows(lows, lookback):
    """Exact match of Pine Script: ta.pivotlow(low, lookback, lookback)"""
    n    = len(lows)
    fire = [None] * n
    for i in range(lookback * 2, n):
        center_idx = i - lookback
        center_val = lows[center_idx]
        window     = lows[i - 2*lookback : i + 1]
        if all(center_val < window[j] for j in range(len(window)) if j != lookback):
            fire[i] = center_val
    return fire


def calc_trends(pivot_highs_fire, pivot_lows_fire, n, stability=1):
    """
    Fix H: trend flips only after N consecutive bull or bear pivots.
    stability=1 → original single-pivot behaviour.
    stability=2 → requires 2 consecutive opposing pivots (less noise).
    """
    trends      = [0] * n
    curr_trend  = 0
    bull_count  = 0
    bear_count  = 0
    ph1 = ph2 = pl1 = pl2 = None

    for i in range(n):
        if pivot_highs_fire[i] is not None:
            ph2, ph1 = ph1, pivot_highs_fire[i]
        if pivot_lows_fire[i] is not None:
            pl2, pl1 = pl1, pivot_lows_fire[i]

        bull_pivot = (
            (pivot_highs_fire[i] is not None and ph2 is not None and pivot_highs_fire[i] > ph2) or
            (pivot_lows_fire[i]  is not None and pl2 is not None and pivot_lows_fire[i]  > pl2)
        )
        bear_pivot = (
            (pivot_highs_fire[i] is not None and ph2 is not None and pivot_highs_fire[i] < ph2) or
            (pivot_lows_fire[i]  is not None and pl2 is not None and pivot_lows_fire[i]  < pl2)
        )

        # Pine Script uses two SEPARATE if-statements — bear runs after bull on same bar
        if bull_pivot:
            bull_count += 1
            bear_count  = 0
        if bear_pivot:
            bear_count += 1
            bull_count  = 0

        if bull_count >= stability and curr_trend != 1:
            curr_trend = 1
        if bear_count >= stability and curr_trend != -1:
            curr_trend = -1

        trends[i] = curr_trend

    return trends


def calc_atr(highs, lows, closes, length):
    """
    Fix E: Wilder's ATR — same as ta.atr(length) in Pine Script.
    Returns list of ATR values aligned to candle index.
    """
    n   = len(closes)
    tr  = [0.0] * n
    atr = [0.0] * n

    for i in range(1, n):
        tr[i] = max(
            highs[i]  - lows[i],
            abs(highs[i]  - closes[i - 1]),
            abs(lows[i]   - closes[i - 1]),
        )

    if n > length:
        atr[length] = sum(tr[1:length + 1]) / length
        for i in range(length + 1, n):
            atr[i] = (atr[i - 1] * (length - 1) + tr[i]) / length

    return atr


def find_base_candle(opens, highs, lows, closes, impulse_bar, side):
    """
    Fix I: scan back up to 5 bars before the impulse to find the
    last opposite-colour candle (origin/base block).
    Bull impulse → last bearish/doji candle (close ≤ open)
    Bear impulse → last bullish/doji candle (close ≥ open)
    Falls back to bar immediately before impulse if none found.
    """
    for j in range(2, 7):        # bars [i-2 … i-6]
        idx = impulse_bar - j
        if idx < 0:
            break
        if side == "bull" and closes[idx] <= opens[idx]:
            return highs[idx], lows[idx]
        if side == "bear" and closes[idx] >= opens[idx]:
            return highs[idx], lows[idx]
    # fallback: candle directly before impulse
    fb = impulse_bar - 1
    return highs[fb], lows[fb]


def detect_inside_fvg(highs, lows, closes, n, recent_bars=100):
    """
    Fix B: check whether the most recent close is inside an active recent FVG.
    Bull FVG: low[i] > high[i-2]  → gap_top = low[i],   gap_bot = high[i-2]
    Bear FVG: high[i] < low[i-2]  → gap_top = low[i-2], gap_bot = high[i]
    A FVG is filled (removed) when price returns to its level.
    Returns True if last close sits inside any FVG created within recent_bars.
    """
    bull_fvgs = []   # (top, bot, created_bar)
    bear_fvgs = []

    for i in range(2, n):
        # Remove FVGs that have been filled
        bull_fvgs = [(t, b, c) for t, b, c in bull_fvgs
                     if not (i > c and lows[i] <= t)]
        bear_fvgs = [(t, b, c) for t, b, c in bear_fvgs
                     if not (i > c and highs[i] >= b)]

        # Detect new FVGs
        if lows[i] > highs[i - 2]:            # bull imbalance
            gap_top, gap_bot = lows[i], highs[i - 2]
            if gap_top > gap_bot:
                bull_fvgs.append((gap_top, gap_bot, i))
        if highs[i] < lows[i - 2]:            # bear imbalance
            gap_top, gap_bot = lows[i - 2], highs[i]
            if gap_top > gap_bot:
                bear_fvgs.append((gap_top, gap_bot, i))

    last_close = closes[n - 1]
    for top, bot, created in bull_fvgs:
        if (n - 1 - created) < recent_bars and bot <= last_close <= top:
            return True
    for top, bot, created in bear_fvgs:
        if (n - 1 - created) < recent_bars and bot <= last_close <= top:
            return True
    return False


def compute_htf_trend(candles):
    """
    Fix F: compute trend state on HTF candles.
    Returns 1 (up), -1 (down), or 0 (neutral/unknown).
    """
    if not candles or len(candles) < LOOKBACK * 2 + 10:
        return 0
    closed = candles[:-1]       # drop live candle
    highs  = [float(c[2]) for c in closed]
    lows   = [float(c[3]) for c in closed]
    n      = len(closed)
    ph_f   = find_pivot_highs(highs, LOOKBACK)
    pl_f   = find_pivot_lows(lows,   LOOKBACK)
    trends = calc_trends(ph_f, pl_f, n, stability=TREND_STABILITY)
    return trends[-1]


def detect_smc_structure(candles, lookback=10, body_break=True):
    """
    Replicates LudoGH68's SMC Structures Pine Script — BOS/CHoCH detection.

    Bar-by-bar simulation of the structure state machine:
      direction 0 = neutral (initial)
      direction 1 = bearish (last flip was a broken low)
      direction 2 = bullish (last flip was a broken high)

    A CHoCH is a direction FLIP; a BOS is a continuation break.

    Returns: (direction_str, choch_ts, origin_bar_ts)
      direction_str : "bullish" | "bearish" | "neutral"
      choch_ts      : timestamp (ms, int) of the last CHoCH that set the
                      current direction, or None if still neutral.
      origin_bar_ts : timestamp of the pivot bar that CAUSED the CHoCH
                      (swing low for bullish CHoCH, swing high for bearish).
                      This is the origin block — zones near this bar are the
                      high-probability pullback targets.
    """
    if not candles or len(candles) < lookback + 10:
        return "neutral", None, None

    closed = candles[:-1]           # drop live candle
    if len(closed) < lookback + 10:
        return "neutral", None, None

    highs  = [float(c[2]) for c in closed]
    lows   = [float(c[3]) for c in closed]
    closes = [float(c[4]) for c in closed]
    ts     = [int(c[0])   for c in closed]
    n      = len(closed)

    def find_pivot_high_bar(bar_i):
        """Find the anchor bar for a new high after a bullish break.
        Matches Pine's get_structure_highest_bar within a lookback window."""
        lb      = min(lookback, bar_i + 1)
        max_i   = bar_i
        max_val = -float("inf")
        for j in range(bar_i - lb + 1, bar_i + 1):
            if highs[j] > max_val:
                max_val = highs[j]
                max_i   = j
        best_i = None
        for i in range(0, lb - 2):
            b0 = bar_i - i
            b1 = bar_i - i - 1
            b2 = bar_i - i - 2
            if b2 < 0:
                break
            if highs[b1] > highs[b2] and highs[b0] <= highs[b1]:
                if b1 >= max_i:
                    best_i = b1
        return best_i if best_i is not None else max_i

    def find_pivot_low_bar(bar_i):
        lb      = min(lookback, bar_i + 1)
        min_i   = bar_i
        min_val = float("inf")
        for j in range(bar_i - lb + 1, bar_i + 1):
            if lows[j] < min_val:
                min_val = lows[j]
                min_i   = j
        best_i = None
        for i in range(0, lb - 2):
            b0 = bar_i - i
            b1 = bar_i - i - 1
            b2 = bar_i - i - 2
            if b2 < 0:
                break
            if lows[b1] < lows[b2] and lows[b0] >= lows[b1]:
                if b1 <= min_i:
                    best_i = b1
        return best_i if best_i is not None else min_i

    # ── State ──
    direction      = 0
    struct_high    = highs[0]
    struct_low     = lows[0]
    high_idx       = 0
    low_idx        = 0
    last_choch_ts  = None
    origin_bar_ts  = None       # timestamp of the pivot that caused the last CHoCH

    for i in range(4, n):
        # Break prices — body (close) or wick (high/low)
        low_break_i   = closes[i]     if body_break else lows[i]
        low_break_1   = closes[i - 1] if body_break else lows[i - 1]
        low_break_2   = closes[i - 2] if body_break else lows[i - 2]
        low_break_3   = closes[i - 3] if body_break else lows[i - 3]
        high_break_i  = closes[i]     if body_break else highs[i]
        high_break_1  = closes[i - 1] if body_break else highs[i - 1]
        high_break_2  = closes[i - 2] if body_break else highs[i - 2]
        high_break_3  = closes[i - 3] if body_break else highs[i - 3]

        fresh_low_broken = (
            low_break_i < struct_low
            and low_break_1 >= struct_low
            and low_break_2 >= struct_low
            and low_break_3 >= struct_low
            and (i - 1) > low_idx
            and (i - 2) > low_idx
            and (i - 3) > low_idx
        )
        continuation_low_broken = direction == 2 and low_break_i < struct_low
        is_low_broken           = fresh_low_broken or continuation_low_broken

        fresh_high_broken = (
            high_break_i > struct_high
            and high_break_1 <= struct_high
            and high_break_2 <= struct_high
            and high_break_3 <= struct_high
            and (i - 1) > high_idx
            and (i - 2) > high_idx
            and (i - 3) > high_idx
        )
        continuation_high_broken = direction == 1 and high_break_i > struct_high
        is_high_broken           = fresh_high_broken or continuation_high_broken

        if is_low_broken:
            # BOS if already bearish, CHoCH if flipping direction
            if direction != 1:
                last_choch_ts = ts[i]
                # Origin of a bearish CHoCH = swing HIGH that price fell from
                new_high_idx  = find_pivot_high_bar(i)
                origin_bar_ts = ts[new_high_idx]
            direction   = 1
            high_idx    = find_pivot_high_bar(i)
            struct_high = highs[high_idx]
            low_idx     = i
            struct_low  = lows[i]

        elif is_high_broken:
            if direction != 2:
                last_choch_ts = ts[i]
                # Origin of a bullish CHoCH = swing LOW that price bounced from
                new_low_idx   = find_pivot_low_bar(i)
                origin_bar_ts = ts[new_low_idx]
            direction   = 2
            high_idx    = i
            struct_high = highs[i]
            low_idx     = find_pivot_low_bar(i)
            struct_low  = lows[low_idx]

        else:
            # Extend running levels while trend runs
            if highs[i] > struct_high and direction in (0, 2):
                struct_high = highs[i]
                high_idx    = i
            elif lows[i] < struct_low and direction in (0, 1):
                struct_low  = lows[i]
                low_idx     = i

    if direction == 2:
        return "bullish", last_choch_ts, origin_bar_ts
    if direction == 1:
        return "bearish", last_choch_ts, origin_bar_ts
    return "neutral", None, None


def run_indicator(candles):
    """
    FARUEXEE indicator — IMMEDIATE TAP ALERT MODE.

    Tap detection rules:
    • Fires the MOMENT price touches the zone — no waiting for candle close
    • Fires on first tap (FIRE_ON_2ND_TEST=False)
    • No rejection-close requirement
    • Live candle is checked against all active zones — alert fires on touch
    • Deduplication happens in run_bot() via _tapAlerted_{zone_id} state key
      — guarantees only ONE alert per zone

    Returns: (active_zones, tapped_zones, inside_fvg)
    """
    live_candle = candles[-1]       # save live candle for immediate tap detection
    candles     = candles[:-1]      # closed bars only for zone building

    if len(candles) < LOOKBACK * 2 + 10:
        return [], [], False

    opens   = [float(c[1]) for c in candles]
    highs   = [float(c[2]) for c in candles]
    lows    = [float(c[3]) for c in candles]
    closes  = [float(c[4]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    ts      = [c[0]        for c in candles]
    n       = len(candles)

    # ── Fix C: avgBody[1] — 20-bar SMA ending at PRIOR bar ──
    avg_bodies = []
    for i in range(n):
        start = max(0, i - 19)
        avg_bodies.append(
            sum(abs(closes[j] - opens[j]) for j in range(start, i + 1)) / (i - start + 1)
        )

    # ── Fix J: volume SMA[1] — 20-bar volume SMA ending at PRIOR bar ──
    vol_sma = []
    for i in range(n):
        start = max(0, i - 19)
        vol_sma.append(sum(volumes[start:i + 1]) / (i - start + 1))

    # ── Fix E: ATR ──
    atr = calc_atr(highs, lows, closes, ATR_LEN)

    # ── Trend with Fix H stability gate ──
    pivot_highs_fire = find_pivot_highs(highs, LOOKBACK)
    pivot_lows_fire  = find_pivot_lows(lows,   LOOKBACK)
    trends           = calc_trends(pivot_highs_fire, pivot_lows_fire, n,
                                   stability=TREND_STABILITY)

    demand_zones = []
    supply_zones = []
    demand_reg   = []
    supply_reg   = []
    tapped_cands = []     # candidate tap alerts — post-filtered to closest per side

    for i in range(max(7, LOOKBACK * 2 + 1), n):
        avg_body      = avg_bodies[i - 1]   # Fix C: prior bar average
        prior_vol_sma = vol_sma[i - 1]      # Fix J: prior bar volume SMA
        trend         = trends[i]
        o = opens[i]; c = closes[i]

        vol_ok       = volumes[i] >= prior_vol_sma * VOLUME_MULT
        bull_impulse = (c > o) and ((c - o) >= avg_body * IMPULSE_STRENGTH) and vol_ok
        bear_impulse = (c < o) and ((o - c) >= avg_body * IMPULSE_STRENGTH) and vol_ok
        bull_gap     = lows[i]  > highs[i - 2]
        bear_gap     = highs[i] < lows[i - 2]

        # ── Fix G: demand tap tracking (wasIn prevents double-counting) ──
        new_demand = []
        for z in demand_zones:
            was_in  = z.get("was_in", False)
            is_in   = i > z["bar"] and lows[i] <= z["top"]
            new_tap = is_in and not was_in

            if new_tap:
                new_taps = z.get("taps", 0) + 1
                if FIRE_ON_2ND_TEST and new_taps < 2:
                    # 1st tap — Pine keeps zone alive (tested), no alert
                    new_demand.append({**z, "taps": new_taps, "was_in": True})
                else:
                    # Fire alert on 2nd tap (or 1st if FIRE_ON_2ND_TEST=False).
                    # Pine entry requires rejection close: close > zone_top.
                    rejection_ok = (not REQUIRE_REJECTION) or (closes[i] > z["top"])
                    if i == n - 1 and rejection_ok:
                        tapped_cands.append({
                            "zone_id":    f"demand_{z['ts']}_{round(z['top'], 6)}",
                            "side":       "buy",
                            "zone_top":   z["top"],
                            "zone_bot":   z["bot"],
                            "entry":      z["entry"],
                            "sl":         z["sl"],
                            "tp1":        z["tp1"],
                            "tp2":        z["tp2"],
                            "tp3":        z["tp3"],
                            "created_ts": z["ts"],
                        })
                    # Pine: on 2nd tap the zone is deleted. On 1st tap w/o rejection,
                    # keep zone alive so a future rejection can fire.
                    if FIRE_ON_2ND_TEST:
                        # 2nd tap — always consume zone (Pine deletes)
                        pass
                    else:
                        # 1st tap mode — consume regardless
                        pass
            elif is_in:
                new_demand.append({**z, "was_in": True})
            else:
                new_demand.append({**z, "was_in": False})
        demand_zones = new_demand

        # ── Fix G: supply tap tracking ──
        new_supply = []
        for z in supply_zones:
            was_in  = z.get("was_in", False)
            is_in   = i > z["bar"] and highs[i] >= z["bot"]
            new_tap = is_in and not was_in

            if new_tap:
                new_taps = z.get("taps", 0) + 1
                if FIRE_ON_2ND_TEST and new_taps < 2:
                    # 1st tap — Pine keeps zone alive (tested), no alert
                    new_supply.append({**z, "taps": new_taps, "was_in": True})
                else:
                    # 2nd tap fire. Pine entry requires rejection close: close < zone_bot.
                    rejection_ok = (not REQUIRE_REJECTION) or (closes[i] < z["bot"])
                    if i == n - 1 and rejection_ok:
                        tapped_cands.append({
                            "zone_id":    f"supply_{z['ts']}_{round(z['bot'], 6)}",
                            "side":       "sell",
                            "zone_top":   z["top"],
                            "zone_bot":   z["bot"],
                            "entry":      z["entry"],
                            "sl":         z["sl"],
                            "tp1":        z["tp1"],
                            "tp2":        z["tp2"],
                            "tp3":        z["tp3"],
                            "created_ts": z["ts"],
                        })
            elif is_in:
                new_supply.append({**z, "was_in": True})
            else:
                new_supply.append({**z, "was_in": False})
        supply_zones = new_supply

        demand_reg = [z for z in demand_reg
                      if not (i > z["bar"] and lows[i] <= z["top"])]
        supply_reg = [z for z in supply_reg
                      if not (i > z["bar"] and highs[i] >= z["bot"])]

        # ── Zone creation — NO HTF filter (Pine Script draws zones unconditionally) ──
        if bull_impulse and trend == 1:
            z_top, z_bot = find_base_candle(opens, highs, lows, closes, i, "bull") \
                           if USE_BASE_CANDLE else (highs[i - 1], lows[i - 1])
            min_h = atr[i] * 0.05 if atr[i] > 0 else 0
            if z_top > z_bot and (z_top - z_bot) > min_h:
                sl_px = (z_bot - atr[i] * ATR_MULT) if USE_ATR_SL \
                        else (z_bot - (z_top - z_bot) * SL_BUFFER)
                entry = z_top
                if bull_gap:
                    tp_cands = sorted(
                        [z["bot"] for z in supply_zones if z["bot"] > entry] +
                        [z["bot"] for z in supply_reg   if z["bot"] > entry]
                    )
                    tp1 = tp_cands[0] if tp_cands else entry + (entry - sl_px) * TP_MULTI
                    tp2 = tp_cands[1] if len(tp_cands) >= 2 else None
                    tp3 = tp_cands[2] if len(tp_cands) >= 3 else None
                    # Pine draws all zones — no MIN_RR check here. Enforced at alert time.
                    demand_zones.append({
                        "bar": i, "ts": ts[i], "top": z_top, "bot": z_bot,
                        "entry": entry, "sl": sl_px,
                        "tp1": tp1, "tp2": tp2, "tp3": tp3,
                        "taps": 0, "was_in": False,
                    })
                else:
                    demand_reg.append({"bar": i, "top": z_top, "bot": z_bot})

        if bear_impulse and trend == -1:
            z_top, z_bot = find_base_candle(opens, highs, lows, closes, i, "bear") \
                           if USE_BASE_CANDLE else (highs[i - 1], lows[i - 1])
            min_h = atr[i] * 0.05 if atr[i] > 0 else 0
            if z_top > z_bot and (z_top - z_bot) > min_h:
                sl_px = (z_top + atr[i] * ATR_MULT) if USE_ATR_SL \
                        else (z_top + (z_top - z_bot) * SL_BUFFER)
                entry = z_bot
                if bear_gap:
                    tp_cands = sorted(
                        [z["top"] for z in demand_zones if z["top"] < entry] +
                        [z["top"] for z in demand_reg   if z["top"] < entry],
                        reverse=True
                    )
                    tp1 = tp_cands[0] if tp_cands else entry - (sl_px - entry) * TP_MULTI
                    tp2 = tp_cands[1] if len(tp_cands) >= 2 else None
                    tp3 = tp_cands[2] if len(tp_cands) >= 3 else None
                    # Pine draws all zones — no MIN_RR check here. Enforced at alert time.
                    supply_zones.append({
                        "bar": i, "ts": ts[i], "top": z_top, "bot": z_bot,
                        "entry": entry, "sl": sl_px,
                        "tp1": tp1, "tp2": tp2, "tp3": tp3,
                        "taps": 0, "was_in": False,
                    })
                else:
                    supply_reg.append({"bar": i, "top": z_top, "bot": z_bot})

    # ── Collect active zones ──
    active_zones = []
    for z in demand_zones:
        active_zones.append({
            "zone_id":    f"demand_{z['ts']}_{round(z['top'], 6)}",
            "side":       "buy",
            "zone_top":   z["top"],
            "zone_bot":   z["bot"],
            "entry":      z["entry"],
            "sl":         z["sl"],
            "tp1":        z["tp1"],
            "tp2":        z["tp2"],
            "tp3":        z["tp3"],
            "created_ts": z["ts"],
        })
    for z in supply_zones:
        active_zones.append({
            "zone_id":    f"supply_{z['ts']}_{round(z['bot'], 6)}",
            "side":       "sell",
            "zone_top":   z["top"],
            "zone_bot":   z["bot"],
            "entry":      z["entry"],
            "sl":         z["sl"],
            "tp1":        z["tp1"],
            "tp2":        z["tp2"],
            "tp3":        z["tp3"],
            "created_ts": z["ts"],
        })

    # ── Live candle tap detection (Pine entry signal criteria) ──
    # Fires when live candle wicks into zone AND has closed back outside it.
    # In 2nd-test mode, zone must already have taps >= 1 (been tested).
    live_high  = float(live_candle[2])
    live_low   = float(live_candle[3])
    live_close = float(live_candle[4])

    for z in demand_zones:
        wick_in      = live_low <= z["top"]
        rejection_ok = (not REQUIRE_REJECTION) or (live_close > z["top"])
        if not (wick_in and rejection_ok):
            continue
        if FIRE_ON_2ND_TEST and z.get("taps", 0) < 1:
            continue
        tapped_cands.append({
            "zone_id":    f"demand_{z['ts']}_{round(z['top'], 6)}",
            "side":       "buy",
            "zone_top":   z["top"],
            "zone_bot":   z["bot"],
            "entry":      z["entry"],
            "sl":         z["sl"],
            "tp1":        z["tp1"],
            "tp2":        z["tp2"],
            "tp3":        z["tp3"],
            "created_ts": z["ts"],
        })

    for z in supply_zones:
        wick_in      = live_high >= z["bot"]
        rejection_ok = (not REQUIRE_REJECTION) or (live_close < z["bot"])
        if not (wick_in and rejection_ok):
            continue
        if FIRE_ON_2ND_TEST and z.get("taps", 0) < 1:
            continue
        tapped_cands.append({
            "zone_id":    f"supply_{z['ts']}_{round(z['bot'], 6)}",
            "side":       "sell",
            "zone_top":   z["top"],
            "zone_bot":   z["bot"],
            "entry":      z["entry"],
            "sl":         z["sl"],
            "tp1":        z["tp1"],
            "tp2":        z["tp2"],
            "tp3":        z["tp3"],
            "created_ts": z["ts"],
        })

    # Fix A: Pine fires an entry signal for only the CLOSEST qualifying zone
    # per side — highest demand top, lowest supply bot. Reduce to that.
    demand_cands = [z for z in tapped_cands if z["side"] == "buy"]
    supply_cands = [z for z in tapped_cands if z["side"] == "sell"]
    tapped_zones = []
    if demand_cands:
        tapped_zones.append(max(demand_cands, key=lambda z: z["zone_top"]))
    if supply_cands:
        tapped_zones.append(min(supply_cands, key=lambda z: z["zone_bot"]))

    # ── Fix B: FVG detection ──
    inside_fvg = detect_inside_fvg(highs, lows, closes, n)

    return active_zones, tapped_zones, inside_fvg


# =============================================================
#   🟢🔴  BOT STATUS ALERTS (Online / Offline)
# =============================================================

def send_status_alert(online: bool):
    """Send a green Online or red Offline embed to Discord."""
    if online:
        embed = {
            "title":     "🟢  Bot is Online",
            "description": (
                f"FARUEXEE Alert Bot has started successfully.\n"
                f"Scanning **{len(SYMBOLS)} pairs** across **{len(TIMEFRAMES)} timeframes** "
                f"every **{CHECK_INTERVAL}s**."
            ),
            "color":     0x00C853,
            "footer":    {"text": "FARUEXEE Alert Bot"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    else:
        embed = {
            "title":       "🔴  Bot is Offline",
            "description": "FARUEXEE Alert Bot has disconnected or crashed. It will restart automatically on Render.",
            "color":       0xD50000,
            "footer":      {"text": "FARUEXEE Alert Bot"},
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        }

    try:
        requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        print(f"  [ERROR] Status alert failed: {e}")


def handle_shutdown(signum, frame):
    """Catch shutdown signals and send Offline alert before exiting."""
    print("\n  [SHUTDOWN] Signal received — sending offline alert...")
    send_status_alert(online=False)
    sys.exit(0)


# =============================================================
#   🔔  DISCORD ALERT
# =============================================================

def send_discord_alert(symbol, timeframe, zone):
    """Send a formatted Discord embed for a new Potential Entry Zone."""

    side      = zone["side"]
    zone_top  = zone["zone_top"]
    zone_bot  = zone["zone_bot"]
    entry     = zone["entry"]
    sl        = zone["sl"]
    tp1       = zone["tp1"]
    tp2       = zone["tp2"]
    tp3       = zone["tp3"]

    is_long    = side == "buy"
    color      = 0x00C853 if is_long else 0xD50000   # green / red
    direction  = "LONG  —  Demand Zone" if is_long else "SHORT  —  Supply Zone"
    arrow      = "⬆️" if is_long else "⬇️"

    sl_dist = abs(entry - sl)
    rr1     = round(abs(tp1 - entry) / sl_dist, 2) if sl_dist > 0 else "—"

    # ── Build TP fields ──
    tp_lines = [f"`{round(tp1, 6)}`  *(1:{rr1}R)*"]

    if tp2 is not None:
        rr2 = round(abs(tp2 - entry) / sl_dist, 2) if sl_dist > 0 else "—"
        tp_lines.append(f"`{round(tp2, 6)}`  *(1:{rr2}R)*")

    if tp3 is not None:
        rr3 = round(abs(tp3 - entry) / sl_dist, 2) if sl_dist > 0 else "—"
        tp_lines.append(f"`{round(tp3, 6)}`  *(1:{rr3}R)*")

    tp_note = ""
    if tp2 is None and tp3 is None:
        tp_note = "\n*TP1 is fallback (no opposing zones found)*"

    zone_range = f"`{round(zone_bot, 6)}  —  {round(zone_top, 6)}`"

    fields = [
        {
            "name":   "Direction",
            "value":  f"{arrow}  **{direction}**",
            "inline": False
        },
        {
            "name":   "Zone (Entry Range)",
            "value":  zone_range,
            "inline": True
        },
        {
            "name":   "Stop Loss",
            "value":  f"`{round(sl, 6)}`",
            "inline": True
        },
        {
            "name":   "\u200b",   # spacer
            "value":  "\u200b",
            "inline": True
        },
        {
            "name":   f"Take Profit  ({len(tp_lines)} target{'s' if len(tp_lines) > 1 else ''})",
            "value":  "\n".join(tp_lines) + tp_note,
            "inline": False
        },
    ]

    embed = {
        "title":       f"⚡  FARUEXEE Signal  |  {symbol}  {timeframe}",
        "color":       color,
        "fields":      fields,
        "footer":      {"text": "FARUEXEE Alert Bot"},
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }

    payload = {"embeds": [embed]}

    try:
        resp = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if resp.status_code == 204:
            return True
        else:
            print(f"  [ERROR] Discord webhook returned {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        print(f"  [ERROR] Discord send failed: {e}")
        return False


def send_discord_tap_alert(symbol, timeframe, zone_info):
    """Send a Discord alert when price taps (enters) a tracked zone."""

    side      = zone_info["side"]
    zone_top  = zone_info.get("zone_top", zone_info["entry"])
    zone_bot  = zone_info.get("zone_bot", zone_info["entry"])
    entry     = zone_info["entry"]
    sl        = zone_info["sl"]
    tp1       = zone_info["tp1"]
    tp2       = zone_info.get("tp2")
    tp3       = zone_info.get("tp3")

    is_long   = side == "buy"
    direction = "LONG  —  Demand Zone" if is_long else "SHORT  —  Supply Zone"
    arrow     = "⬆️" if is_long else "⬇️"

    sl_dist = abs(entry - sl)
    rr1     = round(abs(tp1 - entry) / sl_dist, 2) if sl_dist > 0 else "—"

    tp_lines = [f"`{round(tp1, 6)}`  *(1:{rr1}R)*"]
    if tp2 is not None:
        rr2 = round(abs(tp2 - entry) / sl_dist, 2) if sl_dist > 0 else "—"
        tp_lines.append(f"`{round(tp2, 6)}`  *(1:{rr2}R)*")
    if tp3 is not None:
        rr3 = round(abs(tp3 - entry) / sl_dist, 2) if sl_dist > 0 else "—"
        tp_lines.append(f"`{round(tp3, 6)}`  *(1:{rr3}R)*")

    zone_range = f"`{round(zone_bot, 6)}  —  {round(zone_top, 6)}`"

    fields = [
        {
            "name":   "Direction",
            "value":  f"{arrow}  **{direction}**",
            "inline": False
        },
        {
            "name":   "Zone (Entry Range)",
            "value":  zone_range,
            "inline": True
        },
        {
            "name":   "Stop Loss",
            "value":  f"`{round(sl, 6)}`",
            "inline": True
        },
        {
            "name":   "\u200b",
            "value":  "\u200b",
            "inline": True
        },
        {
            "name":   f"Take Profit  ({len(tp_lines)} target{'s' if len(tp_lines) > 1 else ''})",
            "value":  "\n".join(tp_lines),
            "inline": False
        },
        {
            "name":   "Action",
            "value":  "Price is **inside the zone** — wait for confirmation candle before entering.",
            "inline": False
        },
    ]

    embed = {
        "title":     f"🎯  Zone Tapped  |  {symbol}  {timeframe}",
        "color":     0xFF6D00,   # orange — different from zone-found (green/red)
        "fields":    fields,
        "footer":    {"text": "FARUEXEE Alert Bot"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    payload = {"embeds": [embed]}

    try:
        resp = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if resp.status_code == 204:
            return True
        else:
            print(f"  [ERROR] Tap alert webhook returned {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        print(f"  [ERROR] Tap alert send failed: {e}")
        return False


# =============================================================
#   🎯  CHoCH ORIGIN ZONE DETECTION & ALERTS
# =============================================================

# ± window around the CHoCH origin bar to consider a zone as its "origin"
ORIGIN_MATCH_WINDOW_MS = 4 * 60 * 60 * 1000   # ±4 hours


def find_origin_zone(active_zones, side, origin_bar_ts, choch_ts):
    """
    Find the zone that caused a CHoCH — i.e. the demand/supply zone that price
    bounced from to break structure.

    Rules:
    - Zone side must match the CHoCH direction (bullish CHoCH → demand)
    - Zone must have formed BEFORE the CHoCH bar
    - Zone timestamp must be within ORIGIN_MATCH_WINDOW_MS of the pivot origin bar
    - If multiple qualify, pick the one closest to the origin bar timestamp
    """
    if not origin_bar_ts:
        return None
    origin_bar_ts = int(origin_bar_ts)
    choch_ts_int  = int(choch_ts) if choch_ts else None

    candidates = []
    for z in active_zones:
        if z["side"] != side:
            continue
        z_ts = int(z.get("created_ts", 0))
        if choch_ts_int and z_ts >= choch_ts_int:
            continue                      # zone formed AFTER CHoCH — not the origin
        if abs(z_ts - origin_bar_ts) > ORIGIN_MATCH_WINDOW_MS:
            continue                      # too far from the pivot bar
        candidates.append(z)

    if not candidates:
        return None
    return min(candidates, key=lambda z: abs(int(z["created_ts"]) - origin_bar_ts))


def send_choch_origin_alert(symbol, direction, choch_ts, origins):
    """
    Discord alert fired when a new 4H CHoCH is detected AND at least one
    origin zone has been identified across the scanned timeframes.
    """
    is_bull = direction == "bullish"
    color   = 0x1976D2   # blue — distinct from green/red/orange
    arrow   = "🔷⬆️" if is_bull else "🔷⬇️"

    fields = [{
        "name":   "Structure Shift",
        "value":  f"{arrow}  **4H {direction.upper()} CHoCH detected**",
        "inline": False,
    }]

    for tf, zone in origins.items():
        if zone is None:
            continue
        side_lbl   = "Demand" if zone["side"] == "buy" else "Supply"
        zone_range = f"`{round(zone['zone_bot'], 6)}  —  {round(zone['zone_top'], 6)}`"
        fields.append({
            "name":   f"{tf} Origin {side_lbl} Zone",
            "value":  (f"{zone_range}\n"
                       f"SL: `{round(zone['sl'], 6)}`   "
                       f"TP1: `{round(zone['tp1'], 6)}`"),
            "inline": False,
        })

    fields.append({
        "name":   "What this means",
        "value":  ("This is the zone that caused the CHoCH. "
                   "High-probability pullback target — watch for price to return here."),
        "inline": False,
    })

    embed = {
        "title":     f"🎯  CHoCH Origin Zone  |  {symbol}  4H",
        "color":     color,
        "fields":    fields,
        "footer":    {"text": "FARUEXEE Alert Bot"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        resp = requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=10)
        if resp.status_code == 204:
            return True
        print(f"  [ERROR] Origin alert returned {resp.status_code}: {resp.text}")
        return False
    except Exception as e:
        print(f"  [ERROR] Origin alert send failed: {e}")
        return False


def send_origin_tap_alert(symbol, tf, zone):
    """Discord alert fired when price taps a CHoCH origin zone."""
    is_long   = zone["side"] == "buy"
    direction = "LONG  —  Origin Demand" if is_long else "SHORT  —  Origin Supply"
    arrow     = "🎯⬆️" if is_long else "🎯⬇️"

    sl_dist = abs(zone["entry"] - zone["sl"])
    rr1     = round(abs(zone["tp1"] - zone["entry"]) / sl_dist, 2) if sl_dist > 0 else "—"

    tp_lines = [f"`{round(zone['tp1'], 6)}`  *(1:{rr1}R)*"]
    if zone.get("tp2") is not None:
        rr2 = round(abs(zone["tp2"] - zone["entry"]) / sl_dist, 2) if sl_dist > 0 else "—"
        tp_lines.append(f"`{round(zone['tp2'], 6)}`  *(1:{rr2}R)*")
    if zone.get("tp3") is not None:
        rr3 = round(abs(zone["tp3"] - zone["entry"]) / sl_dist, 2) if sl_dist > 0 else "—"
        tp_lines.append(f"`{round(zone['tp3'], 6)}`  *(1:{rr3}R)*")

    zone_range = f"`{round(zone['zone_bot'], 6)}  —  {round(zone['zone_top'], 6)}`"

    fields = [
        {"name": "Direction",          "value": f"{arrow}  **{direction}**", "inline": False},
        {"name": "Zone (Entry Range)", "value": zone_range,                  "inline": True},
        {"name": "Stop Loss",          "value": f"`{round(zone['sl'], 6)}`", "inline": True},
        {"name": "​",             "value": "​",                    "inline": True},
        {"name": f"Take Profit  ({len(tp_lines)} target{'s' if len(tp_lines) > 1 else ''})",
         "value": "\n".join(tp_lines), "inline": False},
        {"name": "Setup Note",
         "value": ("**CHoCH origin zone tapped.** Price returned to the zone that "
                   "caused the 4H structure break — high-probability continuation setup."),
         "inline": False},
    ]

    embed = {
        "title":     f"🎯  Origin Zone Tapped  |  {symbol}  {tf}",
        "color":     0x1976D2,   # blue
        "fields":    fields,
        "footer":    {"text": "FARUEXEE Alert Bot"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        resp = requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=10)
        if resp.status_code == 204:
            return True
        print(f"  [ERROR] Origin tap alert returned {resp.status_code}: {resp.text}")
        return False
    except Exception as e:
        print(f"  [ERROR] Origin tap alert send failed: {e}")
        return False


# =============================================================
#   💾  STATE MANAGEMENT
# =============================================================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# =============================================================
#   🤖  MAIN BOT LOGIC
# =============================================================

_is_first_run = True   # On startup, silently record all existing zones — no alerts

def run_bot():
    global _is_first_run
    print(f"\n{'='*58}")
    print(f"  Bot Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*58}")

    state        = load_state()
    alerts_sent  = 0
    zones_found  = 0

    for symbol in SYMBOLS:
        # ── SMC CHoCH state (computed once per symbol, applied to all TFs) ──
        smc_direction   = "neutral"
        smc_choch_ts    = None
        smc_origin_ts   = None
        if USE_SMC_CHOCH_FILTER:
            smc_candles = get_candles(symbol, SMC_CHOCH_TIMEFRAME, limit=200)
            if smc_candles:
                smc_direction, smc_choch_ts, smc_origin_ts = detect_smc_structure(
                    smc_candles,
                    lookback   = SMC_STRUCT_LOOKBACK,
                    body_break = SMC_BODY_BREAK,
                )
            print(f"\n  [SMC {SMC_CHOCH_TIMEFRAME}] {symbol} → {smc_direction.upper()}"
                  + (f"  (CHoCH ts: {smc_choch_ts}, origin ts: {smc_origin_ts})"
                     if smc_choch_ts else ""))

        # ── CHoCH Origin tracking state (persisted across scans) ──
        origin_state_key = f"{symbol}_smcOrigin"
        origin_state     = state.get(origin_state_key, {})

        # New CHoCH detected? Reset origin tracking.
        if smc_choch_ts and origin_state.get("choch_ts") != smc_choch_ts:
            print(f"  [SMC] New CHoCH detected — resetting origin tracking.")
            origin_state = {
                "choch_ts":      smc_choch_ts,
                "direction":     smc_direction,
                "origin_bar_ts": smc_origin_ts,
                "origins":       {tf: None for tf in TIMEFRAMES},
                "alerted":       False,
                "tap_alerted":   {tf: False for tf in TIMEFRAMES},
            }
            state[origin_state_key] = origin_state

        for tf in TIMEFRAMES:
            key_prefix = f"{symbol}_{tf}"
            print(f"\n  Scanning {symbol} | {tf}")

            candles = get_candles(symbol, tf, limit=300)
            if not candles or len(candles) < LOOKBACK * 2 + 10:
                print(f"  [WARN] Not enough candle data — skipping.")
                continue

            current_price = float(candles[-1][4])
            print(f"  Price: {current_price}")

            # ── Fix F: HTF trend filter ──
            htf_trend = 0
            if USE_HTF_FILTER:
                htf_tf = HTF_MAP.get(tf)
                if htf_tf:
                    htf_candles = get_candles(symbol, htf_tf, limit=150)
                    htf_trend   = compute_htf_trend(htf_candles)
                    htf_label   = "UP" if htf_trend == 1 else "DOWN" if htf_trend == -1 else "NEUTRAL"
                    print(f"  HTF ({htf_tf}) trend : {htf_label}")

            active_zones, tapped_zones, inside_fvg = run_indicator(candles)
            active_zone_ids = {z["zone_id"] for z in active_zones}

            print(f"  Active zones : {len(active_zones)}")
            if tapped_zones:
                print(f"  Tapped zones : {len(tapped_zones)}")
            if inside_fvg:
                print(f"  Inside FVG   : yes (tap alerts blocked)")
            zones_found += len(active_zones)

            # ── CHoCH Origin: search for origin zone on this TF if not yet found ──
            if (USE_SMC_CHOCH_FILTER
                    and smc_direction != "neutral"
                    and origin_state.get("choch_ts")
                    and origin_state.get("origins", {}).get(tf) is None):
                needed_side = "buy" if smc_direction == "bullish" else "sell"
                origin_zone = find_origin_zone(
                    active_zones,
                    needed_side,
                    origin_state.get("origin_bar_ts"),
                    origin_state.get("choch_ts"),
                )
                if origin_zone:
                    print(f"  [CHoCH Origin] Found on {tf}: {origin_zone['zone_id'][:50]}")
                    origin_state["origins"][tf] = origin_zone
                    state[origin_state_key] = origin_state

            # ── CHoCH Origin: check if a stored origin zone is being tapped ──
            stored_origin = origin_state.get("origins", {}).get(tf)
            if (stored_origin
                    and not origin_state.get("tap_alerted", {}).get(tf, False)
                    and not _is_first_run):
                live_high = float(candles[-1][2])
                live_low  = float(candles[-1][3])
                touched   = (
                    (stored_origin["side"] == "buy"  and live_low  <= stored_origin["zone_top"]) or
                    (stored_origin["side"] == "sell" and live_high >= stored_origin["zone_bot"])
                )
                if touched:
                    print(f"\n  🎯 CHoCH Origin Zone TAPPED on {tf}! Sending alert...")
                    if send_origin_tap_alert(symbol, tf, stored_origin):
                        alerts_sent += 1
                        origin_state["tap_alerted"][tf] = True
                        state[origin_state_key] = origin_state
                    else:
                        print(f"  [ERROR] Origin tap alert failed — will retry next run.")

            # ── Clean up state records for zones no longer active ──
            stale_keys = [
                k for k, v in list(state.items())
                if k.startswith(f"{key_prefix}_")
                and "_tapAlerted_" not in k
                and v.get("zone_id") not in active_zone_ids
            ]
            for k in stale_keys:
                zone_id = state[k].get("zone_id", "")
                print(f"  Zone expired — removing: {zone_id[:50]}...")
                del state[k]
                tap_key = f"{key_prefix}_tapAlerted_{zone_id}"
                if tap_key in state:
                    del state[tap_key]

            if not _is_first_run:
                # ── Tap alerts — closed bar, rejection close, HTF + FVG + SMC filtered ──
                for zone in tapped_zones:
                    tap_key = f"{key_prefix}_tapAlerted_{zone['zone_id']}"
                    if tap_key in state:
                        continue   # already alerted for this zone tap

                    # ── SMC CHoCH confluence filter ──
                    if USE_SMC_CHOCH_FILTER:
                        if smc_direction == "neutral":
                            print(f"  Tap blocked — 4H SMC neutral: {zone['zone_id'][:40]}...")
                            continue
                        if zone["side"] == "buy"  and smc_direction != "bullish":
                            print(f"  Tap blocked — 4H SMC not bullish: {zone['zone_id'][:40]}...")
                            continue
                        if zone["side"] == "sell" and smc_direction != "bearish":
                            print(f"  Tap blocked — 4H SMC not bearish: {zone['zone_id'][:40]}...")
                            continue
                        # Zone must have formed AFTER the CHoCH
                        zone_ts = int(zone.get("created_ts", 0))
                        if smc_choch_ts and zone_ts <= int(smc_choch_ts):
                            print(f"  Tap blocked — zone predates 4H CHoCH: {zone['zone_id'][:40]}...")
                            continue

                    # Fix F: HTF filter — only alert when HTF trend agrees
                    if USE_HTF_FILTER and htf_trend != 0:
                        if zone["side"] == "buy"  and htf_trend != 1:
                            print(f"  Tap blocked — HTF not uptrend: {zone['zone_id'][:40]}...")
                            continue
                        if zone["side"] == "sell" and htf_trend != -1:
                            print(f"  Tap blocked — HTF not downtrend: {zone['zone_id'][:40]}...")
                            continue

                    # Fix B: FVG filter (Pine showImb default = false, so off by default)
                    if USE_FVG_FILTER and inside_fvg:
                        print(f"  Tap blocked — price inside FVG: {zone['zone_id'][:40]}...")
                        continue

                    # Fix D: min RR — Pine checks this on entry signal (not zone creation)
                    entry_p = zone["entry"]
                    sl_p    = zone["sl"]
                    tp1_p   = zone["tp1"]
                    sl_dist = abs(entry_p - sl_p)
                    if sl_dist > 0:
                        rr = abs(tp1_p - entry_p) / sl_dist
                        if rr < MIN_RR:
                            print(f"  Tap blocked — RR {rr:.2f} < {MIN_RR}: {zone['zone_id'][:40]}...")
                            continue

                    side = zone["side"]
                    print(f"\n  Zone Tapped! {'⬆️ LONG' if side == 'buy' else '⬇️ SHORT'}")
                    print(f"  Zone  : {round(zone['zone_bot'], 6)}  —  {round(zone['zone_top'], 6)}")
                    print(f"  SL    : {round(zone['sl'], 6)}")
                    print(f"  TP1   : {round(zone['tp1'], 6)}")

                    success = send_discord_tap_alert(symbol, tf, zone)
                    if success:
                        print(f"  Tap alert sent!")
                        alerts_sent += 1
                        state[tap_key] = {
                            "zone_id":    zone["zone_id"],
                            "alerted_at": datetime.now().isoformat(),
                        }
                    else:
                        print(f"  [ERROR] Tap alert failed — will retry next run.")

            # ── Alert for new zones ──
            for zone in active_zones:
                state_key = f"{key_prefix}_{zone['zone_id']}"

                if state_key in state:
                    print(f"  Already alerted: {zone['zone_id'][:40]}...")
                    continue

                # First run after startup — record existing zones silently, no alert
                if _is_first_run:
                    print(f"  [Startup] Recording existing zone (no alert): {zone['zone_id'][:40]}...")
                    state[state_key] = {
                        "zone_id":    zone["zone_id"],
                        "symbol":     symbol,
                        "timeframe":  tf,
                        "side":       zone["side"],
                        "entry":      zone["entry"],
                        "sl":         zone["sl"],
                        "tp1":        zone["tp1"],
                        "alerted_at": datetime.now().isoformat()
                    }
                    continue

                # ── SMC CHoCH confluence filter ──
                if USE_SMC_CHOCH_FILTER:
                    if smc_direction == "neutral":
                        print(f"  New zone blocked — 4H SMC neutral: {zone['zone_id'][:40]}...")
                        continue
                    if zone["side"] == "buy"  and smc_direction != "bullish":
                        print(f"  New zone blocked — 4H SMC not bullish: {zone['zone_id'][:40]}...")
                        continue
                    if zone["side"] == "sell" and smc_direction != "bearish":
                        print(f"  New zone blocked — 4H SMC not bearish: {zone['zone_id'][:40]}...")
                        continue
                    zone_ts = int(zone.get("created_ts", 0))
                    if smc_choch_ts and zone_ts <= int(smc_choch_ts):
                        print(f"  New zone blocked — predates 4H CHoCH: {zone['zone_id'][:40]}...")
                        continue

                # New zone appeared after startup — send alert
                side  = zone["side"]
                entry = zone["entry"]
                sl    = zone["sl"]
                tp1   = zone["tp1"]

                print(f"\n  New Zone Detected!")
                print(f"  Side  : {'LONG' if side == 'buy' else 'SHORT'}")
                print(f"  Entry : {round(entry, 6)}")
                print(f"  SL    : {round(sl, 6)}")
                print(f"  TP1   : {round(tp1, 6)}")
                if zone["tp2"]: print(f"  TP2   : {round(zone['tp2'], 6)}")
                if zone["tp3"]: print(f"  TP3   : {round(zone['tp3'], 6)}")

                success = send_discord_alert(symbol, tf, zone)

                if success:
                    print(f"  Discord alert sent!")
                    alerts_sent += 1
                    state[state_key] = {
                        "zone_id":    zone["zone_id"],
                        "symbol":     symbol,
                        "timeframe":  tf,
                        "side":       side,
                        "entry":      entry,
                        "sl":         sl,
                        "tp1":        tp1,
                        "alerted_at": datetime.now().isoformat()
                    }
                else:
                    print(f"  [ERROR] Alert failed — will retry next run.")

        # ── CHoCH Origin discovery alert (once per new CHoCH, per symbol) ──
        if (USE_SMC_CHOCH_FILTER
                and not _is_first_run
                and origin_state
                and not origin_state.get("alerted", False)
                and origin_state.get("choch_ts")):
            found_origins = {
                tf: z for tf, z in origin_state.get("origins", {}).items() if z
            }
            if found_origins:
                print(f"\n  🎯 New CHoCH origin(s) found for {symbol} — sending alert.")
                if send_choch_origin_alert(
                    symbol,
                    origin_state["direction"],
                    origin_state["choch_ts"],
                    found_origins,
                ):
                    alerts_sent += 1
                    origin_state["alerted"] = True
                    state[origin_state_key]  = origin_state

        # Save after every symbol — so a crash mid-run doesn't lose sent alerts
        save_state(state)

    if _is_first_run:
        print(f"\n  [Startup] First scan complete — existing zones recorded, no alerts sent.")
        print(f"  [Startup] Bot will now alert only NEW zones going forward.")
        _is_first_run = False

    print(f"\n  Alerts sent this run : {alerts_sent}")
    print(f"  Total zones tracked  : {len(state)}")
    print(f"  State saved.")


# =============================================================
#   ▶️  START
# =============================================================

if __name__ == "__main__":
    # ── Validate webhook before starting ──
    if not DISCORD_WEBHOOK:
        print("\n[ERROR] DISCORD_WEBHOOK environment variable is not set!")
        print("  Set it in your environment or Render dashboard, then run again.\n")
        exit(1)

    keep_alive()

    # ── Register shutdown handler — sends Offline alert on exit ──
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT,  handle_shutdown)

    print("\n" + "="*58)
    print("  FARUEXEE ALERT BOT  —  v2")
    print("="*58)
    print(f"  Symbols     : {SYMBOLS}")
    print(f"  Timeframes  : {TIMEFRAMES}")
    print(f"  Interval    : Every {CHECK_INTERVAL}s")
    print(f"  Lookback    : {LOOKBACK} bars")
    print(f"  Impulse     : {IMPULSE_STRENGTH}x avg body")
    print(f"  SL Buffer   : {SL_BUFFER * 100}% of zone")
    print(f"  TP Fallback : {TP_MULTI}x RR")
    print("="*58)
    print("\n  Listening for FARUEXEE zones...\n")

    # ── Send Online alert ──
    send_status_alert(online=True)

    while True:
        try:
            run_bot()
        except Exception as e:
            print(f"\n  [ERROR] {e}")
        print(f"\n  Next scan in {CHECK_INTERVAL} seconds...")
        time.sleep(CHECK_INTERVAL)
