"""`StubWeaveClient`: a fake Weave for `KRATER_WEAVE_MODE=stub` (development and tests).

Makes no network calls. Users come from a JSON fixture (`weave_stub_users_file`, or the bundled
`stub_users.json` when that setting is blank). `authorization_url` points at the local `/auth/stub`
picker page; `exchange_code` treats the authorization code as the chosen user's `sub` directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

from krater.weave.errors import WeaveAuthError
from krater.weave.types import WeaveIdentity, WeaveUser

#: The bundled fixture, used whenever `settings.weave_stub_users_file` is blank.
DEFAULT_STUB_USERS_FILE = Path(__file__).parent / "stub_users.json"


class StubWeaveClient:
    """A `WeaveClient` backed by a local JSON fixture of fake users. See `DEFAULT_STUB_USERS_FILE` for
    the expected shape: one object per user, with the same fields as a directory `WeaveUser`, plus
    `email_verified` (used for the identity built at "sign-in")."""

    def __init__(self, stub_users_file: str | Path | None = None) -> None:
        path = Path(stub_users_file) if stub_users_file else DEFAULT_STUB_USERS_FILE
        raw_users = json.loads(path.read_text())

        self._users_by_sub: dict[str, WeaveUser] = {}
        self._email_verified_by_sub: dict[str, bool] = {}
        for entry in raw_users:
            user = WeaveUser(
                sub=entry["sub"],
                name=entry["name"],
                email=entry["email"],
                slack_id=entry.get("slack_id"),
                groups=frozenset(entry.get("groups", [])),
                active=entry.get("active", True),
            )
            self._users_by_sub[user.sub] = user
            self._email_verified_by_sub[user.sub] = entry.get("email_verified", True)

    def authorization_url(self, *, state: str, nonce: str, code_verifier: str, redirect_uri: str) -> str:
        del code_verifier  # no real PKCE exchange happens in stub mode
        params = urlencode({"state": state, "nonce": nonce, "redirect_uri": redirect_uri})
        return f"/auth/stub?{params}"

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str, nonce: str) -> WeaveIdentity:
        del code_verifier, redirect_uri, nonce  # the stub trusts the code (a fixture `sub`) directly
        user = self._users_by_sub.get(code)
        if user is None:
            raise WeaveAuthError(f"no stub user with sub {code!r}")
        return WeaveIdentity(
            sub=user.sub,
            name=user.name,
            email=user.email,
            email_verified=self._email_verified_by_sub.get(user.sub, True),
            slack_id=user.slack_id,
            groups=user.groups,
        )

    def get_user(self, sub: str) -> WeaveUser | None:
        return self._users_by_sub.get(sub)

    def get_user_by_slack_id(self, slack_id: str) -> WeaveUser | None:
        for user in self._users_by_sub.values():
            if user.slack_id == slack_id:
                return user
        return None

    def list_users_in_group(self, group: str) -> list[WeaveUser]:
        return [user for user in self._users_by_sub.values() if group in user.groups]

    def list_all_users(self) -> list[WeaveUser]:
        """Every fixture user, in file order. Used by the `/auth/stub` picker page."""
        return list(self._users_by_sub.values())


__all__ = ["DEFAULT_STUB_USERS_FILE", "StubWeaveClient"]
