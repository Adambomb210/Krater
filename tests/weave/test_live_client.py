"""`LiveWeaveClient` against a fake Weave (`httpx.MockTransport`): discovery, JWKS, PKCE, id_token
validation (signature, `iss`, `aud`, `exp`, `nonce`), and the directory API.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from krater.config import Settings
from krater.weave.errors import WeaveAuthError, WeaveUnavailableError
from krater.weave.live import LiveWeaveClient

ISSUER = "https://weave.test"
CLIENT_ID = "krater-client"
CLIENT_SECRET = "krater-secret"
API_BASE = "https://weave.test"
SERVICE_KEY = "svc-key-123"
KID = "test-key-1"
REDIRECT_URI = "https://krater.test/auth/callback"
GOOD_NONCE = "expected-nonce"


def _settings() -> Settings:
    return Settings(
        weave_issuer=ISSUER,
        weave_client_id=CLIENT_ID,
        weave_client_secret=CLIENT_SECRET,
        weave_api_base_url=API_BASE,
        weave_service_key=SERVICE_KEY,
    )


def _id_token(key: RSAKey, **claim_overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "PWLLIVE0001",
        "name": "Lee Live",
        "email": "lee@example.com",
        "email_verified": True,
        "groups": ["ganymede:member"],
        "nonce": GOOD_NONCE,
        "iat": now,
        "exp": now + 300,
    }
    claims.update(claim_overrides)
    return jwt.encode({"alg": "RS256", "kid": KID}, claims, key)


class FakeWeave:
    """A minimal fake of Weave's OIDC + directory endpoints, driven by an `httpx.MockTransport`."""

    def __init__(self, signing_key: RSAKey) -> None:
        self.signing_key = signing_key
        self.requests: list[httpx.Request] = []
        self.id_token: str | None = None
        self.token_status = 200
        self.directory_users: dict[str, dict] = {}
        self.directory_by_slack: dict[str, dict] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/oauth/authorize",
                    "token_endpoint": f"{ISSUER}/oauth/token",
                    "jwks_uri": f"{ISSUER}/oauth/discovery/keys",
                },
            )
        if path == "/oauth/discovery/keys":
            return httpx.Response(200, json={"keys": [self.signing_key.as_dict(private=False)]})
        if path == "/oauth/token":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer", "id_token": self.id_token})
        if path.startswith("/api/v1/users/by_slack_id/"):
            slack_id = path.rsplit("/", 1)[-1]
            user = self.directory_by_slack.get(slack_id)
            return (
                httpx.Response(404, json={"error": "not_found"})
                if user is None
                else httpx.Response(200, json={"user": user})
            )
        if path == "/api/v1/users":
            group = request.url.params.get("group")
            matches = [u for u in self.directory_users.values() if group in u.get("groups", [])]
            return httpx.Response(200, json={"users": matches})
        if path.startswith("/api/v1/users/"):
            sub = path.rsplit("/", 1)[-1]
            user = self.directory_users.get(sub)
            return (
                httpx.Response(404, json={"error": "not_found"})
                if user is None
                else httpx.Response(200, json={"user": user})
            )
        return httpx.Response(404, json={"error": "not_found"})


@pytest.fixture(scope="module")
def rsa_key() -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": KID}, private=True)


@pytest.fixture
def fake_weave(rsa_key: RSAKey) -> FakeWeave:
    return FakeWeave(rsa_key)


@pytest.fixture
def live_client(fake_weave: FakeWeave) -> LiveWeaveClient:
    http_client = httpx.Client(transport=httpx.MockTransport(fake_weave.handler))
    return LiveWeaveClient(_settings(), http_client=http_client)


def test_authorization_url_uses_s256_pkce_and_the_full_scope_set(live_client: LiveWeaveClient) -> None:
    url = live_client.authorization_url(state="s1", nonce="n1", code_verifier="a" * 64, redirect_uri=REDIRECT_URI)
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert url.startswith(f"{ISSUER}/oauth/authorize?")
    assert params["response_type"] == ["code"]
    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert params["scope"] == ["openid profile email groups slack"]
    assert params["state"] == ["s1"]
    assert params["nonce"] == ["n1"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"][0]  # a non-empty S256 challenge was derived from the verifier


def test_exchange_code_accepts_a_good_token(
    fake_weave: FakeWeave, live_client: LiveWeaveClient, rsa_key: RSAKey
) -> None:
    fake_weave.id_token = _id_token(rsa_key)

    identity = live_client.exchange_code(code="c1", code_verifier="v", redirect_uri=REDIRECT_URI, nonce=GOOD_NONCE)

    assert identity.sub == "PWLLIVE0001"
    assert identity.name == "Lee Live"
    assert identity.email == "lee@example.com"
    assert identity.email_verified is True
    assert identity.slack_id is None
    assert identity.groups == frozenset({"ganymede:member"})


def test_exchange_code_sends_the_client_secret_and_code_verifier(
    fake_weave: FakeWeave, live_client: LiveWeaveClient, rsa_key: RSAKey
) -> None:
    fake_weave.id_token = _id_token(rsa_key)

    live_client.exchange_code(code="c1", code_verifier="verifier-value", redirect_uri=REDIRECT_URI, nonce=GOOD_NONCE)

    token_request = next(r for r in fake_weave.requests if r.url.path == "/oauth/token")
    body = token_request.read().decode()
    assert "code_verifier=verifier-value" in body
    assert f"client_secret={CLIENT_SECRET}" in body


@pytest.mark.parametrize(
    "override",
    [
        {"aud": "someone-elses-client"},
        {"iss": "https://not-weave.test"},
        {"nonce": "wrong-nonce"},
        {"exp": int(time.time()) - 60, "iat": int(time.time()) - 120},
    ],
    ids=["wrong-aud", "wrong-iss", "wrong-nonce", "expired"],
)
def test_exchange_code_rejects_bad_claims(
    fake_weave: FakeWeave, live_client: LiveWeaveClient, rsa_key: RSAKey, override: dict
) -> None:
    fake_weave.id_token = _id_token(rsa_key, **override)

    with pytest.raises(WeaveAuthError):
        live_client.exchange_code(code="c1", code_verifier="v", redirect_uri=REDIRECT_URI, nonce=GOOD_NONCE)


def test_exchange_code_rejects_a_bad_signature(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    # Signed by a different key than the one Weave's JWKS publishes, but with the same `kid` -- a real
    # forgery attempt would look exactly like this.
    forged_key = RSAKey.generate_key(2048, parameters={"kid": KID}, private=True)
    fake_weave.id_token = _id_token(forged_key)

    with pytest.raises(WeaveAuthError):
        live_client.exchange_code(code="c1", code_verifier="v", redirect_uri=REDIRECT_URI, nonce=GOOD_NONCE)


def test_exchange_code_raises_weave_auth_error_when_the_code_is_rejected(
    fake_weave: FakeWeave, live_client: LiveWeaveClient
) -> None:
    fake_weave.token_status = 400

    with pytest.raises(WeaveAuthError):
        live_client.exchange_code(code="bad-code", code_verifier="v", redirect_uri=REDIRECT_URI, nonce="n")


def test_exchange_code_raises_weave_unavailable_on_a_5xx(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    fake_weave.token_status = 500

    with pytest.raises(WeaveUnavailableError):
        live_client.exchange_code(code="c1", code_verifier="v", redirect_uri=REDIRECT_URI, nonce="n")


def test_discovery_and_jwks_are_each_fetched_only_once(
    fake_weave: FakeWeave, live_client: LiveWeaveClient, rsa_key: RSAKey
) -> None:
    fake_weave.id_token = _id_token(rsa_key)

    live_client.exchange_code(code="c1", code_verifier="v", redirect_uri=REDIRECT_URI, nonce=GOOD_NONCE)
    live_client.authorization_url(state="s", nonce="n", code_verifier="v", redirect_uri=REDIRECT_URI)

    discovery_hits = [r for r in fake_weave.requests if r.url.path == "/.well-known/openid-configuration"]
    jwks_hits = [r for r in fake_weave.requests if r.url.path == "/oauth/discovery/keys"]
    assert len(discovery_hits) == 1
    assert len(jwks_hits) == 1


def test_get_user_parses_groups_and_slack_id(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    fake_weave.directory_users["PWLDIR0001"] = {
        "sub": "PWLDIR0001",
        "name": "Dee Directory",
        "email": "dee@example.com",
        "slack_id": "U9999",
        "groups": ["ganymede:member", "ganymede:reviewer"],
        "active": True,
    }

    user = live_client.get_user("PWLDIR0001")

    assert user is not None
    assert user.name == "Dee Directory"
    assert user.slack_id == "U9999"
    assert user.groups == frozenset({"ganymede:member", "ganymede:reviewer"})
    assert user.active is True


def test_get_user_treats_absent_or_null_slack_id_as_none(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    fake_weave.directory_users["PWLDIR0002"] = {
        "sub": "PWLDIR0002",
        "name": "Null Slack",
        "email": "nullslack@example.com",
        "slack_id": None,
        "groups": [],
        "active": True,
    }
    fake_weave.directory_users["PWLDIR0003"] = {
        "sub": "PWLDIR0003",
        "name": "No Slack Key",
        "email": "noslackkey@example.com",
        "groups": [],
        "active": True,
    }

    assert live_client.get_user("PWLDIR0002").slack_id is None  # type: ignore[union-attr]
    assert live_client.get_user("PWLDIR0003").slack_id is None  # type: ignore[union-attr]


def test_get_user_returns_none_on_404(live_client: LiveWeaveClient) -> None:
    assert live_client.get_user("no-such-sub") is None


def test_get_user_by_slack_id_parses_and_404s(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    fake_weave.directory_by_slack["U555"] = {
        "sub": "PWLSLACK",
        "name": "Slack User",
        "email": "slack@example.com",
        "slack_id": "U555",
        "groups": [],
        "active": True,
    }

    found = live_client.get_user_by_slack_id("U555")
    assert found is not None
    assert found.sub == "PWLSLACK"
    assert live_client.get_user_by_slack_id("no-such-slack-id") is None


def test_list_users_in_group_filters_by_group(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    fake_weave.directory_users["PWLG1"] = {
        "sub": "PWLG1",
        "name": "G1",
        "email": "g1@example.com",
        "groups": ["ganymede:reviewer"],
        "active": True,
    }
    fake_weave.directory_users["PWLG2"] = {
        "sub": "PWLG2",
        "name": "G2",
        "email": "g2@example.com",
        "groups": ["ganymede:member"],
        "active": True,
    }

    reviewers = live_client.list_users_in_group("ganymede:reviewer")

    assert [u.sub for u in reviewers] == ["PWLG1"]


def test_get_user_sends_the_api_key_header(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    fake_weave.directory_users["PWLKEY"] = {
        "sub": "PWLKEY",
        "name": "Key Check",
        "email": "key@example.com",
        "groups": [],
        "active": True,
    }

    live_client.get_user("PWLKEY")

    directory_request = next(r for r in fake_weave.requests if r.url.path == "/api/v1/users/PWLKEY")
    assert directory_request.headers["X-Api-Key"] == SERVICE_KEY


def test_get_user_result_is_cached_for_the_ttl(fake_weave: FakeWeave, live_client: LiveWeaveClient) -> None:
    fake_weave.directory_users["PWLCACHE"] = {
        "sub": "PWLCACHE",
        "name": "Cache Me",
        "email": "cache@example.com",
        "groups": [],
        "active": True,
    }

    first = live_client.get_user("PWLCACHE")
    second = live_client.get_user("PWLCACHE")

    directory_hits = [r for r in fake_weave.requests if r.url.path == "/api/v1/users/PWLCACHE"]
    assert len(directory_hits) == 1
    assert first == second
