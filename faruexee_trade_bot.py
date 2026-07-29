# =============================================================
#   FARUEXEE TRADE BOT  —  automated execution on Bitget futures
# =============================================================
#
#   Pipeline per scan:
#     1. refresh equity, enforce circuit breakers
#     2. reconcile resting orders (filled? stale? zone invalidated?)
#     3. manage open positions (TP1 hit -> stop to break-even; closed -> journal)
#     4. scan for new zones and place resting limit entries
#
#   Safety properties this file is built around:
#     • no entry order is ever sent without an attached stop loss
#     • no more than MAX_CONCURRENT positions/orders exist at once
#     • one symbol holds at most one position or order, ever
#     • a daily loss breach cancels resting orders and halts new entries
#     • DRY_RUN blocks every state-changing exchange call
#     • restart is safe: live positions and orders are reconciled first
#
#   Run:  python faruexee_trade_bot.py
# =============================================================

import csv
import json
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone

import requests

import trade_config as C
from bitget_client import BitgetClient, BitgetError
from faruexee_engine import EngineConfig, analyze, compute_htf_bias, tradeable_zones


# =============================================================
#   SMALL HELPERS
# =============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def utc_date():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def log(msg, prefix="  "):
    print(f"{prefix}{msg}", flush=True)


def engine_config():
    return EngineConfig(
        lookback=C.LOOKBACK,
        impulse_strength=C.IMPULSE_STRENGTH,
        trend_stability=C.TREND_STABILITY,
        volume_mult=C.VOLUME_MULT,
        require_volume=C.REQUIRE_VOLUME,
        use_base_candle=C.USE_BASE_CANDLE,
        use_atr_sl=C.USE_ATR_SL,
        atr_len=C.ATR_LEN,
        atr_mult=C.ATR_MULT,
        sl_buffer=C.SL_BUFFER,
        min_rr=C.MIN_RR,
        tp_multi=C.TP_MULTI,
        zone_max_age=C.ZONE_MAX_AGE,
        breach_buf_mult=C.BREACH_BUF_MULT,
        fvg_recent_bars=C.FVG_RECENT_BARS,
        require_htf=C.REQUIRE_HTF,
        htf_bias_mode=C.HTF_BIAS_MODE,
        htf_ema_len=C.HTF_EMA_LEN,
        htf_flat_blocks=C.HTF_FLAT_BLOCKS,
    )


# =============================================================
#   DISCORD
# =============================================================

def notify(title, lines, color=0x2F3136):
    if not C.DISCORD_WEBHOOK:
        return
    embed = {
        "title": title,
        "description": "\n".join(lines),
        "color": color,
        "footer": {"text": "FARUEXEE Trade Bot"},
        "timestamp": now_iso(),
    }
    try:
        requests.post(C.DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        log(f"[WARN] Discord notify failed: {e}")


GREEN, RED, ORANGE, BLUE = 0x00C853, 0xD50000, 0xFF6D00, 0x2979FF


# =============================================================
#   TRADE BOT
# =============================================================

class TradeBot:

    def __init__(self):
        self.cfg = engine_config()
        self.client = BitgetClient(
            C.BITGET_API_KEY, C.BITGET_API_SECRET, C.BITGET_PASSPHRASE,
            product_type=C.PRODUCT_TYPE, margin_coin=C.MARGIN_COIN,
        )
        self.state = self._load_state()
        self.live = C.LIVE_TRADING and not C.DRY_RUN
        # Offline = dry run with no credentials. Public market data still
        # flows; account state is simulated so the bot can be exercised
        # end to end before an API key exists.
        self.offline = not self.live and not C.BITGET_API_KEY
        self.equity = 0.0
        self.cycle_count = 0
        self.last_cycle_ts = 0.0
        self.started_at = now_iso()

    # ---------------------------------------------------------
    #   ACCOUNT ACCESS  (simulated when offline)
    # ---------------------------------------------------------

    def _equity(self):
        return C.PAPER_EQUITY if self.offline else self.client.get_equity()

    def _available(self):
        return C.PAPER_EQUITY if self.offline else self.client.get_available()

    def _positions(self):
        return [] if self.offline else self.client.get_positions()

    def _pending(self):
        return [] if self.offline else self.client.get_pending_orders()

    def _history(self, symbol):
        if self.offline:
            return []
        return self.client.get_position_history(symbol=symbol, limit=10)

    # ---------------------------------------------------------
    #   STATE
    # ---------------------------------------------------------

    def _load_state(self):
        if os.path.exists(C.STATE_FILE):
            try:
                with open(C.STATE_FILE, "r") as f:
                    s = json.load(f)
            except Exception as e:
                log(f"[WARN] state file unreadable ({e}) — starting fresh")
                s = {}
        else:
            s = {}
        s.setdefault("orders", {})
        s.setdefault("positions", {})
        s.setdefault("daily", {"date": utc_date(), "start_equity": None})
        s.setdefault("halted", False)
        s.setdefault("halt_reason", "")
        s.setdefault("baseline_equity", None)
        return s

    def _save_state(self):
        tmp = C.STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, C.STATE_FILE)

    def journal(self, row):
        exists = os.path.exists(C.JOURNAL_FILE)
        fields = [
            "closed_at", "symbol", "timeframe", "side", "zone_id",
            "entry", "sl", "tp1", "tp2", "tp3", "rr",
            "size", "risk_usdt", "net_pnl", "outcome", "opened_at",
        ]
        safe = {k: row.get(k, "") for k in fields}
        try:
            with open(C.JOURNAL_FILE, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                if not exists:
                    w.writeheader()
                w.writerow(safe)
        except Exception as e:
            log(f"[WARN] journal write failed: {e}")

    # ---------------------------------------------------------
    #   STARTUP
    # ---------------------------------------------------------

    def _check_storage(self):
        """
        A wiped state file resets the daily-loss baseline and clears the
        halt flag. On a platform with an ephemeral filesystem that happens
        on every restart, so the operator needs to know about it.
        """
        if not C.WARN_EPHEMERAL:
            return

        state_existed = os.path.exists(C.STATE_FILE)
        on_disk = bool(C.DATA_DIR)

        if on_disk:
            log(f"Persistent data directory: {C.DATA_DIR}")
            return

        banner = [
            "",
            "  " + "!" * 62,
            "  EPHEMERAL STORAGE — DATA_DIR is not set.",
            "",
            f"  State   : {C.STATE_FILE}",
            f"  Journal : {C.JOURNAL_FILE}",
            "",
            "  If this host wipes its filesystem on restart (Render without a",
            "  persistent disk, most container platforms), then on every restart:",
            "    - the daily-loss baseline resets, so the 6% daily stop restarts",
            "    - the halt flag clears, so a halted bot resumes trading",
            "    - the trade journal is destroyed",
            "",
            "  Open positions stay safe: stops and take-profits live on the",
            "  exchange, and startup reconciles against it. But the risk",
            "  breakers above are only as durable as this directory.",
            "",
            "  Fix: attach a disk and set DATA_DIR to its mount path.",
            "  Silence: WARN_EPHEMERAL=false",
            "  " + "!" * 62,
            "",
        ]
        print("\n".join(banner), flush=True)

        if self.live:
            notify("Running on ephemeral storage", [
                "`DATA_DIR` is not set, so the daily-loss baseline, the halt "
                "flag and the trade journal will not survive a restart.",
                "Open positions stay protected by exchange-side stops.",
                "Attach a persistent disk and set `DATA_DIR` to fix this.",
            ], ORANGE)

        if not state_existed:
            log("No prior state file — starting with a fresh book")

    def startup(self):
        self._check_storage()

        log("Loading contract specifications...")
        self.client.load_contracts()

        missing = [s for s in C.SYMBOLS if s not in self.client._specs]
        if missing:
            raise SystemExit(
                f"[FATAL] symbols not tradeable on {C.PRODUCT_TYPE}: {', '.join(missing)}"
            )

        self.equity = self._equity()
        log(f"Account equity: {self.equity:.2f} {C.MARGIN_COIN}")

        if self.equity < C.MIN_EQUITY_USDT:
            raise SystemExit(
                f"[FATAL] equity {self.equity:.2f} below MIN_EQUITY_USDT {C.MIN_EQUITY_USDT}"
            )

        if self.state.get("baseline_equity") is None:
            self.state["baseline_equity"] = self.equity

        if self.state["daily"].get("start_equity") is None or \
                self.state["daily"].get("date") != utc_date():
            self.state["daily"] = {"date": utc_date(), "start_equity": self.equity}

        if self.live:
            self._configure_symbols()
        else:
            log("DRY RUN — skipping exchange configuration writes")

        self._reconcile()
        self._save_state()

    def _configure_symbols(self):
        """One-way mode, isolated margin, fixed leverage. Failures are logged,
        not fatal — Bitget rejects these when a position is already open."""
        try:
            self.client.set_position_mode(one_way=True)
            log("Position mode: one-way")
        except BitgetError as e:
            log(f"[WARN] could not set one-way mode: {e}")

        for sym in C.SYMBOLS:
            try:
                self.client.set_margin_mode(sym, C.MARGIN_MODE)
            except BitgetError as e:
                log(f"[WARN] {sym} margin mode: {e}")
            try:
                self.client.set_leverage(sym, C.LEVERAGE)
            except BitgetError as e:
                log(f"[WARN] {sym} leverage: {e}")
        log(f"Configured {len(C.SYMBOLS)} symbols at {C.LEVERAGE}x {C.MARGIN_MODE}")

    def _reconcile(self):
        """
        Sync state with reality so a restart never double-trades.
        Anything open on the exchange that we do not recognise is recorded
        as unmanaged and counted against the concurrency cap.
        """
        try:
            live_positions = self._positions()
            live_orders = self._pending()
        except BitgetError as e:
            log(f"[WARN] reconcile failed ({e}) — continuing with stored state")
            return

        live_pos_symbols = {p["symbol"] for p in live_positions}
        live_order_oids = {o.get("clientOid") for o in live_orders}

        # Drop tracked orders that no longer exist and never became positions.
        for oid in list(self.state["orders"].keys()):
            rec = self.state["orders"][oid]
            if oid not in live_order_oids and rec["symbol"] not in live_pos_symbols:
                log(f"Reconcile: order {rec['symbol']} {oid[:12]} gone — clearing")
                del self.state["orders"][oid]

        # Drop tracked positions the exchange no longer has.
        for sym in list(self.state["positions"].keys()):
            if sym not in live_pos_symbols:
                log(f"Reconcile: position {sym} already closed — journalling")
                self._close_out(sym, reason="closed_while_offline")

        # Record positions we did not open (manual trades, prior runs).
        for p in live_positions:
            sym = p["symbol"]
            if sym not in self.state["positions"]:
                log(f"Reconcile: found unmanaged position on {sym} — will not touch it")
                self.state["positions"][sym] = {
                    "unmanaged": True,
                    "hold_side": p.get("holdSide"),
                    "opened_at": now_iso(),
                    "symbol": sym,
                }

        log(f"Reconciled: {len(self.state['positions'])} position(s), "
            f"{len(self.state['orders'])} resting order(s)")

    # ---------------------------------------------------------
    #   CAPACITY / CIRCUIT BREAKERS
    # ---------------------------------------------------------

    def slots_used(self):
        return len(self.state["positions"]) + len(self.state["orders"])

    def symbol_busy(self, symbol):
        if symbol in self.state["positions"]:
            return True
        return any(o["symbol"] == symbol for o in self.state["orders"].values())

    def zone_already_ordered(self, zone_id):
        return any(o["zone_id"] == zone_id for o in self.state["orders"].values())

    def _utc_midnight_ms(self):
        now = datetime.now(timezone.utc)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(midnight.timestamp() * 1000)

    def _exchange_daily_drawdown(self):
        """
        Today's realised loss as a fraction of equity, read from Bitget's
        position history rather than the local state file.

        Without this the daily stop measures from a baseline stored on
        disk — so on an ephemeral host every restart resets it and the
        limit silently stops existing. This path survives restarts because
        the exchange remembers what the bot forgot.

        Returns None when unavailable; callers fall back to the local
        baseline rather than treating unknown as safe.
        """
        if self.offline:
            return None
        try:
            realized = self.client.get_realized_pnl_since(self._utc_midnight_ms())
        except BitgetError as e:
            log(f"[WARN] realised PnL lookup failed: {e}")
            return None

        if realized >= 0:
            return 0.0
        # Equity already reflects the loss, so today's opening equity was
        # roughly equity - realized (realized being negative here).
        opening = self.equity - realized
        if opening <= 0:
            return None
        return (-realized) / opening

    def check_breakers(self):
        """Returns True when trading may continue."""
        daily = self.state["daily"]

        if daily.get("date") != utc_date():
            log(f"New UTC day — resetting daily loss counter "
                f"(was {daily.get('date')})")
            self.state["daily"] = {"date": utc_date(), "start_equity": self.equity}
            if self.state.get("halted") and self.state.get("halt_reason") == "daily_loss":
                self.state["halted"] = False
                self.state["halt_reason"] = ""
                notify("Trading resumed", ["New UTC day — daily loss stop reset."], BLUE)
            daily = self.state["daily"]

        start = daily.get("start_equity") or self.equity
        local_dd = (start - self.equity) / start if start > 0 else 0.0

        # Take the worse of the local baseline and the exchange's own
        # record of today's realised losses. The local number can be
        # reset by a restart; the exchange number cannot.
        exch_dd = self._exchange_daily_drawdown()
        drawdown = local_dd if exch_dd is None else max(local_dd, exch_dd)
        if exch_dd is not None and exch_dd > local_dd + 0.005:
            log(f"Daily drawdown from exchange history: {exch_dd:.2%} "
                f"(local baseline said {local_dd:.2%})")

        if drawdown >= C.MAX_DAILY_LOSS and not self.state["halted"]:
            self.state["halted"] = True
            self.state["halt_reason"] = "daily_loss"
            log(f"[HALT] daily drawdown {drawdown:.2%} >= {C.MAX_DAILY_LOSS:.2%}")
            self._cancel_all_resting("daily loss stop")
            notify("Trading halted — daily loss stop", [
                f"Drawdown today: **{drawdown:.2%}**",
                f"Limit: {C.MAX_DAILY_LOSS:.2%}",
                f"Equity: {self.equity:.2f} {C.MARGIN_COIN}",
                "Resting orders cancelled. Open positions keep their stops.",
                "New entries resume at the next UTC day.",
            ], RED)

        baseline = self.state.get("baseline_equity") or self.equity
        if self.equity < baseline * C.MIN_EQUITY_FRACTION and not self.state["halted"]:
            self.state["halted"] = True
            self.state["halt_reason"] = "equity_floor"
            log(f"[HALT] equity {self.equity:.2f} below floor "
                f"{baseline * C.MIN_EQUITY_FRACTION:.2f}")
            self._cancel_all_resting("equity floor breached")
            notify("Trading halted — equity floor", [
                f"Equity {self.equity:.2f} is below "
                f"{C.MIN_EQUITY_FRACTION:.0%} of the starting {baseline:.2f}.",
                "This halt does not auto-reset. Investigate before restarting.",
            ], RED)

        return not self.state["halted"]

    def _cancel_all_resting(self, reason):
        for oid, rec in list(self.state["orders"].items()):
            self._cancel_order(oid, rec, reason)

    # ---------------------------------------------------------
    #   ORDER MANAGEMENT
    # ---------------------------------------------------------

    def _cancel_order(self, oid, rec, reason):
        log(f"Cancelling {rec['symbol']} {rec['side']} — {reason}")
        if self.live:
            try:
                self.client.cancel_order(rec["symbol"], client_oid=oid)
            except BitgetError as e:
                # Already gone is fine; anything else is worth surfacing.
                if e.code not in ("22001", "43001", "40109"):
                    log(f"[WARN] cancel failed for {rec['symbol']}: {e}")
        self.state["orders"].pop(oid, None)

    def manage_orders(self, analyses):
        """Check every resting entry for fill, expiry or zone invalidation."""
        for oid, rec in list(self.state["orders"].items()):
            sym = rec["symbol"]

            # 1. Did it fill?
            filled = False
            if self.live:
                try:
                    detail = self.client.get_order(sym, client_oid=oid)
                    state = (detail or {}).get("state", "")
                    if state == "filled":
                        filled = True
                    elif state in ("cancelled", "canceled"):
                        log(f"{sym}: order cancelled externally — clearing")
                        self.state["orders"].pop(oid, None)
                        continue
                except BitgetError as e:
                    log(f"[WARN] order lookup {sym}: {e}")

            if filled:
                self._on_fill(oid, rec)
                continue

            # 2. Expired?
            placed = datetime.fromisoformat(rec["placed_at"])
            age_h = (datetime.now(timezone.utc) - placed).total_seconds() / 3600
            if age_h >= C.ORDER_TTL_HOURS:
                self._cancel_order(oid, rec, f"unfilled for {age_h:.0f}h")
                continue

            # 3. Zone invalidated? The engine drops breached and expired zones,
            #    so a zone that vanished from the analysis is no longer valid.
            key = (sym, rec["timeframe"])
            res = analyses.get(key)
            if res and res["ok"]:
                still_valid = any(z.zone_id == rec["zone_id"] for z in res["zones"])
                if not still_valid:
                    self._cancel_order(oid, rec, "zone invalidated or breached")
                    continue

    def _on_fill(self, oid, rec):
        sym = rec["symbol"]
        hold_side = "long" if rec["side"] == "buy" else "short"
        log(f"FILLED {sym} {rec['side']} @ {rec['entry']}")

        self.state["positions"][sym] = {
            "symbol": sym,
            "timeframe": rec["timeframe"],
            "zone_id": rec["zone_id"],
            "side": rec["side"],
            "hold_side": hold_side,
            "entry": rec["entry"],
            "sl": rec["sl"],
            "tp1": rec["tp1"], "tp2": rec["tp2"], "tp3": rec["tp3"],
            "rr": rec["rr"],
            "orig_size": rec["size"],
            "risk_usdt": rec["risk_usdt"],
            "opened_at": now_iso(),
            "tps_placed": False,
            "be_moved": False,
            "unmanaged": False,
        }
        self.state["orders"].pop(oid, None)
        self._place_tp_ladder(sym)

        pos = self.state["positions"][sym]
        notify(f"Position opened — {sym} {rec['side'].upper()}", [
            f"Timeframe: {rec['timeframe']}",
            f"Entry: `{rec['entry']}`   Size: `{rec['size']}`",
            f"Stop: `{rec['sl']}`   Risk: `{rec['risk_usdt']:.2f}` {C.MARGIN_COIN}",
            f"TP1: `{rec['tp1']}`" + (f"   TP2: `{rec['tp2']}`" if rec['tp2'] else "")
            + (f"   TP3: `{rec['tp3']}`" if rec['tp3'] else ""),
            f"R:R to TP1: {pos['rr']:.2f}",
        ], GREEN if rec["side"] == "buy" else RED)

    def _place_tp_ladder(self, sym):
        """Split the position across TP1/TP2/TP3. The stop is already on the
        exchange from presetStopLossPrice, so a failure here is not fatal."""
        pos = self.state["positions"][sym]
        size = pos["orig_size"]
        targets = [(pos["tp1"], C.TP_SPLIT[0]),
                   (pos["tp2"], C.TP_SPLIT[1]),
                   (pos["tp3"], C.TP_SPLIT[2])]
        active = [(px, w) for px, w in targets if px is not None]

        if not active:
            return
        # Only TP1 exists -> put the whole position on it.
        if len(active) == 1:
            active = [(active[0][0], 1.0)]
        else:
            total_w = sum(w for _, w in active)
            active = [(px, w / total_w) for px, w in active]

        placed = 0.0
        for idx, (px, weight) in enumerate(active):
            last = idx == len(active) - 1
            raw = size - placed if last else size * weight
            qty = self.client.round_size(sym, raw)
            if qty <= 0:
                continue
            price = self.client.round_price(sym, px)
            if self.live:
                try:
                    self.client.place_partial_tp(
                        sym, pos["hold_side"], qty, price,
                        client_oid=f"tp{idx+1}-{uuid.uuid4().hex[:12]}",
                        trigger_type=C.TRIGGER_TYPE,
                    )
                except BitgetError as e:
                    log(f"[WARN] TP{idx+1} placement failed on {sym}: {e}")
                    continue
            placed += qty
            log(f"  TP{idx+1} {sym}: {qty} @ {price}")

        pos["tps_placed"] = True

    # ---------------------------------------------------------
    #   POSITION MANAGEMENT
    # ---------------------------------------------------------

    def manage_positions(self):
        try:
            live_positions = {p["symbol"]: p for p in self._positions()}
        except BitgetError as e:
            log(f"[WARN] position fetch failed: {e}")
            return

        for sym in list(self.state["positions"].keys()):
            pos = self.state["positions"][sym]

            if sym not in live_positions:
                self._close_out(sym, reason="closed")
                continue

            if pos.get("unmanaged"):
                continue

            live = live_positions[sym]
            remaining = float(live.get("total", 0) or 0)

            # Partial fill on TP1 shrinks the position — move the stop to
            # break-even so the rest of the trade cannot become a loser.
            if (C.MOVE_SL_TO_BE and not pos.get("be_moved")
                    and remaining < pos["orig_size"] * 0.99):
                self._move_to_breakeven(sym, pos)

    def _move_to_breakeven(self, sym, pos):
        entry = pos["entry"]
        risk = abs(entry - pos["sl"])
        offset = risk * C.BE_OFFSET_R
        be = entry + offset if pos["hold_side"] == "long" else entry - offset
        be = self.client.round_price(sym, be)

        log(f"{sym}: TP1 filled — moving stop to break-even {be}")
        if self.live:
            try:
                self.client.set_position_stop(
                    sym, pos["hold_side"], be,
                    client_oid=f"be-{uuid.uuid4().hex[:12]}",
                    trigger_type=C.TRIGGER_TYPE,
                )
            except BitgetError as e:
                log(f"[WARN] break-even move failed on {sym}: {e}")
                return

        pos["be_moved"] = True
        pos["sl"] = be
        notify(f"Stop moved to break-even — {sym}", [
            f"TP1 filled. Remaining position now risk-free at `{be}`.",
        ], BLUE)

    def _close_out(self, sym, reason="closed"):
        """Position gone from the exchange — journal it and free the slot."""
        pos = self.state["positions"].pop(sym, None)
        if not pos or pos.get("unmanaged"):
            return

        net_pnl = ""
        outcome = reason
        try:
            for h in self._history(sym):
                if h.get("symbol") == sym:
                    net_pnl = h.get("netProfit", "")
                    break
        except BitgetError as e:
            log(f"[WARN] history lookup for {sym}: {e}")

        try:
            if net_pnl != "":
                outcome = "win" if float(net_pnl) > 0 else "loss"
        except (TypeError, ValueError):
            pass

        self.journal({
            "closed_at": now_iso(),
            "symbol": sym,
            "timeframe": pos.get("timeframe", ""),
            "side": pos.get("side", ""),
            "zone_id": pos.get("zone_id", ""),
            "entry": pos.get("entry", ""),
            "sl": pos.get("sl", ""),
            "tp1": pos.get("tp1", ""), "tp2": pos.get("tp2", ""),
            "tp3": pos.get("tp3", ""), "rr": pos.get("rr", ""),
            "size": pos.get("orig_size", ""),
            "risk_usdt": pos.get("risk_usdt", ""),
            "net_pnl": net_pnl,
            "outcome": outcome,
            "opened_at": pos.get("opened_at", ""),
        })

        log(f"{sym}: position closed ({outcome}) net {net_pnl}")

        # This message is a complete journal row on purpose. On a host with
        # an ephemeral filesystem the CSV is wiped on every restart, so the
        # Discord channel becomes the durable record of what actually
        # happened — which is the only way to learn whether the strategy
        # has an edge.
        held = ""
        try:
            opened = datetime.fromisoformat(pos.get("opened_at", ""))
            hours = (datetime.now(timezone.utc) - opened).total_seconds() / 3600
            held = f"{hours:.1f}h" if hours < 48 else f"{hours / 24:.1f}d"
        except (TypeError, ValueError):
            pass

        risk = pos.get("risk_usdt")
        r_multiple = ""
        try:
            if net_pnl != "" and risk:
                r_multiple = f"  ({float(net_pnl) / float(risk):+.2f}R)"
        except (TypeError, ValueError, ZeroDivisionError):
            pass

        tps = " / ".join(
            str(pos.get(k)) for k in ("tp1", "tp2", "tp3") if pos.get(k) is not None
        )

        notify(f"Position closed — {sym} {str(pos.get('side', '')).upper()}", [
            f"Outcome: **{outcome}**",
            (f"Net PnL: `{net_pnl}` {C.MARGIN_COIN}{r_multiple}"
             if net_pnl != "" else "Net PnL: unavailable"),
            "",
            f"Timeframe `{pos.get('timeframe')}`   Held `{held or 'n/a'}`",
            f"Entry `{pos.get('entry')}`   Stop `{pos.get('sl')}`"
            + ("  (moved to BE)" if pos.get("be_moved") else ""),
            f"Targets `{tps or 'n/a'}`",
            f"Size `{pos.get('orig_size')}`   Risked `{risk}` {C.MARGIN_COIN}"
            f"   Planned R:R `{pos.get('rr')}`",
            f"Opened `{pos.get('opened_at', '')[:19]}`",
        ], GREEN if outcome == "win" else RED if outcome == "loss" else ORANGE)

    # ---------------------------------------------------------
    #   SIZING
    # ---------------------------------------------------------

    def size_position(self, symbol, entry, sl, available):
        """
        Returns (size, risk_usdt, note) — size 0.0 means do not trade.
        Risk-first: size = risk budget / stop distance, then clamped by
        notional and margin caps.
        """
        sl_dist = abs(entry - sl)
        if sl_dist <= 0:
            return 0.0, 0.0, "stop distance is zero"

        risk_usdt = self.equity * C.RISK_PER_TRADE
        raw = risk_usdt / sl_dist
        note = ""

        max_notional = self.equity * C.MAX_NOTIONAL_X_EQUITY
        if raw * entry > max_notional:
            raw = max_notional / entry
            note = f"size capped by MAX_NOTIONAL_X_EQUITY ({C.MAX_NOTIONAL_X_EQUITY}x)"

        margin_budget = available * C.MAX_MARGIN_FRACTION
        max_by_margin = (margin_budget * C.LEVERAGE) / entry
        if raw > max_by_margin:
            raw = max_by_margin
            note = f"size capped by free margin ({C.MAX_MARGIN_FRACTION:.0%})"

        size = self.client.round_size(symbol, raw)
        if size <= 0:
            return 0.0, 0.0, "size below exchange minimum after rounding"

        notional = size * entry
        if notional < self.client.min_notional(symbol):
            return 0.0, 0.0, f"notional {notional:.2f} below exchange minimum"

        actual_risk = size * sl_dist
        return size, actual_risk, note

    # ---------------------------------------------------------
    #   ENTRY
    # ---------------------------------------------------------

    def try_enter(self, symbol, timeframe, zone, price):
        if self.state["halted"]:
            return False
        if self.slots_used() >= C.MAX_CONCURRENT:
            return False
        if self.symbol_busy(symbol):
            return False
        if self.zone_already_ordered(zone.zone_id):
            return False

        entry = self.client.round_price(
            symbol, zone.entry, "down" if zone.side == "buy" else "up"
        )
        sl = self.client.round_price(
            symbol, zone.sl, "down" if zone.side == "buy" else "up"
        )

        # Sanity: the stop must sit on the losing side of the entry.
        if zone.side == "buy" and not (sl < entry < price):
            log(f"  {symbol} {timeframe}: rejected — bad long geometry "
                f"(sl {sl}, entry {entry}, price {price})")
            return False
        if zone.side == "sell" and not (sl > entry > price):
            log(f"  {symbol} {timeframe}: rejected — bad short geometry "
                f"(sl {sl}, entry {entry}, price {price})")
            return False

        distance = abs(entry - price) / price
        if distance > C.MAX_ENTRY_DISTANCE:
            return False

        # Quantise the targets onto the tick grid too. The engine works in
        # raw floats; without this the order record and the Discord message
        # carry values like 73.71553663632267, and the stored TPs would not
        # match what actually gets placed after the fill.
        #
        # Rounding is toward the entry — a target a tick nearer is a target
        # that fills.
        tp_mode = "down" if zone.side == "buy" else "up"
        tp1 = self.client.round_price(symbol, zone.tp1, tp_mode)
        tp2 = (self.client.round_price(symbol, zone.tp2, tp_mode)
               if zone.tp2 is not None else None)
        tp3 = (self.client.round_price(symbol, zone.tp3, tp_mode)
               if zone.tp3 is not None else None)

        # R:R must be re-checked against the prices actually being sent.
        # Rounding moves entry, stop and target independently, so a setup
        # sitting on the threshold can fall under it here.
        sl_dist = abs(entry - sl)
        rr = abs(tp1 - entry) / sl_dist if sl_dist > 0 else 0.0
        if rr < C.MIN_RR:
            log(f"  {symbol} {timeframe}: skipped — R:R {rr:.2f} below "
                f"{C.MIN_RR} after tick rounding")
            return False

        try:
            available = self._available()
        except BitgetError as e:
            log(f"[WARN] balance fetch failed: {e}")
            return False

        size, risk_usdt, note = self.size_position(symbol, entry, sl, available)
        if size <= 0:
            log(f"  {symbol} {timeframe}: skipped — {note}")
            return False
        if note:
            log(f"  {symbol} {timeframe}: {note}")

        oid = f"fx-{uuid.uuid4().hex[:20]}"
        log(f"ENTRY {symbol} {timeframe} {zone.side.upper()} "
            f"size {size} @ {entry}  SL {sl}  TP1 {tp1}  "
            f"R:R {rr:.2f}  risk {risk_usdt:.2f}")

        if self.live:
            try:
                self.client.place_limit_entry(
                    symbol, zone.side, size, entry,
                    stop_loss=sl,
                    client_oid=oid,
                    force=C.ORDER_FORCE,
                    margin_mode=C.MARGIN_MODE,
                )
            except BitgetError as e:
                log(f"[ERROR] order rejected on {symbol}: {e}")
                notify(f"Order rejected — {symbol}", [str(e)], ORANGE)
                return False
        else:
            log("  (dry run — no order sent)")

        self.state["orders"][oid] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "zone_id": zone.zone_id,
            "side": zone.side,
            "entry": entry, "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "rr": round(rr, 4),
            "size": size,
            "risk_usdt": risk_usdt,
            "placed_at": now_iso(),
        }

        notify(f"Limit order placed — {symbol} {zone.side.upper()}", [
            f"Timeframe: {timeframe}",
            f"Entry `{entry}`  ({distance:.2%} from `{price}`)",
            f"Stop `{sl}`   Size `{size}`   Risk `{risk_usdt:.2f}` {C.MARGIN_COIN}",
            f"TP1 `{tp1}`" + (f"  TP2 `{tp2}`" if tp2 else "")
            + (f"  TP3 `{tp3}`" if tp3 else ""),
            f"R:R {rr:.2f}   Slot {self.slots_used()}/{C.MAX_CONCURRENT}",
        ], GREEN if zone.side == "buy" else RED)
        return True

    # ---------------------------------------------------------
    #   MAIN CYCLE
    # ---------------------------------------------------------

    def run_once(self):
        print(f"\n{'=' * 60}")
        print(f"  Scan {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"{'=' * 60}")

        try:
            self.equity = self._equity()
        except BitgetError as e:
            log(f"[ERROR] equity fetch failed — skipping cycle: {e}")
            return

        start = self.state["daily"].get("start_equity") or self.equity
        log(f"Equity {self.equity:.2f} {C.MARGIN_COIN}   "
            f"today {((self.equity - start) / start * 100 if start else 0):+.2f}%   "
            f"slots {self.slots_used()}/{C.MAX_CONCURRENT}")

        can_trade = self.check_breakers()

        # ── Gather analyses first; order management needs them ──
        analyses = {}
        htf_cache = {}
        for symbol in C.SYMBOLS:
            for tf in C.TIMEFRAMES:
                try:
                    candles = self.client.get_candles(symbol, tf, C.CANDLE_LIMIT)
                except BitgetError as e:
                    log(f"[WARN] candles {symbol} {tf}: {e}")
                    continue
                if len(candles) < C.LOOKBACK * 2 + 30:
                    continue

                htf_trend = 0
                htf_tf = C.HTF_MAP.get(tf)
                if C.REQUIRE_HTF and htf_tf:
                    ck = (symbol, htf_tf)
                    if ck not in htf_cache:
                        try:
                            htf_candles = self.client.get_candles(symbol, htf_tf, 200)
                            htf_cache[ck] = compute_htf_bias(htf_candles, self.cfg)
                        except BitgetError as e:
                            log(f"[WARN] HTF {symbol} {htf_tf}: {e}")
                            htf_cache[ck] = 0
                    htf_trend = htf_cache[ck]

                analyses[(symbol, tf)] = analyze(candles, self.cfg, htf_trend)

        self.manage_orders(analyses)
        self.manage_positions()

        # ── New entries ──
        if can_trade:
            for symbol in C.SYMBOLS:
                # A full book stops the whole scan, not just this symbol.
                if self.slots_used() >= C.MAX_CONCURRENT:
                    log(f"Concurrency cap reached ({C.MAX_CONCURRENT}) — "
                        f"no further entries this cycle")
                    break
                if self.symbol_busy(symbol):
                    continue
                for tf in C.TIMEFRAMES:
                    res = analyses.get((symbol, tf))
                    if not res or not res["ok"]:
                        continue
                    entered = False
                    for zone in tradeable_zones(res, self.cfg):
                        if self.try_enter(symbol, tf, zone, res["price"]):
                            entered = True
                            break
                    # One order per symbol per cycle — 4H is scanned first,
                    # so the higher timeframe wins when both qualify.
                    if entered:
                        break
        else:
            log(f"Entries suspended — {self.state.get('halt_reason')}")

        self._save_state()
        self.cycle_count += 1
        self.last_cycle_ts = time.time()
        log(f"Cycle complete — {len(self.state['positions'])} position(s), "
            f"{len(self.state['orders'])} resting order(s)")


# =============================================================
#   ENTRYPOINT
# =============================================================

_bot = None


def handle_shutdown(signum, frame):
    log("\nShutdown signal received.")
    if _bot:
        try:
            _bot._save_state()
        except Exception:
            pass
        notify("Trade bot stopped", [
            "Process is shutting down.",
            "Open positions keep their exchange-side stops and take-profits.",
            "Resting limit orders were left in place.",
        ], ORANGE)
    sys.exit(0)


def main():
    global _bot

    errors = C.validate()
    if errors:
        print("\n[FATAL] configuration errors:\n")
        for e in errors:
            print(f"  - {e}")
        print("\nFix these in your environment or .env, then restart.\n")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("  FARUEXEE TRADE BOT")
    print("=" * 60)
    print(C.summary())
    print("=" * 60 + "\n")

    if C.LIVE_TRADING and not C.DRY_RUN:
        print("  *** LIVE TRADING IS ENABLED — REAL FUNDS ARE AT RISK ***")
        print("  Starting in 10 seconds. Ctrl+C to abort.\n")
        time.sleep(10)

    _bot = TradeBot()
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # Render Web Services must bind $PORT or the deploy is marked
    # unhealthy and restarted. Background Workers need no port.
    if C.ENABLE_WEB:
        try:
            from web_status import start_status_server
            start_status_server(_bot, C.WEB_PORT, get_config_summary=C.summary)
            log(f"Status server listening on port {C.WEB_PORT} "
                f"(/, /health, /status)")
        except Exception as e:
            log(f"[WARN] status server failed to start: {e}")

    _bot.startup()

    notify("Trade bot started", [
        f"Mode: **{'LIVE' if _bot.live else 'DRY RUN'}**",
        f"Equity: `{_bot.equity:.2f}` {C.MARGIN_COIN}",
        f"Symbols: {', '.join(C.SYMBOLS)} on {', '.join(C.TIMEFRAMES)}",
        f"Risk {C.RISK_PER_TRADE:.1%} per trade, max {C.MAX_CONCURRENT} concurrent.",
    ], GREEN if _bot.live else BLUE)

    while True:
        try:
            _bot.run_once()
        except SystemExit:
            raise
        except Exception as e:
            log(f"[ERROR] cycle failed: {type(e).__name__}: {e}")
            notify("Cycle error", [f"`{type(e).__name__}: {e}`",
                                   "Bot continues; positions keep their stops."], ORANGE)
        log(f"Next scan in {C.CHECK_INTERVAL}s\n")
        time.sleep(C.CHECK_INTERVAL)


if __name__ == "__main__":
    main()
