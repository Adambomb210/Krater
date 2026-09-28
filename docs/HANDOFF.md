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
| **Weave** (identity provider) | https://github.com/patchworklabsorg/weave | `main` works as is | Krater needs only plain OIDC; recommended sign-in and security fixes are local patches (section 3) |

`main` in Krater is only the initial commit. All the work is on the PR branch.

## 2. Clone and run Krater locally

### One command

With [uv](https://docs.astral.sh/uv/) installed and Docker running, `scripts/dev/setup.ps1` (Windows) or
`scripts/dev/setup.sh` (macOS, Linux, WSL, Git Bash) runs steps 2 and 3 below and migrates `krater_dev`. It's safe to
re-run. Add `-Check` / `--check` to also run step 4.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dev\setup.ps1 -Check
```

The manual steps follow.

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
- **Full stack in Docker:** copy `.env.example` to an env file outside the repo, then run
  `docker compose --env-file <that file> up --build` with `KRATER_ENV_FILE` set to the same path (a repo-root `.env`
  works too, but leaks into pytest). Add `--profile skypilot` for the SkyPilot server and sign-in proxy. See
  `docs/dev/staging.md` section 4.

### macOS / Linux / WSL
The same steps with bash syntax (install uv with `curl -LsSf https://astral.sh/uv/install.sh | sh`).

## 3. Weave: Krater runs against Weave `main`

**Decision (maintainer, 2026-09-28): Krater needs only plain OIDC sign-in from Weave**, as Weave's `main` branch
already provides it (`openid profile email`: `sub`, `name`, `email`, `email_verified`). Roles, the disabled switch and
Slack links live in Krater's own database; see `docs/weave-integration.md` and `docs/SPEC.md` "Roles &
authentication". Nothing from the old `Krater-Integration` branch is required any more:

- Patch `0001` (the `groups` / `slack_id` claims and the `/api/v1/users` directory API) and the uncommitted
  weave#118 `slack_membership` work are **no longer needed** by Krater. Krater doesn't ask for those scopes and has no
  service key.
- Patches **`0002`/`0003` are still recommended**: they fix two browser CSP bugs that break OAuth sign-in for
  returning users in Chrome and Safari, for any external client, Krater included.
- The **security fixes** in `krater-handoff/weave-patches/WEAVE-BUG-REPORT.md` are worth merging for Weave's own
  sake: an account takeover through unsigned Slack events (critical), owner takeover from the admin panel, locked
  users still signing in to OAuth apps with working tokens, and `/admin` engines reachable after sign-out. Until the
  lockout fix lands, use Krater's own **disable** (`/admin/users`) to shut someone out of Krater; until the
  Slack-events fix lands, prefer Weave subs over emails in `KRATER_BOOTSTRAP_ADMINS`.

**Both are on the Weave branch
[`fix/security-hardening`](https://github.com/patchworklabsorg/weave/tree/fix/security-hardening)** (pushed
2026-09-28; no PR opened yet): `0002` and `0003`, then one commit per security fix, all on `origin/main` 54f702a. Open
a PR from it to merge. The patch files are the same changes. Weave's test suites run on Windows without Ruby via
`E:\Projects\Krater\weave-testenv\run.sh` (Docker; e.g. `WEAVE_REPO=E:/Projects/Krater/weave-branch ./run.sh bundle
exec rspec`).

**First admin on a real deployment:** set `KRATER_BOOTSTRAP_ADMINS` to your Weave sub (e.g. `PWL5A1B2C3D4`) and sign
in; grant everyone else's roles at `/admin/users` (by email for people who haven't signed in yet).

## 4. What's done and verified

- **v1 features:**
  - Weave OIDC sign-in (plain OIDC, against Weave `main`), with roles kept in Krater's database and checked fresh
    on every action; an `/admin/users` page for roles, grants by email, disabling accounts and Slack links;
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
- **Docker Compose stack: verified** on the maintainer's Windows machine (Docker Desktop; stub Weave, fake Slack,
  empty Vast key): migrations, portal, worker jobs, stub sign-in, SeaweedFS screenshot upload and gallery, and
  `--profile skypilot` (service-token bootstrap, live reconcile creating workspaces, and a `sky launch --dryrun`
  through both launch-gate hops). Eight bugs were fixed on the way, among them: the SkyPilot container never
  started and instead printed every secret to its log; SkyPilot and oauth2-proxy were handed all of Krater's
  secrets; basic auth could never work and, once on, blocked member sign-in; the server-side launch-gate call
  pointed at the SkyPilot container itself; restarting the container wiped every project workspace; and the
  image baked in the host's `.venv`. Details in `docs/dev/staging.md`.
- **Not verified yet** (`docs/dev/staging.md` covers all of it):
  - a real Weave sign-in through the Dockerized portal (check Weave's discovery `issuer` matches
    `KRATER_WEAVE_ISSUER`, e.g. `host.docker.internal` vs `localhost`);
  - a full oauth2-proxy / `sky api login` round trip, and `host.docker.internal` from WSL2;
  - a real Vast launch, and billing drift;
  - spot machines and their recovery;
  - real Slack (including the invite changes in section 6).
- **SkyPilot contract CI.** `.github/workflows/skypilot-contract.yml` runs `scripts/dev/skypilot_contract.sh` (a real
  SkyPilot API server, a real Krater process, and a `sky launch --dryrun` walk through the launch gate) nightly, on
  pushes touching the SkyPilot integration or its pin, and manually (Actions, "SkyPilot contract", "Run workflow").
  The SkyPilot version is pinned only in `scripts/dev/skypilot-requirements.txt`: to upgrade, bump it and the
  `berkeleyskypilot/skypilot` tag in `docker-compose.yml` together (CI fails if they differ). Not run on GitHub yet;
  verified in a Linux container that mimics it. Running it with internet found and fixed two reconciler bugs:
  finished projects' workspaces were never deleted (SkyPilot's "No live services." `ClusterNotUpError` wasn't
  recognized), and one finished project whose workspace had vanished blocked every project's provisioning. The
  reconciler now isolates failures per project and records an already-vanished workspace as torn down.

## 5. Decisions and standing instructions (don't re-litigate)

- **PR #1 stays a draft until the maintainer explicitly approves marking it ready.**
- Don't push to branches other than `claude/exciting-sagan-7oh2zh` without permission.
- **Stack:** Python 3.12 / FastAPI / SQLAlchemy 2 / Alembic / Postgres / procrastinate (no Redis). See `CLAUDE.md`
  for conventions: services own the rules, money is integer cents, roles live in Krater's database.
- **Roles live in Krater, Weave is sign-in only** (maintainer decision, 2026-09-28; this **replaces** the earlier
  standing rule "roles come only from Weave groups"). Krater must work against Weave's `main` branch: scopes
  `openid profile email`, no `groups`/`slack_id`/`slack_membership` claims, no directory API, no service key.
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
1. Do the staging run (`docs/dev/staging.md`) against Weave `main` (or the `fix/security-hardening` branch), with your
   Weave sub in `KRATER_BOOTSTRAP_ADMINS`, and bring any failures back to a session to fix on the PR.
2. Open and merge a Weave PR from `fix/security-hardening` (section 3); Krater doesn't depend on it, but Weave needs it.
3. Create Krater's Slack app (`docs/dev/slack-setup.md`).
4. Pick a long-term screenshot storage provider.
5. Approve PR #1 out of draft when ready.

**A Claude session, in suggested order:**
1. ~~**Local dev setup script.**~~ Done: `scripts/dev/setup.ps1` and `scripts/dev/setup.sh` (section 2).
2. ~~**Weave follow-ups**~~ Superseded: Krater now runs against Weave `main` (section 3). Roles, disabling and Slack
   links moved into Krater's database (`krater/services/roles.py`, `/admin/users`); the Slack membership gate
   (`krater/services/slack_membership.py`) asks Slack directly (stored Slack id, else `users.lookupByEmail` with the
   verified email). Channel invites (`krater/services/slack_notify.py`) resolve people the same way and still invite
   with Slack's `force` flag: without it, Slack invited nobody whenever any one invitee failed, including people
   already in the channel. (Found from Slack's documented behavior; not yet seen against real Slack.)
3. ~~**A CI job for the SkyPilot contract test**~~ Done (section 4).
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
  Patch `0001` and the `0004` diff (groups, directory API, `slack_membership`) are no longer needed by Krater.
- **CI is Linux-only, so Windows breakage slips through.** `strftime("%-d")` is glibc-only and raises
  `ValueError: Invalid format string` on Windows (it broke 13 tests there); use `.day` instead. Shell scripts must
  stay LF (`.gitattributes` enforces it) or WSL bash rejects them.

## 8. Prompt to start a fresh local Claude Code session

Open a terminal in the cloned `Krater` folder, start Claude Code, and paste:

> You're continuing work on Krater (this repo, branch `claude/exciting-sagan-7oh2zh`, draft PR
> https://github.com/Adambomb210/Krater/pull/1). Read `docs/HANDOFF.md` first, then `CLAUDE.md` and `docs/SPEC.md`.
> Follow the standing instructions in HANDOFF section 5; in particular, never mark the PR ready without my explicit
> approval. First, get the test suite running locally (HANDOFF section 2) and tell me the result. Then propose what
> to work on from HANDOFF section 6 before starting.
