"""`LiveWeaveClient`: OIDC sign-in and the directory API against a real Weave.

Discovery and the JWKS are fetched once per process and cached. id_tokens are RS256, verified against
that JWKS with `joserfc` (signature, `iss`, `aud`, `exp`, `nonce`); PKCE is S256, as Weave requires.
Directory responses (`get_user` / `get_user_by_slack_id`) are cached for a short TTL to absorb bursts of
lookups (e.g. Slack button clicks). See `docs/weave-integration.md` for the full contract.
"""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry

from krater.config import Settings
from krater.weave.cache import MISSING, TTLCache
from krater.weave.errors import WeaveAuthError, WeaveUnavailableError
from krater.weave.types import WeaveIdentity, WeaveUser

#: How long a directory lookup (`get_user` / `get_user_by_slack_id`) is trusted before re-fetching.
DIRECTORY_CACHE_TTL_SECONDS = 60.0

SCOPES = "openid profile email groups slack"


def _pkce_code_challenge_s256(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class LiveWeaveClient:
    """A `WeaveClient` backed by a real Weave over HTTP. `http_client` is injectable for tests
    (`httpx.MockTransport`); production code leaves it out and gets a real `httpx.Client`."""

    def __init__(self, settings: Settings, *, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._http = http_client if http_client is not None else httpx.Client(timeout=10.0)
        self._discovery_doc: dict | None = None
        self._jwk_set: KeySet | None = None
        self._directory_cache: TTLCache[tuple[str, str], WeaveUser | None] = TTLCache(DIRECTORY_CACHE_TTL_SECONDS)

    # -- OIDC ------------------------------------------------------------------------------------

    def authorization_url(self, *, state: str, nonce: str, code_verifier: str, redirect_uri: str) -> str:
        endpoint = self._discovery()["authorization_endpoint"]
        params = {
            "response_type": "code",
            "client_id": self._settings.weave_client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            "nonce": nonce,
            "code_challenge": _pkce_code_challenge_s256(code_verifier),
            "code_challenge_method": "S256",
        }
        return f"{endpoint}?{urlencode(params)}"

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str) -> WeaveIdentity:
        token_endpoint = self._discovery()["token_endpoint"]
        try:
            response = self._http.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": self._settings.weave_client_id,
                    "client_secret": self._settings.weave_client_secret,
                    "code_verifier": code_verifier,
                },
            )
        except httpx.HTTPError as exc:
            raise WeaveUnavailableError("could not reach Weave's token endpoint") from exc

        if response.status_code >= 500:
            raise WeaveUnavailableError(f"Weave's token endpoint returned {response.status_code}")
        if response.status_code != 200:
            raise WeaveAuthError(f"token exchange failed: {response.status_code} {response.text}")

        id_token = response.json().get("id_token")
        if not id_token:
            raise WeaveAuthError("token response had no id_token")

        return self._verify_id_token(id_token, nonce=nonce)

    def _discovery(self) -> dict:
        if self._discovery_doc is None:
            self._discovery_doc = self._get_json(f"{self._settings.weave_issuer}/.well-known/openid-configuration")
        return self._discovery_doc

    def _jwks(self) -> KeySet:
        if self._jwk_set is None:
            jwks_uri = self._discovery()["jwks_uri"]
            self._jwk_set = KeySet.import_key_set(self._get_json(jwks_uri))
        return self._jwk_set

    def _get_json(self, url: str) -> dict:
        try:
            response = self._http.get(url)
        except httpx.HTTPError as exc:
            raise WeaveUnavailableError(f"could not reach Weave at {url}") from exc
        if response.status_code != 200:
            raise WeaveUnavailableError(f"Weave returned {response.status_code} for {url}")
        return response.json()

    def _verify_id_token(self, id_token: str, *, nonce: str) -> WeaveIdentity:
        try:
            token = jwt.decode(id_token, self._jwks(), algorithms=["RS256"])
        except JoseError as exc:
            raise WeaveAuthError(f"id_token failed signature/decoding checks: {exc}") from exc

        registry = JWTClaimsRegistry(
            iss={"essential": True, "value": self._settings.weave_issuer},
            aud={"essential": True, "value": self._settings.weave_client_id},
            exp={"essential": True},
            nonce={"essential": True, "value": nonce},
        )
        try:
            registry.validate(token.claims)
        except JoseError as exc:
            raise WeaveAuthError(f"id_token claims failed validation: {exc}") from exc

        claims = token.claims
        return WeaveIdentity(
            sub=claims["sub"],
            name=claims.get("name", ""),
            email=claims.get("email", ""),
            email_verified=bool(claims.get("email_verified", False)),
            slack_id=claims.get("slack_id"),
            groups=frozenset(claims.get("groups", [])),
        )

    # -- Directory ---------------------------------------------------------------------------------

    def get_user(self, sub: str) -> WeaveUser | None:
        return self._cached_directory_lookup(("sub", sub), f"{self._settings.weave_api_base_url}/api/v1/users/{sub}")

    def get_user_by_slack_id(self, slack_id: str) -> WeaveUser | None:
        return self._cached_directory_lookup(
            ("slack_id", slack_id), f"{self._settings.weave_api_base_url}/api/v1/users/by_slack_id/{slack_id}"
        )

    def list_users_in_group(self, group: str) -> list[WeaveUser]:
        url = f"{self._settings.weave_api_base_url}/api/v1/users"
        response = self._directory_get(url, params={"group": group})
        if response.status_code == 404:
            return []
        self._raise_unless_ok(response)
        return [self._parse_user(entry) for entry in response.json()["users"]]

    def _cached_directory_lookup(self, cache_key: tuple[str, str], url: str) -> WeaveUser | None:
        cached = self._directory_cache.get(cache_key)
        if cached is not MISSING:
            return cached

        response = self._directory_get(url)
        if response.status_code == 404:
            user: WeaveUser | None = None
        else:
            self._raise_unless_ok(response)
            user = self._parse_user(response.json()["user"])

        self._directory_cache.set(cache_key, user)
        return user

    def _directory_get(self, url: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            return self._http.get(url, params=params, headers={"X-Api-Key": self._settings.weave_service_key})
        except httpx.HTTPError as exc:
            raise WeaveUnavailableError(f"could not reach Weave's directory at {url}") from exc

    @staticmethod
    def _raise_unless_ok(response: httpx.Response) -> None:
        if response.status_code != 200:
            raise WeaveUnavailableError(f"Weave's directory returned {response.status_code}")

    @staticmethod
    def _parse_user(data: dict) -> WeaveUser:
        return WeaveUser(
            sub=data["sub"],
            name=data["name"],
            email=data["email"],
            slack_id=data.get("slack_id"),
            groups=frozenset(data.get("groups", [])),
            active=bool(data.get("active", False)),
        )


__all__ = ["LiveWeaveClient"]
