"""`krater.services.users.sign_in`: upserting the user from the OIDC identity, then refusing disabled
users, applying stub seeds, bootstrap admins and pending grants, and refusing non-members."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from krater.models import AuditEvent, PendingRoleGrant, User, UserRole
from krater.services import roles
from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER
from krater.services.users import STUB_SEED_REASON, StubSeed, sign_in, upsert_user_from_identity
from krater.weave.types import WeaveIdentity


def _identity(
    sub: str = "PWLSIGNIN01", *, email: str = "signin@example.com", verified: bool = True, name: str = "Sig Nin"
) -> WeaveIdentity:
    return WeaveIdentity(sub=sub, name=name, email=email, email_verified=verified)


def _roles(db_session: Session, user: User) -> frozenset[str]:
    return roles.roles_for(db_session, user)


def test_upsert_creates_then_updates_the_same_user_without_touching_the_slack_link(db_session: Session) -> None:
    created = upsert_user_from_identity(db_session, _identity(name="Original", email="original@example.com"))
    created.slack_user_id = "U12345"
    db_session.flush()

    updated = upsert_user_from_identity(
        db_session, _identity(name="Updated", email="updated@example.com", verified=False)
    )

    assert updated.id == created.id
    assert updated.display_name == "Updated"
    assert updated.email == "updated@example.com"
    assert updated.email_verified is False
    assert updated.slack_user_id == "U12345"
    rows = db_session.execute(select(User).where(User.weave_sub == "PWLSIGNIN01")).scalars().all()
    assert len(rows) == 1


def test_a_member_signs_in_and_last_login_is_stamped(db_session: Session) -> None:
    user = upsert_user_from_identity(db_session, _identity())
    db_session.add(UserRole(user_id=user.id, role=GROUP_MEMBER))
    db_session.flush()

    result = sign_in(db_session, _identity())

    assert result.status == "ok"
    assert result.user.id == user.id
    assert result.user.last_login_at is not None


def test_someone_without_the_member_role_is_refused_but_their_row_is_kept(db_session: Session) -> None:
    result = sign_in(db_session, _identity())

    assert result.status == "not_a_member"
    assert result.user.last_login_at is None
    assert db_session.get(User, result.user.id) is not None


def test_a_disabled_user_is_refused_before_any_grant_is_applied(db_session: Session) -> None:
    user = upsert_user_from_identity(db_session, _identity())
    user.disabled_at = datetime.now(UTC)
    db_session.add(PendingRoleGrant(email="signin@example.com", role=GROUP_MEMBER))
    db_session.flush()

    result = sign_in(db_session, _identity(), bootstrap_admins=["PWLSIGNIN01"])

    assert result.status == "disabled"
    assert _roles(db_session, user) == frozenset()
    assert db_session.scalars(select(PendingRoleGrant)).all() != []


def test_pending_grants_are_applied_for_a_verified_email_then_deleted_and_audited(
    db_session: Session, make_user
) -> None:
    granter = make_user(display_name="Granter")
    db_session.add(PendingRoleGrant(email="signin@example.com", role=GROUP_MEMBER, granted_by_id=granter.id))
    db_session.add(PendingRoleGrant(email="signin@example.com", role=GROUP_REVIEWER, granted_by_id=granter.id))
    db_session.add(PendingRoleGrant(email="someone-else@example.com", role=GROUP_MEMBER))
    db_session.flush()

    result = sign_in(db_session, _identity(email="SignIn@Example.com"))

    assert result.status == "ok"
    assert _roles(db_session, result.user) == frozenset({GROUP_MEMBER, GROUP_REVIEWER})
    remaining = db_session.scalars(select(PendingRoleGrant.email)).all()
    assert remaining == ["someone-else@example.com"]
    role_rows = db_session.scalars(select(UserRole).where(UserRole.user_id == result.user.id)).all()
    assert {row.granted_by_id for row in role_rows} == {granter.id}
    applied = db_session.scalars(select(AuditEvent).where(AuditEvent.action == roles.AUDIT_PENDING_ROLE_APPLY)).all()
    assert sorted(event.payload["role"] for event in applied) == [GROUP_MEMBER, GROUP_REVIEWER]
    assert all(event.actor_id is None for event in applied)


def test_pending_grants_are_not_applied_for_an_unverified_email(db_session: Session) -> None:
    db_session.add(PendingRoleGrant(email="signin@example.com", role=GROUP_MEMBER))
    db_session.flush()

    result = sign_in(db_session, _identity(verified=False))

    assert result.status == "not_a_member"
    assert len(db_session.scalars(select(PendingRoleGrant)).all()) == 1


@pytest.mark.parametrize("entry", ["PWLSIGNIN01", "pwlsignin01", "SIGNIN@example.com"])
def test_a_bootstrap_admin_gets_admin_and_member(db_session: Session, entry: str) -> None:
    result = sign_in(db_session, _identity(), bootstrap_admins=["someone@else.example", entry])

    assert result.status == "ok"
    assert _roles(db_session, result.user) == frozenset({GROUP_MEMBER, GROUP_ADMIN})
    grants = db_session.scalars(select(AuditEvent).where(AuditEvent.action == roles.AUDIT_ROLE_GRANT)).all()
    assert {event.payload["role"] for event in grants} == {GROUP_MEMBER, GROUP_ADMIN}
    assert all(event.actor_id is None and event.reason == roles.BOOTSTRAP_REASON for event in grants)


def test_a_bootstrap_email_needs_a_verified_email(db_session: Session) -> None:
    result = sign_in(db_session, _identity(verified=False), bootstrap_admins=["signin@example.com"])

    assert result.status == "not_a_member"
    assert _roles(db_session, result.user) == frozenset()


def test_bootstrap_only_adds_what_is_missing(db_session: Session) -> None:
    sign_in(db_session, _identity(), bootstrap_admins=["PWLSIGNIN01"])
    sign_in(db_session, _identity(), bootstrap_admins=["PWLSIGNIN01"])

    grants = db_session.scalars(select(AuditEvent).where(AuditEvent.action == roles.AUDIT_ROLE_GRANT)).all()
    assert len(grants) == 2


def test_a_stub_seed_adds_roles_and_the_slack_id(db_session: Session) -> None:
    seed = StubSeed(groups=frozenset({GROUP_MEMBER, GROUP_REVIEWER, "weave:something-else"}), slack_id="U0SEED")

    result = sign_in(db_session, _identity(), stub_seed=seed)

    assert result.status == "ok"
    assert _roles(db_session, result.user) == frozenset({GROUP_MEMBER, GROUP_REVIEWER})
    assert result.user.slack_user_id == "U0SEED"
    grants = db_session.scalars(select(AuditEvent).where(AuditEvent.action == roles.AUDIT_ROLE_GRANT)).all()
    assert {event.reason for event in grants} == {STUB_SEED_REASON}


def test_a_stub_seed_never_steals_a_slack_id_linked_to_someone_else(db_session: Session, make_user) -> None:
    make_user(slack_user_id="U0SEED")

    result = sign_in(db_session, _identity(), stub_seed=StubSeed(groups=frozenset({GROUP_MEMBER}), slack_id="U0SEED"))

    assert result.user.slack_user_id is None
