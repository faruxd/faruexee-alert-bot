# =============================================================
#   FARUEXEE ENGINE  —  pure strategy logic, no side effects
# =============================================================
#
#   This module contains ONLY maths. It never touches the network,
#   the filesystem, or the exchange. That makes it testable and
#   keeps the trading bot's risk logic separate from signal logic.
#
#   It is the v2 logic (Pine "FARUEXEE Strategy [v2]"), which adds
#   these fixes over the alert bot's A-J version:
#
#     K  fresh-tap tracking       — a zone must be left before re-entry counts
#     L  FVG filter always active — never silently disabled
#     M  breach invalidation      — close past the far edge kills the zone
#     N  zone max age             — stale zones stop producing signals
#     Q  base-candle off-by-one   — scan starts at the bar before the impulse
#     R  directional FVG block    — longs blocked by bearish FVGs only
#
#   The alert bot (faruexee_alert_bot.py) is deliberately untouched
#   and keeps running its own copy of the older logic.
# =============================================================

from dataclasses import dataclass, field


# =============================================================
#   CONFIG
# =============================================================

@dataclass
class EngineConfig:
    lookback: int            = 20      # swing pivot lookback
    impulse_strength: float  = 1.5     # impulse body vs 20-bar avg body
    trend_stability: int     = 2       # opposing pivots needed to flip trend
    volume_mult: float       = 1.5     # impulse volume vs 20-bar avg volume
    require_volume: bool     = True
    use_base_candle: bool    = True    # zone = origin block, not impulse bar
    use_atr_sl: bool         = True
    atr_len: int             = 14
    atr_mult: float          = 0.5     # SL distance beyond zone, in ATR
    sl_buffer: float         = 0.25    # fallback SL, as fraction of zone height
    min_rr: float            = 1.5     # reject setups below this TP1 R:R
    tp_multi: float          = 2.0     # fallback TP when no opposing zone exists
    zone_max_age: int        = 300     # bars before a zone expires  (fix N)
    breach_buf_mult: float   = 0.1     # breach buffer in ATR         (fix M)
    fvg_recent_bars: int     = 100     # FVG recency window           (fix B)
    require_htf: bool        = True    # HTF trend must agree         (fix F)

    # HTF bias mode — "ema" or "structure".
    #
    # "structure" reuses the 20/20 pivot trend on the higher timeframe.
    # It is unusable on the daily: Bitget returns at most 90 daily candles,
    # a 20/20 pivot needs 41 bars to confirm, and the stability gate needs
    # TWO consecutive same-direction pivots. Across 89 bars you get 0-1
    # pivots, so the daily trend is permanently 0 and every 4H setup is
    # blocked. Verified against live data on all five majors.
    #
    # "ema" compares HTF close to an HTF EMA. Resolves immediately, does
    # not repaint, and works on any candle budget.
    htf_bias_mode: str       = "ema"
    htf_ema_len: int         = 50

    # When the HTF bias genuinely cannot be determined (bias == 0), should
    # that block entries? False = treat "unknown" as "no objection".
    htf_flat_blocks: bool    = False


@dataclass
class Zone:
    """A supply or demand zone that has passed every entry filter."""
    zone_id: str
    side: str                   # "buy" (demand/long) or "sell" (supply/short)
    top: float
    bot: float
    entry: float                # limit price — zone edge facing the market
    sl: float
    tp1: float
    tp2: float | None
    tp3: float | None
    rr: float                   # R:R to TP1
    created_ts: int             # candle timestamp the zone was born on
    created_bar: int
    age_bars: int
    taps: int = 0
    was_in: bool = False
    extras: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "zone_id": self.zone_id, "side": self.side,
            "top": self.top, "bot": self.bot,
            "entry": self.entry, "sl": self.sl,
            "tp1": self.tp1, "tp2": self.tp2, "tp3": self.tp3,
            "rr": self.rr,
            "created_ts": self.created_ts,
            "age_bars": self.age_bars,
            "taps": self.taps,
        }


# =============================================================
#   PIVOTS / TREND  (identical maths to the alert bot)
# =============================================================

def find_pivot_highs(highs, lookback):
    """Match of Pine ta.pivothigh(high, lookback, lookback)."""
    n = len(highs)
    fire = [None] * n
    for i in range(lookback * 2, n):
        center = highs[i - lookback]
        window = highs[i - 2 * lookback: i + 1]
        if all(center > window[j] for j in range(len(window)) if j != lookback):
            fire[i] = center
    return fire


def find_pivot_lows(lows, lookback):
    """Match of Pine ta.pivotlow(low, lookback, lookback)."""
    n = len(lows)
    fire = [None] * n
    for i in range(lookback * 2, n):
        center = lows[i - lookback]
        window = lows[i - 2 * lookback: i + 1]
        if all(center < window[j] for j in range(len(window)) if j != lookback):
            fire[i] = center
    return fire


def calc_trends(ph_fire, pl_fire, n, stability=1):
    """Trend state with the fix-H stability gate."""
    trends = [0] * n
    curr = 0
    bull_count = bear_count = 0
    ph1 = ph2 = pl1 = pl2 = None

    for i in range(n):
        if ph_fire[i] is not None:
            ph2, ph1 = ph1, ph_fire[i]
        if pl_fire[i] is not None:
            pl2, pl1 = pl1, pl_fire[i]

        bull_pivot = (
            (ph_fire[i] is not None and ph2 is not None and ph_fire[i] > ph2) or
            (pl_fire[i] is not None and pl2 is not None and pl_fire[i] > pl2)
        )
        bear_pivot = (
            (ph_fire[i] is not None and ph2 is not None and ph_fire[i] < ph2) or
            (pl_fire[i] is not None and pl2 is not None and pl_fire[i] < pl2)
        )

        if bull_pivot:
            bull_count += 1
            bear_count = 0
        if bear_pivot:
            bear_count += 1
            bull_count = 0

        if bull_count >= stability and curr != 1:
            curr = 1
        if bear_count >= stability and curr != -1:
            curr = -1

        trends[i] = curr

    return trends


def calc_atr(highs, lows, closes, length):
    """Wilder's ATR — same as Pine ta.atr(length)."""
    n = len(closes)
    tr = [0.0] * n
    atr = [0.0] * n

    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    if n > length:
        atr[length] = sum(tr[1:length + 1]) / length
        for i in range(length + 1, n):
            atr[i] = (atr[i - 1] * (length - 1) + tr[i]) / length

    return atr


def find_base_candle(opens, highs, lows, closes, impulse_bar, side):
    """
    Fix I + Q: the base candle is the most recent opposite-colour candle
    BEFORE the impulse. The scan starts at [impulse-1] — the alert bot
    started at [impulse-2] and skipped the nearest candidate.
    Falls back to the impulse candle itself.
    """
    for j in range(1, 6):
        idx = impulse_bar - j
        if idx < 0:
            break
        if side == "bull" and closes[idx] <= opens[idx]:
            return highs[idx], lows[idx]
        if side == "bear" and closes[idx] >= opens[idx]:
            return highs[idx], lows[idx]
    return highs[impulse_bar], lows[impulse_bar]


# =============================================================
#   FVG  —  fix B (recency) + L (always on) + R (directional)
# =============================================================

def detect_fvg_state(highs, lows, closes, n, recent_bars=100):
    """
    Returns (inside_bull_fvg, inside_bear_fvg) for the most recent close.

    Longs are blocked by BEARISH FVGs (overhead imbalance = resistance).
    Shorts are blocked by BULLISH FVGs (imbalance below = support).
    Blocking both directions on any gap — what the old code did — throws
    away good setups.
    """
    bull_fvgs = []
    bear_fvgs = []

    for i in range(2, n):
        bull_fvgs = [(t, b, c) for t, b, c in bull_fvgs if not (i > c and lows[i] <= t)]
        bear_fvgs = [(t, b, c) for t, b, c in bear_fvgs if not (i > c and highs[i] >= b)]

        if lows[i] > highs[i - 2]:
            gap_top, gap_bot = lows[i], highs[i - 2]
            if gap_top > gap_bot:
                bull_fvgs.append((gap_top, gap_bot, i))
        if highs[i] < lows[i - 2]:
            gap_top, gap_bot = lows[i - 2], highs[i]
            if gap_top > gap_bot:
                bear_fvgs.append((gap_top, gap_bot, i))

    last_close = closes[n - 1]
    inside_bull = any(
        (n - 1 - c) < recent_bars and b <= last_close <= t
        for t, b, c in bull_fvgs
    )
    inside_bear = any(
        (n - 1 - c) < recent_bars and b <= last_close <= t
        for t, b, c in bear_fvgs
    )
    return inside_bull, inside_bear


# =============================================================
#   HTF TREND
# =============================================================

def calc_ema(values, length):
    """Standard EMA, seeded with the SMA of the first `length` values."""
    n = len(values)
    if n < length:
        return []
    k = 2.0 / (length + 1)
    out = [None] * n
    seed = sum(values[:length]) / length
    out[length - 1] = seed
    prev = seed
    for i in range(length, n):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def compute_htf_bias(candles, cfg: EngineConfig):
    """
    Directional bias on the higher timeframe, from CLOSED bars only so it
    cannot repaint as the live HTF bar develops.

    Returns 1 (up), -1 (down) or 0 (undetermined).
    """
    if not candles or len(candles) < 3:
        return 0

    closed = candles[:-1]
    if not closed:
        return 0

    mode = (cfg.htf_bias_mode or "ema").lower()

    if mode == "structure":
        if len(closed) < cfg.lookback * 2 + 10:
            return 0
        highs = [float(c[2]) for c in closed]
        lows = [float(c[3]) for c in closed]
        ph = find_pivot_highs(highs, cfg.lookback)
        pl = find_pivot_lows(lows, cfg.lookback)
        trends = calc_trends(ph, pl, len(closed), stability=cfg.trend_stability)
        return trends[-1]

    # EMA mode
    closes = [float(c[4]) for c in closed]
    if len(closes) < cfg.htf_ema_len:
        return 0
    ema = calc_ema(closes, cfg.htf_ema_len)
    if not ema or ema[-1] is None:
        return 0
    last, ref = closes[-1], ema[-1]
    if last > ref:
        return 1
    if last < ref:
        return -1
    return 0


# Backwards-compatible alias — older call sites used this name.
compute_htf_trend = compute_htf_bias


# =============================================================
#   MAIN ANALYSIS
# =============================================================

def analyze(candles, cfg: EngineConfig, htf_trend: int = 0):
    """
    Build the current set of live, untapped, qualifying zones.

    candles: Bitget format, oldest -> newest, [ts, o, h, l, c, vol, ...].
             The final element is assumed to be the LIVE (unclosed) candle
             and is used only for the current price.

    Returns a dict:
        {
          "ok": bool,
          "reason": str | None,        # why analysis was skipped
          "trend": int,
          "htf_trend": int,
          "price": float,
          "atr": float,
          "inside_bull_fvg": bool,
          "inside_bear_fvg": bool,
          "zones": [Zone, ...],        # untapped and still valid
        }
    """
    empty = {
        "ok": False, "reason": "insufficient data", "trend": 0,
        "htf_trend": htf_trend, "price": 0.0, "atr": 0.0,
        "inside_bull_fvg": False, "inside_bear_fvg": False, "zones": [],
    }

    if not candles or len(candles) < cfg.lookback * 2 + 30:
        return empty

    live = candles[-1]
    closed = candles[:-1]

    opens = [float(c[1]) for c in closed]
    highs = [float(c[2]) for c in closed]
    lows = [float(c[3]) for c in closed]
    closes = [float(c[4]) for c in closed]
    volumes = [float(c[5]) for c in closed]
    ts = [int(c[0]) for c in closed]
    n = len(closed)

    price = float(live[4])

    # ── 20-bar averages, both ending at the PRIOR bar (fix C) ──
    avg_bodies = []
    vol_sma = []
    for i in range(n):
        start = max(0, i - 19)
        span = i - start + 1
        avg_bodies.append(sum(abs(closes[j] - opens[j]) for j in range(start, i + 1)) / span)
        vol_sma.append(sum(volumes[start:i + 1]) / span)

    atr = calc_atr(highs, lows, closes, cfg.atr_len)

    ph = find_pivot_highs(highs, cfg.lookback)
    pl = find_pivot_lows(lows, cfg.lookback)
    trends = calc_trends(ph, pl, n, stability=cfg.trend_stability)

    demand_zones: list[dict] = []
    supply_zones: list[dict] = []
    demand_reg: list[dict] = []
    supply_reg: list[dict] = []

    start_bar = max(7, cfg.lookback * 2 + 1)

    for i in range(start_bar, n):
        avg_body = avg_bodies[i - 1]
        prior_vol = vol_sma[i - 1]
        trend = trends[i]
        o, c = opens[i], closes[i]
        breach_buf = atr[i] * cfg.breach_buf_mult

        # ── Fix M/N: kill breached and stale zones BEFORE tap logic ──
        demand_zones = [
            z for z in demand_zones
            if not (i > z["bar"] and closes[i] < z["bot"] - breach_buf)
            and (i - z["bar"]) <= cfg.zone_max_age
        ]
        supply_zones = [
            z for z in supply_zones
            if not (i > z["bar"] and closes[i] > z["top"] + breach_buf)
            and (i - z["bar"]) <= cfg.zone_max_age
        ]
        demand_reg = [
            z for z in demand_reg
            if not (i > z["bar"] and closes[i] < z["bot"] - breach_buf)
            and (i - z["bar"]) <= cfg.zone_max_age
        ]
        supply_reg = [
            z for z in supply_reg
            if not (i > z["bar"] and closes[i] > z["top"] + breach_buf)
            and (i - z["bar"]) <= cfg.zone_max_age
        ]

        # ── Fix K: tap tracking. was_in prevents one long stay inside
        #    the zone from being counted as several separate tests. ──
        for z in demand_zones:
            is_in = i > z["bar"] and lows[i] <= z["top"]
            if is_in and not z["was_in"]:
                z["taps"] += 1
            z["was_in"] = is_in

        for z in supply_zones:
            is_in = i > z["bar"] and highs[i] >= z["bot"]
            if is_in and not z["was_in"]:
                z["taps"] += 1
            z["was_in"] = is_in

        # Regular zones are TP targets only — consumed on first touch.
        demand_reg = [z for z in demand_reg if not (i > z["bar"] and lows[i] <= z["top"])]
        supply_reg = [z for z in supply_reg if not (i > z["bar"] and highs[i] >= z["bot"])]

        # ── Impulse detection ──
        vol_ok = True
        if cfg.require_volume:
            # A symbol with no usable volume feed must not silently
            # disable every zone — bypass instead.
            vol_ok = prior_vol <= 0 or volumes[i] >= prior_vol * cfg.volume_mult

        bull_impulse = c > o and (c - o) >= avg_body * cfg.impulse_strength and vol_ok
        bear_impulse = c < o and (o - c) >= avg_body * cfg.impulse_strength and vol_ok
        bull_gap = lows[i] > highs[i - 2]
        bear_gap = highs[i] < lows[i - 2]

        # ── Demand zone creation ──
        if bull_impulse and trend == 1:
            if cfg.use_base_candle:
                z_top, z_bot = find_base_candle(opens, highs, lows, closes, i, "bull")
            else:
                z_top, z_bot = highs[i], lows[i]

            min_h = atr[i] * 0.05 if atr[i] > 0 else 0.0
            if z_top > z_bot and (z_top - z_bot) > min_h:
                sl_px = (z_bot - atr[i] * cfg.atr_mult) if cfg.use_atr_sl \
                    else (z_bot - (z_top - z_bot) * cfg.sl_buffer)
                entry = z_top
                if bull_gap:
                    cands = sorted(
                        [z["bot"] for z in supply_zones if z["bot"] > entry] +
                        [z["bot"] for z in supply_reg if z["bot"] > entry]
                    )
                    tp1 = cands[0] if cands else entry + (entry - sl_px) * cfg.tp_multi
                    tp2 = cands[1] if len(cands) >= 2 else None
                    tp3 = cands[2] if len(cands) >= 3 else None
                    sl_d = entry - sl_px
                    if sl_d > 0 and (tp1 - entry) / sl_d >= cfg.min_rr:
                        demand_zones.append({
                            "bar": i, "ts": ts[i], "top": z_top, "bot": z_bot,
                            "entry": entry, "sl": sl_px,
                            "tp1": tp1, "tp2": tp2, "tp3": tp3,
                            "rr": (tp1 - entry) / sl_d,
                            "taps": 0, "was_in": False,
                        })
                else:
                    demand_reg.append({"bar": i, "top": z_top, "bot": z_bot})

        # ── Supply zone creation ──
        if bear_impulse and trend == -1:
            if cfg.use_base_candle:
                z_top, z_bot = find_base_candle(opens, highs, lows, closes, i, "bear")
            else:
                z_top, z_bot = highs[i], lows[i]

            min_h = atr[i] * 0.05 if atr[i] > 0 else 0.0
            if z_top > z_bot and (z_top - z_bot) > min_h:
                sl_px = (z_top + atr[i] * cfg.atr_mult) if cfg.use_atr_sl \
                    else (z_top + (z_top - z_bot) * cfg.sl_buffer)
                entry = z_bot
                if bear_gap:
                    cands = sorted(
                        [z["top"] for z in demand_zones if z["top"] < entry] +
                        [z["top"] for z in demand_reg if z["top"] < entry],
                        reverse=True,
                    )
                    tp1 = cands[0] if cands else entry - (sl_px - entry) * cfg.tp_multi
                    tp2 = cands[1] if len(cands) >= 2 else None
                    tp3 = cands[2] if len(cands) >= 3 else None
                    sl_d = sl_px - entry
                    if sl_d > 0 and (entry - tp1) / sl_d >= cfg.min_rr:
                        supply_zones.append({
                            "bar": i, "ts": ts[i], "top": z_top, "bot": z_bot,
                            "entry": entry, "sl": sl_px,
                            "tp1": tp1, "tp2": tp2, "tp3": tp3,
                            "rr": (entry - tp1) / sl_d,
                            "taps": 0, "was_in": False,
                        })
                else:
                    supply_reg.append({"bar": i, "top": z_top, "bot": z_bot})

    inside_bull, inside_bear = detect_fvg_state(
        highs, lows, closes, n, recent_bars=cfg.fvg_recent_bars
    )

    zones: list[Zone] = []
    for z in demand_zones:
        zones.append(Zone(
            zone_id=f"demand_{z['ts']}_{round(z['top'], 8)}",
            side="buy", top=z["top"], bot=z["bot"],
            entry=z["entry"], sl=z["sl"],
            tp1=z["tp1"], tp2=z["tp2"], tp3=z["tp3"], rr=z["rr"],
            created_ts=z["ts"], created_bar=z["bar"],
            age_bars=n - 1 - z["bar"], taps=z["taps"], was_in=z["was_in"],
        ))
    for z in supply_zones:
        zones.append(Zone(
            zone_id=f"supply_{z['ts']}_{round(z['bot'], 8)}",
            side="sell", top=z["top"], bot=z["bot"],
            entry=z["entry"], sl=z["sl"],
            tp1=z["tp1"], tp2=z["tp2"], tp3=z["tp3"], rr=z["rr"],
            created_ts=z["ts"], created_bar=z["bar"],
            age_bars=n - 1 - z["bar"], taps=z["taps"], was_in=z["was_in"],
        ))

    return {
        "ok": True,
        "reason": None,
        "trend": trends[-1],
        "htf_trend": htf_trend,
        "price": price,
        "atr": atr[-1] if atr else 0.0,
        "inside_bull_fvg": inside_bull,
        "inside_bear_fvg": inside_bear,
        "zones": zones,
    }


def htf_agrees(htf_bias: int, wanted: int, cfg: EngineConfig) -> bool:
    """
    HTF gate. An outright disagreement always blocks. An undetermined
    bias (0) blocks only when htf_flat_blocks is on — otherwise a
    timeframe that cannot produce a reading would veto every trade.
    """
    if htf_bias == wanted:
        return True
    if htf_bias == 0:
        return not cfg.htf_flat_blocks
    return False


def tradeable_zones(result: dict, cfg: EngineConfig) -> list[Zone]:
    """
    Filter analysis output down to zones that may receive a resting
    limit order right now.

    A zone qualifies when:
      • it is still untapped (taps == 0) — the limit sits at virgin price
      • local trend agrees with the zone direction
      • HTF trend agrees, when the HTF filter is on
      • price has not already traded through the entry level
      • the opposing-direction FVG filter is clear
    """
    if not result["ok"]:
        return []

    price = result["price"]
    trend = result["trend"]
    htf = result["htf_trend"]
    out = []

    for z in result["zones"]:
        if z.taps != 0:
            continue

        if z.side == "buy":
            if trend != 1:
                continue
            if cfg.require_htf and not htf_agrees(htf, 1, cfg):
                continue
            if result["inside_bear_fvg"]:
                continue
            # A buy limit must rest BELOW the market, otherwise it fills
            # instantly as a taker at a worse price than the zone.
            if z.entry >= price:
                continue
        else:
            if trend != -1:
                continue
            if cfg.require_htf and not htf_agrees(htf, -1, cfg):
                continue
            if result["inside_bull_fvg"]:
                continue
            if z.entry <= price:
                continue

        if z.rr < cfg.min_rr:
            continue

        out.append(z)

    # Closest to price first — that is the zone most likely to fill.
    out.sort(key=lambda z: abs(z.entry - price))
    return out
