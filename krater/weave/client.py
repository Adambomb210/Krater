"""The `WeaveClient` protocol every adapter (live or stub) implements.

Nothing outside `krater.weave` should know Weave's URLs, scopes or claim shapes -- go through this
interface. Krater uses Weave for sign-in only; see `docs/weave-integration.md` for the contract.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from krater.weave.types import WeaveIdentity


@runtime_checkable
class WeaveClient(Protocol):
    def authorization_url(self, *, state: str, nonce: str, code_verifier: str, redirect_uri: str) -> str:
        """The URL to send the browser to, to start sign-in (PKCE S256 challenge included)."""
        ...

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str) -> WeaveIdentity:
        """Exchange an authorization code for a verified identity.

        Raises `WeaveAuthError` if the code, PKCE verifier or resulting id_token don't check out, and
        `WeaveUnavailableError` if Weave couldn't be reached.
        """
        ...
