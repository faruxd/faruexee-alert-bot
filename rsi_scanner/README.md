# RSI Reset Scanner

Scans 34 symbols on **1D and 4H** and posts **one Discord digest per run**
naming every symbol whose RSI(7) crossed back out of an extreme.

Read-only. Holds no API keys, places no orders, shares no state with
`faruexee_trade_bot.py` or `cf_bot`. It cannot affect a live position.

---

## The signal

| | |
|---|---|
| **Bullish reset** | RSI(7) was **below 30** on the prior closed bar and is **at or above 30** on the latest |
| **Bearish reset** | RSI(7) was **above 70** on the prior closed bar and is **at or below 70** on the latest |

The crossing bar must be the **latest** one. A symbol that climbed out of
oversold three bars ago does not fire now.

### 1D stands alone. 4H must agree with the daily.

A 4H reset only alerts when the **daily RSI is on the same side of 50**:

- Daily RSI **above 50** → bullish regime → 4H **bullish** resets fire, bearish suppressed
- Daily RSI **below 50** → bearish regime → 4H **bearish** resets fire, bullish suppressed
- Daily RSI exactly 50 → no bias, everything suppressed

That is buying dips inside an uptrend and selling rallies inside a downtrend —
the same trade-with-the-higher-timeframe rule as the HTF filter in
`faruexee_alert_bot.py`. Flip it with `BIAS_MIDLINE`.

This filter is not cosmetic. Measured over 80 days across all 34 symbols:

| | Signals | Alerts/day |
|---|---|---|
| 1D resets | 229 | 2.9 |
| 4H resets, **unfiltered** | 1,404 | 17.6 |
| 4H resets, **daily-confirmed** | 171 | 2.1 |
| **Combined, as shipped** | **400** | **5.0** |

**The daily filter removes 88% of 4H alerts.** Without it the channel gets
~20 notifications a day and stops being read.

### What this signal is not

The 30/70 cross is the noisiest of the reset definitions and **fires against
strong trends** — RSI can sit above 70 for weeks in a real rally, and a cross
back below 70 will flag a top that isn't one. It can also retrigger: RSI
wobbling around 30 can cross up, fall back, and cross up again in a week.
Treat it as a watchlist filter, not an entry.

Period is **7**, not Wilder's 14. Shorter swings wider and touches the levels
more often — at 30/70 that is roughly 2.8x the alert rate of RSI(14). If it
proves too noisy, `RSI_OVERSOLD=20` / `RSI_OVERBOUGHT=80` cuts it back.

---

## The duplicate guard

**A run that finds no freshly closed bar posts nothing.**

This exists because a schedule set to every ten minutes fired constantly,
re-read the same closed daily bar each time, and re-posted the same digest all
day. The scan is stateless by design — it cannot remember what it already sent
— so instead it refuses to report a bar that closed more than
`MAX_BAR_AGE_MINUTES` ago. A fresh signal and a stale re-read are
distinguishable from the clock alone.

It **bounds** the damage rather than eliminating it: a ten-minute cron still
gets a few posts inside the window. **The actual fix is the schedule.**

- **Render Cron** is reliable, so `MAX_BAR_AGE_MINUTES=30` is fine
- **GitHub Actions** is routinely 5–30 min late, so keep it at 90 or late runs
  are silently dropped

---

## Two things about Bitget that are not obvious

Both verified against the live API, not assumed.

**1. Bitget's native daily bar closes at 16:00 UTC, not midnight.** Daily
candles come back stamped `16:00` — they run 16:00 to 16:00 UTC, i.e. midnight
in UTC+8. RSI on those bars will *not* match a TradingView chart set to UTC.

So the default (`DAY_BOUNDARY=utc`) **resamples 4H bars into true UTC-midnight
days**. `DAY_BOUNDARY=exchange` uses Bitget's native 1D bars instead. The 4H
bars need no correction — they already sit on UTC boundaries.

**2. History is capped at 90 days on every granularity.** `limit=1000` on `1D`
returns 90 rows; `4H` returns 540 — the same 90 days. `history-candles` does
not help. Data retention, not paging. Fine for RSI(7); rules out any 200-day
lookback, the same limit that pins the trade bot's HTF filter to an EMA.

---

## Configuration

All via environment variables. Every one has a working default.

| Variable | Default | Meaning |
|---|---|---|
| `DISCORD_RSI_WEBHOOK` | *(none)* | Webhook URL. Must be `https://`. Absent = print only. |
| `RSI_PERIOD` | `7` | RSI lookback. **Not** Wilder's 14. |
| `RSI_OVERSOLD` | `30` | Bullish reset level |
| `RSI_OVERBOUGHT` | `70` | Bearish reset level |
| `RSI_TIMEFRAMES` | `1D,4H` | `1D` alone is valid; `1D` cannot be removed |
| `BIAS_MIDLINE` | `50` | Which side of this the daily must be on for a 4H signal |
| `MAX_BAR_AGE_MINUTES` | `90` | Older than this = a re-read, post nothing |
| `DAY_BOUNDARY` | `utc` | `utc` (resampled 4H) or `exchange` (native 1D) |
| `POST_WHEN_EMPTY` | `false` | Post even with zero signals |
| `RSI_DRY_RUN` | `false` | Compute and print, never post |

A bad value **raises at startup** rather than falling back to a default — a
typo'd threshold must not look like it took effect.

---

## Universe — 34 symbols

**30 crypto by market cap, memes excluded**, plus 4 non-crypto carried over
from the original alert bot. All verified present on Bitget USDT-FUTURES.

```
BTC ETH BNB SOL XRP ADA AVAX LINK LTC DOT POL SUI TRX UNI BCH
NEAR APT ICP HBAR ETC FIL ATOM XLM AAVE TAO CRO RENDER ONDO ARB OP
XAU XAG XPT CL            <- non-crypto, tagged in the digest
```

**DOGE and SHIB were dropped.** Both were in the original list; both are the
names most likely to whipsaw through a level on sentiment rather than respect
structure. Re-add them in `symbols.py` if you disagree.

**TON and MNT are absent** — Bitget does not list them as USDT-M perps. An
unlisted symbol fails softly, logging a fetch error every run forever, so
verify against the contracts endpoint before adding one.

---

## Running locally

```bash
pip install -r rsi_scanner/requirements.txt
```

```bash
python -m rsi_scanner --dry-run
```

```bash
python -m pytest rsi_scanner/tests -q
```

The RSI is verified against Wilder's published reference series at period 14 —
that check validates the algorithm, independent of the deployed period. If it
fails, the maths is wrong.

---

## Deploying

**Schedule: `5 0,4,8,12,16,20 * * *`** — six runs a day, five minutes after
each 4H bar closes. Only the **00:05** run can report a 1D signal, because
that is the only one where the UTC day has also just closed.

### GitHub Actions (free)

`.github/workflows/rsi-daily.yml`. Add `DISCORD_RSI_WEBHOOK` under
Settings → Secrets → Actions. Scheduled workflows only fire from the
**default branch**, and so does the manual "Run workflow" button.

### Render Cron Job (paid)

`rsi_scanner/render.yaml` holds the reference settings. It is **inert** —
Render only reads a `render.yaml` at the repo root, and the root one here
deploys the live trade bot. Adding this to it would let a Blueprint sync
overwrite dashboard settings on a service holding real positions.

| Field | Value |
|---|---|
| Root Directory | **blank** — a root dir breaks the `rsi_scanner` import |
| Build | `pip install -r rsi_scanner/requirements.txt` |
| Start | `python -m rsi_scanner` |
| Schedule | `5 0,4,8,12,16,20 * * *` |

**Do not run both.** Two schedulers means two digests. Disable one.

**Why a Cron Job, not a Worker or Web Service:** the process runs ~30 seconds
and exits. A Worker expects a long-lived process and would restart it in a hot
loop. A free Web Service sleeps after 15 minutes without HTTP and would miss
its own schedule.

---

## Design notes

**One digest per run, not one per symbol.** Every bar on a timeframe closes at
the same instant and crypto is correlated, so a market-wide reset produces a
dozen-plus hits at once. Separate posts would trip Discord's rate limit,
arrive out of order, and be unreadable on a phone.

**A timeframe with no fresh bar is omitted from the digest**, not rendered
empty. On a 04:00 run only 4H is news, and a "Daily" header there would imply
otherwise. The digest also dates itself from the newest *reported* bar.

**Suppressed 4H signals are counted in the footer.** A silent filter that
swallows everything must not be indistinguishable from a quiet market.

**The daily series is fetched on every run** regardless of timeframe, because
the 4H filter needs it. That is 2 requests per symbol per run, not 1.

**Exit codes.** `0` = the scan ran, signals or not. `1` = it could not run, or
every symbol failed. A *Discord* failure does not fail the run — the scan did
its job, and a red X for a webhook outage would train you to ignore red X's.
