# Daily RSI Reset Scanner

Scans a fixed universe once per closed daily bar and posts **one Discord
digest** naming every symbol whose RSI(14) crossed back out of an extreme.

Read-only. Holds no API keys, places no orders, and shares no state with
`faruexee_trade_bot.py` or `cf_bot`. It cannot affect a live position.

---

## The signal

| | |
|---|---|
| **Bullish reset** | RSI(14) was **below 30** on the prior closed daily bar and is **at or above 30** on the latest |
| **Bearish reset** | RSI(14) was **above 70** on the prior closed daily bar and is **at or below 70** on the latest |

The crossing bar must be the **latest** one. A symbol that climbed out of
oversold three days ago does not fire today.

This signal can retrigger — RSI wobbling around 30 can cross up, fall back,
and cross up again inside a week. That is the honest behaviour of a threshold
cross, not a bug. It is also the weakest of the three reset definitions in
strong trends: RSI can sit above 70 for weeks in a real rally, and a bearish
cross back below 70 will fire against a move that keeps going. Treat it as a
watchlist filter, not an entry.

---

## Two things about Bitget that are not obvious

Both were verified against the live API, not assumed.

**1. Bitget's native daily bar closes at 16:00 UTC, not midnight.**
Daily candles come back stamped `16:00` — they run 16:00→16:00 UTC, i.e.
midnight in UTC+8. RSI computed on those bars will *not* match a TradingView
chart set to UTC, because the bars are cut in different places.

So the default (`DAY_BOUNDARY=utc`) **resamples 4H bars into true
UTC-midnight days**. Set `DAY_BOUNDARY=exchange` to use Bitget's native 1D
bars instead. Check which one your chart uses before trusting the numbers.

**2. History is capped at 90 days on every granularity.**
`limit=1000` on `1D` returns 90 rows. `4H` returns 540 rows — the same 90
days. `history-candles` does not help. This is data retention, not paging.

90 days is comfortably enough for RSI(14) to converge (~75 smoothing steps
past the seed). It rules out anything needing a 200-day lookback — the same
limit that pins the trade bot's HTF filter to an EMA rather than structure.

---

## Setup

**1. Create a Discord webhook** for a new channel (keep it separate from the
zone-tap alerts so a once-a-day digest doesn't get buried):
Channel → Edit Channel → Integrations → Webhooks → New Webhook → Copy URL.

**2. Add it as a repo secret:**
Settings → Secrets and variables → Actions → New repository secret
- Name: `DISCORD_RSI_WEBHOOK`
- Value: the webhook URL

**3. Test before trusting it:**
Actions → "Daily RSI reset scan" → Run workflow → `dry_run = true`.
Prints the digest to the run log and posts nothing.

That's it. The cron runs at 00:10 UTC daily.

---

## Configuration

All via environment variables. Every one has a working default.

| Variable | Default | Meaning |
|---|---|---|
| `DISCORD_RSI_WEBHOOK` | *(none)* | Webhook URL. Must be `https://`. Absent = print only. |
| `RSI_SYMBOLS` | full universe | Comma-separated override |
| `RSI_PERIOD` | `14` | RSI lookback |
| `RSI_OVERSOLD` | `30` | Bullish reset level |
| `RSI_OVERBOUGHT` | `70` | Bearish reset level |
| `DAY_BOUNDARY` | `utc` | `utc` (resampled 4H) or `exchange` (native 1D, 16:00 close) |
| `POST_WHEN_EMPTY` | `false` | Post a digest even with zero signals |
| `RSI_DRY_RUN` | `false` | Compute and print, never post |

A bad value **raises at startup** rather than silently falling back to a
default — a typo'd threshold must not look like it took effect.

---

## Universe — 29 symbols

All 19 from `faruexee_alert_bot.py`, plus 10 to bring crypto coverage to
roughly the top 20 by market cap.

```
BTC ETH BNB SOL XRP ADA AVAX LINK LTC DOT DOGE POL SUI TRX UNI
BCH NEAR APT ICP HBAR ETC FIL ATOM XLM SHIB
XAU XAG XPT CL            <- non-crypto, tagged ⧉ in the digest
```

**TON and MNT are absent** — Bitget does not list them as USDT-M perpetuals.
An unlisted symbol does not fail loudly; it logs a fetch error every run
forever. Verify before adding:

```bash
curl -s "https://api.bitget.com/api/v2/mix/market/contracts?productType=USDT-FUTURES" | grep -c '"XYZUSDT"'
```

---

## Running locally

```bash
pip install -r rsi_scanner/requirements.txt
python -m rsi_scanner --dry-run
```

Tests — the RSI is verified against Wilder's published reference series, so a
failure here means the maths is wrong:

```bash
python -m pytest rsi_scanner/tests -q
```

---

## Design notes

**One digest, not N alerts.** Every daily bar closes at the same instant and
crypto is heavily correlated, so a market-wide reset day produces fifteen-plus
hits simultaneously. Separate posts would trip Discord's rate limit, arrive
out of order, and be unreadable on a phone.

**No state file.** The 60-second alert bot needs `alert_state.json` because it
re-evaluates the same bar 1,440 times a day. This runs once per closed bar and
evaluates each bar exactly once, so there is nothing to deduplicate — and no
dedup bug to have.

**Exit codes.** `0` = the scan ran, signals or not. `1` = it could not run, or
every symbol failed. A *Discord* failure does not fail the run: the scan did
its job, and a red X for a webhook outage would train you to ignore red X's.

**Not a web service.** A once-daily 30-second job does not need a process kept
alive 24/7. If you later need guaranteed delivery — GitHub can skip scheduled
runs under load, and disables schedules after 60 days of repo inactivity —
move it to Render Cron rather than a web service.

---

## Deploying on Render instead

`rsi_scanner/render.yaml` holds the reference settings. It is **inert** — Render
only reads a `render.yaml` at the repo root, and the root one here deploys the
live trade bot. Adding this to it would let a Blueprint sync overwrite dashboard
settings on a service holding real positions. Copy the values in by hand.

**Dashboard → New → Cron Job**

| Field | Value |
|---|---|
| Repository | `faruxd/faruexee-alert-bot` |
| Branch | `main` |
| Root Directory | **blank** — a root dir breaks the `rsi_scanner` import |
| Build Command | `pip install -r rsi_scanner/requirements.txt` |
| Start Command | `python -m rsi_scanner` |
| Schedule | `10 0 * * *` (UTC always — Render ignores local time) |
| Instance Type | smallest available |

Environment: `DISCORD_RSI_WEBHOOK` (secret), `DAY_BOUNDARY=utc`,
`POST_WHEN_EMPTY=false`, `PYTHON_VERSION=3.12.7`.

**Measured cost of a run:** 18 seconds wall time, 13 MB peak heap, on the full
29-symbol universe. Almost all of it is network wait.

**Cron Jobs are paid** — there is no free tier for them, unlike Web Services.
The GitHub Actions workflow does the identical job for free. Render buys you
guaranteed delivery instead of GitHub's best-effort scheduler.

**Do not run both.** Two schedulers means two digests every morning. Disable one:
Actions → "Daily RSI reset scan" → ⋯ → Disable workflow.

**Why a Cron Job, not a Worker or Web Service:** this process runs 18 seconds and
exits. A Background Worker expects a long-lived process and would restart it in a
hot loop. A free Web Service sleeps after 15 minutes without HTTP and would miss
its own schedule.
