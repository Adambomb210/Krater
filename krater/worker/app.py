"""The procrastinate App: background jobs and periodic tasks, backed by the same Postgres database.

Run with: `uv run procrastinate --app=krater.worker.app.app worker`
"""

from __future__ import annotations

import logging

import procrastinate

from krater.config import get_settings

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
