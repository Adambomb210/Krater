# Krater handoff

Everything a fresh Claude Code session (or a human) needs to pick up Krater. Written 2026-09-27 at the end of the
cloud session that built v1.

**If you're a Claude session: read this file, then `CLAUDE.md`, then `docs/SPEC.md`, before changing anything.**

---

## 1. What exists

**Krater** is the Project Ganymede portal (Patchwork Labs). It covers proposal review, dollar compute budgets
enforced on SkyPilot/Vast.ai, Slack-based review, a public gallery and a GPU pricing page.

| Repo | Where | Branch | State |
| --- | --- | --- | --- |
| **Krater** | https://github.com/Adambomb210/Krater | `claude/exciting-sagan-7oh2zh` | **Draft PR [#1](https://github.com/Adambomb210/Krater/pull/1)**, CI green, 511 tests |
| **Weave** (identity provider) | https://github.com/patchworklabsorg/weave | **not on GitHub**: 3 local commits (patches below) | Needs someone with push access |

`main` in Krater is only the initial commit. All the work is on the PR branch.

## 2. Clone and run Krater locally

### Windows (PowerShell) with Docker Desktop

```powershell
# 1. Clone the PR branch
git clone -b claude/exciting-sagan-7oh2zh https://github.com/Adambomb210/Krater.git
cd Krater

# 2. Install uv (Python tool manager) if needed, then Python 3.12 and the dependencies
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv python install 3.12
uv sync

# 3. A Postgres for dev and tests (matches the tests' default URL: root:root@localhost:5432)
docker run -d --name krater-pg -e POSTGRES_USER=root -e POSTGRES_PASSWORD=root -p 5432:5432 postgres:16
docker exec krater-pg psql -U root -d postgres -c "CREATE DATABASE krater_dev" -c "CREATE DATABASE krater_test"

# 4. Tests and lint
uv run pytest
uv run ruff check . ; uv run ruff format --check .

# 5. Run the app in stub mode (fake users, fake SkyPilot/Slack/S3), then open http://localhost:8000
$env:KRATER_DATABASE_URL = "postgresql+psycopg://root:root@localhost:5432/krater_dev"
uv run alembic upgrade head
uv run uvicorn krater.web.app:create_app --factory --reload
```

- **Don't create a `.env` in the repo root while running tests.** pydantic-settings auto-loads it, and live settings
  leak into the test suite. Use a separately named file and load it into your shell.
- Background worker (optional locally): `uv run procrastinate --app=krater.worker.app.app worker`.
- **Full stack in Docker:** copy `.env.example` to `.env`, then run `docker compose up --build`. Add
  `--profile skypilot` for the SkyPilot server and sign-in proxy. See `docs/dev/staging.md`.

### macOS / Linux / WSL
The same steps with bash syntax (install uv with `curl -LsSf https://astral.sh/uv/install.sh | sh`).

## 3. Apply the Weave changes (not on GitHub)

Krater depends on three Weave commits: the `groups` / `slack_id` claims, the `/api/v1/users` directory API, and two
browser sign-in fixes. They're in the handoff zip as `weave-patches/0001…0003`.

**If you already applied `0001` on your `Krater-Integration` branch** (you did, from the earlier patch), apply
only the other two:

```powershell
cd C:\Projects\Weave\weave
git checkout Krater-Integration
git am "C:\path\to\krater-handoff\weave-patches\0002-*.patch" "C:\path\to\krater-handoff\weave-patches\0003-*.patch"
```

**From a fresh Weave clone**, apply all three:

```powershell
git clone https://github.com/patchworklabsorg/weave.git; cd weave
git checkout -b Krater-Integration origin/main
git am C:\path\to\krater-handoff\weave-patches\*.patch
```

Then push `Krater-Integration` and open a Weave PR. That needs push access to `patchworklabsorg/weave`; the Claude
GitHub App isn't installed there. `0002` first widened a CSP rule globally, and `0003` narrows it again. Apply both.

## 4. What's done and verified

- **v1 features:**
  - Weave OIDC sign-in, with live role checks;
  - the proposal → review → approval workflow, amendments, and completion review;
  - the configurable approval policy and the append-only budget ledger;
  - SkyPilot: the launch gate and the reconcile job (workspaces, spend, 80% warning, 100% teardown);
  - Slack review channels;
  - screenshot uploads and the gallery;
  - the `/pricing` page and the budget estimator;
  - production hardening.
- **Verified here:** 511 tests pass, CI is green, and pip-audit is clean. A security review found 7 issues and
  all are fixed. Live tests ran against a real Weave, a real SkyPilot 0.13.0 server (`scripts/dev/skypilot_contract.sh`)
  and a real SeaweedFS. Details are in the PR description and `docs/dev/*.md`.
- **Not verified yet** (steps 1–10 of `docs/dev/staging.md` cover all of it):
  - the Docker Compose stack actually running;
  - a real oauth2-proxy sign-in;
  - a real Vast launch, and billing drift;
  - spot machines and their recovery;
  - the serve-status "no services" path;
  - real Slack.

## 5. Decisions and standing instructions (don't re-litigate)

- **PR #1 stays a draft until the maintainer explicitly approves marking it ready.**
- Don't push to branches other than `claude/exciting-sagan-7oh2zh` without permission.
- **Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 / Alembic / Postgres / procrastinate (no Redis). See `CLAUDE.md`
  for conventions: services own the rules, money is integer cents, roles come only from Weave groups.
- **SkyPilot is pinned to 0.13.0.** Krater talks to it over plain REST, with no `skypilot` package dependency
  (it's 453 MB and conflicts with Krater's dependencies). One private, Vast-only workspace per project; members
  sign in to SkyPilot with Weave via oauth2-proxy.
- **The launch gate is publicly reachable.** Members' own machines call it, and its URL token is visible to them.
  So it must stay free of side effects and only enforce on the server-side call.
- **Storage** is a temporary SeaweedFS container. MinIO was rejected (its community edition was archived in 2025).
  The long-term provider is undecided.
- **Prices** come from SkyPilot's public Vast catalog CSV, which is the same data SkyPilot's own price listing reads.
- **Staging** runs on the maintainer's own Windows machine: Docker Desktop, with the `sky` CLI in WSL2.
- **Donated idle compute** is parked. The design notes are in `docs/FUTURE.md`.

## 6. What to do next

**The maintainer:**
1. Do the staging run (`docs/dev/staging.md`) and bring any failures back to a session to fix on the PR.
2. Get the Weave commits pushed and reviewed (section 3).
3. Create Krater's Slack app (`docs/dev/slack-setup.md`).
4. Pick a long-term screenshot storage provider.
5. Approve PR #1 out of draft when ready.

**A Claude session, in suggested order:**
1. **Local dev setup script.** Make first-time setup one command, e.g. a `scripts/dev/setup` that starts the
   Postgres container, creates the databases and runs `uv sync`, for Windows and bash.
2. **Weave follow-ups** (in the Weave repo):
   - locked/suspended users can still sign in to OAuth apps. Fix `resource_owner_authenticator` in
     `config/initializers/doorkeeper.rb` to use the same checks as `ApplicationController#load_authenticated_user`,
     and revoke Doorkeeper tokens on lock or suspend;
   - weave#118: a Slack-membership claim. Then Krater can drop its email-based Slack check
     (`krater/services/slack_membership.py`).
3. **A CI job for the SkyPilot contract test**, run nightly and on SkyPilot upgrades.
4. The remaining items in `docs/FUTURE.md`.

## 7. Gotchas learned the hard way

- **Test real servers, not just fakes.** Real-server testing caught 3 SkyPilot wire-format bugs and 2 Weave browser
  bugs that mocks never would have. Re-run `scripts/dev/skypilot_contract.sh` after touching `krater/skypilot/`. It
  needs a venv with `skypilot[vast]==0.13.0`; see `docs/dev/skypilot-contract.md`.
- **Keep autogenerate away from procrastinate's tables.** `alembic/env.py` filters out `procrastinate_*` tables;
  without that, autogenerated migrations try to drop them.
- **Postgres `now()` is frozen per transaction**, so timestamps that must order rows within one transaction are
  stamped in Python (see `SpendSnapshot`).
- **Rate limiting is off when `KRATER_ENV=test`.** Its own tests switch it on explicitly.
- **SkyPilot's serve-status call fails in sandboxes without direct internet** (its network probe). That's expected
  there; on a normal machine it isn't.
- **Test the Weave sign-in flow in a real browser.** Weave's CSP `form-action` applies to the whole redirect chain,
  so curl-based tests miss breakage.
- **Earlier patch files are now obsolete.** `secfix-wip.patch` and `weave-krater-integration-fixes.patch` are
  superseded: the security fixes are merged on the PR branch, and the Weave fixes are patches `0002`/`0003` here.

## 8. Prompt to start a fresh local Claude Code session

Open a terminal in the cloned `Krater` folder, start Claude Code, and paste:

> You're continuing work on Krater (this repo, branch `claude/exciting-sagan-7oh2zh`, draft PR
> https://github.com/Adambomb210/Krater/pull/1). Read `docs/HANDOFF.md` first, then `CLAUDE.md` and `docs/SPEC.md`.
> Follow the standing instructions in HANDOFF section 5; in particular, never mark the PR ready without my explicit
> approval. First, get the test suite running locally (HANDOFF section 2) and tell me the result. Then propose what
> to work on from HANDOFF section 6 before starting.
