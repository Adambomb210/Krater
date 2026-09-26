"""Turning a verified Weave identity into (or onto) a `User` row."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from krater.models import User
from krater.weave import WeaveIdentity


def upsert_user_from_identity(session: Session, identity: WeaveIdentity) -> User:
    """Create or update the `User` row for `identity`, by `weave_sub`.

    Refreshes `display_name`, `email`, `slack_user_id`, `groups_cached` and `last_login_at` from the
    identity. Flushes but does not commit -- the caller (a router, inside a request) commits.
    """
    user = session.execute(select(User).where(User.weave_sub == identity.sub)).scalar_one_or_none()
    if user is None:
        user = User(weave_sub=identity.sub)
        session.add(user)

    user.display_name = identity.name
    user.email = identity.email
    user.slack_user_id = identity.slack_id
    user.groups_cached = sorted(identity.groups)
    user.last_login_at = datetime.now(UTC)

    session.flush()
    return user


__all__ = ["upsert_user_from_identity"]
