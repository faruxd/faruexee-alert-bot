# cf_bot

Bitget USDT-M perpetual futures bot. Two strategies share one safety layer; `strategy.name` in `config.yaml` selects which runs.

| | **ema_scalper** (default) | **forced_flow** |
|---|---|---|
| Idea | Trade with the 15m trend on a 5m EMA cross | Fade liquidation-cascade overshoot |
| Entry | Passive limit, **market fallback** after 25s | Post-only limit, never chases |
| Stop | 1.5 × ATR(14) | 1.25 × ATR beyond the cascade bar's extreme |
| Target | 2R | The level the cascade started from |
| Time stop | 24 bars (2h) | 12 bars (1h) |
| Warm-up | 60 bars | **8,640 bars (30 days)** |

---

## Quick start

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

```bash
python -m cf_bot                                          # live bot
python -m backtest --symbol BTC/USDT:USDT --days 90       # scalper backtest
python -m pytest                                          # 382 tests, no network
```

Credentials come from the environment only — the bot does not parse `.env`. See `.env.example`.
Deployment: **[DEPLOY_RENDER.md](DEPLOY_RENDER.md)**.

---

## ema_scalper

**15m EMA(50)** sets direction — longs only above it, shorts only below. A **5m EMA(9)/EMA(21) cross** in that direction triggers the entry, and only on the bar where the cross actually happens (a cross three bars ago is already priced in).

Stop is 1.5 × ATR(14) on the 5m, preset on the exchange. Target is 2R. Flat at market after 24 bars.

### Why the stop is wide, and why that matters more than it looks

Position size is `equity × risk / stop_distance`, and fees are charged on **notional**. A tighter stop means a bigger position, which means more fees for the same 1% of risk:

| Stop distance | Notional | Taker round trip | Cost per trade |
|---|---|---|---|
| 0.2% | 5.0× equity | 0.60% of equity | **0.60R** |
| 0.3% | 3.3× equity | 0.40% of equity | **0.40R** |
| 0.6% | 1.7× equity | 0.20% of equity | **0.20R** |
| 1.0% | 1.0× equity | 0.12% of equity | **0.12R** |

At a 0.3% stop, every round trip costs 0.40R before the market moves at all. **Do not lower `atr_mult` without redoing this arithmetic.**

### The two-leg entry

You asked for market entry because limit orders don't fill. Paying taker on every entry is expensive, so the entry does both:

1. Rest a post-only limit at the signal bar's close (**0.020% maker**)
2. If unfilled after `entry_limit_timeout_seconds`, cancel and take it at market (**0.060% taker**)

Post-only rejection — price already moved through the level — skips straight to step 2. Both legs carry the stop and target as presets, so neither can leave a position bare, and they use **different** deterministic order IDs so the fallback isn't rejected as a duplicate of the order just cancelled.

---

## Risk model (both strategies)

`risk_pct` is **1.0 = 1%**, and `MAX_RISK_PCT` is also in percent — same unit, compared directly, no conversion anywhere.

**One position at a time, across all symbols.** The eight symbols widen the search for a signal; they do not multiply risk.

| Guard | Limit |
|---|---|
| Concurrent positions | 1, global |
| Entries per UTC day | 3 |
| Orders per hour | 12 |
| Daily loss limit | −2% of day-start equity |
| Consecutive losses | 3 |
| Settlement blackout | ±15 min of 00:00 / 08:00 / 16:00 UTC |
| Pre-settlement flatten | 2 min before |

Every guard is a pure function over exchange-derived data. **A restart resets no counter** — the daily entry count comes from fills, the loss limit and streak from closed-position history. No state file exists.

---

## Safety properties

| Property | Enforced in |
|---|---|
| `live` is never a fallback; needs an explicit confirmation env var | `config.py: resolve_mode` |
| 1% risk ceiling, not configurable | `constants.py: MAX_RISK_PCT` |
| Demo cannot transmit an order — refused before a request is built | `exchange.py: _require_trading_enabled` |
| Credentials never rendered in a repr or traceback | `config.py: Credentials.__repr__` |
| Secrets scrubbed from every log line | `logging_setup.py: redact_secrets` |
| One-way mode and no-withdraw key required | `preflight.py` |
| Exchange is authoritative; nothing persisted | `reconcile.py` |
| Stop preset on the entry order — no unprotected window | `exchange.py` |
| Fill without protection → flatten immediately at market | `orders.py: _verify_or_flatten` |
| Deterministic order IDs — a timeout retry cannot duplicate | `ids.py` |
| Unfilled entries aged out by exchange timestamp | `orders.py: cancel_expired_entries` |
| Kill switch fails closed if unreadable | `killswitch.py` |

There is **no in-memory stop-loss monitoring** anywhere. The exchange holds the stop.

---

## Backtester

Imports the *same* strategy functions the live bot calls, and replicates the live guards.

```bash
python -m backtest --symbol BTC/USDT:USDT --days 90                    # scalper
python -m backtest --symbol BTC/USDT:USDT --days 180 --strategy forced_flow
python -m backtest --csv data/btc.csv --symbol BTC/USDT:USDT           # offline
```

**Scalper fills are modelled as always market, at the next bar's open, always taker, plus 3 bps slippage.** Live, the passive leg fills some of the time at maker, so real fees should land below this. The result is a **floor, not a forecast**.

The 15m trend series is resampled from the same 5m data, aligned to wall-clock 15m boundaries, and a 15m bar is only visible once it has closed — reading a forming one would be lookahead.

**Forced flow fills** are stricter: a post-only limit fills only if a *later* bar trades *through* it, a touch is not a fill, stops fill at trigger +3 bps, and when a bar could have hit both stop and target the stop wins.

Fees: maker 0.020%, taker 0.060% (Bitget VIP 0, verified).

**Do not loop this over a parameter grid.** Walk-forward is a separate step run by a human.

---

## Before you deploy

**Set leverage on Bitget.** A 1.5×ATR stop on BTC 5m is roughly 0.5%, so 1% risk means ~2× equity of notional. The bot **never sets leverage** — it uses whatever is configured per symbol. Too low and every order is rejected for margin. 10× or more is fine; it doesn't change your risk, which the stop fixes at 1%.

**Minimum order size.** At small equity, 1% risk can compute below Bitget's minimum. The bot **skips** rather than rounding up past the risk ceiling. Logged as `entry.below_min_size`.

**Nothing here has run against the real exchange.** Everything is tested against fakes. The main untested assumption is that Bitget returns `presetStopLossPrice` where `_has_protection` looks for it — if not, every filled entry reads as unprotected and gets flattened immediately. Run `MODE=demo` first and confirm the logged state matches the Bitget UI.

---

## Known limits

- **Bitget serves ~30 days of 5m candles** from the recent endpoint. Fine for the scalper (60-bar warm-up); the forced-flow strategy consumes exactly that window before it can trade at all.
- **Funding history is not fetched by the backtester.** `--funding 0` assumes it was always benign, which is optimistic. Only affects forced flow, whose regime filter reads it.
- **A position whose open time the exchange does not report cannot be aged**, so its time stop will not fire. Exchange-side stop and target still apply. Logged as `position.age_unknown`.
- **The loop blocks for `entry_limit_timeout_seconds` during a scalper entry.** Reconciliation is delayed by that long, once per entry.
