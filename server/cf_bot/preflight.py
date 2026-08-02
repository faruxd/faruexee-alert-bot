"""
Startup assertions.

Every function here answers one yes/no question about whether it is safe to
proceed, and raises PreflightError if the answer is no. None of them return a
boolean that a caller could accidentally ignore.

All of them fail closed. If we cannot determine the answer -- the field is
missing, the response is a shape we do not recognise, the call errors -- that
counts as failure, not as permission to continue.
"""

from __future__ import annotations

from cf_bot.constants import (
    FORBIDDEN_AUTHORITY_SUBSTRINGS,
    HEDGE_POSITION_MODE,
    REQUIRED_POSITION_MODE,
)
from cf_bot.exchange import BitgetClient, ExchangeError


class PreflightError(Exception):
    """A startup assertion failed. The bot must not trade."""


async def assert_one_way_position_mode(client: BitgetClient) -> str:
    """
    Require one-way position mode.

    Hedge mode lets a symbol hold simultaneous long and short positions. Every
    piece of downstream logic -- reconciliation, the max-1-position guard, stop
    attachment -- assumes one position per symbol. Running this bot against a
    hedge-mode account would not merely misreport; it would attach a stop to the
    wrong leg.
    """
    try:
        mode = await client.fetch_position_mode()
    except ExchangeError as exc:
        raise PreflightError(f"could not determine position mode: {exc}") from exc

    if mode == REQUIRED_POSITION_MODE:
        return mode

    if mode == HEDGE_POSITION_MODE:
        raise PreflightError(
            f"account is in {HEDGE_POSITION_MODE!r}. This bot requires "
            f"{REQUIRED_POSITION_MODE!r}. Change it in the Bitget UI "
            "(Futures -> Settings -> Position Mode) with no open positions, then "
            "restart. The bot will not change it for you."
        )

    raise PreflightError(
        f"account reported an unrecognised position mode {mode!r}. Expected "
        f"{REQUIRED_POSITION_MODE!r}. Refusing to start."
    )


async def assert_cannot_withdraw(client: BitgetClient) -> tuple[str, ...]:
    """
    Require that this API key holds no withdrawal or transfer permission.

    The bot needs read + trade and nothing else. A key that can move funds is a
    key that can empty the account if it leaks or if this process misbehaves.

    Matching is on substrings of the lowercased authority name, so a rename on
    Bitget's side to e.g. 'withdrawal' still trips it. Over-triggering here is
    cheap; under-triggering is not.
    """
    try:
        authorities = await client.fetch_authorities()
    except ExchangeError as exc:
        raise PreflightError(
            f"could not read API key permissions, so cannot prove the key lacks "
            f"withdrawal rights: {exc}"
        ) from exc

    offending = [
        auth
        for auth in authorities
        if any(bad in auth.lower() for bad in FORBIDDEN_AUTHORITY_SUBSTRINGS)
    ]
    if offending:
        raise PreflightError(
            f"API key holds forbidden permission(s): {offending}. This bot requires a "
            "read + trade key only. Create a new key on Bitget without withdrawal or "
            "transfer rights and restart."
        )

    if not authorities:
        raise PreflightError(
            "API key reported an empty permission list. Cannot confirm trade access "
            "or absence of withdrawal rights. Refusing to start."
        )

    return authorities


async def run_all(client: BitgetClient, log) -> dict:
    """
    Run every preflight assertion in order. Raises PreflightError on the first failure.

    Returns a small dict of what was verified, for the startup log line.
    """
    log.info("preflight.start")

    position_mode = await assert_one_way_position_mode(client)
    log.info("preflight.position_mode_ok", position_mode=position_mode)

    authorities = await assert_cannot_withdraw(client)
    log.info("preflight.permissions_ok", authorities=list(authorities))

    log.info("preflight.passed")
    return {"position_mode": position_mode, "authorities": list(authorities)}
