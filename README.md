# Krater
The management utility for Project Ganymede

Krater is the Project Ganymede portal: proposal review, compute budget allocation and enforcement, and the public
gallery of completed projects.

## Docs

- [Spec](docs/SPEC.md): product spec, data model, workflow, deployment, open questions
- [Weave integration](docs/weave-integration.md): sign-in, roles, and the Weave changes Krater depends on
- [SkyPilot integration](docs/skypilot-integration.md): workspaces, the admin-policy launch gate, and the spend reconciler

## Development

Stack, layout and conventions are documented in [CLAUDE.md](CLAUDE.md). Quick start:

```bash
# Once: install Python 3.12 and the project's dependencies.
uv python install 3.12
uv sync

# Once: create the local databases (adjust to your Postgres superuser).
createdb krater_dev
createdb krater_test

# Copy and adjust environment variables (KRATER_DATABASE_URL etc.).
cp .env.example .env

# Apply migrations, then run the web app and worker (in separate terminals).
uv run alembic upgrade head
uv run uvicorn krater.web.app:create_app --factory --reload
uv run procrastinate --app=krater.worker.app.app worker

# Lint, format and test.
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

`KRATER_TEST_DATABASE_URL` (default `postgresql+psycopg://root:root@localhost:5432/krater_test`) points
tests at a separate database; see `tests/conftest.py`.

The full stack (Postgres, a one-shot migration, the portal and the worker) also runs under
`docker compose up --build`.
