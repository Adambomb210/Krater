# Running the live Weave e2e check

This proves Krater's `KRATER_WEAVE_MODE=live` path (OIDC sign-in, the directory API) against a **real**
running Weave, not the stub. It needs a Weave checkout (`patchworklabsorg/weave`) alongside this repo,
with Ruby/Rails runnable and its dev Postgres database migrated.

See `docs/weave-integration.md` for the contract this exercises, and
`tests/live/test_weave_live.py` for the check itself.

## 1. Provision Weave

```bash
uv run python scripts/dev/weave_e2e_setup.py --weave-dir ../weave
```

This runs `scripts/dev/weave_e2e_provision.rb` inside the Weave checkout (via `bin/rails runner`) to
idempotently create, in Weave's own database:

- four users: `e2e-member@ganymede.test` (`ganymede:member`), `e2e-reviewer@ganymede.test`
  (`ganymede:member` + `ganymede:reviewer`, with a `slack_id` set), `e2e-admin@ganymede.test`
  (`ganymede:member` + `ganymede:admin`), and `e2e-nonmember@ganymede.test` (no groups);
- a confidential OAuth application ("Krater (e2e)") with redirect URI
  `http://localhost:8201/auth/callback` and scopes `openid profile email groups slack` (**recreated**
  every run, since its secret is hashed at rest and only readable right after creation);
- a Service ("Krater (e2e)") with a `directory:read` Service::Key (likewise recreated every run).

It then writes two **gitignored** files in this repo's root:

- `.weave_e2e_fixture.json` -- everything `tests/live/test_weave_live.py` reads: the OAuth client
  id/secret, the service API key, and each user's email/`sub`/`slack_id`.
- `.env.weave-e2e` -- the `KRATER_WEAVE_*` settings pointing at that application/key, ready to `source`.

Re-run this script whenever you need fresh users/credentials, or after restarting from a clean Weave
database. It's idempotent for the users and groups; the OAuth app and service key are always rotated.

Options: `--weave-dir` (default `../weave`), `--krater-base-url` (default `http://localhost:8201`),
`--ruby-shims` (default `/opt/rbenv/shims`, prepended to `PATH` so `bundle`/`rails` resolve).

## 2. Run Weave

```bash
cd ../weave
bin/rails db:migrate   # if you haven't already
bin/rails tailwindcss:build
bin/rails server -p 3000 -b 0.0.0.0
```

Weave needs an OIDC signing key and a Lockbox master key in **encrypted credentials**
(`config/credentials/development.yml.enc` + `.key`), which have no `ENV` fallback outside test. If your
checkout doesn't have real ones (e.g. a fresh container with no master key), generate dev-only ones with
`bin/rails runner` and `ActiveSupport::EncryptedConfiguration` -- see that file's own comments in
`config/initializers/doorkeeper_openid_connect.rb` and `config/initializers/lockbox.rb`. **Never commit
a generated key or the resulting `.yml.enc` you can't otherwise reproduce.**

Sign-in is by magic link. Rather than running Weave's mailer/Solid Queue worker just to read an email
back out of `letter_opener`, both `scripts/dev/weave_e2e_setup.py`'s fixture and
`tests/live/test_weave_live.py` mint a fresh, valid `User::MagicLink` token directly (`bin/rails runner
'User::MagicLink.issue!(user).token'`) and drive Weave's real confirmation endpoint
(`GET`/`POST /auth/magic_link/:token`) with it -- functionally identical to clicking the emailed link,
without needing the queue worker up. This is why the live pytest suite needs `WEAVE_REPO_DIR` (a Weave
checkout with `bundle`/`rails` runnable) for its sign-in tests specifically.

## 3. Run Krater in live mode

Do **not** copy `.env.weave-e2e`'s contents into this repo's `.env` -- pydantic-settings auto-loads
`.env`, so a live `KRATER_WEAVE_MODE` there would silently leak into `uv run pytest` too. Source it
directly instead:

```bash
set -a; source .env.weave-e2e; set +a
export KRATER_DATABASE_URL=postgresql+psycopg://root:root@localhost:5432/krater_e2e_weave
export KRATER_SECRET_KEY=some-dev-secret
uv run alembic upgrade head
uv run uvicorn krater.web.app:create_app --factory --port 8201
```

At this point you can sign in at `http://localhost:8201/login` as any of the four fixture users (there's
no password -- mint a magic link as above, or add a `/auth/stub`-style shortcut of your own for manual
poking) and drive the same flow this doc's automated check does.

## 4. Run the check

```bash
export KRATER_LIVE_BASE_URL=http://localhost:8201            # default; only needed if you changed the port
export KRATER_LIVE_DATABASE_URL=$KRATER_DATABASE_URL          # so the test can read the users table
export WEAVE_REPO_DIR=../weave                                 # default; needed for the sign-in tests
export WEAVE_DATABASE_URL=postgresql://root:root@localhost/patchwork-idp_development  # needed for the group-removal test
uv run pytest -m live
```

Each variable's test(s) skip individually, with a clear reason, if it's unset -- `WEAVE_E2E_FIXTURE` (or
the default `.weave_e2e_fixture.json`) is the only one the whole module needs; without it the module
skips entirely.

Magic-link tokens are single-use and expire in 15 minutes, so re-run step 1 before re-running the sign-in
tests if you've already consumed that run's tokens.

### What "authorization uses fresh data" means here

`LiveWeaveClient` caches directory responses (`get_user`, `get_user_by_slack_id`) for ~60 seconds per
process, to absorb bursts of lookups (docs/weave-integration.md). That means:

- **In a running Krater**, removing a group in Weave takes up to 60 seconds (the cache TTL) to affect
  Krater's next `fresh_actor`-gated action -- or takes effect immediately if you restart Krater (a fresh
  process has an empty cache). Both are legitimate ways to observe it; restarting is far faster than
  waiting out real wall-clock time.
- **`test_group_removal_is_reflected_by_a_fresh_client`** takes the "fresh process" route without
  restarting anything: it edits Weave's `group_memberships` table directly (needs `WEAVE_DATABASE_URL`)
  and asserts against a brand-new `LiveWeaveClient` instance, which has never cached that user and so
  reflects the removal immediately -- exactly what Krater's own cache would show once it expires or the
  process restarts.

## What this doesn't cover

This file's automated check drives the OAuth/PKCE/magic-link dance with `httpx`, not a browser. The
manual pass this was built from used a real Chromium (Playwright) and caught two real, browser-only
Weave bugs that a plain HTTP client can't see:

1. The magic-link confirmation form (`app/views/auth/magic_link_login.html.erb`) submitted over Turbo
   (`fetch`). When confirming resumes an OAuth flow that's already authorized, that fetch gets redirected
   straight through to the client's (cross-origin) `redirect_uri`, which Weave's CSP `connect-src`
   correctly blocks for `fetch` -- silently breaking sign-in for any returning user in a real browser.
   Fixed by adding `turbo: false`, matching the (already `turbo: false`) `/oauth/authorize` forms.
2. Weave's CSP `form-action 'self'` is enforced by Chromium against every redirect a form submission
   leads to, not just its immediate target. Signing in while an OAuth authorization is pending ends in a
   redirect to the client's (cross-origin) `redirect_uri` once consent already exists, so the sign-in forms
   were blocked. Fixed narrowly: while an authorization is pending, Weave's sign-in pages add **only that
   client's registered redirect origins** to `form-action` (the consent screen already did this). An
   earlier fix that allowed all `https:` origins on every page was replaced, since it weakened the login
   form's protection.

Both are fixed on the Weave branch used for this check. If you're re-running this against a Weave
checkout that predates that fix, sign-in will appear to hang or silently fail in a real browser (it still
works via `httpx`, which doesn't enforce CSP) -- check the browser console for CSP violation messages.
