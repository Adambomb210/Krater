"""Signing in: turning a verified Weave identity into (or onto) a `User` row, and deciding whether that
user may use Krater. Roles come from Krater's own database (`krater.services.roles`)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import User
from krater.services import roles
from krater.services.actor import GROUP_MEMBER
from krater.weave import WeaveIdentity

STUB_SEED_REASON = "stub fixture"


def upsert_user_from_identity(session: Session, identity: WeaveIdentity) -> User:
    """Create or update the `User` row for `identity`, by `weave_sub`.

    Refreshes `display_name`, `email` and `email_verified` from the identity. Leaves `slack_user_id`,
    `last_login_at` and `disabled_at` alone. Flushes but does not commit.
    """
    user = session.execute(sa.select(User).where(User.weave_sub == identity.sub)).scalar_one_or_none()
    if user is None:
        user = User(weave_sub=identity.sub)
        session.add(user)

    user.display_name = identity.name or identity.email or identity.sub
    user.email = identity.email
    user.email_verified = identity.email_verified

    session.flush()
    return user


@dataclass(frozen=True)
class StubSeed:
    """Dev-only extras from the stub fixture, seeded at stub sign-in (never with a live Weave)."""

    groups: frozenset[str]
    slack_id: str | None


SignInStatus = Literal["ok", "disabled", "not_a_member"]


@dataclass(frozen=True)
class SignInResult:
    user: User
    status: SignInStatus


def _seed_stub_slack_id(session: Session, user: User, slack_id: str | None) -> None:
    if not slack_id or user.slack_user_id is not None:
        return
    taken = session.scalars(sa.select(User.id).where(User.slack_user_id == slack_id, User.id != user.id)).first()
    if taken is None:
        user.slack_user_id = slack_id
        session.flush()


def sign_in(
    session: Session,
    identity: WeaveIdentity,
    *,
    bootstrap_admins: Iterable[str] = (),
    stub_seed: StubSeed | None = None,
) -> SignInResult:
    """Record a sign-in and decide whether it's allowed. Flushes, never commits: the caller commits
    whatever the outcome, so a refused user's row (and any grants applied) is kept for admins to see.

    1. Upsert the user from the identity.
    2. A disabled user is refused before anything else happens.
    3. Stub mode only: seed the fixture's groups and Slack id.
    4. Bootstrap admins get `ganymede:admin` and `ganymede:member` if missing.
    5. Pending grants for the user's email are applied, if Weave says the email is verified.
    6. Without `ganymede:member`, the user is refused as not a member.
    """
    user = upsert_user_from_identity(session, identity)
    if user.is_disabled:
        return SignInResult(user=user, status="disabled")

    if stub_seed is not None:
        roles.seed_roles(session, user, sorted(stub_seed.groups), reason=STUB_SEED_REASON)
        _seed_stub_slack_id(session, user, stub_seed.slack_id)

    roles.apply_bootstrap_admin(session, user, entries=bootstrap_admins)
    roles.apply_pending_grants(session, user)

    if GROUP_MEMBER not in roles.roles_for(session, user):
        return SignInResult(user=user, status="not_a_member")

    user.last_login_at = datetime.now(UTC)
    session.flush()
    return SignInResult(user=user, status="ok")


__all__ = ["STUB_SEED_REASON", "SignInResult", "StubSeed", "sign_in", "upsert_user_from_identity"]
