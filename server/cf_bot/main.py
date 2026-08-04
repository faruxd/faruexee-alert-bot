"""
Entrypoint.

Sequence:
    load config -> configure logging -> kill-switch check -> load credentials
    -> connect -> preflight -> reconcile -> log full state
    -> loop { kill switch, reconcile, exits, guards, entry } until signalled
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from cf_bot.config import (
    AppConfig,
    ConfigError,
    Credentials,
    load_app_config,
    load_credentials,
)
from cf_bot.constants import (
    EXIT_CONFIG_ERROR,
    EXIT_KILL_SWITCH,
    EXIT_OK,
    EXIT_PREFLIGHT_FAILED,
    EXIT_RECONCILE_FAILED,
    EXIT_UNHANDLED,
    EXIT_UNPROTECTED_POSITION,
    MODE_LIVE,
)
from cf_bot.exchange import BitgetClient, DemoModeRefusal, ExchangeError
from cf_bot.killswitch import kill_file_present
from cf_bot.logging_setup import configure_logging, get_logger
from cf_bot.notify import (
    DiscordNotifier,
    critical_message,
    position_closed_message,
    position_opened_message,
)
from cf_bot.orders import ExecutionError, RateLimiter, UnprotectedPositionError, flatten
from cf_bot.preflight import PreflightError, run_all as run_preflight
from cf_bot.reconcile import ReconcileError, reconcile
from cf_bot.state import AccountState
from cf_bot.trader import Trader, run_iteration

# Order timestamps older than this are irrelevant to the per-hour guard.
ORDER_HISTORY_RETENTION_MS = 2 * 3_600_000


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, shutdown: asyncio.Event, log) -> None:
    """
    Graceful shutdown on SIGINT/SIGTERM.

    add_signal_handler is the correct asyncio mechanism but is not implemented
    on Windows, where we fall back to signal.signal. The deploy target is Linux;
    the fallback exists so the bot is developable on Windows.
    """

    def _request_shutdown(signum, *_):
        log.warning("signal.received", signal=int(signum))
        shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig)
        except (NotImplementedError, AttributeError, RuntimeError, ValueError):
            try:
                signal.signal(sig, _request_shutdown)
            except (OSError, ValueError):
                log.warning("signal.handler_unavailable", signal=int(sig))


async def _engage_kill_switch(
    client: Optional[BitgetClient],
    state: Optional[AccountState],
    log,
    limiter: RateLimiter,
    kill_file: Path,
    notifier: Optional["DiscordNotifier"] = None,
) -> int:
    """
    Cancel every order, flatten every position, exit.

    If a flatten fails we say so at ERROR and still exit -- a bot that cannot
    close a position must not keep running and trading around it. The operator
    needs to know exactly what is left.
    """
    log.error("killswitch.engaged", kill_file=str(kill_file), action="cancel all, flatten all")

    if client is None or state is None:
        log.error(
            "killswitch.halted_early",
            note="engaged before the exchange was reachable; nothing was placed by this process",
        )
        return EXIT_KILL_SWITCH

    failures: list[str] = []
    symbols_to_clear = {p.symbol for p in state.live_positions} | set(client.symbols)

    for symbol in sorted(symbols_to_clear):
        try:
            await flatten(client, symbol, log, limiter, reason="kill switch")
        except DemoModeRefusal:
            log.warning("killswitch.demo_no_flatten", symbol=symbol)
        except (ExecutionError, ExchangeError) as exc:
            failures.append(f"{symbol}: {exc}")
            log.error("killswitch.flatten_failed", symbol=symbol, error=str(exc))

    if failures:
        log.error(
            "killswitch.manual_intervention_required",
            failures=failures,
            note="close these by hand in the Bitget UI",
        )
        if notifier is not None:
            await notifier.send(
                critical_message(
                    "KILL SWITCH — COULD NOT FLATTEN EVERYTHING",
                    "\n".join(failures) + "\n\nClose these by hand in the Bitget UI.",
                ),
                allow_duplicate=True,
            )
    else:
        log.error("killswitch.all_clear", note="all orders cancelled and positions flat")
        if notifier is not None:
            await notifier.send(
                critical_message(
                    "KILL SWITCH ENGAGED", "All orders cancelled, all positions flat. Bot stopped."
                ),
                allow_duplicate=True,
            )

    return EXIT_KILL_SWITCH


def _position_key(position) -> tuple[str, str]:
    return (position.symbol, position.side)


async def _notify_position_changes(
    notifier: DiscordNotifier,
    previous: Optional[AccountState],
    current: AccountState,
    log,
) -> None:
    """
    Announce positions that appeared or disappeared between two snapshots.

    Diffing reconciled state rather than hooking the entry path is deliberate:
    it catches every position however it arose -- placed by the bot, filled from
    a resting order between iterations, or opened by hand in the Bitget UI.

    Never raises. A notification problem must not disturb the trading loop.
    """
    if previous is None:
        return

    before = {_position_key(p): p for p in previous.live_positions}
    after = {_position_key(p): p for p in current.live_positions}

    for key in after.keys() - before.keys():
        position = after[key]
        log.info("notify.position_opened", position=position.describe())
        await notifier.send(
            position_opened_message(position, current.mode, current.equity)
        )

    for key in before.keys() - after.keys():
        symbol, side = key
        # Realised PnL comes from the exchange's own closed-position history,
        # not from anything we computed.
        realised = None
        for closed in current.todays_closed_positions:
            if closed.symbol == symbol:
                realised = closed.realised_pnl
        log.info("notify.position_closed", symbol=symbol, side=side)
        await notifier.send(
            position_closed_message(symbol, side, realised, current.equity)
        )


async def _run_loop(config: AppConfig, credentials: Credentials, log) -> int:
    """The supervised body of the process. Returns a process exit code."""
    shutdown = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), shutdown, log)

    client = BitgetClient(credentials, config.settings, config.mode)
    limiter = RateLimiter()
    trader = Trader()
    notifier = DiscordNotifier.from_env(log)
    last_state: Optional[AccountState] = None

    try:
        try:
            await client.connect()
        except ExchangeError as exc:
            log.error("startup.connect_failed", error=str(exc))
            return EXIT_PREFLIGHT_FAILED

        log.info("startup.connected", symbols=list(client.symbols))

        try:
            await run_preflight(client, log)
        except PreflightError as exc:
            log.error("startup.preflight_failed", error=str(exc))
            return EXIT_PREFLIGHT_FAILED

        try:
            last_state = await reconcile(client, config.mode)
        except ReconcileError as exc:
            log.error("startup.reconcile_failed", error=str(exc))
            return EXIT_RECONCILE_FAILED

        log.info("startup.state", **last_state.log_payload())
        log.info(
            "startup.trading",
            mode=config.mode,
            trading_enabled=client.trading_enabled,
            loop_interval_seconds=config.settings.runtime.loop_interval_seconds,
        )

        last_heartbeat = 0.0
        last_comparable = last_state.comparable()

        while not shutdown.is_set():
            if kill_file_present(config.kill_file):
                return await _engage_kill_switch(
                    client, last_state, log, limiter, config.kill_file, notifier
                )

            try:
                state = await reconcile(client, config.mode)
            except ReconcileError as exc:
                # A single failed poll is not fatal, but we never keep a stale
                # snapshot around pretending it is current, and we never trade
                # off one.
                log.error("loop.reconcile_failed", error=str(exc))
                await _sleep_or_shutdown(shutdown, config.settings.runtime.loop_interval_seconds)
                continue

            await _notify_position_changes(notifier, last_state, state, log)
            last_state = state

            comparable = state.comparable()
            if comparable != last_comparable:
                log.info("state.changed", **state.log_payload())
                last_comparable = comparable
                last_heartbeat = time.monotonic()
            elif time.monotonic() - last_heartbeat >= config.settings.runtime.heartbeat_seconds:
                log.info("state.heartbeat", **state.log_payload())
                last_heartbeat = time.monotonic()

            try:
                await run_iteration(client, config, state, trader, log, limiter)
            except UnprotectedPositionError as exc:
                log.error(
                    "fatal.unprotected_position",
                    error=str(exc),
                    action="halting; the exchange holds a position we could not protect or close",
                )
                await notifier.send(
                    critical_message(
                        "UNPROTECTED POSITION — BOT HALTED",
                        f"{exc}\n\nCheck the account in the Bitget UI now.",
                    ),
                    allow_duplicate=True,
                )
                return EXIT_UNPROTECTED_POSITION
            except ExecutionError as exc:
                log.error("loop.execution_failed", error=str(exc))

            cutoff = state.fetched_at_ms - ORDER_HISTORY_RETENTION_MS
            trader.order_timestamps = tuple(
                ts for ts in trader.order_timestamps if ts >= cutoff
            )

            await _sleep_or_shutdown(shutdown, config.settings.runtime.loop_interval_seconds)

        log.info("shutdown.graceful", reason="signal")
        return EXIT_OK

    finally:
        await client.close()
        log.info("shutdown.exchange_closed")


async def _sleep_or_shutdown(shutdown: asyncio.Event, seconds: float) -> None:
    """
    Async wait that wakes early on a shutdown signal.

    This is why the constraint forbids time.sleep() in the live loop: a blocking
    sleep would make the bot deaf to SIGTERM for up to a full interval.
    """
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)
    except (asyncio.TimeoutError, TimeoutError):
        pass


def run(argv: Optional[list[str]] = None) -> int:
    """Synchronous wrapper. Returns the process exit code; never raises."""
    argv = sys.argv[1:] if argv is None else argv
    working_dir = Path.cwd()
    config_path = Path(argv[0]) if argv else working_dir / "config.yaml"

    try:
        config = load_app_config(config_path, working_dir)
    except ConfigError as exc:
        print(f"FATAL: configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    try:
        configure_logging(
            level=config.settings.logging.level,
            log_file=config.log_file,
            max_bytes=config.settings.logging.max_bytes,
            backup_count=config.settings.logging.backup_count,
        )
    except OSError as exc:
        print(f"FATAL: could not open log file {config.log_file}: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    log = get_logger()
    log.info("startup.config", **config.log_payload())

    if config.mode == MODE_LIVE:
        log.warning(
            "startup.live_mode",
            note="MODE=live. This process will place real orders with real funds.",
        )

    if kill_file_present(config.kill_file):
        log.error(
            "killswitch.engaged_at_startup",
            kill_file=str(config.kill_file),
            action="refusing to start",
        )
        return EXIT_KILL_SWITCH

    try:
        credentials = load_credentials()
    except ConfigError as exc:
        log.error("startup.credentials_missing", error=str(exc))
        return EXIT_CONFIG_ERROR

    try:
        return asyncio.run(_run_loop(config, credentials, log))
    except KeyboardInterrupt:
        log.info("shutdown.keyboard_interrupt")
        return EXIT_OK
    except Exception as exc:
        log.exception("fatal.unhandled", error=str(exc))
        return EXIT_UNHANDLED
