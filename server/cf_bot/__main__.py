"""Allows `python -m cf_bot`, which is what the systemd unit invokes."""

import sys

from cf_bot.main import run

if __name__ == "__main__":
    sys.exit(run())
