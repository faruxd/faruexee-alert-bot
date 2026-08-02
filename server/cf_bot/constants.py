"""
Non-negotiable, non-configurable invariants.

Nothing in this module may be overridden by config.yaml, by an environment
variable, or at runtime. If a value belongs here, changing it is a deliberate
source-code edit that shows up in a diff and in code review -- that is the
entire point.

This module must not import anything else from cf_bot.
"""

from decimal import Decimal

# ---------------------------------------------------------------------------
# Risk ceiling
# ---------------------------------------------------------------------------
# Expressed in PERCENT of account equity, per trade. 1.0 means 1%.
#
# UNITS: config.yaml `risk.risk_pct` is ALSO in percent, so the comparison
# against this ceiling is unit-for-unit with no conversion anywhere. This is
# deliberate: a fraction-vs-percent mix-up is a 100x sizing error, and a 100x
# sizing error on a live account is unrecoverable. One unit, everywhere.
MAX_RISK_PCT = Decimal("1.0")

# ---------------------------------------------------------------------------
# Operating mode
# ---------------------------------------------------------------------------
MODE_DEMO = "demo"
MODE_LIVE = "live"
VALID_MODES = (MODE_DEMO, MODE_LIVE)

# The literal value the operator must set to arm live trading. Any other value,
# including "true", "1", "YES" with different case, or an unset variable, means
# live trading is refused.
LIVE_CONFIRMATION_ENV = "I_UNDERSTAND_THIS_TRADES_REAL_MONEY"
LIVE_CONFIRMATION_VALUE = "yes"

# ---------------------------------------------------------------------------
# Exchange invariants
# ---------------------------------------------------------------------------
# Bitget v2: GET /api/v2/mix/account/account -> data.posMode
# Verified against https://www.bitget.com/api-doc/contract/account/Get-Single-Account
REQUIRED_POSITION_MODE = "one_way_mode"
HEDGE_POSITION_MODE = "hedge_mode"

# Bitget v2: GET /api/v2/spot/account/info -> data.authorities  e.g. ["trade","readonly"]
# Verified against https://www.bitget.com/api-doc/spot/account/Get-Account-Info
#
# Any authority whose lowercased name contains one of these substrings causes
# startup to abort. Substring matching (not exact) is intentional: Bitget has
# used several spellings over time and we would rather abort on a harmless
# near-match than run with a key that can move funds off the exchange.
FORBIDDEN_AUTHORITY_SUBSTRINGS = frozenset({"withdraw", "transfer"})

# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------
KILL_FILE_NAME = "KILL"

# ---------------------------------------------------------------------------
# Process exit codes. systemd and the operator both read these.
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_PREFLIGHT_FAILED = 3
EXIT_KILL_SWITCH = 4
EXIT_UNHANDLED = 5
EXIT_RECONCILE_FAILED = 6
# A position exists that we could neither protect nor close. The worst state the
# bot can reach; it halts rather than trading around it.
EXIT_UNPROTECTED_POSITION = 7
