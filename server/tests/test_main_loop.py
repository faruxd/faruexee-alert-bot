"""
Loop-level behaviour: idling, the kill switch mid-run, and aborting on a failed
preflight. Still no network -- BitgetClient is swapped for the fake.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from cf_bot import main as main_module
from cf_bot.config import AppConfig, Credentials, load_settings
from cf_bot.constants import (
    EXIT_KILL_SWITCH,
    EXIT_OK,
    EXIT_PREFLIGHT_FAILED,
    EXIT_RECONCILE_FAILED,
)
from cf_bot.exchange import ExchangeError
from cf_bot.logging_setup import get_logger
from tests.conftest import FakeBitgetClient


@pytest.fixture
def app_config(write_config, valid_config_yaml, tmp_path) -> AppConfig:
    text = valid_config_yaml.replace("loop_interval_seconds: 15.0", "loop_interval_seconds: 1.0")
    settings = load_settings(write_config(text))
    return AppConfig(settings=settings, mode="demo", working_dir=tmp_path)


@pytest.fixture
def creds() -> Credentials:
    return Credentials("k", "s", "p")


@pytest.fixture
def patch_client(monkeypatch):
    """Swap the real client for the fake, and return the instance the loop will use."""

    def _install(client: FakeBitgetClient) -> FakeBitgetClient:
        monkeypatch.setattr(main_module, "BitgetClient", lambda *a, **kw: client)
        return client

    return _install


# --- sleep primitive -------------------------------------------------------


async def test_sleep_wakes_early_on_shutdown():
    """
    Why the constraint forbids time.sleep(): a blocking sleep would make the bot
    deaf to SIGTERM for a whole interval.
    """
    shutdown = asyncio.Event()
    shutdown.set()
    loop = asyncio.get_running_loop()
    started = loop.time()
    await main_module._sleep_or_shutdown(shutdown, seconds=30)
    assert loop.time() - started < 1.0


async def test_sleep_returns_after_timeout_when_not_signalled():
    shutdown = asyncio.Event()
    await main_module._sleep_or_shutdown(shutdown, seconds=0.05)
    assert not shutdown.is_set()


# --- preflight aborts ------------------------------------------------------


async def test_hedge_mode_account_aborts_before_idling(app_config, creds, patch_client):
    client = patch_client(FakeBitgetClient(position_mode="hedge_mode"))
    code = await main_module._run_loop(app_config, creds, get_logger())
    assert code == EXIT_PREFLIGHT_FAILED
    assert client.close_calls == 1, "exchange session must be closed even on abort"


async def test_withdraw_capable_key_aborts_before_idling(app_config, creds, patch_client):
    client = patch_client(FakeBitgetClient(authorities=("readonly", "trade", "withdraw")))
    code = await main_module._run_loop(app_config, creds, get_logger())
    assert code == EXIT_PREFLIGHT_FAILED
    assert client.close_calls == 1


async def test_first_reconcile_failure_aborts(app_config, creds, patch_client, exchange_error):
    client = FakeBitgetClient()
    client.positions = exchange_error
    patch_client(client)
    code = await main_module._run_loop(app_config, creds, get_logger())
    assert code == EXIT_RECONCILE_FAILED
    assert client.close_calls == 1


# --- kill switch mid-run ---------------------------------------------------


async def test_kill_file_created_mid_run_halts_the_loop(
    app_config, creds, patch_client, ccxt_position_long
):
    client = patch_client(FakeBitgetClient(positions=[ccxt_position_long]))

    async def create_kill_file_shortly():
        await asyncio.sleep(0.3)
        app_config.kill_file.write_text("", encoding="utf-8")

    asyncio.create_task(create_kill_file_shortly())
    code = await asyncio.wait_for(
        main_module._run_loop(app_config, creds, get_logger()), timeout=10
    )

    assert code == EXIT_KILL_SWITCH
    assert client.close_calls == 1


async def test_kill_switch_cancels_and_flattens(
    app_config, creds, patch_client, ccxt_position_long, ccxt_stop_order, caplog
):
    """The full contract: cancel all orders, flatten all positions, exit."""
    caplog.set_level(logging.INFO)
    client = patch_client(
        FakeBitgetClient(positions=[ccxt_position_long], open_orders=[ccxt_stop_order])
    )
    app_config.kill_file.write_text("", encoding="utf-8")

    code = await asyncio.wait_for(
        main_module._run_loop(app_config, creds, get_logger()), timeout=10
    )

    assert code == EXIT_KILL_SWITCH
    assert "BTC/USDT:USDT" in client.cancel_all_calls, "orders were not cancelled"
    assert any(o["kind"] == "close" for o in client.sent_orders), "position was not closed"
    assert "killswitch.engaged" in caplog.text
    assert "killswitch.all_clear" in caplog.text
    assert client.close_calls == 1


async def test_kill_switch_escalates_when_it_cannot_flatten(
    app_config, creds, patch_client, ccxt_position_long, caplog
):
    """
    A flatten that fails must be reported loudly and by name -- the operator has
    to know exactly what is still open. It still exits; a bot that cannot close
    a position must not keep trading around it.
    """
    caplog.set_level(logging.ERROR)
    client = patch_client(FakeBitgetClient(positions=[ccxt_position_long]))

    async def refuse_to_close(*args, **kwargs):
        raise ExchangeError("venue rejected the close")

    client.create_reduce_only_market = refuse_to_close
    app_config.kill_file.write_text("", encoding="utf-8")

    code = await asyncio.wait_for(
        main_module._run_loop(app_config, creds, get_logger()), timeout=10
    )

    assert code == EXIT_KILL_SWITCH
    assert "killswitch.flatten_failed" in caplog.text
    assert "manual_intervention_required" in caplog.text
    assert "BTC/USDT:USDT" in caplog.text


async def test_kill_switch_in_demo_does_not_transmit(
    app_config, creds, patch_client, ccxt_position_long, caplog
):
    caplog.set_level(logging.INFO)
    client = patch_client(FakeBitgetClient(positions=[ccxt_position_long], mode="demo"))
    app_config.kill_file.write_text("", encoding="utf-8")

    code = await asyncio.wait_for(
        main_module._run_loop(app_config, creds, get_logger()), timeout=10
    )

    assert code == EXIT_KILL_SWITCH
    assert client.sent_orders == []


# --- idling ----------------------------------------------------------------


async def test_loop_idles_and_shuts_down_gracefully(app_config, creds, patch_client, monkeypatch):
    """It polls repeatedly, places nothing, and returns 0 when signalled."""
    client = patch_client(FakeBitgetClient())

    captured = {}
    real_sleep = main_module._sleep_or_shutdown

    async def sleep_then_signal(shutdown, seconds):
        captured["shutdown"] = shutdown
        if client.reconcile_calls >= 3:
            shutdown.set()
        await real_sleep(shutdown, 0.01)

    monkeypatch.setattr(main_module, "_sleep_or_shutdown", sleep_then_signal)

    code = await asyncio.wait_for(
        main_module._run_loop(app_config, creds, get_logger()), timeout=10
    )

    assert code == EXIT_OK
    assert client.reconcile_calls >= 3, "loop must re-reconcile every iteration"
    assert client.close_calls == 1


async def test_loop_survives_a_transient_reconcile_failure(
    app_config, creds, patch_client, monkeypatch, exchange_error
):
    """One bad poll is logged loudly but does not kill the process."""
    client = patch_client(FakeBitgetClient())
    real_sleep = main_module._sleep_or_shutdown
    state = {"iteration": 0}

    async def sleep_then_break(shutdown, seconds):
        state["iteration"] += 1
        if state["iteration"] == 1:
            client.positions = exchange_error  # next poll fails
        elif state["iteration"] == 2:
            client.positions = []  # recovered
        elif state["iteration"] >= 4:
            shutdown.set()
        await real_sleep(shutdown, 0.01)

    monkeypatch.setattr(main_module, "_sleep_or_shutdown", sleep_then_break)

    code = await asyncio.wait_for(
        main_module._run_loop(app_config, creds, get_logger()), timeout=10
    )
    assert code == EXIT_OK


async def test_demo_mode_never_transmits_an_order(app_config, creds, patch_client, monkeypatch):
    """
    The mode gate, end to end.

    Even with a signal-producing setup, MODE=demo must reach the end of the loop
    having sent nothing. The client refuses before a request is built, so
    `sent_orders` staying empty is the proof.
    """
    client = patch_client(FakeBitgetClient(mode="demo"))
    assert client.trading_enabled is False

    real_sleep = main_module._sleep_or_shutdown

    async def stop_after_two(shutdown, seconds):
        if client.reconcile_calls >= 2:
            shutdown.set()
        await real_sleep(shutdown, 0.01)

    monkeypatch.setattr(main_module, "_sleep_or_shutdown", stop_after_two)
    code = await asyncio.wait_for(
        main_module._run_loop(app_config, creds, get_logger()), timeout=10
    )

    assert code == EXIT_OK
    assert client.sent_orders == [], "demo mode transmitted an order"


# --- kill switch at startup ------------------------------------------------
#
# On Render the kill switch is an environment variable, and changing it
# RESTARTS the service -- so the switch always arrives as a startup event and
# the mid-run flatten path can never run there. An earlier version refused to
# start without connecting, leaving a live position open while reporting the
# bot as killed.


async def test_kill_at_startup_connects_and_flattens(
    app_config, creds, patch_client, ccxt_position_long, caplog
):
    caplog.set_level(logging.INFO)
    client = patch_client(FakeBitgetClient(positions=[ccxt_position_long]))
    app_config.kill_file.write_text("", encoding="utf-8")

    code = await asyncio.wait_for(
        main_module._kill_and_flatten(app_config, creds, get_logger()), timeout=10
    )

    assert code == EXIT_KILL_SWITCH
    assert client.connect_calls == 1, "never connected, so never flattened"
    assert any(o["kind"] == "close" for o in client.sent_orders), "position left open"
    assert client.close_calls == 1


async def test_kill_at_startup_flattens_even_if_reconcile_fails(
    app_config, creds, patch_client, ccxt_position_long, exchange_error, caplog
):
    """Not knowing the state is a reason to close, not a reason to walk away."""
    caplog.set_level(logging.ERROR)
    client = patch_client(FakeBitgetClient(positions=[ccxt_position_long]))
    client.closed_positions = exchange_error  # breaks reconcile only

    code = await asyncio.wait_for(
        main_module._kill_and_flatten(app_config, creds, get_logger()), timeout=10
    )

    assert code == EXIT_KILL_SWITCH
    assert any(o["kind"] == "close" for o in client.sent_orders)
    assert "killswitch.reconcile_failed" in caplog.text


async def test_kill_at_startup_reports_when_it_cannot_connect(
    app_config, creds, patch_client, caplog
):
    caplog.set_level(logging.ERROR)
    client = patch_client(FakeBitgetClient())

    async def no_connect():
        raise ExchangeError("venue unreachable")

    client.connect = no_connect
    app_config.kill_file.write_text("", encoding="utf-8")

    code = await asyncio.wait_for(
        main_module._kill_and_flatten(app_config, creds, get_logger()), timeout=10
    )

    assert code == EXIT_KILL_SWITCH
    assert "killswitch.connect_failed" in caplog.text
    assert "untouched" in caplog.text


async def test_kill_at_startup_skips_preflight(
    app_config, creds, patch_client, ccxt_position_long
):
    """
    A hedge-mode account or a bad key must not stop the flatten. The account
    being in a strange state is often WHY the operator hit the switch.
    """
    client = patch_client(
        FakeBitgetClient(positions=[ccxt_position_long], position_mode="hedge_mode")
    )
    code = await asyncio.wait_for(
        main_module._kill_and_flatten(app_config, creds, get_logger()), timeout=10
    )
    assert code == EXIT_KILL_SWITCH
    assert any(o["kind"] == "close" for o in client.sent_orders)
