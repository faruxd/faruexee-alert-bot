# =============================================================
#   RISK + ENGINE TESTS  —  no network, no keys, no orders
# =============================================================
#
#   Covers the code paths where a bug costs money:
#     • position sizing and its caps
#     • exchange precision rounding
#     • concurrency and per-symbol limits
#     • the daily-loss circuit breaker
#     • entry geometry rejection
#     • engine breach invalidation and fresh-tap counting
#
#   Run:  python test_risk.py
# =============================================================

import os
import sys
import tempfile

os.environ.setdefault("TRADE_STATE_FILE", os.path.join(tempfile.gettempdir(), "t_state.json"))
os.environ.setdefault("TRADE_JOURNAL_FILE", os.path.join(tempfile.gettempdir(), "t_journal.csv"))

import trade_config as C
from bitget_client import BitgetClient, BitgetError
from faruexee_engine import (
    EngineConfig, analyze, calc_ema, compute_htf_bias, htf_agrees, tradeable_zones,
)

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# =============================================================
#   FAKE EXCHANGE  —  deterministic specs, no network
# =============================================================

FAKE_SPECS = {
    "BTCUSDT": {"symbol": "BTCUSDT", "pricePlace": "1", "priceEndStep": "1",
                "volumePlace": "4", "minTradeNum": "0.0001",
                "sizeMultiplier": "0.0001", "minTradeUSDT": "5"},
    "XRPUSDT": {"symbol": "XRPUSDT", "pricePlace": "4", "priceEndStep": "1",
                "volumePlace": "0", "minTradeNum": "1",
                "sizeMultiplier": "1", "minTradeUSDT": "5"},
    "SOLUSDT": {"symbol": "SOLUSDT", "pricePlace": "3", "priceEndStep": "1",
                "volumePlace": "1", "minTradeNum": "0.1",
                "sizeMultiplier": "0.1", "minTradeUSDT": "5"},
}


def fake_client():
    c = BitgetClient("", "", "")
    c._specs = dict(FAKE_SPECS)
    return c


def make_bot(equity=1000.0):
    """A TradeBot with a stubbed exchange, safe to poke at."""
    import faruexee_trade_bot as T
    bot = T.TradeBot.__new__(T.TradeBot)
    bot.cfg = EngineConfig()
    bot.client = fake_client()
    bot.state = {"orders": {}, "positions": {},
                 "daily": {"date": T.utc_date(), "start_equity": equity},
                 "halted": False, "halt_reason": "", "baseline_equity": equity}
    bot.live = False
    bot.offline = True
    bot.equity = equity
    return bot


# =============================================================
#   PRECISION
# =============================================================

def test_rounding():
    print("\nPrecision rounding")
    c = fake_client()

    check("BTC price to 0.1 tick", c.round_price("BTCUSDT", 64385.07) == 64385.1,
          c.round_price("BTCUSDT", 64385.07))
    check("BTC price rounds down for long entry",
          c.round_price("BTCUSDT", 64385.07, "down") == 64385.0,
          c.round_price("BTCUSDT", 64385.07, "down"))
    check("BTC price rounds up for short entry",
          c.round_price("BTCUSDT", 64385.01, "up") == 64385.1,
          c.round_price("BTCUSDT", 64385.01, "up"))
    check("XRP price to 4dp", c.round_price("XRPUSDT", 1.086549) == 1.0865,
          c.round_price("XRPUSDT", 1.086549))

    check("size floors to step", c.round_size("SOLUSDT", 22.97) == 22.9,
          c.round_size("SOLUSDT", 22.97))
    check("XRP size floors to integer", c.round_size("XRPUSDT", 137.9) == 137.0,
          c.round_size("XRPUSDT", 137.9))
    check("below minimum returns 0", c.round_size("XRPUSDT", 0.4) == 0.0,
          c.round_size("XRPUSDT", 0.4))
    check("exactly minimum is allowed", c.round_size("BTCUSDT", 0.0001) == 0.0001,
          c.round_size("BTCUSDT", 0.0001))
    check("never rounds size up", c.round_size("BTCUSDT", 0.00019) == 0.0001,
          c.round_size("BTCUSDT", 0.00019))


# =============================================================
#   SIZING
# =============================================================

def test_sizing():
    print("\nPosition sizing")
    bot = make_bot(1000.0)

    # 2% of 1000 = 20 USDT risk. Stop 1000 wide on a 60000 entry.
    size, risk, note = bot.size_position("BTCUSDT", 60000.0, 59000.0, 10000.0)
    check("risk-based size", abs(size - 0.02) < 1e-9, f"size={size}")
    check("actual risk near budget", abs(risk - 20.0) < 0.5, f"risk={risk}")

    # A very tight stop would demand an enormous position -> notional cap.
    size, risk, note = bot.size_position("BTCUSDT", 60000.0, 59990.0, 10000.0)
    notional = size * 60000.0
    check("notional cap enforced", notional <= 1000.0 * C.MAX_NOTIONAL_X_EQUITY + 1,
          f"notional={notional}")
    check("cap is reported", "capped" in note, note)
    check("capped risk is BELOW budget, never above", risk <= 20.0 + 1e-6,
          f"risk={risk}")

    # Free margin also constrains size.
    size, risk, note = bot.size_position("BTCUSDT", 60000.0, 59000.0, 10.0)
    margin_used = (size * 60000.0) / C.LEVERAGE
    check("margin cap enforced", margin_used <= 10.0 * C.MAX_MARGIN_FRACTION + 0.01,
          f"margin={margin_used}")

    # Degenerate stop must be refused outright.
    size, risk, note = bot.size_position("BTCUSDT", 60000.0, 60000.0, 10000.0)
    check("zero stop distance refused", size == 0.0, f"size={size}")

    # Tiny account -> below exchange minimum -> refuse rather than trade wrong.
    small = make_bot(20.0)
    size, risk, note = small.size_position("XRPUSDT", 1.0865, 1.0, 20.0)
    check("dust account refused", size == 0.0 or size * 1.0865 >= 5.0,
          f"size={size} note={note}")


# =============================================================
#   CAPS AND BREAKERS
# =============================================================

def test_caps():
    print("\nConcurrency and circuit breakers")
    import faruexee_trade_bot as T
    bot = make_bot(1000.0)

    check("empty book has no slots used", bot.slots_used() == 0)

    bot.state["orders"]["a"] = {"symbol": "BTCUSDT", "zone_id": "z1"}
    bot.state["positions"]["ETHUSDT"] = {"symbol": "ETHUSDT"}
    check("orders and positions both consume slots", bot.slots_used() == 2,
          bot.slots_used())
    check("symbol with resting order is busy", bot.symbol_busy("BTCUSDT"))
    check("symbol with position is busy", bot.symbol_busy("ETHUSDT"))
    check("unrelated symbol is free", not bot.symbol_busy("SOLUSDT"))
    check("zone dedupe works", bot.zone_already_ordered("z1"))
    check("unknown zone not deduped", not bot.zone_already_ordered("z2"))

    # Third slot fills; a fourth entry must be refused.
    bot.state["orders"]["b"] = {"symbol": "SOLUSDT", "zone_id": "z3"}
    check("cap reached at MAX_CONCURRENT",
          bot.slots_used() >= C.MAX_CONCURRENT)

    class Z:
        side, zone_id = "buy", "znew"
        entry, sl, tp1, tp2, tp3, rr = 1.0, 0.9, 1.3, None, None, 3.0
    check("entry refused when book is full",
          bot.try_enter("XRPUSDT", "1H", Z(), 1.1) is False)

    # Daily loss halt.
    bot2 = make_bot(1000.0)
    bot2.equity = 1000.0 * (1 - C.MAX_DAILY_LOSS) - 1
    ok = bot2.check_breakers()
    check("daily loss halts trading", ok is False and bot2.state["halted"])
    check("halt reason recorded", bot2.state["halt_reason"] == "daily_loss")

    bot3 = make_bot(1000.0)
    bot3.equity = 990.0
    check("small drawdown does not halt", bot3.check_breakers() is True)

    # Equity floor.
    bot4 = make_bot(1000.0)
    bot4.state["daily"]["start_equity"] = 700.0
    bot4.equity = 690.0
    bot4.check_breakers()
    check("equity floor halts trading", bot4.state["halted"])


# =============================================================
#   ENTRY GEOMETRY
# =============================================================

def test_geometry():
    print("\nEntry geometry guards")
    bot = make_bot(1000.0)

    class LongOK:
        side, zone_id = "buy", "g1"
        entry, sl, tp1, tp2, tp3, rr = 59000.0, 58000.0, 62000.0, None, None, 3.0

    class LongBadSL:
        side, zone_id = "buy", "g2"
        entry, sl, tp1, tp2, tp3, rr = 59000.0, 60000.0, 62000.0, None, None, 3.0

    class ShortBadSL:
        side, zone_id = "sell", "g3"
        entry, sl, tp1, tp2, tp3, rr = 61000.0, 60000.0, 58000.0, None, None, 3.0

    check("valid long accepted", bot.try_enter("BTCUSDT", "4H", LongOK(), 60000.0) is True)

    bot2 = make_bot(1000.0)
    check("long with stop above entry refused",
          bot2.try_enter("BTCUSDT", "4H", LongBadSL(), 60000.0) is False)

    bot3 = make_bot(1000.0)
    check("short with stop below entry refused",
          bot3.try_enter("BTCUSDT", "4H", ShortBadSL(), 60000.0) is False)

    # Buy limit must rest below market, else it is a market order in disguise.
    bot4 = make_bot(1000.0)

    class LongAboveMarket:
        side, zone_id = "buy", "g4"
        entry, sl, tp1, tp2, tp3, rr = 61000.0, 60000.0, 65000.0, None, None, 3.0
    check("buy limit above market refused",
          bot4.try_enter("BTCUSDT", "4H", LongAboveMarket(), 60000.0) is False)

    # Far-away entries are not trades.
    bot5 = make_bot(1000.0)

    class LongFar:
        side, zone_id = "buy", "g5"
        entry, sl, tp1, tp2, tp3, rr = 50000.0, 49000.0, 56000.0, None, None, 3.0
    check("entry beyond MAX_ENTRY_DISTANCE refused",
          bot5.try_enter("BTCUSDT", "4H", LongFar(), 60000.0) is False)


# =============================================================
#   ENGINE
# =============================================================

def synth(n=260, base=100.0):
    """Flat synthetic candles: [ts, o, h, l, c, vol]."""
    out = []
    for i in range(n):
        out.append([str(1700000000000 + i * 3600000),
                    f"{base}", f"{base + 0.5}", f"{base - 0.5}", f"{base}", "1000"])
    return out


def test_engine():
    print("\nEngine behaviour")
    cfg = EngineConfig()

    ema = calc_ema([float(i) for i in range(1, 101)], 50)
    check("EMA returns aligned series", len(ema) == 100 and ema[48] is None
          and ema[49] is not None)
    check("EMA tracks a rising series", ema[-1] < 100.0 and ema[-1] > 70.0,
          ema[-1])

    up = [[0, 0, 0, 0, f"{100 + i}", 0] for i in range(80)]
    check("EMA bias reads uptrend", compute_htf_bias(up, cfg) == 1)
    down = [[0, 0, 0, 0, f"{200 - i}", 0] for i in range(80)]
    check("EMA bias reads downtrend", compute_htf_bias(down, cfg) == -1)
    check("EMA bias undetermined on short history",
          compute_htf_bias(up[:10], cfg) == 0)

    check("agreeing HTF passes", htf_agrees(1, 1, cfg))
    check("opposing HTF blocks", not htf_agrees(-1, 1, cfg))
    check("undetermined HTF passes by default", htf_agrees(0, 1, cfg))
    strict = EngineConfig(htf_flat_blocks=True)
    check("undetermined HTF blocks when strict", not htf_agrees(0, 1, strict))

    flat = analyze(synth(), cfg, htf_trend=1)
    check("flat market yields no zones", flat["ok"] and len(flat["zones"]) == 0,
          len(flat["zones"]))

    short_data = analyze(synth(30), cfg, htf_trend=1)
    check("insufficient data handled", short_data["ok"] is False)

    res = {"ok": True, "price": 100.0, "trend": 1, "htf_trend": 1,
           "inside_bull_fvg": False, "inside_bear_fvg": True, "zones": []}

    from faruexee_engine import Zone
    z = Zone(zone_id="t", side="buy", top=99.0, bot=98.0, entry=99.0, sl=97.0,
             tp1=105.0, tp2=None, tp3=None, rr=3.0, created_ts=0,
             created_bar=0, age_bars=5, taps=0)

    res["zones"] = [z]
    check("bearish FVG blocks a long", len(tradeable_zones(res, cfg)) == 0)

    res["inside_bear_fvg"] = False
    check("clear FVG allows the long", len(tradeable_zones(res, cfg)) == 1)

    z.taps = 1
    check("already-tapped zone is not re-entered",
          len(tradeable_zones(res, cfg)) == 0)

    z.taps = 0
    res["htf_trend"] = -1
    check("opposing HTF blocks the long", len(tradeable_zones(res, cfg)) == 0)

    res["htf_trend"] = 1
    res["trend"] = -1
    check("opposing local trend blocks the long",
          len(tradeable_zones(res, cfg)) == 0)

    res["trend"] = 1
    z.rr = 0.5
    check("sub-minimum R:R blocked", len(tradeable_zones(res, cfg)) == 0)

    z.rr = 3.0
    res["price"] = 98.5
    check("entry already passed by price is skipped",
          len(tradeable_zones(res, cfg)) == 0)


# =============================================================
#   TP LADDER
# =============================================================

def test_tp_coverage():
    """
    The ladder must always cover the WHOLE position.

    Regression: slices were computed as floats and floored individually,
    so 0.4 on a 0.1 grid produced 0.2 + 0.1 and silently dropped the last
    0.1 to float error — a quarter of the position left with no target,
    exiting only via the stop. Coarse grids (SOL 0.1, XRP 1) made this the
    normal case on a small account, not an edge case.
    """
    print("\nTake-profit ladder covers the full position")

    class RecordingClient(BitgetClient):
        def __init__(self):
            super().__init__("", "", "")
            self._specs = dict(FAKE_SPECS)
            self.placed = []

        def place_partial_tp(self, symbol, hold_side, size, trigger_price, **kw):
            self.placed.append(size)
            return {}

    import faruexee_trade_bot as T

    cases = [
        ("SOLUSDT", [0.1, 0.2, 0.3, 0.4, 0.7, 1.1, 22.9, 5.5]),
        ("XRPUSDT", [1, 2, 3, 5, 7, 37, 93, 137]),
        ("BTCUSDT", [0.0001, 0.0002, 0.0003, 0.0008, 0.0021, 0.0137]),
    ]

    all_ok = True
    for symbol, sizes in cases:
        for size in sizes:
            for tp2, tp3 in ((97.0, 96.0), (None, None), (97.0, None)):
                bot = T.TradeBot.__new__(T.TradeBot)
                bot.cfg = EngineConfig()
                bot.client = RecordingClient()
                bot.live = True
                bot.offline = False
                bot.equity = 1000.0
                bot.state = {"orders": {}, "positions": {},
                             "daily": {"date": T.utc_date(), "start_equity": 1000.0},
                             "halted": False, "halt_reason": "",
                             "baseline_equity": 1000.0}
                bot.state["positions"][symbol] = {
                    "symbol": symbol, "hold_side": "short", "orig_size": size,
                    "entry": 100.0, "sl": 101.0,
                    "tp1": 98.0, "tp2": tp2, "tp3": tp3, "tps_placed": False,
                }
                import io
                import contextlib
                with contextlib.redirect_stdout(io.StringIO()):
                    bot._place_tp_ladder(symbol)

                total = sum(bot.client.placed)
                if abs(total - size) > 1e-9:
                    all_ok = False
                    check(f"{symbol} size {size} tp2={tp2} fully covered", False,
                          f"slices={bot.client.placed} sum={total} want={size}")

    check("every position size is fully covered by its TP ladder", all_ok)

    # A ladder must never sell more than the position holds either.
    bot = T.TradeBot.__new__(T.TradeBot)
    bot.cfg = EngineConfig()
    bot.client = RecordingClient()
    bot.live = True
    bot.offline = False
    bot.equity = 1000.0
    bot.state = {"orders": {}, "positions": {},
                 "daily": {"date": T.utc_date(), "start_equity": 1000.0},
                 "halted": False, "halt_reason": "", "baseline_equity": 1000.0}
    bot.state["positions"]["SOLUSDT"] = {
        "symbol": "SOLUSDT", "hold_side": "long", "orig_size": 0.3,
        "entry": 100.0, "sl": 99.0,
        "tp1": 102.0, "tp2": 103.0, "tp3": 104.0, "tps_placed": False,
    }
    import io
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        bot._place_tp_ladder("SOLUSDT")
    check("ladder never exceeds position size",
          sum(bot.client.placed) <= 0.3 + 1e-9,
          f"sum={sum(bot.client.placed)}")


def test_size_units():
    print("\nSize-step arithmetic")
    c = fake_client()
    check("0.4 - 0.3 float drift still counts as one step",
          c.size_units("SOLUSDT", 0.4 - 0.30000000000000004) == 1,
          c.size_units("SOLUSDT", 0.4 - 0.30000000000000004))
    check("round_size survives the same drift",
          c.round_size("SOLUSDT", 0.4 - 0.30000000000000004) == 0.1,
          c.round_size("SOLUSDT", 0.4 - 0.30000000000000004))
    check("half a step never rounds up", c.size_units("SOLUSDT", 0.05) == 0,
          c.size_units("SOLUSDT", 0.05))
    check("units round-trip to size", c.units_to_size("SOLUSDT", 7) == 0.7,
          c.units_to_size("SOLUSDT", 7))
    check("integer grid round-trips", c.units_to_size("XRPUSDT", 37) == 37.0,
          c.units_to_size("XRPUSDT", 37))


def test_hold_side():
    """
    Regression: Bitget's position endpoint reports holdSide as "buy"/"sell"
    in one-way mode, but the TPSL endpoints only accept "long"/"short" and
    reject anything else with code 43011. _on_fill trusted the position
    value verbatim, so every take-profit was rejected while the entry and
    its attached stop went through — a live position with no targets.
    """
    print("\nPosition side normalisation")
    c = fake_client()

    check("long stays long", c.norm_hold_side("long") == "long")
    check("short stays short", c.norm_hold_side("short") == "short")
    check("buy maps to long", c.norm_hold_side("buy") == "long")
    check("sell maps to short", c.norm_hold_side("sell") == "short")
    check("case and spacing tolerated", c.norm_hold_side(" SELL ") == "short")
    check("empty falls back", c.norm_hold_side("", fallback="short") == "short")
    check("None falls back", c.norm_hold_side(None, fallback="buy") == "long")

    try:
        c.norm_hold_side("sideways")
        check("garbage raises rather than sending a bad order", False)
    except BitgetError:
        check("garbage raises rather than sending a bad order", True)

    # The TPSL payload must never carry buy/sell.
    sent = {}

    class CaptureClient(BitgetClient):
        def __init__(self):
            super().__init__("", "", "")
            self._specs = dict(FAKE_SPECS)

        def _request(self, method, path, params=None, body=None, **kw):
            sent[path] = body
            return {}

    cc = CaptureClient()
    cc.place_partial_tp("SOLUSDT", "sell", 0.2, 73.7)
    payload = sent.get("/api/v2/mix/order/place-tpsl-order", {})
    check("partial TP sends long/short, never buy/sell",
          payload.get("holdSide") == "short", payload.get("holdSide"))

    sent.clear()
    cc.set_position_stop("SOLUSDT", "buy", 76.3)
    payload = sent.get("/api/v2/mix/order/place-tpsl-order", {})
    check("break-even stop sends long/short too",
          payload.get("holdSide") == "long", payload.get("holdSide"))

    # And the whole fill path, end to end.
    import faruexee_trade_bot as T
    bot = T.TradeBot.__new__(T.TradeBot)
    bot.cfg = EngineConfig()
    bot.client = fake_client()
    bot.live = False
    bot.offline = True
    bot.equity = 1000.0
    bot.state = {"orders": {}, "positions": {},
                 "daily": {"date": T.utc_date(), "start_equity": 1000.0},
                 "halted": False, "halt_reason": "", "baseline_equity": 1000.0}
    rec = {"symbol": "SOLUSDT", "timeframe": "1H", "zone_id": "z",
           "side": "sell", "entry": 75.4, "sl": 76.3, "tp1": 73.7,
           "tp2": None, "tp3": None, "rr": 2.0, "size": 0.4,
           "risk_usdt": 0.35, "placed_at": T.now_iso()}
    import io
    import contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        bot._on_fill("o1", rec,
                     {"symbol": "SOLUSDT", "holdSide": "sell", "total": "0.4"})
    check("_on_fill stores a normalised side",
          bot.state["positions"]["SOLUSDT"]["hold_side"] == "short",
          bot.state["positions"]["SOLUSDT"]["hold_side"])


def test_orphan_adoption():
    """
    A host without persistent storage loses the state file on every
    restart, so a position opened before a redeploy comes back as an
    orphan. Ignoring orphans leaves live positions with no take-profit
    and no break-even move, and silently makes the operator place
    targets by hand after every deploy.
    """
    print("\nOrphan position adoption")
    import faruexee_trade_bot as T

    POS = {"symbol": "BTCUSDT", "holdSide": "sell", "total": "0.0009",
           "openPriceAvg": "64775.5"}

    def build(plans):
        class Stub(BitgetClient):
            def __init__(self):
                super().__init__("", "", "")
                self._specs = dict(FAKE_SPECS)
                self.tps = []

            def get_plan_orders(self, symbol=None, plan_type="profit_loss"):
                return plans

            def place_partial_tp(self, sym, hold, size, px, **kw):
                self.tps.append((size, px))
                return {}

        b = T.TradeBot.__new__(T.TradeBot)
        b.cfg = EngineConfig()
        b.client = Stub()
        b.live = True
        b.offline = False
        b.equity = 20.96
        b.state = {"orders": {}, "positions": {}, "daily": {},
                   "halted": False, "halt_reason": "",
                   "baseline_equity": 20.99, "cooldowns": {}}
        return b

    alerts = []
    original_notify = T.notify
    T.notify = lambda title, lines, color=0: alerts.append(title)
    import io
    import contextlib

    try:
        # Stop present, no target -> adopt and place one.
        bot = build([{"planType": "pos_loss", "triggerPrice": "65240.7"}])
        with contextlib.redirect_stdout(io.StringIO()):
            bot._handle_orphan(POS)
        pos = bot.state["positions"]["BTCUSDT"]
        check("orphan with a stop is adopted", pos.get("adopted") is True)
        check("orphan is managed, not ignored", pos.get("unmanaged") is False)
        check("entry read from the exchange", pos["entry"] == 64775.5, pos["entry"])
        check("stop read from plan orders", pos["sl"] == 65240.7, pos["sl"])
        check("a target is placed for the full size",
              len(bot.client.tps) == 1 and abs(bot.client.tps[0][0] - 0.0009) < 1e-9,
              bot.client.tps)
        # Short: target must sit BELOW entry.
        check("adopted target is on the profitable side",
              bot.client.tps[0][1] < 64775.5, bot.client.tps[0][1])

        # Already has a target -> do not duplicate.
        alerts.clear()
        bot = build([{"planType": "pos_loss", "triggerPrice": "65240.7"},
                     {"planType": "profit_plan", "triggerPrice": "63845"}])
        with contextlib.redirect_stdout(io.StringIO()):
            bot._handle_orphan(POS)
        check("existing target is not duplicated", bot.client.tps == [],
              bot.client.tps)

        # No stop at all -> loud alert, that is unbounded risk.
        alerts.clear()
        bot = build([])
        with contextlib.redirect_stdout(io.StringIO()):
            bot._handle_orphan(POS)
        check("a position with no stop raises an alert",
              any("NO STOP LOSS" in a for a in alerts), alerts)
    finally:
        T.notify = original_notify


def test_tp_ladder():
    print("\nTake-profit ladder")
    bot = make_bot(1000.0)
    bot.state["positions"]["SOLUSDT"] = {
        "symbol": "SOLUSDT", "hold_side": "short", "orig_size": 10.0,
        "entry": 75.0, "sl": 76.0, "tp1": 73.0, "tp2": 72.0, "tp3": 71.0,
        "tps_placed": False,
    }
    bot._place_tp_ladder("SOLUSDT")
    check("ladder marked placed", bot.state["positions"]["SOLUSDT"]["tps_placed"])

    bot2 = make_bot(1000.0)
    bot2.state["positions"]["SOLUSDT"] = {
        "symbol": "SOLUSDT", "hold_side": "long", "orig_size": 10.0,
        "entry": 70.0, "sl": 69.0, "tp1": 73.0, "tp2": None, "tp3": None,
        "tps_placed": False,
    }
    bot2._place_tp_ladder("SOLUSDT")
    check("single TP does not crash", bot2.state["positions"]["SOLUSDT"]["tps_placed"])

    check("splits sum to 1.0", abs(sum(C.TP_SPLIT) - 1.0) < 1e-9)


# =============================================================
#   CONFIG GUARDS
# =============================================================

def test_config():
    print("\nConfiguration guards")
    check("shipped defaults validate", C.validate() == [], C.validate())
    check("risk x concurrency within portfolio cap",
          C.RISK_PER_TRADE * C.MAX_CONCURRENT <= C.MAX_PORTFOLIO_RISK + 1e-9)
    check("HTF mode defaults to ema", C.HTF_BIAS_MODE == "ema")
    check("live trading is off by default unless explicitly set",
          C.LIVE_TRADING is False or os.environ.get("LIVE_TRADING"))


# =============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  FARUEXEE TRADE BOT  —  risk and engine tests")
    print("=" * 60)

    test_rounding()
    test_sizing()
    test_caps()
    test_geometry()
    test_engine()
    test_size_units()
    test_hold_side()
    test_orphan_adoption()
    test_tp_coverage()
    test_tp_ladder()
    test_config()

    print("\n" + "=" * 60)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 60 + "\n")
    sys.exit(1 if FAIL else 0)
