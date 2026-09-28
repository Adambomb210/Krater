"""Data carried out of the Weave adapter.

`WeaveIdentity` comes from a freshly verified id_token, at sign-in time. It carries only the standard
OIDC claims Weave's main branch issues for the `openid profile email` scopes; roles and Slack links
live in Krater's own database. See `docs/weave-integration.md`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WeaveIdentity:
    """A verified identity, built from an id_token's claims."""

    sub: str
    name: str
    email: str
    email_verified: bool
