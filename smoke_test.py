# =============================================================
#   SMOKE TEST  —  no API keys, no orders, no risk
# =============================================================
#
#   Verifies the whole signal path against live Bitget public data:
#     • candles fetch and contract specs
#     • engine analysis (trend, HTF, zones, FVG state)
#     • which zones would receive a resting limit order
#     • position sizing and precision rounding for a simulated account
#
#   Run this before ever setting API keys:
#       python smoke_test.py
#       python smoke_test.py --equity 500
# =============================================================

import argparse

import trade_config as C
from bitget_client import BitgetClient, BitgetError
from faruexee_engine import EngineConfig, analyze, compute_htf_bias, tradeable_zones


def build_cfg():
    return EngineConfig(
        lookback=C.LOOKBACK, impulse_strength=C.IMPULSE_STRENGTH,
        trend_stability=C.TREND_STABILITY, volume_mult=C.VOLUME_MULT,
        require_volume=C.REQUIRE_VOLUME, use_base_candle=C.USE_BASE_CANDLE,
        use_atr_sl=C.USE_ATR_SL, atr_len=C.ATR_LEN, atr_mult=C.ATR_MULT,
        sl_buffer=C.SL_BUFFER, min_rr=C.MIN_RR, tp_multi=C.TP_MULTI,
        zone_max_age=C.ZONE_MAX_AGE, breach_buf_mult=C.BREACH_BUF_MULT,
        fvg_recent_bars=C.FVG_RECENT_BARS, require_htf=C.REQUIRE_HTF,
        htf_bias_mode=C.HTF_BIAS_MODE, htf_ema_len=C.HTF_EMA_LEN,
        htf_flat_blocks=C.HTF_FLAT_BLOCKS,
    )


def trend_word(t):
    return "UP" if t == 1 else "DOWN" if t == -1 else "flat"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--equity", type=float, default=1000.0,
                    help="simulated account equity for sizing maths")
    args = ap.parse_args()

    cfg = build_cfg()
    client = BitgetClient("", "", "", product_type=C.PRODUCT_TYPE,
                          margin_coin=C.MARGIN_COIN)

    print("\n" + "=" * 66)
    print("  FARUEXEE SMOKE TEST  —  public data only, no orders")
    print("=" * 66)
    print(C.summary())
    print(f"  Simulated equity: {args.equity:.2f} {C.MARGIN_COIN}")
    print("=" * 66)

    print("\nLoading contract specs...")
    specs = client.load_contracts()
    print(f"  {len(specs)} contracts available on {C.PRODUCT_TYPE}")

    missing = [s for s in C.SYMBOLS if s not in specs]
    if missing:
        print(f"  [FAIL] not tradeable: {', '.join(missing)}")
    else:
        print(f"  [OK] all {len(C.SYMBOLS)} configured symbols exist")

    for s in C.SYMBOLS:
        if s in specs:
            sp = specs[s]
            print(f"      {s:<10} tick={float(client.price_tick(s)):<12g} "
                  f"minQty={sp['minTradeNum']:<8} volPlace={sp['volumePlace']}")

    total_zones = 0
    total_tradeable = 0
    htf_cache = {}

    for symbol in C.SYMBOLS:
        if symbol not in specs:
            continue
        for tf in C.TIMEFRAMES:
            print(f"\n{'-' * 66}")
            print(f"  {symbol}  {tf}")
            print(f"{'-' * 66}")

            try:
                candles = client.get_candles(symbol, tf, C.CANDLE_LIMIT)
            except BitgetError as e:
                print(f"  [FAIL] candle fetch: {e}")
                continue

            if len(candles) < C.LOOKBACK * 2 + 30:
                print(f"  [SKIP] only {len(candles)} candles")
                continue

            htf_trend = 0
            htf_tf = C.HTF_MAP.get(tf)
            if C.REQUIRE_HTF and htf_tf:
                key = (symbol, htf_tf)
                if key not in htf_cache:
                    try:
                        htf_cache[key] = compute_htf_bias(
                            client.get_candles(symbol, htf_tf, 200), cfg)
                    except BitgetError:
                        htf_cache[key] = 0
                htf_trend = htf_cache[key]

            res = analyze(candles, cfg, htf_trend)
            if not res["ok"]:
                print(f"  [SKIP] {res['reason']}")
                continue

            print(f"  candles={len(candles)}  price={res['price']}  "
                  f"atr={res['atr']:.6g}")
            print(f"  trend={trend_word(res['trend'])}  "
                  f"HTF({htf_tf})={trend_word(res['htf_trend'])}  "
                  f"bullFVG={res['inside_bull_fvg']}  bearFVG={res['inside_bear_fvg']}")

            zones = res["zones"]
            total_zones += len(zones)
            print(f"  live zones: {len(zones)}")
            for z in zones[:6]:
                side = "LONG " if z.side == "buy" else "SHORT"
                print(f"    {side} {z.bot:.6g}-{z.top:.6g}  entry={z.entry:.6g}  "
                      f"sl={z.sl:.6g}  tp1={z.tp1:.6g}  rr={z.rr:.2f}  "
                      f"taps={z.taps}  age={z.age_bars}b")

            ready = tradeable_zones(res, cfg)
            total_tradeable += len(ready)
            if not ready:
                print("  tradeable now: none")
                continue

            print(f"  tradeable now: {len(ready)}")
            for z in ready:
                entry = client.round_price(symbol, z.entry,
                                           "down" if z.side == "buy" else "up")
                sl = client.round_price(symbol, z.sl,
                                        "down" if z.side == "buy" else "up")
                sl_dist = abs(entry - sl)
                risk = args.equity * C.RISK_PER_TRADE
                raw = risk / sl_dist if sl_dist > 0 else 0

                capped = ""
                max_notional = args.equity * C.MAX_NOTIONAL_X_EQUITY
                if raw * entry > max_notional:
                    raw = max_notional / entry
                    capped = "  (notional-capped)"

                size = client.round_size(symbol, raw)
                notional = size * entry
                actual_risk = size * sl_dist
                dist_pct = abs(entry - res["price"]) / res["price"]

                verdict = "WOULD ORDER"
                if size <= 0:
                    verdict = "SKIP (below exchange minimum)"
                elif notional < client.min_notional(symbol):
                    verdict = f"SKIP (notional {notional:.2f} below minimum)"
                elif dist_pct > C.MAX_ENTRY_DISTANCE:
                    verdict = f"SKIP (entry {dist_pct:.2%} away)"

                print(f"    -> {z.side.upper():<5} entry={entry} sl={sl} "
                      f"size={size} notional={notional:.2f} "
                      f"risk={actual_risk:.2f} ({actual_risk / args.equity:.2%}) "
                      f"dist={dist_pct:.2%}{capped}")
                print(f"       {verdict}")

    print(f"\n{'=' * 66}")
    print(f"  Total live zones      : {total_zones}")
    print(f"  Total tradeable now   : {total_tradeable}")
    print(f"  Concurrency cap       : {C.MAX_CONCURRENT}")
    print(f"{'=' * 66}")
    print("  No orders were placed. No API keys were used.\n")


if __name__ == "__main__":
    main()
