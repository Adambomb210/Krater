"""`LiveWeaveClient` against a fake Weave (`httpx.MockTransport`): discovery, JWKS, PKCE, and id_token
validation (signature, `iss`, `aud`, `exp`, `nonce`). Weave is used for sign-in only.
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
from krater.weave.types import WeaveIdentity

ISSUER = "https://weave.test"
CLIENT_ID = "krater-client"
CLIENT_SECRET = "krater-secret"
KID = "test-key-1"
REDIRECT_URI = "https://krater.test/auth/callback"
GOOD_NONCE = "expected-nonce"


def _settings() -> Settings:
    return Settings(
        weave_issuer=ISSUER,
        weave_client_id=CLIENT_ID,
        weave_client_secret=CLIENT_SECRET,
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
        "nonce": GOOD_NONCE,
        "iat": now,
        "exp": now + 300,
    }
    claims.update(claim_overrides)
    return jwt.encode({"alg": "RS256", "kid": KID}, claims, key)


class FakeWeave:
    """A minimal fake of Weave's OIDC endpoints, driven by an `httpx.MockTransport`."""

    def __init__(self, signing_key: RSAKey) -> None:
        self.signing_key = signing_key
        self.requests: list[httpx.Request] = []
        self.id_token: str | None = None
        self.token_status = 200

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


def test_authorization_url_uses_s256_pkce_and_only_the_standard_scopes(live_client: LiveWeaveClient) -> None:
    url = live_client.authorization_url(state="s1", nonce="n1", code_verifier="a" * 64, redirect_uri=REDIRECT_URI)
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert url.startswith(f"{ISSUER}/oauth/authorize?")
    assert params["response_type"] == ["code"]
    assert params["client_id"] == [CLIENT_ID]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert params["scope"] == ["openid profile email"]
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


def test_exchange_code_ignores_non_standard_claims(
    fake_weave: FakeWeave, live_client: LiveWeaveClient, rsa_key: RSAKey
) -> None:
    # A Weave that still sends the old Krater-Integration claims must not change anything: roles and
    # Slack links come from Krater's own database now.
    fake_weave.id_token = _id_token(rsa_key, groups=["ganymede:admin"], slack_id="U1234", slack_membership="full")

    identity = live_client.exchange_code(code="c1", code_verifier="v", redirect_uri=REDIRECT_URI, nonce=GOOD_NONCE)

    assert identity == WeaveIdentity(sub="PWLLIVE0001", name="Lee Live", email="lee@example.com", email_verified=True)


@pytest.mark.parametrize("email_verified", [False, "true", None], ids=["false", "string", "absent"])
def test_exchange_code_only_trusts_a_boolean_true_email_verified(
    fake_weave: FakeWeave, live_client: LiveWeaveClient, rsa_key: RSAKey, email_verified: object
) -> None:
    fake_weave.id_token = _id_token(rsa_key, email_verified=email_verified)

    identity = live_client.exchange_code(code="c1", code_verifier="v", redirect_uri=REDIRECT_URI, nonce=GOOD_NONCE)

    assert identity.email_verified is False


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
