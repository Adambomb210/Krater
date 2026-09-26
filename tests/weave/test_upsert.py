"""`upsert_user_from_identity`: creates a `User` on first sign-in, updates it on the next."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from krater.models import User
from krater.services.users import upsert_user_from_identity
from krater.weave.types import WeaveIdentity


def test_upsert_creates_then_updates_the_same_user(db_session: Session) -> None:
    first_identity = WeaveIdentity(
        sub="PWLUPSERT001",
        name="Original Name",
        email="original@example.com",
        email_verified=True,
        slack_id=None,
        groups=frozenset({"ganymede:member"}),
    )

    created = upsert_user_from_identity(db_session, first_identity)
    db_session.commit()

    assert created.weave_sub == "PWLUPSERT001"
    assert created.display_name == "Original Name"
    assert created.email == "original@example.com"
    assert created.slack_user_id is None
    assert created.groups_cached == ["ganymede:member"]
    assert created.last_login_at is not None
    first_login_at = created.last_login_at

    only_row = db_session.execute(select(User).where(User.weave_sub == "PWLUPSERT001")).scalars().all()
    assert len(only_row) == 1

    second_identity = WeaveIdentity(
        sub="PWLUPSERT001",
        name="Updated Name",
        email="updated@example.com",
        email_verified=True,
        slack_id="U12345",
        groups=frozenset({"ganymede:member", "ganymede:reviewer"}),
    )

    updated = upsert_user_from_identity(db_session, second_identity)
    db_session.commit()

    assert updated.id == created.id
    assert updated.display_name == "Updated Name"
    assert updated.email == "updated@example.com"
    assert updated.slack_user_id == "U12345"
    assert updated.groups_cached == ["ganymede:member", "ganymede:reviewer"]
    assert updated.last_login_at >= first_login_at

    rows = db_session.execute(select(User).where(User.weave_sub == "PWLUPSERT001")).scalars().all()
    assert len(rows) == 1
