# Deploying the trade bot to Render

**Where the API keys go: Render Dashboard → your service → Environment → Environment Variables.**
Not in any file, not in git. Full walkthrough in step 4.

---

## Before you start

Run these locally one last time:

```bash
python test_risk.py
```

```bash
python smoke_test.py --equity 1000
```

The first must show `60 passed, 0 failed`. The second shows exactly which orders would be
placed with your account size. If either looks wrong, fix it before deploying.

---

## Step 1 — Push to GitHub

Render deploys from a repo. Confirm `.env` is ignored before you push:

```bash
git check-ignore -v .env
```

That must print a match. If it prints nothing, **stop** — your keys would go public.

```bash
git add -A && git commit -m "Add FARUEXEE trade bot with Render deployment"
```

```bash
git push origin main
```

`.gitignore` already excludes `.env`, `trade_state.json` and `trade_journal.csv`.

---

## Step 2 — Create the service

In Render: **New → Blueprint**, pick your repo. Render reads `render.yaml` and creates a
**Background Worker** named `faruexee-trade-bot` with a 1 GB persistent disk at `/var/data`.

Worker rather than Web Service, deliberately:

| | Background Worker | Free Web Service |
|---|---|---|
| Sleeps when idle | Never | After 15 min |
| Persistent disk | Yes | Not on free tier |
| Cost | ~$7/mo + ~$0.25 disk | Free |

A trading bot that falls asleep stops managing positions — no break-even moves, no stale
order cleanup, no daily-loss tracking. Your stops stay live on Bitget either way, but the
bot's own risk controls go with it. For real money the worker is the right call.

**If you want the free tier anyway:** in `render.yaml` change `type: worker` to
`type: web`, delete the `disk:` block, and add `ENABLE_WEB=true`. You lose persistence —
read the "Ephemeral storage" section below so you know exactly what that costs you.

---

## Step 3 — Set Python version

Render defaults to an older Python. `render.yaml` pins `PYTHON_VERSION=3.12.7`, which
covers the `float | None` syntax the engine uses. Nothing to do unless you removed it.

---

## Step 4 — Paste your API keys

This is the part you asked about.

1. Open your service in the Render dashboard
2. Left sidebar → **Environment**
3. You will see `BITGET_API_KEY`, `BITGET_API_SECRET`, `BITGET_PASSPHRASE` and
   `DISCORD_WEBHOOK` listed **with empty values** — that is `sync: false` doing its job.
   Render knows they exist but refuses to take values from the repo.
4. Click the pencil / **Edit** next to each and paste the value
5. Click **Save Changes**

| Variable | Value |
|---|---|
| `BITGET_API_KEY` | your API key |
| `BITGET_API_SECRET` | your secret key |
| `BITGET_PASSPHRASE` | the passphrase you set when creating the key |
| `DISCORD_WEBHOOK` | your webhook URL |

Render stores these encrypted and injects them at runtime. They never touch your repo,
your logs, or this conversation — I have not seen them and do not need to.

**Check your Bitget key permissions before saving:**

- **Trade** — must be ON, the bot cannot place orders without it
- **Withdraw** — must be OFF, the bot never needs it and it caps the damage if the key leaks
- **IP whitelist** — bind it to your Render outbound IPs (Settings → Outbound IP Addresses).
  Strongly recommended: a leaked key that only works from one IP is close to worthless.

---

## Step 5 — First deploy runs in dry run

`render.yaml` ships `LIVE_TRADING=false` and `DRY_RUN=true`. The first deploy scans, sizes
positions and logs decisions without sending a single order.

Watch **Logs** in the dashboard. You want to see:

```
Mode            : DRY RUN (no orders sent)
Persistent data directory: /var/data
Loading contract specifications...
Account equity: <your real balance>
Reconciled: 0 position(s), 0 resting order(s)
```

If equity reads your real balance, your keys are working.

Leave it here for a few days. Compare its calls against your chart.

---

## Step 6 — Go live

When you are satisfied, in **Environment**:

```
LIVE_TRADING = true
DRY_RUN      = false
```

Save. Render restarts the service. The bot prints a 10-second warning banner before its
first live cycle, and Discord gets a "Trade bot started" message with mode `LIVE`.

`autoDeploy: false` is set on purpose — a git push will not silently redeploy a bot that
is holding positions. Deploy manually when you choose to.

---

## Ephemeral storage — read this if you skip the disk

Without `DATA_DIR` on a real disk, every restart wipes the bot's memory:

- **The daily-loss baseline resets.** Your 6% daily stop measures from equity at startup.
  A bot that restarts twice a day has, in practice, no daily loss limit.
- **The halt flag clears.** A bot you halted comes back trading.
- **The journal is destroyed.** The record you need to find out whether the strategy
  actually works disappears.

Open positions are *not* at risk from this — stops and take-profits sit on Bitget, and
startup reconciles against the exchange. It is the bot's own risk controls that are fragile.

The bot prints a loud banner and sends a Discord warning at startup when it detects this,
so you cannot lose the disk by accident and not notice.

---

## Operating it

**Status page** (Web Service deployments only): `/` human readable, `/status` JSON,
`/health` returns 503 once the loop stalls for 15 minutes so Render restarts it.

**Emergency stop, no redeploy:** Environment → set `LIVE_TRADING=false` → Save. Render
restarts within a minute and the bot stops placing orders. Existing positions keep their
exchange-side stops.

**Full stop:** Suspend the service. Then go to Bitget and close or manage positions
yourself — a suspended bot manages nothing.

**Halt without stopping:** edit `halted` to `true` in `/var/data/trade_state.json` via
Render's shell.

**Reading the journal:** Render shell, then `cat /var/data/trade_journal.csv`. Every
closed trade with entry, stop, targets, size, risk and real net PnL from Bitget.

---

## Deployment checklist

- [ ] `python test_risk.py` → 60 passed
- [ ] `python smoke_test.py` reviewed
- [ ] `git check-ignore -v .env` prints a match
- [ ] Pushed to GitHub, `.env` not in the repo
- [ ] Blueprint deployed, disk mounted at `/var/data`
- [ ] Four secrets pasted in the Render dashboard
- [ ] Bitget key: Trade ON, Withdraw OFF, IP bound
- [ ] Logs show your real equity in dry run
- [ ] Watched dry run for several days
- [ ] Flipped `LIVE_TRADING=true`, `DRY_RUN=false`
- [ ] Discord confirms mode LIVE
- [ ] First live trade checked by hand on Bitget — size, stop and targets where expected
