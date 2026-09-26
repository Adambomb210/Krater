"""Turning a verified Weave identity into (or onto) a `User` row."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from krater.models import User
from krater.weave import WeaveIdentity
from krater.weave.types import WeaveUser


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


def upsert_user_from_weave_user(session: Session, weave_user: WeaveUser) -> User:
    """Create or update the `User` row for a directory-sourced `WeaveUser`.

    Mirrors `upsert_user_from_identity`, minus `last_login_at` (this isn't a sign-in). Used when someone
    who has never signed in to Krater's web UI acts through Slack -- e.g. a reviewer clicking Approve --
    so `record_review`'s `Actor` still has a real `User` row to attach the `Review` to.
    """
    user = session.execute(select(User).where(User.weave_sub == weave_user.sub)).scalar_one_or_none()
    if user is None:
        user = User(weave_sub=weave_user.sub)
        session.add(user)

    user.display_name = weave_user.name
    user.email = weave_user.email
    user.slack_user_id = weave_user.slack_id
    user.groups_cached = sorted(weave_user.groups)

    session.flush()
    return user


__all__ = ["upsert_user_from_identity", "upsert_user_from_weave_user"]
