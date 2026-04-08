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
    "XAUUSDT","XAGUSDT"
]   # 50 pairs total

TIMEFRAMES = ["30m", "1H", "4H", "1D"]   # Timeframes to scan

CHECK_INTERVAL = 60    # Seconds between each bot run

# ── Indicator Settings — must match your TradingView input values ──
LOOKBACK         = 20    # Swing Lookback bars           (TradingView: 20)
IMPULSE_STRENGTH = 1.5   # Impulse Strength × avg body   (TradingView: 1.5)
SL_BUFFER        = 0.25   # SL Buffer × zone height       (TradingView: 0.25)
TP_MULTI         = 2.0   # TP Fallback multiplier        (TradingView: 2)

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
#   📊  FARUEXEE INDICATOR — PINE SCRIPT ACCURATE REPLICATION
#
#   Only detects Potential Entry Zones:
#   ★ Demand: bullImpulse + trend==1  + bullGap  → Long alert
#   ★ Supply: bearImpulse + trend==-1 + bearGap  → Short alert
# =============================================================

def find_pivot_highs(highs, lookback):
    """
    Exact match of Pine Script: ta.pivothigh(high, lookback, lookback)
    Signal fires at bar i, represents the pivot from lookback bars ago.
    Uses strict inequality matching Pine Script behaviour.
    """
    n    = len(highs)
    fire = [None] * n

    for i in range(lookback * 2, n):
        center_idx = i - lookback
        center_val = highs[center_idx]
        window     = highs[i - 2*lookback : i + 1]

        is_pivot = all(
            center_val > window[j]
            for j in range(len(window)) if j != lookback
        )
        if is_pivot:
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

        is_pivot = all(
            center_val < window[j]
            for j in range(len(window)) if j != lookback
        )
        if is_pivot:
            fire[i] = center_val

    return fire


def calc_trends(pivot_highs_fire, pivot_lows_fire, n):
    """
    Replicates Pine Script trend state variable:
      HH or HL → trend =  1 (uptrend)
      LH or LL → trend = -1 (downtrend)
      else     → carries forward
    """
    trends = [0] * n
    ph1 = ph2 = pl1 = pl2 = None

    for i in range(n):
        if pivot_highs_fire[i] is not None:
            ph2, ph1 = ph1, pivot_highs_fire[i]
        if pivot_lows_fire[i] is not None:
            pl2, pl1 = pl1, pivot_lows_fire[i]

        changed = False

        if pivot_highs_fire[i] is not None and ph2 is not None:
            if pivot_highs_fire[i] > ph2:
                trends[i] = 1;  changed = True
            elif pivot_highs_fire[i] < ph2:
                trends[i] = -1; changed = True

        if pivot_lows_fire[i] is not None and pl2 is not None:
            if pivot_lows_fire[i] > pl2:
                if not changed: trends[i] = 1;  changed = True
            elif pivot_lows_fire[i] < pl2:
                if not changed: trends[i] = -1; changed = True

        if not changed:
            trends[i] = trends[i - 1] if i > 0 else 0

    return trends


def run_indicator(candles):
    """
    Simulates FARUEXEE Pine Script bar-by-bar.
    Returns list of currently ACTIVE Potential Entry Zones.

    NOTE: The last candle from Bitget is always the live (unclosed) candle.
    We strip it so the indicator only runs on confirmed closed candles —
    matching TradingView's default barstate.isconfirmed behaviour.
    """
    live_candle = candles[-1]  # save live candle for immediate tap detection
    candles = candles[:-1]     # drop live candle — only use closed bars for zone detection

    if len(candles) < LOOKBACK * 2 + 10:
        return [], []   # must match the tuple return at the end

    opens  = [float(c[1]) for c in candles]
    highs  = [float(c[2]) for c in candles]
    lows   = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    ts     = [c[0] for c in candles]   # candle timestamps — stable across runs
    n      = len(candles)

    # avgBody = ta.sma(math.abs(close - open), 20)
    avg_bodies = []
    for i in range(n):
        start  = max(0, i - 19)
        bodies = [abs(closes[j] - opens[j]) for j in range(start, i + 1)]
        avg_bodies.append(sum(bodies) / len(bodies))

    pivot_highs_fire = find_pivot_highs(highs, LOOKBACK)
    pivot_lows_fire  = find_pivot_lows(lows,   LOOKBACK)
    trends           = calc_trends(pivot_highs_fire, pivot_lows_fire, n)

    demand_zones    = []   # Potential Entry Demand (bullGap)  → alerted
    supply_zones    = []   # Potential Entry Supply (bearGap)  → alerted
    demand_reg      = []   # Regular demand (no gap)           → TP source only
    supply_reg      = []   # Regular supply (no gap)           → TP source only
    tapped_last_bar = []   # Zones tapped on the current bar   → tap alert

    for i in range(2, n):
        avg_body = avg_bodies[i]
        trend    = trends[i]

        o = opens[i];  h = highs[i];  l = lows[i];  c = closes[i]

        bull_impulse = (c > o) and ((c - o) >= avg_body * IMPULSE_STRENGTH)
        bear_impulse = (c < o) and ((o - c) >= avg_body * IMPULSE_STRENGTH)

        bull_gap = lows[i]  > highs[i - 2]
        bear_gap = highs[i] < lows[i - 2]

        # Tap logic — remove zones price has entered (runs before zone creation)
        # Also track which Potential Entry zones are tapped on the last bar
        new_demand = []
        for z in demand_zones:
            if i > z["bar"] and lows[i] <= z["top"]:
                if i == n - 1:   # tapped on the current (last) bar
                    tapped_last_bar.append({
                        "zone_id": f"demand_{z['ts']}_{round(z['top'], 6)}",
                        "side":    "buy",
                        "entry":   z["entry"],
                        "sl":      z["sl"],
                        "tp1":     z["tp1"],
                        "tp2":     z["tp2"],
                        "tp3":     z["tp3"],
                    })
            else:
                new_demand.append(z)
        demand_zones = new_demand

        new_supply = []
        for z in supply_zones:
            if i > z["bar"] and highs[i] >= z["bot"]:
                if i == n - 1:   # tapped on the current (last) bar
                    tapped_last_bar.append({
                        "zone_id": f"supply_{z['ts']}_{round(z['bot'], 6)}",
                        "side":    "sell",
                        "entry":   z["entry"],
                        "sl":      z["sl"],
                        "tp1":     z["tp1"],
                        "tp2":     z["tp2"],
                        "tp3":     z["tp3"],
                    })
            else:
                new_supply.append(z)
        supply_zones = new_supply

        demand_reg = [z for z in demand_reg
                      if not (i > z["bar"] and lows[i] <= z["top"])]
        supply_reg = [z for z in supply_reg
                      if not (i > z["bar"] and highs[i] >= z["bot"])]

        # ── Demand Zone (bullImpulse + uptrend) ──
        if bull_impulse and trend == 1:
            z_top = highs[i - 1]
            z_bot = lows[i - 1]
            if z_top > z_bot:
                sl_px = z_bot - (z_top - z_bot) * SL_BUFFER
                entry = z_top

                if bull_gap:
                    tp_cands = sorted(
                        [z["bot"] for z in supply_zones if z["bot"] > entry] +
                        [z["bot"] for z in supply_reg   if z["bot"] > entry]
                    )
                    tp1 = tp_cands[0] if len(tp_cands) >= 1 \
                          else entry + (entry - sl_px) * TP_MULTI
                    tp2 = tp_cands[1] if len(tp_cands) >= 2 else None
                    tp3 = tp_cands[2] if len(tp_cands) >= 3 else None
                    demand_zones.append({
                        "bar": i, "ts": ts[i], "top": z_top, "bot": z_bot,
                        "entry": entry, "sl": sl_px,
                        "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    })
                else:
                    demand_reg.append({"bar": i, "top": z_top, "bot": z_bot})

        # ── Supply Zone (bearImpulse + downtrend) ──
        if bear_impulse and trend == -1:
            z_top = highs[i - 1]
            z_bot = lows[i - 1]
            if z_top > z_bot:
                sl_px = z_top + (z_top - z_bot) * SL_BUFFER
                entry = z_bot

                if bear_gap:
                    tp_cands = sorted(
                        [z["top"] for z in demand_zones if z["top"] < entry] +
                        [z["top"] for z in demand_reg   if z["top"] < entry],
                        reverse=True
                    )
                    tp1 = tp_cands[0] if len(tp_cands) >= 1 \
                          else entry - (sl_px - entry) * TP_MULTI
                    tp2 = tp_cands[1] if len(tp_cands) >= 2 else None
                    tp3 = tp_cands[2] if len(tp_cands) >= 3 else None
                    supply_zones.append({
                        "bar": i, "ts": ts[i], "top": z_top, "bot": z_bot,
                        "entry": entry, "sl": sl_px,
                        "tp1": tp1, "tp2": tp2, "tp3": tp3,
                    })
                else:
                    supply_reg.append({"bar": i, "top": z_top, "bot": z_bot})

    # ── Collect all active Potential Entry Zones ──
    active_zones = []

    for z in demand_zones:
        active_zones.append({
            "zone_id": f"demand_{z['ts']}_{round(z['top'], 6)}",
            "side":    "buy",
            "entry":   z["entry"],
            "sl":      z["sl"],
            "tp1":     z["tp1"],
            "tp2":     z["tp2"],
            "tp3":     z["tp3"],
        })

    for z in supply_zones:
        active_zones.append({
            "zone_id": f"supply_{z['ts']}_{round(z['bot'], 6)}",
            "side":    "sell",
            "entry":   z["entry"],
            "sl":      z["sl"],
            "tp1":     z["tp1"],
            "tp2":     z["tp2"],
            "tp3":     z["tp3"],
        })

    # ── Check live candle for immediate zone taps ──
    live_high = float(live_candle[2])
    live_low  = float(live_candle[3])

    live_taps = []
    for z in demand_zones:
        if live_low <= z["top"]:
            live_taps.append({
                "zone_id": f"demand_{z['ts']}_{round(z['top'], 6)}",
                "side":    "buy",
                "entry":   z["entry"],
                "sl":      z["sl"],
                "tp1":     z["tp1"],
                "tp2":     z["tp2"],
                "tp3":     z["tp3"],
            })

    for z in supply_zones:
        if live_high >= z["bot"]:
            live_taps.append({
                "zone_id": f"supply_{z['ts']}_{round(z['bot'], 6)}",
                "side":    "sell",
                "entry":   z["entry"],
                "sl":      z["sl"],
                "tp1":     z["tp1"],
                "tp2":     z["tp2"],
                "tp3":     z["tp3"],
            })

    return active_zones, tapped_last_bar, live_taps


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

    side  = zone["side"]
    entry = zone["entry"]
    sl    = zone["sl"]
    tp1   = zone["tp1"]
    tp2   = zone["tp2"]
    tp3   = zone["tp3"]

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

    fields = [
        {
            "name":   "Direction",
            "value":  f"{arrow}  **{direction}**",
            "inline": False
        },
        {
            "name":   "Entry",
            "value":  f"`{round(entry, 6)}`",
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

    side    = zone_info["side"]
    entry   = zone_info["entry"]
    sl      = zone_info["sl"]
    tp1     = zone_info["tp1"]
    tp2     = zone_info.get("tp2")
    tp3     = zone_info.get("tp3")

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

    fields = [
        {
            "name":   "Direction",
            "value":  f"{arrow}  **{direction}**",
            "inline": False
        },
        {
            "name":   "Entry",
            "value":  f"`{round(entry, 6)}`",
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
        for tf in TIMEFRAMES:
            key_prefix = f"{symbol}_{tf}"
            print(f"\n  Scanning {symbol} | {tf}")

            candles = get_candles(symbol, tf, limit=300)
            if not candles or len(candles) < LOOKBACK * 2 + 10:
                print(f"  [WARN] Not enough candle data — skipping.")
                continue

            current_price = float(candles[-1][4])
            print(f"  Price: {current_price}")

            active_zones, tapped_zones, live_taps = run_indicator(candles)
            active_zone_ids  = {z["zone_id"] for z in active_zones}
            tapped_zone_ids  = {z["zone_id"]: z for z in tapped_zones}

            print(f"  Active zones : {len(active_zones)}")
            if tapped_zones:
                print(f"  Tapped zones : {len(tapped_zones)}")
            if live_taps:
                print(f"  Live taps    : {len(live_taps)}")
            zones_found += len(active_zones)

            # ── Handle zones that are no longer active ──
            stale_keys = [
                k for k, v in state.items()
                if k.startswith(key_prefix)
                and not k.startswith(f"{key_prefix}_liveTap_")
                and v.get("zone_id") not in active_zone_ids
            ]
            for k in stale_keys:
                zone_id   = state[k].get("zone_id")
                zone_info = tapped_zone_ids.get(zone_id)

                if zone_info:
                    # Zone was tapped on a closed candle — send tap alert
                    print(f"\n  Zone Tapped (closed candle)! Sending alert...")
                    success = send_discord_tap_alert(symbol, tf, zone_info)
                    if success:
                        print(f"  Tap alert sent!")
                    else:
                        print(f"  [ERROR] Tap alert failed.")
                else:
                    print(f"  Zone expired — removing from state: {k}")

                del state[k]
                # also clean up any live tap state for this zone
                live_tap_key = f"{key_prefix}_liveTap_{zone_id}"
                if live_tap_key in state:
                    del state[live_tap_key]

            # ── Immediate tap alerts — live candle touching zone ──
            for zone in live_taps:
                live_tap_key = f"{key_prefix}_liveTap_{zone['zone_id']}"

                if live_tap_key in state:
                    continue   # already alerted for this touch — skip

                print(f"\n  Zone Tapped (live candle)! Sending immediate alert...")
                success = send_discord_tap_alert(symbol, tf, zone)
                if success:
                    print(f"  Immediate tap alert sent!")
                    state[live_tap_key] = {
                        "zone_id":    zone["zone_id"],
                        "alerted_at": datetime.now().isoformat()
                    }
                else:
                    print(f"  [ERROR] Immediate tap alert failed.")

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
