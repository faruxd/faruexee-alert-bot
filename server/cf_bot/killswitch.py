"""
Kill switch.

Deliberately its own module despite being tiny. This is the operator's last
resort and it must be auditable in one screen without reading anything else.

TWO TRIGGERS
------------
1. A file named KILL in the working directory. This is the primary trigger and
   the one to use when you have shell access.

2. The environment variable CF_KILL set to any of 1/true/yes. This exists
   because the deploy target is Render, where a file-based switch is unreachable
   without a shell -- and a kill switch you cannot reach is not a kill switch.
   Setting it in the Render dashboard restarts the service, which comes back up,
   sees the variable and halts before placing anything.

Either trigger causes: cancel all orders, flatten all positions, exit non-zero.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

KILL_ENV_VAR = "CF_KILL"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def kill_env_set(env: Optional[Mapping[str, str]] = None) -> bool:
    """True if the environment kill switch is engaged."""
    env = os.environ if env is None else env
    raw = env.get(KILL_ENV_VAR)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


def kill_file_present(kill_file: Path, env: Optional[Mapping[str, str]] = None) -> bool:
    """
    Return True if the kill switch is engaged by either trigger.

    Fails closed: if the path cannot be inspected at all (permissions, a broken
    mount, a race with the filesystem), we report the switch as ENGAGED. The
    reasoning is asymmetric-cost -- a false positive stops a bot that was fine,
    which costs nothing but a restart; a false negative lets a bot keep trading
    while the operator believes they have stopped it.
    """
    if kill_env_set(env):
        return True
    try:
        return kill_file.exists()
    except OSError:
        return True
