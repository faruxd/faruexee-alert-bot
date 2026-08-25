"""
Entry point:  python -m rsi_scanner

Exit codes matter here, because a cron's only ambient signal is red or green:

    0  the scan ran. Signals or no signals, that is a success.
    1  the scan could not run, or every symbol failed. Something is broken
       and the run history should show it.

A Discord post that fails does NOT fail the run. The scan did its job; a
webhook outage is not a scanner bug, and a red X for it would train you to
ignore red X's.
"""

from __future__ import annotations

import datetime as dt
import sys

from .config import Config
from .notify import build_digest, build_messages, post_all
from .scan import scan


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    started = dt.datetime.now(dt.timezone.utc)
    print("=" * 62)
    print(f"  Daily RSI Reset Scanner  —  {started.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 62)

    try:
        config = Config.from_env()
    except ValueError as exc:
        print(f"[FATAL] bad configuration: {exc}")
        return 1

    if "--dry-run" in argv:
        config.dry_run = True

    print(
        f"  symbols={len(config.symbols)}  RSI({config.period})  "
        f"levels={config.oversold:g}/{config.overbought:g}  "
        f"tf={','.join(config.timeframes)}  bias>{config.bias_midline:g}"
        f"{'+unconfirmed4H' if config.alert_4h_unconfirmed else ''}  "
        f"day={config.day_boundary}  max_bar_age={config.max_bar_age_minutes:g}m  "
        f"webhook={'yes' if config.webhook_url else 'NO'}"
        + ("  [DRY RUN]" if config.dry_run else "")
    )
    print("-" * 62)

    result = scan(config)

    print("-" * 62)
    print(
        f"  scanned={result.scanned}  bullish={len(result.bullish)}  "
        f"bearish={len(result.bearish)}  suppressed={result.suppressed}  "
        f"failed={len(result.failures)}"
    )
    print(f"  fresh timeframes: {result.reported or 'NONE'}")
    for tf, age in sorted(result.stale.items()):
        print(f"    {tf} skipped — last bar closed {age:.0f} min ago (limit "
              f"{config.max_bar_age_minutes:g})")
    if result.bars_available is not None:
        print(f"  daily bars available: {result.bars_available}")

    if result.scanned == 0:
        print("[FATAL] no symbol could be scanned — check network or symbol list")
        return 1

    message = build_digest(
        result,
        boundary=config.day_boundary,
        oversold=config.oversold,
        overbought=config.overbought,
    )

    print("-" * 62)
    print(message)
    print("-" * 62)

    if config.dry_run:
        print("  [DRY RUN] nothing sent")
        return 0

    # THE DUPLICATE GUARD. No timeframe had a freshly closed bar, so this run
    # is a re-read of something already reported -- almost always a schedule
    # firing too often. Posting again would repeat a digest the user has seen.
    if not result.reported:
        print("  no freshly closed bar on any timeframe; not posting")
        print("  (if you expected a post, your schedule is firing between bar "
              "closes — see MAX_BAR_AGE_MINUTES)")
        return 0

    if not result.signals and not config.post_when_empty:
        print("  no signals; not posting (set POST_WHEN_EMPTY=true to post anyway)")
        return 0

    messages = build_messages(
        result,
        boundary=config.day_boundary,
        oversold=config.oversold,
        overbought=config.overbought,
    )
    if len(messages) > 1:
        print(f"  digest split into {len(messages)} messages")
    if post_all(config.webhook_url, messages):
        print(f"  posted to Discord ({len(messages)} message(s))")
    else:
        print("  post did not succeed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
