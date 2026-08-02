# cf_bot — "Forced Flow" (CF)

Bitget USDT-M perpetual futures bot. All five phases built.

**Thesis:** perp liquidation engines submit price-insensitive market orders. Cascades overshoot fair value and revert once the forced flow exhausts. We fade the overshoot.

---

## Quick start

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Credentials come from the environment and nowhere else — the bot does not parse `.env`. See `.env.example`.

```bash
python -m cf_bot          # live bot
python -m backtest --symbol BTC/USDT:USDT --days 180
python -m pytest          # 324 tests, no network
```

Deployment: **[DEPLOY_RENDER.md](DEPLOY_RENDER.md)** — read the worker-vs-web-service section before deploying.

---

## The strategy

Three tunable parameters. There is no fourth, there are no indicators, and there is no signal-strength position scaling.

| Param | Meaning | Default |
|---|---|---|
| `k` | Displacement threshold (ATR units) | 2.5 |
| `s` | Stop distance (ATR units) | 1.25 |
| `p` | ATR percentile floor | 30 |

Fixed by convention, not tunable: ATR period 14, percentile lookback 30d, entry valid 3 bars, time stop 12 bars.

On the close of bar `i`, with `D = close[i] - open[i]`:

- `D >= k*ATR[i-1]` → **short** setup; `D <= -k*ATR[i-1]` → **long** setup
- Entry: post-only limit at `close[i]`, valid bars `i+1..i+3`
- Stop: `low[i] - s*ATR` (long) / `high[i] + s*ATR` (short), preset on the exchange
- Target: `open[i]` — where the cascade started
- Time stop: flatten at market on the close of `entry_bar + 12`

**Regime filter** — all three must pass or the system is off:
1. `ATR[i-1]` within the [p-th, 90th] percentile of the **trailing** 30 days of 5m ATR
2. `abs(last settled funding) <= 0.10%` per 8h
3. Not inside a settlement blackout

---

## Risk model

`risk_pct` is **1.0 = 1%**, and `MAX_RISK_PCT` in `constants.py` is also in percent. Same unit, compared directly, no conversion anywhere — a fraction-vs-percent slip is a 100× sizing error.

**One position at a time, across all symbols.** The eight configured symbols widen the search for a signal; they do not multiply risk. Per-symbol limits would put 8% at risk simultaneously and breach the −2% daily loss limit on the first two losers.

| Guard | Limit |
|---|---|
| Concurrent positions | 1, global |
| Entries per UTC day | 3 |
| Orders per hour | 12 |
| Daily loss limit | −2% of day-start equity |
| Consecutive losses | 3 |
| Settlement blackout | ±15 min of 00:00 / 08:00 / 16:00 UTC |
| Pre-settlement flatten | 2 min before |

Every guard is a pure function over exchange-derived data. **A restart does not reset any counter** — the daily entry count comes from fills, the loss limit and streak from closed-position history. There is no state file.

---

## Safety properties, and where they live

| Property | Enforced in |
|---|---|
| `live` is never a fallback; needs an explicit confirmation env var | `config.py: resolve_mode` |
| 1% risk ceiling, not configurable | `constants.py: MAX_RISK_PCT` |
| Demo cannot transmit an order — refused before a request is built | `exchange.py: _require_trading_enabled` |
| Credentials from env only, never rendered in a repr or traceback | `config.py: Credentials.__repr__` |
| Secrets scrubbed from every log line | `logging_setup.py: redact_secrets` |
| One-way position mode required | `preflight.py` |
| Key proven unable to withdraw or transfer | `preflight.py` |
| Exchange is authoritative; nothing persisted | `reconcile.py` |
| All-or-nothing snapshots, never partial | `reconcile.py: reconcile` |
| Stop preset on the entry order — no unprotected window | `exchange.py: create_entry_with_protection` |
| Fill without protection → flatten immediately at market | `orders.py: place_entry_with_protection` |
| Deterministic client order IDs — a timeout retry cannot duplicate | `ids.py` |
| Kill switch fails closed if unreadable | `killswitch.py` |
| No blocking sleep in the loop | `main.py: _sleep_or_shutdown` |

There is **no in-memory stop-loss monitoring** anywhere in this codebase. The exchange holds the stop.

---

## Backtester

Imports the *same* `evaluate()` the live bot calls — the strategy is never reimplemented, only driven — and replicates the live guards, because a backtest that ignores them measures a strategy the bot will never run.

The fill model is the important part:

- A post-only limit at P fills **only if a later bar trades through P**. Never because the signal bar touched it — the signal bar's close *is* P.
- A touch is not a fill. `high == limit` means you were at the back of the queue.
- Stops fill at trigger **+3 bps adverse slippage**. A stop is a market order fired into the move.
- When a bar could have hit both stop and target, the **stop** is taken.
- Fees: maker 0.020%, taker 0.060% (Bitget VIP 0, verified).

```bash
python -m backtest --symbol BTC/USDT:USDT --days 180 --save data/btc.csv
python -m backtest --csv data/btc.csv --symbol BTC/USDT:USDT
```

Two honesty notes the report prints for you: `--funding 0` (the default) assumes funding was always benign, which is optimistic; and the 30-day warm-up consumes the first 8,640 bars, so `--days 180` gives roughly 150 days of actual testing.

**Do not loop this over a parameter grid.** Walk-forward is a separate step run by a human. A grid search here is curve fitting and will not survive live.

---

## Verified against live sources

Field names were confirmed against current Bitget v2 docs and ccxt 4.5.70's generated method table, not recalled from memory:

- `GET /api/v2/mix/account/account` → `posMode` ∈ `one_way_mode` | `hedge_mode`
- `GET /api/v2/spot/account/info` → `authorities`, e.g. `["trade","readonly"]`
- `stopLoss`/`takeProfit` params → `presetStopLossPrice` / `presetStopSurplusPrice` on the entry order
- post-only → `force: 'post_only'`; passphrase → ccxt's `password`
- Fees 0.020% / 0.060% at VIP 0

---

## Known limits

- **Bitget serves ~30 days of 5m candles from the recent endpoint**, which is exactly the warm-up window. The live bot is fine (it needs precisely that much); the backtester pages the history-candles endpoint via `since` for anything deeper.
- **Funding history is not fetched by the backtester.** The regime filter's funding condition is evaluated live but assumed in backtests.
- **A position whose open time the exchange does not report cannot be aged**, so its time stop will not fire. It still carries its exchange-side stop and target. Logged as `position.age_unknown`.
