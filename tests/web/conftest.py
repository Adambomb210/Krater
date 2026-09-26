"""Shared fixtures for tests/web: quick, direct-to-service project setup for a signed-in stub user.

HTTP-level tests exercise routing, auth wiring, CSRF and error-mapping -- the service layer's own
rules already have thorough unit tests in tests/services. So these fixtures skip the HTTP round trip
for *setup* (e.g. getting a project into `pending_review`) and call the service directly, using an
`Actor` built the same way `fresh_actor` would from the signed-in `User`'s real stub groups.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.models import Project, ReviewDecision, ReviewSource, User
from krater.services import projects as project_service
from krater.services.actor import Actor
from krater.weave import get_weave_client
from krater.weave.types import WeaveUser


def actor_for(user: User) -> Actor:
    """An `Actor` for `user`, using their real (stub) groups -- not the session's cached copy."""
    return Actor(user=user, groups=frozenset(user.groups_cached))


@pytest.fixture
def create_project(db_session: Session) -> Callable[..., Project]:
    """Factory: create a draft project for `user`, via the service (bypassing the HTTP form)."""

    def _create(user: User, **kwargs) -> Project:
        kwargs.setdefault("title", "Test Project")
        kwargs.setdefault("write_up", "A write-up.")
        kwargs.setdefault("budget_requested_cents", 10_000)
        return project_service.create_project(db_session, actor_for(user), **kwargs)

    return _create


@pytest.fixture
def submitted_project(db_session: Session, create_project: Callable[..., Project]) -> Callable[..., Project]:
    """Factory: create and submit a project for `user`, landing it in `pending_review`."""

    def _submit(user: User, **kwargs) -> Project:
        project = create_project(user, **kwargs)
        return project_service.submit(db_session, actor_for(user), project=project)

    return _submit


@pytest.fixture
def approved_project(db_session: Session, submitted_project: Callable[..., Project]) -> Callable[..., Project]:
    """Factory: create, submit and get `reviewer` to approve a project for `user`."""

    def _approve(user: User, reviewer: User, **kwargs) -> Project:
        project = submitted_project(user, **kwargs)
        project_service.record_review(
            db_session,
            actor_for(reviewer),
            revision=project.current_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )
        db_session.refresh(project)
        return project

    return _approve


class _FakeWeaveClient:
    """A `WeaveClient` that answers `get_user` with a fixed `WeaveUser`, regardless of `sub`."""

    def __init__(self, user: WeaveUser) -> None:
        self._user = user

    def get_user(self, sub: str) -> WeaveUser:
        del sub
        return self._user


@pytest.fixture
def revoke_membership(client: TestClient) -> Callable[[User], None]:
    """Make `fresh_actor` see `user` as no longer a Ganymede member on their *next* request, by
    overriding `get_weave_client` -- simulates their Weave groups changing after they signed in
    (Krater's own `/login` already refuses a non-member outright, so this is the only way an
    already-signed-in session can end up "not a member" for `fresh_actor` to catch).
    """

    def _revoke(user: User) -> None:
        revoked = WeaveUser(
            sub=user.weave_sub, name=user.display_name, email=user.email, slack_id=None, groups=frozenset(), active=True
        )
        client.app.dependency_overrides[get_weave_client] = lambda: _FakeWeaveClient(revoked)

    yield _revoke
    client.app.dependency_overrides.pop(get_weave_client, None)
