"""Shared fixtures for `tests/services`: quick user/actor factories."""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy.orm import Session

from krater.models import User
from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER, Actor

_counter = itertools.count()


@pytest.fixture
def make_user(db_session: Session):
    """Factory: create and flush a `User` with a unique `weave_sub`/email."""

    def _make_user(*, display_name: str = "Test User", email: str | None = None) -> User:
        n = next(_counter)
        user = User(
            weave_sub=f"PWLTEST{n:06d}",
            display_name=display_name,
            email=email or f"user{n}@example.com",
        )
        db_session.add(user)
        db_session.flush()
        return user

    return _make_user


@pytest.fixture
def make_actor(make_user):
    """Factory: build an `Actor` with the given groups, backed by a fresh (or supplied) `User`."""

    def _make_actor(*, groups: frozenset[str] = frozenset(), user: User | None = None, **user_kwargs) -> Actor:
        return Actor(user=user or make_user(**user_kwargs), groups=groups)

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
