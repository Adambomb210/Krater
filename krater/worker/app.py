"""The procrastinate App: background jobs and periodic tasks, backed by the same Postgres database.

Run with: `uv run procrastinate --app=krater.worker.app.app worker`
"""

from __future__ import annotations

import logging

import procrastinate

from krater.config import get_settings
from krater.db import get_sessionmaker
from krater.services.skypilot_sync import reconcile
from krater.skypilot import SkyPilotError, get_skypilot_client

logger = logging.getLogger(__name__)


def _psycopg_conninfo(database_url: str) -> str:
    """Turn a SQLAlchemy-style URL (`postgresql+psycopg://...`) into a plain libpq conninfo string."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=_psycopg_conninfo(get_settings().database_url)),
)


@app.periodic(cron="* * * * *")
@app.task(name="heartbeat")
def heartbeat(timestamp: int) -> None:
    """Log once a minute, so it's easy to see the worker and its scheduler are alive."""
    logger.info("krater worker heartbeat", extra={"timestamp": timestamp})


def _skypilot_reconcile_cron() -> str:
    """`*/N * * * *` from `skypilot_reconcile_interval_minutes` -- built at import time, so changing
    the setting takes effect the next time the worker (and its scheduler) restarts."""
    return f"*/{get_settings().skypilot_reconcile_interval_minutes} * * * *"


@app.periodic(cron=_skypilot_reconcile_cron())
@app.task(name="skypilot_reconcile")
def skypilot_reconcile(timestamp: int) -> None:
    """Provision/tear down SkyPilot workspaces, take spend snapshots, and enforce budgets.

    Thin wrapper around `krater.services.skypilot_sync.reconcile`; see there for the actual logic. Also
    runnable directly for manual runs/debugging via `python -m krater.skypilot.reconcile_once`.
    """
    del timestamp
    settings = get_settings()
    session = get_sessionmaker()()
    try:
        reconcile(session, get_skypilot_client(), warn_percent=settings.skypilot_budget_warn_percent)
    except SkyPilotError:
        # `reconcile` already catches per-step SkyPilot errors and logs+continues; this is a last-resort
        # net for anything that still escapes (e.g. a step raising before its own try/except is reached).
        logger.exception("krater.skypilot_reconcile task failed")
    finally:
        session.close()
