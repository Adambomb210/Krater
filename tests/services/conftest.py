"""Shared fixtures for `tests/services`: quick user/actor factories."""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy.orm import Session

from krater.models import User, UserRole
from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER, Actor

_counter = itertools.count()


@pytest.fixture
def make_user(db_session: Session):
    """Factory: create and flush a `User` with a unique `weave_sub`/email."""

    def _make_user(
        *,
        display_name: str = "Test User",
        email: str | None = None,
        email_verified: bool = True,
        slack_user_id: str | None = None,
    ) -> User:
        n = next(_counter)
        user = User(
            weave_sub=f"PWLTEST{n:06d}",
            display_name=display_name,
            email=email or f"user{n}@example.com",
            email_verified=email_verified,
            slack_user_id=slack_user_id,
        )
        db_session.add(user)
        db_session.flush()
        return user

    return _make_user


@pytest.fixture
def make_actor(db_session: Session, make_user):
    """Factory: build an `Actor` with the given groups, backed by a fresh (or supplied) `User`. The
    groups are also stored as the user's Krater roles, so anything that reads roles from the database
    (channel invites, Slack clicks, `roles.authorize`) sees the same thing."""

    def _make_actor(*, groups: frozenset[str] = frozenset(), user: User | None = None, **user_kwargs) -> Actor:
        user = user or make_user(**user_kwargs)
        for role in sorted(groups):
            db_session.add(UserRole(user_id=user.id, role=role))
        db_session.flush()
        return Actor(user=user, groups=groups)

    return _make_actor


@pytest.fixture
def member(make_actor) -> Actor:
    """A plain Ganymede member: can submit, but not review or admin-override."""
    return make_actor(groups=frozenset({GROUP_MEMBER}))


@pytest.fixture
def reviewer(make_actor) -> Actor:
    """A Ganymede reviewer (and member)."""
    return make_actor(groups=frozenset({GROUP_MEMBER, GROUP_REVIEWER}))


@pytest.fixture
def admin(make_actor) -> Actor:
    """A Ganymede admin (and member)."""
    return make_actor(groups=frozenset({GROUP_MEMBER, GROUP_ADMIN}))
