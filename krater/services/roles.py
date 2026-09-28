"""Krater's roles: who holds `ganymede:member` / `ganymede:reviewer` / `ganymede:admin`, who is disabled,
and the admin actions that change either.

By maintainer decision Krater runs against Weave's main branch, which offers plain OIDC sign-in only,
so roles live in Krater's own database (`user_roles`, `pending_role_grants`, `users.disabled_at`). Every
authorization check reads them fresh from here; nothing is cached in the session.

Framework-free: admin routes in `krater.web.routers.admin_users` call the actor-taking functions below;
sign-in (`krater.services.users.sign_in`) calls the system hooks (`apply_bootstrap_admin`,
`apply_pending_grants`, `seed_roles`). Every change writes an `AuditEvent`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from krater.models import AuditEvent, PendingRoleGrant, User, UserRole
from krater.services import audit
from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER, Actor
from krater.services.errors import (
    AccountDisabled,
    InvalidState,
    NotAllowed,
    NotAMember,
    NotFound,
    ValidationFailed,
)

#: The roles an admin can grant, in display order.
KNOWN_ROLES: tuple[str, ...] = (GROUP_MEMBER, GROUP_REVIEWER, GROUP_ADMIN)

AUDIT_ROLE_GRANT = "role_grant"
AUDIT_ROLE_REVOKE = "role_revoke"
AUDIT_PENDING_ROLE_GRANT = "pending_role_grant"
AUDIT_PENDING_ROLE_CANCEL = "pending_role_cancel"
AUDIT_PENDING_ROLE_APPLY = "pending_role_apply"
AUDIT_USER_DISABLE = "user_disable"
AUDIT_USER_ENABLE = "user_enable"
AUDIT_SLACK_USER_ID_SET = "slack_user_id_set"

BOOTSTRAP_REASON = "bootstrap"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Slack user ids: `U…` for regular users, `W…` for Enterprise Grid.
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,63}$")


# --------------------------------------------------------------------------------------------------
# Reading roles
# --------------------------------------------------------------------------------------------------


def roles_for(session: Session, user: User) -> frozenset[str]:
    """Every role `user` holds right now, straight from the database."""
    return frozenset(session.scalars(sa.select(UserRole.role).where(UserRole.user_id == user.id)))


def roles_by_user(session: Session, user_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, frozenset[str]]:
    """`{user_id: roles}` for every id in `user_ids` (users with no roles map to an empty set)."""
    ids = list(user_ids)
    result: dict[uuid.UUID, set[str]] = {user_id: set() for user_id in ids}
    if ids:
        rows = session.execute(sa.select(UserRole.user_id, UserRole.role).where(UserRole.user_id.in_(ids)))
        for user_id, role in rows:
            result[user_id].add(role)
    return {user_id: frozenset(roles) for user_id, roles in result.items()}


def actor_for(session: Session, user: User) -> Actor:
    """An `Actor` with `user`'s current roles, without refusing anyone. For display and navigation;
    anything that authorizes an action uses `authorize` instead."""
    return Actor(user=user, groups=roles_for(session, user))


def authorize(session: Session, user: User) -> Actor:
    """An `Actor` for `user`, for authorizing an action right now.

    Raises `AccountDisabled` if an admin disabled the account, and `NotAMember` unless the user holds
    `ganymede:member`. Callers check reviewer/admin on the returned actor themselves.
    """
    if user.is_disabled:
        raise AccountDisabled("Your Krater account has been disabled. Ask a Ganymede admin if this is a mistake.")
    actor = actor_for(session, user)
    if not actor.is_member:
        raise NotAMember("You're not a Ganymede member in Krater. Ask a Ganymede admin to add you.")
    return actor


def active_users_with_role(session: Session, role: str) -> list[User]:
    """Every user who holds `role` and isn't disabled, e.g. every reviewer to invite to a channel."""
    stmt = (
        sa.select(User)
        .join(UserRole, UserRole.user_id == User.id)
        .where(UserRole.role == role, User.disabled_at.is_(None))
        .order_by(User.display_name, User.id)
    )
    return list(session.scalars(stmt))


@dataclass(frozen=True)
class UserWithRoles:
    user: User
    roles: frozenset[str]


def list_users_with_roles(session: Session) -> list[UserWithRoles]:
    """Every user, with their roles, for the admin users page: disabled users last, then by name."""
    users = list(
        session.scalars(sa.select(User).order_by(User.disabled_at.is_not(None), User.display_name, User.email))
    )
    roles = roles_by_user(session, (user.id for user in users))
    return [UserWithRoles(user=user, roles=roles[user.id]) for user in users]


def list_pending_grants(session: Session) -> list[PendingRoleGrant]:
    return list(session.scalars(sa.select(PendingRoleGrant).order_by(PendingRoleGrant.email, PendingRoleGrant.role)))


def get_user(session: Session, *, user_id: uuid.UUID) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise NotFound(f"No user with id {user_id}.")
    return user


# --------------------------------------------------------------------------------------------------
# Admin actions
# --------------------------------------------------------------------------------------------------


def _require_admin(actor: Actor) -> None:
    if not actor.is_admin:
        raise NotAllowed("Only a Ganymede admin can change roles or accounts.")


def _validate_role(role: str) -> str:
    role = role.strip()
    if role not in KNOWN_ROLES:
        raise ValidationFailed({"role": f"Unknown role {role!r}."})
    return role


def _normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if not _EMAIL_RE.match(normalized) or len(normalized) > 255:
        raise ValidationFailed({"email": "Enter a valid email address."})
    return normalized


def _clean_reason(reason: str | None) -> str | None:
    reason = (reason or "").strip()
    return reason or None


def _lock_admin_roles(session: Session) -> None:
    """Serialize concurrent changes that could remove an admin, so two admins demoting each other at
    the same moment can't both pass `_ensure_another_admin_remains` and leave nobody."""
    session.execute(sa.select(UserRole.id).where(UserRole.role == GROUP_ADMIN).with_for_update())


def _effective_admin_ids(session: Session) -> set[uuid.UUID]:
    """Users who can act as an admin right now: not disabled, holding both admin and member (the
    member role is what `authorize` checks first)."""
    admin_ids = sa.select(UserRole.user_id).where(UserRole.role == GROUP_ADMIN)
    member_ids = sa.select(UserRole.user_id).where(UserRole.role == GROUP_MEMBER)
    stmt = sa.select(User.id).where(User.id.in_(admin_ids), User.id.in_(member_ids), User.disabled_at.is_(None))
    return set(session.scalars(stmt))


def _ensure_another_admin_remains(session: Session, user: User) -> None:
    _lock_admin_roles(session)
    admins = _effective_admin_ids(session)
    if admins == {user.id}:
        raise InvalidState(f"{user.display_name} is the last Ganymede admin. Make someone else an admin first.")


def _insert_role(session: Session, *, user: User, role: str, granted_by_id: uuid.UUID | None) -> bool:
    """Insert the role unless it's already held; `True` if a row was added. `ON CONFLICT` rather than
    a pre-check, so a double-submitted form can't fail on the unique constraint."""
    stmt = (
        pg_insert(UserRole)
        .values(id=uuid.uuid4(), user_id=user.id, role=role, granted_by_id=granted_by_id)
        .on_conflict_do_nothing(constraint="uq_user_roles_user_id_role")
        .returning(UserRole.id)
    )
    return session.execute(stmt).scalar_one_or_none() is not None


def grant_role(session: Session, actor: Actor, *, user: User, role: str, reason: str | None = None) -> bool:
    """Give `user` a role. `False` (and no audit event) if they already had it."""
    _require_admin(actor)
    role = _validate_role(role)
    added = _insert_role(session, user=user, role=role, granted_by_id=actor.user.id)
    if added:
        audit.record(
            session,
            actor,
            AUDIT_ROLE_GRANT,
            payload={"user_id": str(user.id), "role": role},
            reason=_clean_reason(reason),
        )
    return added


def revoke_role(session: Session, actor: Actor, *, user: User, role: str, reason: str | None = None) -> bool:
    """Take a role away from `user`. `False` (and no audit event) if they didn't have it.

    Refuses (`InvalidState`) to remove the actor's own admin or member role, or to leave Krater without
    an admin who can still act.
    """
    _require_admin(actor)
    role = _validate_role(role)
    if role in (GROUP_ADMIN, GROUP_MEMBER):
        if user.id == actor.user.id:
            raise InvalidState(f"You can't remove your own {role} role. Ask another admin.")
        _ensure_another_admin_remains(session, user)

    result = session.execute(sa.delete(UserRole).where(UserRole.user_id == user.id, UserRole.role == role))
    removed = result.rowcount > 0
    if removed:
        audit.record(
            session,
            actor,
            AUDIT_ROLE_REVOKE,
            payload={"user_id": str(user.id), "role": role},
            reason=_clean_reason(reason),
        )
    return removed


@dataclass(frozen=True)
class EmailGrantResult:
    """What `grant_role_by_email` did: granted straight to an existing user, or left a pending grant."""

    email: str
    role: str
    user: User | None  # set when granted directly
    created: bool  # False if the user already had it / the same grant was already pending


def grant_role_by_email(
    session: Session, actor: Actor, *, email: str, role: str, reason: str | None = None
) -> EmailGrantResult:
    """Grant `role` to whoever signs in with `email`.

    If exactly one existing user has that email, verified at their last sign-in, they get the role now.
    Otherwise it waits in `pending_role_grants` and is applied the next time someone signs in with that
    email and Weave says it's verified (`apply_pending_grants`).
    """
    _require_admin(actor)
    role = _validate_role(role)
    normalized = _normalize_email(email)

    matches = list(
        session.scalars(sa.select(User).where(sa.func.lower(User.email) == normalized, User.email_verified.is_(True)))
    )
    if len(matches) == 1:
        user = matches[0]
        added = grant_role(session, actor, user=user, role=role, reason=reason)
        return EmailGrantResult(email=normalized, role=role, user=user, created=added)

    stmt = (
        pg_insert(PendingRoleGrant)
        .values(id=uuid.uuid4(), email=normalized, role=role, granted_by_id=actor.user.id)
        .on_conflict_do_nothing(constraint="uq_pending_role_grants_email_role")
        .returning(PendingRoleGrant.id)
    )
    created = session.execute(stmt).scalar_one_or_none() is not None
    if created:
        audit.record(
            session,
            actor,
            AUDIT_PENDING_ROLE_GRANT,
            payload={"email": normalized, "role": role},
            reason=_clean_reason(reason),
        )
    return EmailGrantResult(email=normalized, role=role, user=None, created=created)


def cancel_pending_grant(session: Session, actor: Actor, *, grant_id: uuid.UUID, reason: str | None = None) -> None:
    _require_admin(actor)
    grant = session.get(PendingRoleGrant, grant_id)
    if grant is None:
        raise NotFound(f"No pending grant with id {grant_id}.")
    audit.record(
        session,
        actor,
        AUDIT_PENDING_ROLE_CANCEL,
        payload={"email": grant.email, "role": grant.role},
        reason=_clean_reason(reason),
    )
    session.delete(grant)
    session.flush()


def _require_reason(reason: str | None) -> str:
    cleaned = _clean_reason(reason)
    if cleaned is None:
        raise ValidationFailed({"reason": "A reason is required."})
    return cleaned


def disable_user(session: Session, actor: Actor, *, user: User, reason: str) -> None:
    """Disable `user`'s account: they can't sign in, and every authorization check refuses them.

    Refuses to disable the actor themselves or the last admin who can still act.
    """
    _require_admin(actor)
    cleaned = _require_reason(reason)
    if user.id == actor.user.id:
        raise InvalidState("You can't disable your own account.")
    if user.is_disabled:
        raise InvalidState(f"{user.display_name} is already disabled.")
    _ensure_another_admin_remains(session, user)

    user.disabled_at = datetime.now(UTC)
    session.flush()
    audit.record(session, actor, AUDIT_USER_DISABLE, payload={"user_id": str(user.id)}, reason=cleaned)


def enable_user(session: Session, actor: Actor, *, user: User, reason: str) -> None:
    _require_admin(actor)
    cleaned = _require_reason(reason)
    if not user.is_disabled:
        raise InvalidState(f"{user.display_name} isn't disabled.")

    user.disabled_at = None
    session.flush()
    audit.record(session, actor, AUDIT_USER_ENABLE, payload={"user_id": str(user.id)}, reason=cleaned)


def set_slack_user_id(
    session: Session, actor: Actor, *, user: User, slack_user_id: str | None, reason: str | None = None
) -> None:
    """Set (or, with a blank value, clear) the Slack account Krater uses for `user`: for someone whose
    Slack email differs from their Weave one, or to fix a wrong automatic match."""
    _require_admin(actor)
    new_id = (slack_user_id or "").strip().upper() or None
    if new_id is not None:
        if not _SLACK_USER_ID_RE.match(new_id):
            raise ValidationFailed({"slack_user_id": "Slack user ids look like U0123ABCD."})
        holder = session.scalars(sa.select(User).where(User.slack_user_id == new_id, User.id != user.id)).first()
        if holder is not None:
            raise ValidationFailed({"slack_user_id": f"{new_id} is already linked to {holder.display_name}."})

    previous = user.slack_user_id
    if previous == new_id:
        return
    user.slack_user_id = new_id
    session.flush()
    audit.record(
        session,
        actor,
        AUDIT_SLACK_USER_ID_SET,
        payload={"user_id": str(user.id), "previous": previous, "slack_user_id": new_id},
        reason=_clean_reason(reason),
    )


# --------------------------------------------------------------------------------------------------
# Sign-in hooks (no human actor)
# --------------------------------------------------------------------------------------------------


def _system_grant(
    session: Session, *, user: User, role: str, reason: str, granted_by_id: uuid.UUID | None = None
) -> bool:
    added = _insert_role(session, user=user, role=role, granted_by_id=granted_by_id)
    if added:
        audit.record(session, None, AUDIT_ROLE_GRANT, payload={"user_id": str(user.id), "role": role}, reason=reason)
    return added


def parse_bootstrap_admins(raw: str) -> list[str]:
    """`KRATER_BOOTSTRAP_ADMINS`, split on commas: each entry a Weave sub or an email address."""
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def is_bootstrap_admin(user: User, entries: Iterable[str]) -> bool:
    """Whether `user` matches an entry: by Weave sub, or by email when Weave verified that email."""
    for entry in entries:
        if "@" in entry:
            if user.email_verified and entry.lower() == user.email.strip().lower():
                return True
        elif entry.upper() == user.weave_sub.upper():
            return True
    return False


def apply_bootstrap_admin(session: Session, user: User, *, entries: Iterable[str]) -> list[str]:
    """Give a bootstrap admin `ganymede:admin` and `ganymede:member` if they're missing. Returns the
    roles added. Runs at every sign-in, so revoking a bootstrap admin only sticks once they're removed
    from `KRATER_BOOTSTRAP_ADMINS`."""
    if not is_bootstrap_admin(user, entries):
        return []
    return [
        role
        for role in (GROUP_MEMBER, GROUP_ADMIN)
        if _system_grant(session, user=user, role=role, reason=BOOTSTRAP_REASON)
    ]


def apply_pending_grants(session: Session, user: User) -> list[str]:
    """Turn every pending grant for `user`'s email into a real role, then delete them. Only when the
    email is verified: otherwise anyone could claim someone else's grant by signing up with their email.
    Returns the roles added."""
    if not user.email_verified:
        return []
    email = user.email.strip().lower()
    grants = list(session.scalars(sa.select(PendingRoleGrant).where(PendingRoleGrant.email == email)))
    added: list[str] = []
    for grant in grants:
        if _insert_role(session, user=user, role=grant.role, granted_by_id=grant.granted_by_id):
            added.append(grant.role)
        session.add(
            AuditEvent(
                actor_id=None,
                action=AUDIT_PENDING_ROLE_APPLY,
                payload={
                    "user_id": str(user.id),
                    "email": email,
                    "role": grant.role,
                    "granted_by_id": str(grant.granted_by_id) if grant.granted_by_id else None,
                },
            )
        )
        session.delete(grant)
    session.flush()
    return added


def seed_roles(session: Session, user: User, roles: Iterable[str], *, reason: str) -> list[str]:
    """Grant each of `roles` that `user` is missing. Used only in stub mode, to turn the dev fixture's
    `groups` into Krater roles; never with a live Weave."""
    return [
        role for role in roles if role in KNOWN_ROLES and _system_grant(session, user=user, role=role, reason=reason)
    ]


__all__ = [
    "AUDIT_PENDING_ROLE_APPLY",
    "AUDIT_PENDING_ROLE_CANCEL",
    "AUDIT_PENDING_ROLE_GRANT",
    "AUDIT_ROLE_GRANT",
    "AUDIT_ROLE_REVOKE",
    "AUDIT_SLACK_USER_ID_SET",
    "AUDIT_USER_DISABLE",
    "AUDIT_USER_ENABLE",
    "BOOTSTRAP_REASON",
    "KNOWN_ROLES",
    "EmailGrantResult",
    "UserWithRoles",
    "active_users_with_role",
    "actor_for",
    "apply_bootstrap_admin",
    "apply_pending_grants",
    "authorize",
    "cancel_pending_grant",
    "disable_user",
    "enable_user",
    "get_user",
    "grant_role",
    "grant_role_by_email",
    "is_bootstrap_admin",
    "list_pending_grants",
    "list_users_with_roles",
    "parse_bootstrap_admins",
    "revoke_role",
    "roles_by_user",
    "roles_for",
    "seed_roles",
    "set_slack_user_id",
]
