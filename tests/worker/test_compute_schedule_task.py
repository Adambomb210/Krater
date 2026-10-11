"""The `compute_schedule` periodic task: every minute, under a lock, and only with KRATER_COMPUTE_MODE=vast."""

from __future__ import annotations

from unittest.mock import patch

from krater.config import get_settings
from krater.worker.app import COMPUTE_SCHEDULER_LOCK, app, compute_schedule


def test_compute_schedule_runs_every_minute_under_a_lock() -> None:
    periodic_task = app.periodic_registry.periodic_tasks[("compute_schedule", "")]

    assert periodic_task.cron == "* * * * *"
    assert compute_schedule.lock == COMPUTE_SCHEDULER_LOCK
    assert compute_schedule.queueing_lock == COMPUTE_SCHEDULER_LOCK


def test_compute_schedule_does_nothing_unless_compute_mode_is_vast(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "off")
    with (
        patch("krater.worker.app.get_vast_client") as mock_client,
        patch("krater.worker.app.compute_scheduler") as mock_scheduler,
    ):
        compute_schedule.func(timestamp=1_791_700_000)

        mock_client.assert_not_called()
        mock_scheduler.run_pass.assert_not_called()


def test_compute_schedule_does_nothing_without_a_vast_client(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    with (
        patch("krater.worker.app.get_vast_client", return_value=None),
        patch("krater.worker.app.compute_scheduler") as mock_scheduler,
    ):
        compute_schedule.func(timestamp=1_791_700_000)

        mock_scheduler.run_pass.assert_not_called()


def test_compute_schedule_runs_a_pass(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    with (
        patch("krater.worker.app.get_vast_client") as mock_client,
        patch("krater.worker.app.compute_scheduler") as mock_scheduler,
        patch("krater.worker.app.get_sessionmaker") as mock_sessionmaker,
    ):
        session = mock_sessionmaker.return_value.return_value
        compute_schedule.func(timestamp=1_791_700_000)

        args, _ = mock_scheduler.run_pass.call_args
        assert args[0] is session and args[1] is mock_client.return_value
        session.close.assert_called_once()
