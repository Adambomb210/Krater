"""`StubWeaveClient`: a fake Weave for `KRATER_WEAVE_MODE=stub` (development and tests).

Makes no network calls. Users come from a JSON fixture (`weave_stub_users_file`, or the bundled
`stub_users.json` when that setting is blank). `authorization_url` points at the local `/auth/stub`
picker page; `exchange_code` treats the authorization code as the chosen user's `sub` directly.

Each fixture user also lists `groups` and an optional `slack_id`. A real Weave carries neither; stub
sign-in (`krater.web.routers.auth`) seeds them into Krater's own `user_roles` and `users.slack_user_id`
so dev flows work without an admin granting roles first. The app refuses stub mode in production.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from krater.weave.errors import WeaveAuthError
from krater.weave.types import WeaveIdentity

#: The bundled fixture, used whenever `settings.weave_stub_users_file` is blank.
DEFAULT_STUB_USERS_FILE = Path(__file__).parent / "stub_users.json"


@dataclass(frozen=True)
class StubUser:
    """One fixture user: the identity stub sign-in returns, plus the dev-only role/Slack seed."""

    sub: str
    name: str
    email: str
    email_verified: bool
    slack_id: str | None
    groups: frozenset[str]

    def identity(self) -> WeaveIdentity:
        return WeaveIdentity(sub=self.sub, name=self.name, email=self.email, email_verified=self.email_verified)


class StubWeaveClient:
    """A `WeaveClient` backed by a local JSON fixture of fake users. See `DEFAULT_STUB_USERS_FILE` for
    the expected shape: one object per user with `sub`, `name`, `email`, and optionally
    `email_verified` (default true), `slack_id` and `groups`."""

    def __init__(self, stub_users_file: str | Path | None = None) -> None:
        path = Path(stub_users_file) if stub_users_file else DEFAULT_STUB_USERS_FILE
        raw_users = json.loads(path.read_text())

        self._users_by_sub: dict[str, StubUser] = {}
        for entry in raw_users:
            user = StubUser(
                sub=entry["sub"],
                name=entry["name"],
                email=entry["email"],
                email_verified=entry.get("email_verified", True),
                slack_id=entry.get("slack_id"),
                groups=frozenset(entry.get("groups", [])),
            )
            self._users_by_sub[user.sub] = user

    def authorization_url(self, *, state: str, nonce: str, code_verifier: str, redirect_uri: str) -> str:
        del code_verifier  # no real PKCE exchange happens in stub mode
        params = urlencode({"state": state, "nonce": nonce, "redirect_uri": redirect_uri})
        return f"/auth/stub?{params}"

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str) -> WeaveIdentity:
        del code_verifier, redirect_uri, nonce  # the stub trusts the code (a fixture `sub`) directly
        user = self._users_by_sub.get(code)
        if user is None:
            raise WeaveAuthError(f"no stub user with sub {code!r}")
        return user.identity()

    def stub_user(self, sub: str) -> StubUser | None:
        """The fixture entry for `sub`, for seeding roles at stub sign-in."""
        return self._users_by_sub.get(sub)

    def list_all_users(self) -> list[StubUser]:
        """Every fixture user, in file order. Used by the `/auth/stub` picker page."""
        return list(self._users_by_sub.values())


__all__ = ["DEFAULT_STUB_USERS_FILE", "StubUser", "StubWeaveClient"]
