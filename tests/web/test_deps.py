"""`fresh_actor` must read roles and the disabled flag from Krater's database on every call, and
`require_user` must actually redirect signed-out requests to `/login`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import pytest
from fastapi import APIRouter, Depends, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.models import User, UserRole
from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER, Actor
from krater.web.app import create_app
from krater.web.deps import (
    current_user,
    fresh_actor,
    require_admin,
    require_reviewer,
    require_user,
    session_actor,
)


def _make_request(session_data: dict) -> object:
    class _FakeRequest:
        session = session_data

    return _FakeRequest()


def _make_user(db_session: Session, *, roles: list[str]) -> User:
    user = User(weave_sub="PWLDEPSTEST", display_name="Dep Test", email="dep@example.com", email_verified=True)
    db_session.add(user)
    db_session.flush()
    for role in roles:
        db_session.add(UserRole(user_id=user.id, role=role))
    db_session.flush()
    return user


def test_current_user_is_none_without_a_session(db_session: Session) -> None:
    assert current_user(_make_request({}), db_session) is None


def test_current_user_looks_up_the_session_user_id(db_session: Session) -> None:
    user = _make_user(db_session, roles=[GROUP_MEMBER])

    found = current_user(_make_request({"user_id": str(user.id)}), db_session)

    assert found is not None
    assert found.id == user.id


def test_current_user_ignores_a_corrupt_session_value(db_session: Session) -> None:
    assert current_user(_make_request({"user_id": "not-a-uuid"}), db_session) is None


def test_fresh_actor_sees_a_role_removed_after_sign_in(db_session: Session) -> None:
    user = _make_user(db_session, roles=[GROUP_MEMBER, GROUP_REVIEWER])
    assert fresh_actor(user, db_session).is_reviewer

    db_session.execute(delete(UserRole).where(UserRole.user_id == user.id, UserRole.role == GROUP_REVIEWER))
    actor = fresh_actor(user, db_session)

    assert isinstance(actor, Actor)
    assert actor.is_member
    assert not actor.is_reviewer
    with pytest.raises(HTTPException) as exc_info:
        require_reviewer(actor)
    assert exc_info.value.status_code == 403


def test_fresh_actor_rejects_a_disabled_user(db_session: Session) -> None:
    user = _make_user(db_session, roles=[GROUP_MEMBER, GROUP_ADMIN])
    user.disabled_at = datetime.now(UTC)

    with pytest.raises(HTTPException) as exc_info:
        fresh_actor(user, db_session)
    assert exc_info.value.status_code == 403
    assert "disabled" in exc_info.value.detail


def test_fresh_actor_rejects_a_user_who_is_not_a_member(db_session: Session) -> None:
    user = _make_user(db_session, roles=[GROUP_REVIEWER])

    with pytest.raises(HTTPException) as exc_info:
        fresh_actor(user, db_session)
    assert exc_info.value.status_code == 403


def test_session_actor_reports_roles_without_refusing(db_session: Session) -> None:
    user = _make_user(db_session, roles=[GROUP_REVIEWER])
    user.disabled_at = datetime.now(UTC)

    actor = session_actor(user, db_session)

    assert actor.groups == frozenset({GROUP_REVIEWER})


def test_require_admin_needs_the_admin_role(db_session: Session) -> None:
    user = _make_user(db_session, roles=[GROUP_MEMBER, GROUP_REVIEWER])

    with pytest.raises(HTTPException) as exc_info:
        require_admin(fresh_actor(user, db_session))
    assert exc_info.value.status_code == 403

    db_session.add(UserRole(user_id=user.id, role=GROUP_ADMIN))
    db_session.flush()
    admin_actor = fresh_actor(user, db_session)
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
