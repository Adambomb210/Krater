"""Shared fixtures for tests/web: quick, direct-to-service project setup for a signed-in stub user.

HTTP-level tests exercise routing, auth wiring, CSRF and error-mapping -- the service layer's own
rules already have thorough unit tests in tests/services. So these fixtures skip the HTTP round trip
for *setup* (e.g. getting a project into `pending_review`) and call the service directly, using an
`Actor` built the same way `fresh_actor` would, from the signed-in `User`'s Krater roles (which stub
sign-in seeds from the fixture's `groups`).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session, object_session

from krater.models import Project, ReviewDecision, ReviewSource, User, UserRole
from krater.services import projects as project_service
from krater.services import roles
from krater.services.actor import GROUP_MEMBER, Actor

#: A real PNG magic-byte header, for tests that need `confirm_screenshot`'s signature check to pass.
PNG_SIGNATURE = bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)) + b"\x00" * 8


def actor_for(user: User) -> Actor:
    """An `Actor` for `user`, with their current Krater roles."""
    session = object_session(user)
    assert session is not None
    return roles.actor_for(session, user)


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


@pytest.fixture
def revoke_membership(db_session: Session) -> Callable[[User], None]:
    """Take `ganymede:member` away from an already-signed-in `user`, straight in the database, so
    `fresh_actor` sees the change on their *next* request (sign-in itself already refuses non-members)."""

    def _revoke(user: User) -> None:
        db_session.execute(delete(UserRole).where(UserRole.user_id == user.id, UserRole.role == GROUP_MEMBER))
        db_session.flush()

    return _revoke
