"""Kill switch. The asymmetry test is the important one."""

from __future__ import annotations

from pathlib import Path

import pytest

from cf_bot.killswitch import kill_env_set, kill_file_present


# --- file trigger ----------------------------------------------------------


def test_absent_kill_file_means_not_engaged(tmp_path: Path):
    assert kill_file_present(tmp_path / "KILL", env={}) is False


def test_present_kill_file_means_engaged(tmp_path: Path):
    kill_file = tmp_path / "KILL"
    kill_file.write_text("", encoding="utf-8")
    assert kill_file_present(kill_file, env={}) is True


def test_empty_kill_file_still_engages(tmp_path: Path):
    """Presence is the signal. Contents are irrelevant; `touch KILL` must work."""
    kill_file = tmp_path / "KILL"
    kill_file.touch()
    assert kill_file_present(kill_file, env={}) is True


def test_a_directory_named_kill_also_engages(tmp_path: Path):
    kill_file = tmp_path / "KILL"
    kill_file.mkdir()
    assert kill_file_present(kill_file, env={}) is True


def test_unreadable_path_fails_closed(monkeypatch, tmp_path: Path):
    """
    If we cannot tell whether the switch is engaged, we report ENGAGED.

    A false positive costs a restart. A false negative leaves a bot trading
    while the operator believes it is stopped.
    """
    kill_file = tmp_path / "KILL"

    def boom(self):
        raise OSError("simulated filesystem failure")

    monkeypatch.setattr(Path, "exists", boom)
    assert kill_file_present(kill_file, env={}) is True


# --- environment trigger ---------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes", "on"])
def test_env_trigger_engages(value):
    assert kill_env_set({"CF_KILL": value}) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "   "])
def test_env_trigger_off_values(value):
    assert kill_env_set({"CF_KILL": value}) is False


def test_env_trigger_absent():
    assert kill_env_set({}) is False


def test_env_trigger_works_without_a_kill_file(tmp_path: Path):
    """The Render path: no shell, so the file is unreachable."""
    assert kill_file_present(tmp_path / "KILL", env={"CF_KILL": "1"}) is True


def test_either_trigger_is_sufficient(tmp_path: Path):
    kill_file = tmp_path / "KILL"
    kill_file.touch()
    assert kill_file_present(kill_file, env={"CF_KILL": "0"}) is True
