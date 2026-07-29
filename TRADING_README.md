# FARUEXEE Trade Bot

Automated execution of the FARUEXEE supply/demand strategy on Bitget USDT-M futures.

Your existing `faruexee_alert_bot.py` is **untouched** and keeps sending Discord alerts
exactly as before. The trade bot is a separate program that runs alongside it.

---

## What it does

Every scan cycle:

1. Reads account equity and enforces the circuit breakers.
2. Reconciles resting orders — filled, expired, or zone-invalidated.
3. Manages open positions — moves the stop to break-even after TP1, journals closes.
4. Scans for new zones and places resting limit entries at the zone edge.

Entries are **resting limit orders** at the zone edge with the stop attached to the
order itself, so the position is never naked on the exchange. Take-profits are placed
as a 50/30/20 ladder once the entry fills.

---

## Files

| File | Purpose |
|---|---|
| `faruexee_engine.py` | Pure strategy maths. No network, no files, no exchange. |
| `bitget_client.py` | Signed Bitget v2 REST client with precision-aware rounding. |
| `trade_config.py` | Every tunable setting, read from environment variables. |
| `faruexee_trade_bot.py` | The bot. Risk, order lifecycle, position management. |
| `smoke_test.py` | Signal check against live public data. No keys, no orders. |
| `test_risk.py` | 60 offline tests for sizing, caps, breakers and engine logic. |
| `.env.example` | Template for your configuration. |

Runtime files (gitignored): `trade_state.json`, `trade_journal.csv`.

---

## Setup

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Verify the strategy produces sane signals — no keys needed**

```bash
python smoke_test.py --equity 1000
```

This prints, for every symbol and timeframe: the trend, the HTF bias, live zones, and
exactly which orders the bot would place with what size and risk. Nothing is sent
anywhere. Read this output carefully before going further.

**3. Run the offline tests**

```bash
python test_risk.py
```

All 60 must pass. They cover position sizing, the notional and margin caps, the
concurrency limit, the daily-loss halt, and entry geometry rejection.

**4. Create your `.env`**

```bash
cp .env.example .env
```

Fill in your Bitget credentials. Create the API key yourself in Bitget under
**API Management**:

- Enable **Trade** permission — needed to place and cancel orders.
- Do **not** enable Withdraw.
- Bind your server IP if the bot runs from a fixed host.

I never handle these values; the bot reads them from the environment at startup.

**5. Watch a dry run against the live market**

With `LIVE_TRADING=false` and `DRY_RUN=true`, the bot does everything except send
orders — it scans, sizes, logs decisions and writes state.

```bash
python faruexee_trade_bot.py
```

Leave it running for a few days. Compare its decisions against the chart. This is
your last checkpoint before real money.

**6. Go live**

In `.env`:

```
LIVE_TRADING=true
DRY_RUN=false
```

The bot prints a warning and waits 10 seconds before starting, so Ctrl+C still saves you.

---

## Risk model

The configuration you chose, and what enforces it:

| Rule | Setting | Enforced by |
|---|---|---|
| 2% of equity risked per trade | `RISK_PER_TRADE=0.02` | `size_position()` — size = risk budget ÷ stop distance |
| Max 3 positions or orders at once | `MAX_CONCURRENT=3` | `slots_used()` checked before every entry |
| 6% total portfolio risk | `MAX_PORTFOLIO_RISK=0.06` | Startup validation rejects configs that exceed it |
| One symbol, one trade | — | `symbol_busy()` — stops 1H and 4H both trading BTC |
| Stop trading after a 6% day | `MAX_DAILY_LOSS=0.06` | Cancels resting orders, halts entries until next UTC day |
| Hard floor at 70% of starting equity | `MIN_EQUITY_FRACTION=0.70` | Halts permanently; does not auto-reset |
| Position notional ≤ 3× equity | `MAX_NOTIONAL_X_EQUITY=3.0` | Caps size when a tight stop would demand a huge position |
| ≤30% of free margin per trade | `MAX_MARGIN_FRACTION=0.30` | Caps size again against available balance |
| No entries further than 5% away | `MAX_ENTRY_DISTANCE=0.05` | A limit order 20% away is not a trade |

When a cap binds, the bot **reduces size** — it never increases risk above the budget.
Tests assert this explicitly.

Position sizing is risk-first, not leverage-first:

```
risk_usdt = equity × 0.02
size      = risk_usdt ÷ |entry − stop|
```

Leverage only determines how much margin the position consumes. Changing `LEVERAGE`
does not change how much you lose when a stop is hit.

---

## Two things you should know

**The strategy is still unbacktested.** You asked for a profitable strategy. What this
delivers is your indicator's logic, executed faithfully and safely. Whether it is
profitable is an open question — the chart labels measure from a more optimistic fill
than you actually get. `trade_journal.csv` records every closed trade with entry, stop,
targets, size, risk and real net PnL from Bitget's position history. After 30–50 trades
that file answers the question honestly. Until then, keep the size small.

**A bug found while building this.** The HTF filter was configured to use the 20/20 pivot
structure trend on the daily. Bitget serves at most 90 daily candles; a 20/20 pivot needs
41 bars to confirm and the stability gate needs two consecutive same-direction pivots. The
daily trend is therefore *permanently* undetermined — verified live on all five majors.

That would have blocked 100% of 4H entries forever. The engine now defaults to
`HTF_BIAS_MODE=ema` (price vs a 50-period HTF EMA), which resolves immediately and does
not repaint. Config validation refuses to start with `structure` mode on a 4H timeframe.

Your alert bot has the same root cause with a different symptom: it treats an
undetermined HTF as "no objection", so its 4H scans have silently had **no HTF filter at
all**. Worth porting the EMA bias across when you get a chance.

---

## Operations

**Discord notifications** — reuses `DISCORD_WEBHOOK`. Sends: order placed, position
opened, stop moved to break-even, position closed with PnL, order rejected, cycle errors,
and both halt types. Point it at a separate channel from the alert bot if you want them
apart.

**Restarting is safe.** On startup the bot reads live positions and pending orders and
reconciles them against its state file. Positions it did not open are recorded as
unmanaged: they count against the concurrency cap but the bot never touches them.

**Stopping the bot does not close anything.** Stops and take-profits live on the
exchange, not in the bot. If the process dies, open positions stay protected. Resting
limit orders also stay — cancel them manually if you are stopping for a while.

**Kill switch.** To halt without stopping the process, set `"halted": true` in
`trade_state.json`. To resume, set it back to `false`.

---

## Tuning, in order of impact

1. **Leave everything alone for 30–50 trades.** Tuning against fewer than that is noise.
2. **Then read the journal.** Win rate, average R, and which timeframe earns its keep.
3. **Adjust `MIN_RR` before anything else.** It is the single highest-leverage filter.
4. **Only then touch `RISK_PER_TRADE`** — and only upward if the journal shows positive
   expectancy over a meaningful sample.

If you want to trade the metals and oil pairs from your alert list (`XAUUSDT`, `XAGUSDT`,
`XPTUSDT`, `CLUSDT`), add them to `TRADE_SYMBOLS` — but run `smoke_test.py` first. They
have different tick sizes, contract minimums and session liquidity than crypto, and the
sizing needs checking per symbol before you trust it.
