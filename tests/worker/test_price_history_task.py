"""The `price_history_record` periodic task: registered on its schedule, a no-op without a Vast client."""

from __future__ import annotations

from unittest.mock import patch

from krater.worker.app import app, price_history_record


def test_price_history_record_is_registered_every_ten_minutes() -> None:
    periodic_task = app.periodic_registry.periodic_tasks[("price_history_record", "")]

    assert periodic_task.cron == "*/10 * * * *"


def test_price_history_record_does_nothing_without_a_vast_client() -> None:
    with (
        patch("krater.worker.app.get_vast_client", return_value=None),
        patch("krater.worker.app.price_history") as mock_history,
        patch("krater.worker.app.get_sessionmaker") as mock_sessionmaker,
    ):
        price_history_record.func(timestamp=1_791_700_000)

        mock_history.record.assert_not_called()
        mock_sessionmaker.assert_not_called()


def test_price_history_record_records_and_commits() -> None:
    with (
        patch("krater.worker.app.get_vast_client") as mock_client,
        patch("krater.worker.app.price_history") as mock_history,
        patch("krater.worker.app.get_sessionmaker") as mock_sessionmaker,
    ):
        session = mock_sessionmaker.return_value.return_value
        price_history_record.func(timestamp=1_791_700_000)

        args, kwargs = mock_history.record.call_args
        assert args == (session, mock_client.return_value)
        assert kwargs["disk_gb"] == 32
        assert "RTX 4090" in kwargs["gpu_names"]
        assert kwargs["now"].timestamp() == 1_791_700_000
        session.commit.assert_called_once()
        session.close.assert_called_once()
