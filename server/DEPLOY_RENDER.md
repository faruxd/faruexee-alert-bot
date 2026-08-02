# Deploying cf_bot on Render

## Read this first: worker, not web service

`render.yaml` defines a **Background Worker**. That is deliberate and it costs money — workers have no free tier (Starter is ~$7/mo).

A free **Web Service** would be cheaper and it will not work safely for this bot. Free web services sleep after ~15 minutes without inbound HTTP. A sleeping cf_bot:

- does not run its **time stop**, so a position meant to close after 12 bars stays open indefinitely
- does not run the **settlement flatten**, so you hold through funding
- does not **reconcile**, so it is blind to fills, liquidations and manual changes
- does not take entries, so you also lose the upside

Your open positions are not defenceless while it sleeps — the stop and take-profit are preset on the exchange and keep working. But every rule the bot enforces *itself* stops being enforced, and it wakes up with a stale picture. For a live-money bot that is not an acceptable trade for $7/mo.

If you deploy it as a free web service anyway, understand you are relying entirely on the exchange-side stop and nothing else.

---

## Do NOT use a Blueprint for this repo

Render Blueprints read `render.yaml` from the **repository root**. This repo already has one there, and it deploys the older `faruexee-trade-bot`. Connecting a Blueprint would:

- deploy the wrong bot, and
- put a Blueprint sync in a position to overwrite dashboard settings on a service that is currently live.

`server/render.yaml` is **never read by Render** in this repo. It exists as a reference for the settings below.

Create cf_bot as a **manual service** instead. It touches nothing that already exists.

---

## Setup

1. Push the `server/` directory to the repo. Then Render → **New** → **Background Worker**:

   | Setting | Value |
   |---|---|
   | Repository | `faruxd/faruexee-alert-bot` |
   | Branch | `main` |
   | **Root Directory** | **`server`** ← easy to miss, required |
   | Runtime | Python 3 |
   | Build Command | `pip install -r requirements.txt` |
   | Start Command | `python -m cf_bot` |
   | Instance Type | Starter (workers have no free tier) |
   | **Auto-Deploy** | **Off** |

   Set `PYTHON_VERSION=3.12.7` as an env var. The code was developed on 3.14 and pinned deps support both; 3.12 is what Render reliably provides.

2. Set the secrets in the Render dashboard. **Type these into Render yourself — never paste API keys into a chat, a config file, or a commit.**

   | Variable | Value |
   |---|---|
   | `BITGET_API_KEY` | your key |
   | `BITGET_API_SECRET` | your secret |
   | `BITGET_API_PASSPHRASE` | your passphrase |

   The key must have **read + trade only**. Startup aborts if it reports withdrawal or transfer rights.

3. **First deploy with `MODE=demo`.** It connects, runs preflight, reconciles and logs everything, but the client refuses to transmit any order. Confirm the logged state matches what you see in the Bitget UI before arming it.

4. To arm: set `MODE=live` **and** `I_UNDERSTAND_THIS_TRADES_REAL_MONEY=yes` (exactly that lowercase string).

`autoDeploy` is off. Do not turn it on — you do not want a push to `main` restarting a bot that is holding a position.

---

## Stopping it

Two triggers, either is sufficient:

**From the dashboard** (no shell needed): set `CF_KILL=1`. Render restarts the service, it comes up, sees the flag, cancels all orders, flattens all positions, and exits non-zero.

**With a shell** (paid plans):

```bash
touch KILL
```

Both do the same thing. If a flatten fails, it logs `killswitch.manual_intervention_required` naming exactly what is still open, and still exits — a bot that cannot close a position must not keep trading around it.

---

## What to expect in the logs

**Startup is slow the first time.** The regime filter needs 30 days of trailing 5m ATR — 8,640 bars per symbol, fetched ~1,000 at a time. With 8 symbols that is roughly 70 paginated requests before the first scan completes. After that the bot only tops up ~50 bars per symbol and only re-evaluates a symbol when a new bar has actually closed.

**`entry.below_min_size`** — this one matters for you specifically, since you are starting small. If 1% of your equity buys less than Bitget's minimum order size on a symbol, the bot **skips the trade**. It does not round up, because rounding up would silently exceed the 1% risk ceiling. If you see this constantly, your account is too small for that symbol, not the bot misbehaving.

**`entry.blocked`** — a guard stopped it. The reason names which one.

**No persistent disk.** `logs/cf_bot.jsonl` is wiped on every restart. That is survivable because the bot stores no state on disk: the daily entry count, the loss limit and the losing streak are all re-derived from exchange data on every loop. Nothing resets when the process does. If you want durable logs, use Render's log drain.

---

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Clean shutdown on signal |
| 2 | Configuration or credential error |
| 3 | Preflight failed (position mode / permissions / connect) |
| 4 | Kill switch engaged |
| 5 | Unhandled exception |
| 6 | Could not build the initial account snapshot |
| 7 | **A position exists that could not be protected or closed** |

Code 7 is the one to alarm on. It means the bot found a filled position with no stop on the exchange and could not flatten it. It halts rather than trading around it. Go look at the account.

---

## Before you arm it

- [ ] Ran in `MODE=demo` and the logged state matched the Bitget UI
- [ ] Account is in **one-way** position mode (the bot refuses hedge mode)
- [ ] API key has no withdrawal or transfer rights
- [ ] Backtested on real history: `python -m backtest --symbol BTC/USDT:USDT --days 180`
- [ ] You know `CF_KILL=1` is how you stop it
