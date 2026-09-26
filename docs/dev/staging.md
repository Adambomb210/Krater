# Staging runbook (maintainer's Windows machine)

A from-scratch setup for running the full stack -- Krater, a local Weave, SkyPilot, oauth2-proxy -- on one Windows
machine, plus a test script that exercises budget enforcement end to end against a real (tiny) Vast rental. Read
`docs/skypilot-integration.md` and `docs/dev/skypilot-spike.md` first; this doc assumes their design and facts.

**Where things run:** Docker Desktop (with the WSL2 backend) hosts every container. The `sky` CLI, and anything that
needs to reach containers by `localhost`, runs **inside WSL2**, not in PowerShell -- SkyPilot's CLI is Linux/macOS-only
upstream, and Docker Desktop's WSL2 integration makes `localhost` resolve the same way from both sides anyway, so
there's no reason to fight it from Windows directly. Commands below are labelled **PowerShell** or **WSL2 (bash)**.

## 1. Prerequisites

**PowerShell** (one-time):

```powershell
# Docker Desktop, with WSL2 as its backend (Settings > General > "Use the WSL 2 based engine").
winget install Docker.DockerDesktop
wsl --install -d Ubuntu
```

Then, in Docker Desktop's Settings > Resources > WSL Integration, enable integration for your Ubuntu distro.

**WSL2 (bash)** (one-time):

```bash
# Python via uv (Krater's own toolchain) and the sky CLI, in their own venvs so nothing collides.
curl -LsSf https://astral.sh/uv/install.sh | sh
pipx install "skypilot[vast]"   # or: uv tool install "skypilot[vast]"
sky --version                    # confirm it matches the pinned image tag in docker-compose.yml (0.13.0)

git clone https://github.com/<your-fork-or-org>/Krater.git
cd Krater
```

## 2. A local Weave

Weave provides sign-in for both Krater and the SkyPilot proxy. Run the `Krater-Integration` branch (see
`docs/weave-integration.md`) -- it's the one with the `groups` claim and the directory API Krater depends on.

**WSL2 (bash):**

```bash
git clone https://github.com/patchworklabsorg/weave.git ~/weave
cd ~/weave
git checkout Krater-Integration
bin/setup            # installs gems, prepares the dev database, etc. -- see Weave's own README for prerequisites
bin/rails server -p 3000
```

Leave that running (or use `bin/dev` if you also want Weave's asset watchers). Weave is now at
`http://localhost:3000`.

### Register two OAuth apps

At `http://localhost:3000/admin/oauth_applications`, create two **confidential** applications (see
`docs/weave-integration.md` "Registration" for the general shape):

1. **Krater** itself:
   - Redirect URI: `http://localhost:8000/auth/callback`
   - Scopes: `openid profile email groups`
   - Note the client id/secret for `KRATER_WEAVE_CLIENT_ID`/`KRATER_WEAVE_CLIENT_SECRET`.

2. **SkyPilot proxy** (a *separate* app -- never reuse Krater's own credentials here, per
   `docs/skypilot-integration.md` section 0):
   - Redirect URI: `http://localhost:8080/oauth2/callback` (oauth2-proxy's default callback path, on the port
     `auth-proxy` publishes -- see `.env`'s `SKYPILOT_AUTH_PROXY_PORT`).
   - Scopes: `openid profile email groups`
   - Note the client id/secret for `KRATER_SKYPILOT_AUTH_CLIENT_ID`/`KRATER_SKYPILOT_AUTH_CLIENT_SECRET`.

### Issue a service key

Also at `/admin/oauth_applications` (or wherever Weave's `Krater-Integration` branch puts service-key management --
check that branch's `docs/OAUTH.md` if the path has moved), issue a service key scoped to **`directory:read`** only.
This is `KRATER_WEAVE_SERVICE_KEY`, used for Krater's directory lookups (`get_user`, group membership) -- it should
never be able to act as a user, only read the directory.

Give your own Weave user the `ganymede:member` group (and `ganymede:admin` if you want to approve your own test
project) through whatever admin UI Weave's `Krater-Integration` branch adds for group management.

## 3. A separate, small-credit Vast.ai account

**Use a throwaway or clearly-separated Vast account, never the production one.** Create it at vast.ai, add the
smallest amount of credit that lets you launch anything (a few dollars covers a `datacenter_only` CPU instance for a
short test), and generate an API key under Account > API Keys.

```bash
mkdir -p ~/.config/vastai
echo "<your-test-account-api-key>" > ~/.config/vastai/vast_api_key
chmod 600 ~/.config/vastai/vast_api_key
```

You'll point `KRATER_SKYPILOT_VAST_KEY_FILE` at this file's path (from WSL2's filesystem, e.g.
`/home/<you>/.config/vastai/vast_api_key` -- Docker Desktop's WSL2 integration mounts this fine as a bind mount as
long as `docker compose` itself is also run from within WSL2).

## 4. `.env`

**WSL2 (bash)**, from the repo root:

```bash
cp .env.example .env
```

Then edit `.env` (see the comments in `.env.example` for what each does):

```ini
KRATER_WEAVE_MODE=live
KRATER_WEAVE_ISSUER=http://host.docker.internal:3000
KRATER_WEAVE_CLIENT_ID=<from step 2>
KRATER_WEAVE_CLIENT_SECRET=<from step 2>
KRATER_WEAVE_API_BASE_URL=http://host.docker.internal:3000
KRATER_WEAVE_SERVICE_KEY=<from step 2>

KRATER_SKYPILOT_MODE=live
KRATER_PUBLIC_URL=http://localhost:8000
KRATER_SKYPILOT_POLICY_TOKEN=<openssl rand -hex 32>
KRATER_SKYPILOT_BASIC_AUTH_USER=admin
KRATER_SKYPILOT_BASIC_AUTH_PASSWORD=<a real password -- used once, in step 5>
KRATER_SKYPILOT_VAST_KEY_FILE=/home/<you>/.config/vastai/vast_api_key

KRATER_SKYPILOT_AUTH_CLIENT_ID=<from step 2>
KRATER_SKYPILOT_AUTH_CLIENT_SECRET=<from step 2>
KRATER_SKYPILOT_AUTH_COOKIE_SECRET=<python -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())">
KRATER_SKYPILOT_PUBLIC_URL=http://localhost:46580
KRATER_SKYPILOT_AUTH_COOKIE_SECURE=false   # plain http on localhost; set true (and use https) for anything else

# Tiny, deliberately conservative for a test run -- see "Safety notes" below.
KRATER_SKYPILOT_AUTODOWN_IDLE_MINUTES=5
KRATER_SKYPILOT_MAX_HOURLY_COST_CENTS=50
```

`host.docker.internal` is how containers reach Weave running directly on the WSL2/Windows host; Docker Desktop wires
this up automatically. `KRATER_WEAVE_SERVICE_KEY` must be issued with `directory:read` (step 2).

## 5. Bring the stack up

**WSL2 (bash):**

```bash
docker compose --profile skypilot up --build
```

This starts everything, including `skypilot` and `auth-proxy` (the `skypilot` profile), on top of the usual
`db`/`migrate`/`portal`/`worker`/`storage` services.

### Bootstrap the SkyPilot service-account token

One-time, once `skypilot` is up (needs `ENABLE_BASIC_AUTH` + `ENABLE_SERVICE_ACCOUNTS`, both already set by
`docker-compose.yml` -- see `docs/dev/skypilot-spike.md` section 3 for why both are required, and its Surprise #8 for
why this call must come from *outside* the container, not `docker compose exec`, since loopback requests bypass
Basic Auth):

```bash
curl -u "$KRATER_SKYPILOT_BASIC_AUTH_USER:$KRATER_SKYPILOT_BASIC_AUTH_PASSWORD" \
  -X POST http://localhost:46580/users/service-account-tokens \
  -H 'Content-Type: application/json' \
  -d '{"token_name": "krater-admin"}'
```

Copy the returned `token` (starts `sky_...`) into `.env`'s `KRATER_SKYPILOT_SERVICE_TOKEN`, then
`docker compose --profile skypilot up -d portal worker` to pick it up.

## 6. Test script

1. **Create a project.** Sign in to Krater at `http://localhost:8000` with your Weave account, submit a small
   proposal.
2. **Approve it** (as an admin -- via `/admin` or the review flow). This should provision a private SkyPilot
   workspace named `ganymede-<project id>` and save it on the project.
3. **Confirm the workspace appears:**
   ```bash
   curl -H "Authorization: Bearer $KRATER_SKYPILOT_SERVICE_TOKEN" \
     -X POST http://localhost:46580/workspaces -d '{}' -H 'Content-Type: application/json'
   # poll GET /api/get?request_id=... per docs/dev/skypilot-spike.md section 2 -- the workspace and
   # your project's Weave email should be in the returned mapping.
   ```
4. **Sign in the `sky` CLI as a member:**
   ```bash
   sky api login -e http://localhost:8080     # the auth-proxy port, not 46580 directly
   ```
   This opens a browser to Weave sign-in (via oauth2-proxy). Use the same Weave account as your project's submitter.
5. **Launch a tiny job** targeting your project's workspace explicitly (required -- see
   `docs/skypilot-integration.md` section 2 for what happens if you don't):
   ```bash
   sky launch -w ganymede-<project-id> -y -c staging-test --cpus 1 --infra vast \
     --down --idle-minutes-to-autostop 5 \
     -- "echo hello from staging && sleep 60"
   ```
   Add `resources.vast.datacenter_only: true` in the task YAML (or `--vast-datacenter-only` if the CLI flag exists in
   your installed version) for a more reliable host, per `docs/skypilot-integration.md`'s Vast.ai caveats.
6. **Watch spend accrue.** The reconciler runs every `KRATER_SKYPILOT_RECONCILE_INTERVAL_MINUTES` (default 5) and
   writes a `SpendSnapshot`; the project page should show it climbing.
7. **Confirm the 80% warning** posts (Slack, or wherever the reconciler notifies in your build) once estimated spend
   crosses `KRATER_SKYPILOT_BUDGET_WARN_PERCENT` (default 80) of the ceiling.
8. **Confirm 100% teardown:** once spend reaches the ceiling, the reconciler should tear down the workspace's
   clusters/managed jobs, and a follow-up `sky launch -w ganymede-<project-id> ...` should now be **rejected** by the
   policy endpoint with a clear "budget exhausted" message (test this directly too: it's the fastest way to confirm
   `krater/services/launch_policy.py`'s reject path against a real `sky launch`, not just its unit tests).

## 7. Comparing spend against Vast billing

SkyPilot's `cost_report` is a **catalog-price × uptime estimate**, not a bill (see
`docs/skypilot-integration.md`'s "What SkyPilot does and doesn't provide" table). After a test run:

1. Note the `SpendSnapshot.estimated_spend_cents` Krater recorded for the project.
2. Check the actual charge in your Vast account's billing/instance history for the same time window.
3. The two won't match exactly -- SkyPilot prices from a cached catalog (`vast/vms.csv`) while the real rental comes
   from live `search_offers`, and Vast can add disk charges SkyPilot doesn't price in. Note the delta as a percentage;
   a handful of real runs is what `docs/skypilot-integration.md`'s open item (drift/safety-margin) needs before
   picking a margin to build into ceilings.

## Safety notes

- **Tiny ceilings.** `KRATER_SKYPILOT_MAX_HOURLY_COST_CENTS` and every test project's budget should be small enough
  that a mistake costs cents, not dollars -- this is what step 5's `50` cents/hour is for.
- **`datacenter_only`.** Prefer it for reliability (per the Vast.ai caveats above); it also tends to avoid the
  cheapest, least predictable consumer-grade offers.
- **Autodown, always.** Never launch without `--down`/`idle_minutes_to_autostop` here -- the whole point of this
  environment is to test that Krater's policy *forces* this even if you forget, but don't rely on that while you're
  still bringing the stack up for the first time.
- **Never the production Vast key or a real Weave instance.** This runbook's entire point is a disposable, low-stakes
  sandbox; keep it that way. Tear the stack down (`docker compose --profile skypilot down -v`) when you're done
  testing, and rotate/delete the test Vast key afterward if you're not going to reuse this setup.
