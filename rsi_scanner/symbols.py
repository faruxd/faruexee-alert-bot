"""
The scan universe: top 30 crypto by market cap with memes removed, plus the
four non-crypto instruments the original alert bot already covered.

Every symbol was verified present in Bitget's live USDT-FUTURES contract list.
TON and MNT are absent because Bitget does not list them as USDT-M perps.
An unlisted symbol does not fail loudly -- it logs a fetch error every run
forever -- so check before adding:

    curl -s "https://api.bitget.com/api/v2/mix/market/contracts?productType=USDT-FUTURES" \
      | python -c "import json,sys; print('XYZUSDT' in {c['symbol'] for c in json.load(sys.stdin)['data']})"
"""

from __future__ import annotations

from typing import List

# Top 30 by market cap, meme coins excluded.
#
# DOGE and SHIB were dropped deliberately. Both were in the original alert
# bot's list; both are the names most likely to whipsaw through a level on
# sentiment rather than respect structure, which is exactly what this scan is
# trying not to alert on. Re-add them here if you disagree -- nothing else
# depends on their absence.
CRYPTO: List[str] = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "DOTUSDT",
    "POLUSDT", "SUIUSDT", "TRXUSDT", "UNIUSDT", "BCHUSDT",
    "NEARUSDT", "APTUSDT", "ICPUSDT", "HBARUSDT", "ETCUSDT",
    "FILUSDT", "ATOMUSDT", "XLMUSDT", "AAVEUSDT", "TAOUSDT",
    "CROUSDT", "RENDERUSDT", "ONDOUSDT", "ARBUSDT", "OPUSDT",
]

# Metals and crude, carried over from faruexee_alert_bot.py. RSI is
# indifferent to the underlying, but these trade on different sessions --
# gold and crude have real weekend gaps where crypto does not -- so they are
# tagged in the digest rather than silently mixed in.
NON_CRYPTO_LIST: List[str] = ["XAUUSDT", "XAGUSDT", "XPTUSDT", "CLUSDT"]

NON_CRYPTO = set(NON_CRYPTO_LIST)


def default_universe() -> List[str]:
    """Deduplicated, order-preserving. 34 symbols: 30 crypto + 4 other."""
    seen = set()
    out: List[str] = []
    for symbol in CRYPTO + NON_CRYPTO_LIST:
        if symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def display_name(symbol: str) -> str:
    """BTCUSDT -> BTC. Digest lines are read on a phone."""
    return symbol[:-4] if symbol.endswith("USDT") else symbol
