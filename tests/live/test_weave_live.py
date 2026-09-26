"""Live Krater <-> Weave integration check.

Unlike the rest of the suite, this drives a **real** Weave (OIDC discovery/JWKS, the magic-link sign-in
flow, the `/oauth/authorize` consent screen, and the `/api/v1/users` directory API) and a **real**
running Krater in `KRATER_WEAVE_MODE=live`, over plain HTTP -- no mocks. See `docs/dev/weave-e2e.md` for
how to bring both up and provision the fixture users this file reads.

It does the same OAuth Authorization Code + PKCE round trip a browser does (confirm a magic link, submit
the consent form, land back on Krater's `/auth/callback`), but drives it directly with `httpx` rather
than a browser: same redirects, same cookies, same real signed id_token, without a browser dependency in
the Python test suite. The interactive proof with an actual browser (Playwright/Chromium) that this asset
is derived from is described in that doc, alongside the exact bugs it caught that `httpx` alone could not
(Turbo intercepting the sign-in form, and `form-action` CSP blocking the OAuth redirect) -- both are
Weave view/CSP issues invisible to a plain HTTP client, which is why that manual pass still matters even
with this file in place.

Every test here is marked `live` (deselected by default -- see `pyproject.toml`) and skips, individually
or at module scope, with a clear reason when what it needs isn't configured. Nothing here is required for
`uv run pytest`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from krater.config import Settings
from krater.weave.live import LiveWeaveClient

pytestmark = pytest.mark.live

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _env_path(name: str, default: Path | None = None) -> Path | None:
    raw = os.environ.get(name)
    if raw:
        return Path(raw)
    return default


FIXTURE_PATH = _env_path("WEAVE_E2E_FIXTURE", REPO_ROOT / ".weave_e2e_fixture.json")
KRATER_BASE_URL = os.environ.get("KRATER_LIVE_BASE_URL", "http://localhost:8201")
KRATER_DATABASE_URL = os.environ.get("KRATER_LIVE_DATABASE_URL") or os.environ.get("KRATER_DATABASE_URL")
WEAVE_REPO_DIR = _env_path("WEAVE_REPO_DIR", REPO_ROOT.parent / "weave")
WEAVE_DATABASE_URL = os.environ.get("WEAVE_DATABASE_URL")
RBENV_SHIMS_DIR = os.environ.get("RBENV_SHIMS_DIR", "/opt/rbenv/shims")


def _load_fixture() -> dict[str, Any] | None:
    if FIXTURE_PATH is None or not FIXTURE_PATH.exists():
        return None
    return json.loads(FIXTURE_PATH.read_text())


_FIXTURE = _load_fixture()

if _FIXTURE is None:
    pytest.skip(
        "no live-Weave fixture found (set WEAVE_E2E_FIXTURE, or run "
        "`uv run python scripts/dev/weave_e2e_setup.py` first -- see docs/dev/weave-e2e.md)",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def fixture() -> dict[str, Any]:
    assert _FIXTURE is not None  # module-level skip above guarantees this
    return _FIXTURE


@pytest.fixture(scope="module")
def weave_settings(fixture: dict[str, Any]) -> Settings:
    return Settings(
        weave_mode="live",
        weave_issuer=fixture["issuer"],
        weave_client_id=fixture["oauth_client_id"],
        weave_client_secret=fixture["oauth_client_secret"],
        weave_api_base_url=fixture["issuer"],
        weave_service_key=fixture["service_key"],
    )


@pytest.fixture
def weave_client(weave_settings: Settings) -> Iterator[LiveWeaveClient]:
    client = LiveWeaveClient(weave_settings)
    yield client


def _require_krater_db() -> str:
    if not KRATER_DATABASE_URL:
        pytest.skip("KRATER_LIVE_DATABASE_URL (or KRATER_DATABASE_URL) is not set")
    return KRATER_DATABASE_URL


def _krater_user_row(email: str) -> dict[str, Any] | None:
    dsn = _require_krater_db().replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "select weave_sub, display_name, groups_cached, slack_user_id from users where email = %s",
            (email,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "weave_sub": row[0],
            "display_name": row[1],
            "groups_cached": row[2],
            "slack_user_id": row[3],
        }


# --------------------------------------------------------------------------------------------------
# Directory API, via a real LiveWeaveClient against the real running Weave.
# --------------------------------------------------------------------------------------------------


def test_discovery_and_jwks_are_reachable(fixture: dict[str, Any]) -> None:
    resp = httpx.get(f"{fixture['issuer']}/.well-known/openid-configuration", timeout=10)
    resp.raise_for_status()
    doc = resp.json()
    assert doc["issuer"] == fixture["issuer"]
    assert "RS256" in doc["id_token_signing_alg_values_supported"]

    jwks = httpx.get(doc["jwks_uri"], timeout=10)
    jwks.raise_for_status()
    assert jwks.json()["keys"]


def test_get_user_matches_fixture_for_each_role(fixture: dict[str, Any], weave_client: LiveWeaveClient) -> None:
    member = weave_client.get_user(fixture["users"]["member"]["sub"])
    assert member is not None
    assert member.active
    assert member.groups == frozenset({"ganymede:member"})

    reviewer = weave_client.get_user(fixture["users"]["reviewer"]["sub"])
    assert reviewer is not None
    assert "ganymede:reviewer" in reviewer.groups
    assert reviewer.slack_id == fixture["users"]["reviewer"]["slack_id"]

    admin = weave_client.get_user(fixture["users"]["admin"]["sub"])
    assert admin is not None
    assert "ganymede:admin" in admin.groups

    non_member = weave_client.get_user(fixture["users"]["non_member"]["sub"])
    assert non_member is not None
    assert "ganymede:member" not in non_member.groups


def test_get_user_unknown_sub_returns_none(weave_client: LiveWeaveClient) -> None:
    assert weave_client.get_user("PWL0000000000000-does-not-exist") is None


def test_list_users_in_group_reviewer(fixture: dict[str, Any], weave_client: LiveWeaveClient) -> None:
    reviewers = weave_client.list_users_in_group("ganymede:reviewer")
    subs = {u.sub for u in reviewers}
    assert fixture["users"]["reviewer"]["sub"] in subs
    assert fixture["users"]["member"]["sub"] not in subs


def test_get_user_by_slack_id(fixture: dict[str, Any], weave_client: LiveWeaveClient) -> None:
    user = weave_client.get_user_by_slack_id(fixture["users"]["reviewer"]["slack_id"])
    assert user is not None
    assert user.sub == fixture["users"]["reviewer"]["sub"]


# --------------------------------------------------------------------------------------------------
# The full OAuth Authorization Code + PKCE round trip, against real running Krater + Weave.
# --------------------------------------------------------------------------------------------------


def _issue_magic_link(email: str) -> str:
    if WEAVE_REPO_DIR is None or not (WEAVE_REPO_DIR / "bin" / "rails").exists():
        pytest.skip(f"WEAVE_REPO_DIR ({WEAVE_REPO_DIR}) is not a Weave checkout -- can't mint a magic link")

    env = dict(os.environ)
    env["PATH"] = f"{RBENV_SHIMS_DIR}:{env.get('PATH', '')}"
    env.setdefault("DATABASE_URL", "postgres://root:root@localhost")
    env["RAILS_ENV"] = env.get("RAILS_ENV", "development")
    ruby = f'u = User.find_by!(email: {email!r}); puts User::MagicLink.issue!(u, requested_ip: "127.0.0.1").token'
    result = subprocess.run(
        ["bundle", "exec", "rails", "runner", ruby],
        cwd=WEAVE_REPO_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(f"could not mint a magic link for {email}:\n{result.stderr}")
    return result.stdout.strip().splitlines()[-1]


_AUTHENTICITY_TOKEN_RE = re.compile(r'name="authenticity_token"\s+value="([^"]*)"')
_HIDDEN_FIELD_RE = re.compile(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"')


def _authorize_form_fields(html: str) -> dict[str, str]:
    """The Doorkeeper consent screen's *first* form (POST -- "Authorize"; the second is the DELETE
    "Deny" form). Both share hidden field names, so this has to stop at the first `</form>`."""
    form_html = html.split("<form", 2)[1]
    form_html = form_html.split("</form>", 1)[0]
    fields = dict(_HIDDEN_FIELD_RE.findall(form_html))
    token_match = _AUTHENTICITY_TOKEN_RE.search(form_html)
    if token_match:
        fields["authenticity_token"] = token_match.group(1)
    fields["commit"] = "Authorize"
    return fields


@dataclass
class SignInResult:
    final_response: httpx.Response


def _sign_in(client: httpx.Client, fixture: dict[str, Any], email: str) -> SignInResult:
    """Drives the real Authorization Code + PKCE flow: Krater `/login` -> Weave's magic-link
    confirmation (using a freshly minted token in place of "the user clicked the emailed link") ->
    the OAuth consent screen (submitted for real, when Weave shows one) -> back to Krater's
    `/auth/callback`. Returns the final response Krater gave (a redirect on success, a 403 page for
    a non-member).
    """
    weave_base = fixture["issuer"]

    resp = client.get(f"{KRATER_BASE_URL}/login")
    assert resp.status_code == 302, f"Krater /login didn't redirect into Weave: {resp.status_code}"
    authorize_url = resp.headers["location"]
    assert authorize_url.startswith(weave_base), authorize_url

    # This GET is what puts client_id / the return-to authorize URL into Weave's own session
    # (AuthController#oauth_login) -- without it, confirming the magic link lands on Weave's
    # dashboard instead of resuming the OAuth flow.
    resp = client.get(authorize_url)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith(f"{weave_base}/oauth/login")

    token = _issue_magic_link(email)
    resp = client.get(f"{weave_base}/auth/magic_link/{token}")
    assert resp.status_code == 200, "magic link wasn't live (already used, expired, or unknown)"
    csrf = _AUTHENTICITY_TOKEN_RE.search(resp.text)
    assert csrf, "no CSRF token on the magic-link confirmation page"

    resp = client.post(
        f"{weave_base}/auth/magic_link/{token}",
        data={"authenticity_token": csrf.group(1)},
    )
    assert resp.status_code == 302, f"magic-link confirmation didn't redirect: {resp.status_code}"
    next_url = resp.headers["location"]

    if next_url.startswith(f"{weave_base}/oauth/authorize"):
        resp = client.get(next_url)
        if resp.status_code == 200:
            # First-time consent: submit the real "Authorize" form.
            fields = _authorize_form_fields(resp.text)
            resp = client.post(f"{weave_base}/oauth/authorize", data=fields)
        assert resp.status_code == 302, f"OAuth authorize didn't redirect: {resp.status_code} {resp.text[:300]}"
        next_url = resp.headers["location"]

    assert next_url.startswith(f"{KRATER_BASE_URL}/auth/callback"), next_url
    final = client.get(next_url)
    return SignInResult(final_response=final)


@pytest.fixture
def http_client() -> Iterator[httpx.Client]:
    with httpx.Client(follow_redirects=False, timeout=15) as client:
        yield client


def test_full_oidc_signin_persists_member(fixture: dict[str, Any], http_client: httpx.Client) -> None:
    _require_krater_db()
    email = fixture["users"]["member"]["email"]
    result = _sign_in(http_client, fixture, email)
    assert result.final_response.status_code == 302, "sign-in should end with Krater redirecting home"

    row = _krater_user_row(email)
    assert row is not None, "Krater never created/updated the user row"
    assert row["weave_sub"] == fixture["users"]["member"]["sub"]
    assert "ganymede:member" in row["groups_cached"]


def test_full_oidc_signin_persists_reviewer_slack_id(fixture: dict[str, Any], http_client: httpx.Client) -> None:
    _require_krater_db()
    email = fixture["users"]["reviewer"]["email"]
    result = _sign_in(http_client, fixture, email)
    assert result.final_response.status_code == 302

    row = _krater_user_row(email)
    assert row is not None
    assert row["slack_user_id"] == fixture["users"]["reviewer"]["slack_id"]
    assert "ganymede:reviewer" in row["groups_cached"]


def test_oidc_signin_rejects_non_member(fixture: dict[str, Any], http_client: httpx.Client) -> None:
    email = fixture["users"]["non_member"]["email"]
    result = _sign_in(http_client, fixture, email)
    assert result.final_response.status_code == 403


# --------------------------------------------------------------------------------------------------
# Group removal takes effect without a new sign-in (docs/weave-integration.md: "Authorization uses
# fresh data"). LiveWeaveClient caches directory responses for ~60s per docs/dev/weave-e2e.md to
# absorb bursts of lookups; rather than sleeping past that TTL, this constructs its own fresh
# `LiveWeaveClient` (an empty cache, same as Krater's process would have after the TTL, or after a
# restart) and asserts *that* reflects the change immediately, straight from Weave's Postgres.
# --------------------------------------------------------------------------------------------------


def test_group_removal_is_reflected_by_a_fresh_client(fixture: dict[str, Any], weave_settings: Settings) -> None:
    if not WEAVE_DATABASE_URL:
        pytest.skip("WEAVE_DATABASE_URL is not set -- can't manipulate Weave's group_memberships directly")

    reviewer_sub = fixture["users"]["reviewer"]["sub"]
    with psycopg.connect(WEAVE_DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("select id from users where p_id = %s", (reviewer_sub,))
        (user_id,) = cur.fetchone()
        cur.execute("select id from groups where name = 'ganymede:reviewer'")
        (group_id,) = cur.fetchone()

        cur.execute(
            "delete from group_memberships where user_id = %s and group_id = %s returning id",
            (user_id, group_id),
        )
        deleted = cur.fetchall()
        conn.commit()

        try:
            fresh_client = LiveWeaveClient(weave_settings)  # empty directory cache
            user = fresh_client.get_user(reviewer_sub)
            assert user is not None
            assert "ganymede:reviewer" not in user.groups, "fresh lookup still shows the removed group"
        finally:
            # Restore it (granted_by NULL is fine for this fixture) so re-running the suite, or the
            # interactive proof, without re-provisioning still has a working reviewer.
            if deleted:
                cur.execute(
                    "insert into group_memberships (user_id, group_id, created_at, updated_at) "
                    "values (%s, %s, now(), now()) on conflict do nothing",
                    (user_id, group_id),
                )
                conn.commit()
