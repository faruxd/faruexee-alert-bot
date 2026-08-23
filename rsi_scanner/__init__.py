"""
Daily RSI reset scanner.

Scans a fixed symbol universe once per closed daily bar and posts a single
Discord digest naming every symbol whose RSI(14) crossed back out of an
extreme. Read-only: it holds no keys, places no orders, and touches nothing
the trading bots depend on.
"""

__version__ = "1.0.0"
