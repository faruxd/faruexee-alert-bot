"""
The scan universe.

Every symbol here was verified present in Bitget's live USDT-FUTURES contract
list. Two obvious names are absent because Bitget does not list them as
USDT-M perpetuals: TON and MNT. Adding an unlisted symbol does not fail
loudly -- it just logs a fetch error every single run forever -- so check
before adding:

    curl -s "https://api.bitget.com/api/v2/mix/market/contracts?productType=USDT-FUTURES" \
      | python -c "import json,sys; print('XYZUSDT' in {c['symbol'] for c in json.load(sys.stdin)['data']})"
"""

from __future__ import annotations

from typing import List

# Already scanned by faruexee_alert_bot.py. Kept verbatim so the two bots
# cover the same names.
DISCORD_BOT_SYMBOLS: List[str] = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "DOTUSDT",
    "DOGEUSDT", "POLUSDT", "SUIUSDT", "TRXUSDT", "UNIUSDT",
    "XAUUSDT", "XAGUSDT", "XPTUSDT", "CLUSDT",
]

# Added to bring crypto coverage up to roughly the top 20 by market cap.
TOP_CAP_ADDITIONS: List[str] = [
    "BCHUSDT", "NEARUSDT", "APTUSDT", "ICPUSDT", "HBARUSDT",
    "ETCUSDT", "FILUSDT", "ATOMUSDT", "XLMUSDT", "SHIBUSDT",
]

# Non-crypto members of the existing list. RSI is indifferent to the
# underlying, but these trade on different sessions -- gold and crude have
# real weekend gaps where crypto does not -- so they are tagged for the
# digest rather than silently mixed in.
NON_CRYPTO = {"XAUUSDT", "XAGUSDT", "XPTUSDT", "CLUSDT"}


def default_universe() -> List[str]:
    """Deduplicated, order-preserving union of both lists."""
    seen = set()
    out: List[str] = []
    for symbol in DISCORD_BOT_SYMBOLS + TOP_CAP_ADDITIONS:
        if symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def display_name(symbol: str) -> str:
    """BTCUSDT -> BTC. Digest lines are read on a phone."""
    return symbol[:-4] if symbol.endswith("USDT") else symbol
