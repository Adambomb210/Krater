"""`fresh_actor` must re-check Weave rather than trust the session's cached groups, and `require_user`
must actually redirect signed-out requests to `/login`.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import APIRouter, Depends, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.models import User
from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER, Actor
from krater.weave.types import WeaveUser
from krater.web.app import create_app
from krater.web.deps import current_user, fresh_actor, require_admin, require_reviewer, require_user


class _FakeWeaveClient:
    """Answers `get_user` with whatever `WeaveUser` (or `None`) the test hands it."""

    def __init__(self, user: WeaveUser | None) -> None:
        self._user = user

    def get_user(self, sub: str) -> WeaveUser | None:
        return self._user


def _make_request(session_data: dict) -> object:
    class _FakeRequest:
        session = session_data

    return _FakeRequest()


def _make_user(db_session: Session, *, groups_cached: list[str]) -> User:
    user = User(
        weave_sub="PWLDEPSTEST",
        display_name="Dep Test",
        email="dep@example.com",
        groups_cached=groups_cached,
    )
    db_session.add(user)
    db_session.flush()
    return user


def test_current_user_is_none_without_a_session(db_session: Session) -> None:
    assert current_user(_make_request({}), db_session) is None


def test_current_user_looks_up_the_session_user_id(db_session: Session) -> None:
    user = _make_user(db_session, groups_cached=[GROUP_MEMBER])

    found = current_user(_make_request({"user_id": str(user.id)}), db_session)

    assert found is not None
    assert found.id == user.id


def test_current_user_ignores_a_corrupt_session_value(db_session: Session) -> None:
    assert current_user(_make_request({"user_id": "not-a-uuid"}), db_session) is None


def test_fresh_actor_uses_live_groups_not_the_session_cache(db_session: Session) -> None:
    # Signed in as a reviewer, but Weave now says the reviewer group was removed.
    user = _make_user(db_session, groups_cached=[GROUP_MEMBER, GROUP_REVIEWER])
    live_user = WeaveUser(
        sub=user.weave_sub,
        name=user.display_name,
        email=user.email,
        slack_id=None,
        groups=frozenset({GROUP_MEMBER}),
        active=True,
    )

    actor = fresh_actor(user, _FakeWeaveClient(live_user))

    assert isinstance(actor, Actor)
    assert actor.is_member
    assert not actor.is_reviewer
    with pytest.raises(HTTPException) as exc_info:
        require_reviewer(actor)
    assert exc_info.value.status_code == 403


def test_fresh_actor_rejects_an_inactive_user(db_session: Session) -> None:
    user = _make_user(db_session, groups_cached=[GROUP_MEMBER])
    live_user = WeaveUser(
        sub=user.weave_sub,
        name=user.display_name,
        email=user.email,
        slack_id=None,
        groups=frozenset({GROUP_MEMBER}),
        active=False,
    )

    with pytest.raises(HTTPException) as exc_info:
        fresh_actor(user, _FakeWeaveClient(live_user))
    assert exc_info.value.status_code == 403


def test_fresh_actor_rejects_a_user_no_longer_a_member(db_session: Session) -> None:
    user = _make_user(db_session, groups_cached=[GROUP_MEMBER])
    live_user = WeaveUser(
        sub=user.weave_sub,
        name=user.display_name,
        email=user.email,
        slack_id=None,
        groups=frozenset(),
        active=True,
    )

    with pytest.raises(HTTPException):
        fresh_actor(user, _FakeWeaveClient(live_user))


def test_fresh_actor_rejects_a_user_unknown_to_weave(db_session: Session) -> None:
    user = _make_user(db_session, groups_cached=[GROUP_MEMBER])

    with pytest.raises(HTTPException):
        fresh_actor(user, _FakeWeaveClient(None))


def test_require_admin_needs_the_admin_group(db_session: Session) -> None:
    user = _make_user(db_session, groups_cached=[GROUP_MEMBER, GROUP_REVIEWER])
    live_user = WeaveUser(
        sub=user.weave_sub,
        name=user.display_name,
        email=user.email,
        slack_id=None,
        groups=frozenset({GROUP_MEMBER, GROUP_REVIEWER}),
        active=True,
    )
    actor = fresh_actor(user, _FakeWeaveClient(live_user))

    with pytest.raises(HTTPException) as exc_info:
        require_admin(actor)
    assert exc_info.value.status_code == 403

    admin_live_user = WeaveUser(
        sub=user.weave_sub,
        name=user.display_name,
        email=user.email,
        slack_id=None,
        groups=frozenset({GROUP_MEMBER, GROUP_REVIEWER, GROUP_ADMIN}),
        active=True,
    )
    admin_actor = fresh_actor(user, _FakeWeaveClient(admin_live_user))
    assert require_admin(admin_actor) is admin_actor


def test_require_user_redirects_to_login_with_next_when_signed_out(db_session: Session) -> None:
    """An end-to-end check (through real FastAPI dependency resolution) that `require_user` actually
    produces the redirect, not just that the plain-Python-call behavior above is right."""
    app = create_app()
    app.dependency_overrides[get_session] = lambda: db_session

    probe_router = APIRouter()

    @probe_router.get("/__test/require-user")
    def _probe(user: Annotated[User, Depends(require_user)]) -> dict:
        return {"id": str(user.id)}

    app.include_router(probe_router)

    with TestClient(app, follow_redirects=False) as test_client:
        response = test_client.get("/__test/require-user?foo=bar")

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F__test%2Frequire-user%3Ffoo%3Dbar"
