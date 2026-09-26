# SkyPilot contract check

A repeatable check that Krater's SkyPilot integration works against a **real SkyPilot 0.13.0 API
server** -- no Docker, no real Vast key, dry runs only. Read `docs/skypilot-integration.md` (the
design) and `docs/dev/skypilot-spike.md` (facts pinned down by hands-on testing, including section 7's
bugs this check exists to catch) first.

## What it proves

1. **The client contract** (`krater/skypilot/live.py`, `krater/services/skypilot_sync.py`): a private,
   Vast-only workspace is created with the right `allowed_users`; a team change updates it; `cost_report`,
   `list_clusters` and `list_managed_jobs` succeed and parse cleanly with no real clusters; completing or
   withdrawing a project tears the workspace down.
2. **The launch gate** (`krater/services/launch_policy.py`, `krater/web/routers/skypilot_policy.py`),
   exercised with the real `sky` CLI as a signed-in non-admin user: a dry-run launch targeting the
   project's workspace is allowed, with the forced autodown and capped `max_hourly_cost` visible in the
   mutated request; no workspace, the `default` workspace, an over-budget project, and a wrong policy
   token are all rejected or fail closed, with Krater's own message shown verbatim.

## Two pieces

- **`tests/live/test_skypilot_live.py`** (`@pytest.mark.live`, deselected by default): `LiveSkyPilotClient`'s
  full workspace lifecycle plus the reconciler's read calls, run against a real server -- the biggest
  bug surface (see `docs/dev/skypilot-spike.md` section 7: `StatusBody.refresh`'s enum, `/jobs/queue`'s
  `ClusterNotUpError`-as-500). Plus three fail-closed checks against a *really-running* Krater process
  (not the in-process `TestClient`), since those need a real HTTP round trip: no workspace, `default`
  workspace, and a wrong policy token. Skips cleanly (each test's `reason` says which env vars to set)
  when nothing is running -- `SKYPILOT_LIVE_API_URL`/`SKYPILOT_LIVE_SERVICE_TOKEN` for the client tests,
  `SKYPILOT_LIVE_KRATER_BASE_URL`/`SKYPILOT_LIVE_POLICY_TOKEN` for the process ones.
- **`scripts/dev/skypilot_contract.sh`**: starts a real SkyPilot API server and a real Krater process,
  wires them together, runs the pytest file above, then (unless `--skip-launch-gate`) walks through
  every launch-gate scenario with the real `sky` CLI, as a real signed-in non-admin service-account
  user. Stops both servers on exit, success or failure.

## Running it

```bash
export SKYPILOT_VENV=/path/to/a/venv/with/skypilot[vast]==0.13.0
export KRATER_DATABASE_URL=postgresql+psycopg://root:root@localhost:5432/krater_dev   # must exist
scripts/dev/skypilot_contract.sh
```

`SKYPILOT_VENV` is deliberately not `uv sync`'d into Krater's own venv: `skypilot[vast]` is ~450MB and
pulls in a second SQLAlchemy, two Postgres drivers, pandas, and more (`docs/dev/skypilot-spike.md`
section 6) -- it's a separate tool the script shells out to, not a Krater dependency. Build it once
with `uv venv "$SKYPILOT_VENV" && uv pip install --python "$SKYPILOT_VENV/bin/python" 'skypilot[vast]==0.13.0'`.

The script is otherwise self-contained: it picks a fresh temp `WORKDIR` (isolated `HOME`/`~/.sky`
config for both an "admin" and a "member" persona, so nothing touches your real `~/.sky`), generates a
random policy token, mints both service-account tokens, runs migrations, and cleans up its own
processes on exit (`KEEP_WORKDIR=1` keeps the temp dir and logs for debugging a failure).

## Two bootstrapping tricks worth knowing about

Both are dead ends without them, and neither is documented by SkyPilot itself -- see
`docs/dev/skypilot-spike.md` Surprises #5, #6 and #8 for the underlying facts.

1. **Minting the first service-account token needs an admin user, which needs a service-account token.**
   Broken by two unauthenticated-loopback quirks: `POST /users/create` has no auth check in its handler
   at all, and `BasicAuthMiddleware` bypasses itself entirely for loopback peers -- so a plain `curl` from
   `127.0.0.1` with no credentials can create a user with `role: admin` outright. `POST
   /users/service-account-tokens` *does* require a real authenticated caller even from loopback, so that
   one call has to go out over the host's own non-loopback address instead (`hostname -I`), using the
   admin user's Basic Auth credentials. The resulting token is still seeded with `rbac.default_role`
   (here, `user`) regardless of who created it -- promoting it to `admin` is one more unauthenticated
   loopback call, to `POST /users/update`.
2. **The "signed-in non-admin member" is a second service-account token, not a real SSO login.** This
   environment has neither Docker nor a running oauth2-proxy, so there's no way to do the real Weave-SSO
   flow `docs/skypilot-integration.md` section 0 describes. A service-account token minted with the
   default (`user`) role is a legitimate, distinct, non-admin SkyPilot identity, which is what the launch
   gate actually cares about -- but it isn't an email, so it can't appear in Krater's own
   `allowed_users` (built entirely from Weave emails, `krater/services/skypilot_sync.py`'s
   `_team_emails`). The script grants this one test identity access to the demo project's workspace with
   one extra, out-of-band `workspaces/batch_add_users` call (by the SA's internal id, not through
   Krater), so the `sky` CLI can actually target it -- Krater's own provisioning contract is verified
   separately, by inspecting `allowed_users` after a real `sync_workspaces` call, without needing that
   grant at all.

## Bugs this run found and fixed (in `krater/skypilot/live.py`)

See `docs/dev/skypilot-spike.md` section 7 for the full detail on each. Summary: `list_clusters` sent
`refresh: false` where the real server wants the string `"NONE"`; `list_managed_jobs`/
`cancel_managed_jobs` raised on a workspace that never had a managed job (`ClusterNotUpError`, delivered
as an HTTP 500 with the usual poll-status dict nested under `detail`) instead of treating it as empty;
and `delete_workspace` wasn't actually idempotent against a real server despite the `SkyPilotClient`
protocol promising callers it would be. All three now have regression tests against a fake server in
`tests/skypilot/test_live_client.py` and are exercised against the real thing here.
