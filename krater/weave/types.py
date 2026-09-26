"""Data carried out of the Weave adapter.

`WeaveIdentity` comes from a freshly verified id_token, at sign-in time. `WeaveUser` comes from the
directory API, and is what Weave says about a user *right now* -- see `docs/weave-integration.md`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WeaveIdentity:
    """A verified identity, built from an id_token's claims. `groups` is empty, never omitted."""

    sub: str
    name: str
    email: str
    email_verified: bool
    slack_id: str | None
    groups: frozenset[str]


@dataclass(frozen=True)
class WeaveUser:
    """A directory lookup result. `active` is Weave's `status == "active"`."""

    sub: str
    name: str
    email: str
    slack_id: str | None
    groups: frozenset[str]
    active: bool
