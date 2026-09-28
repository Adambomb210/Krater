"""`krater.services.roles`: reading roles, the admin actions that change roles and accounts (each
audited), and their guard rails."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from krater.models import AuditEvent, PendingRoleGrant, UserRole
from krater.services import roles
from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER, Actor
from krater.services.errors import (
    AccountDisabled,
    InvalidState,
    NotAllowed,
    NotAMember,
    NotFound,
    ValidationFailed,
)


def _events(db_session: Session, action: str) -> list[AuditEvent]:
    return list(db_session.scalars(select(AuditEvent).where(AuditEvent.action == action)))


# -- Reading ---------------------------------------------------------------------------------------


def test_roles_for_and_roles_by_user_read_the_table(db_session: Session, reviewer: Actor, make_user) -> None:
    nobody = make_user()

    assert roles.roles_for(db_session, reviewer.user) == frozenset({GROUP_MEMBER, GROUP_REVIEWER})
    assert roles.roles_by_user(db_session, [reviewer.user.id, nobody.id]) == {
        reviewer.user.id: frozenset({GROUP_MEMBER, GROUP_REVIEWER}),
        nobody.id: frozenset(),
    }


def test_authorize_returns_an_actor_with_current_roles(db_session: Session, reviewer: Actor) -> None:
    actor = roles.authorize(db_session, reviewer.user)

    assert actor.is_member and actor.is_reviewer and not actor.is_admin


def test_authorize_refuses_a_disabled_user_first(db_session: Session, admin: Actor) -> None:
    admin.user.disabled_at = datetime.now(UTC)

    with pytest.raises(AccountDisabled):
        roles.authorize(db_session, admin.user)


def test_authorize_refuses_a_non_member(db_session: Session, make_actor) -> None:
    reviewer_only = make_actor(groups=frozenset({GROUP_REVIEWER}))

    with pytest.raises(NotAMember):
        roles.authorize(db_session, reviewer_only.user)


def test_active_users_with_role_skips_disabled_users(db_session: Session, reviewer: Actor, make_actor) -> None:
    disabled = make_actor(groups=frozenset({GROUP_MEMBER, GROUP_REVIEWER}))
    disabled.user.disabled_at = datetime.now(UTC)
    make_actor(groups=frozenset({GROUP_MEMBER}))

    assert roles.active_users_with_role(db_session, GROUP_REVIEWER) == [reviewer.user]


def test_list_users_with_roles_puts_disabled_users_last(db_session: Session, make_actor) -> None:
    disabled = make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Aaron Disabled")
    disabled.user.disabled_at = datetime.now(UTC)
    active = make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Zed Active")

    listed = [entry.user for entry in roles.list_users_with_roles(db_session)]

    assert listed.index(active.user) < listed.index(disabled.user)


# -- Grant / revoke ----------------------------------------------------------------------------------


def test_grant_role_adds_it_once_and_audits(db_session: Session, admin: Actor, member: Actor) -> None:
    assert roles.grant_role(db_session, admin, user=member.user, role=GROUP_REVIEWER, reason="trusted") is True
    assert roles.grant_role(db_session, admin, user=member.user, role=GROUP_REVIEWER) is False

    assert roles.roles_for(db_session, member.user) == frozenset({GROUP_MEMBER, GROUP_REVIEWER})
    row = db_session.scalars(
        select(UserRole).where(UserRole.user_id == member.user.id, UserRole.role == GROUP_REVIEWER)
    ).one()
    assert row.granted_by_id == admin.user.id
    (event,) = _events(db_session, roles.AUDIT_ROLE_GRANT)
    assert event.actor_id == admin.user.id
    assert event.payload == {"user_id": str(member.user.id), "role": GROUP_REVIEWER}
    assert event.reason == "trusted"


def test_grant_role_rejects_an_unknown_role(db_session: Session, admin: Actor, member: Actor) -> None:
    with pytest.raises(ValidationFailed) as exc_info:
        roles.grant_role(db_session, admin, user=member.user, role="weave:admin")
    assert "role" in exc_info.value.errors


@pytest.mark.parametrize(
    "action",
    [
        lambda s, a, u: roles.grant_role(s, a, user=u, role=GROUP_REVIEWER),
        lambda s, a, u: roles.revoke_role(s, a, user=u, role=GROUP_MEMBER),
        lambda s, a, u: roles.grant_role_by_email(s, a, email="x@example.com", role=GROUP_MEMBER),
        lambda s, a, u: roles.disable_user(s, a, user=u, reason="r"),
        lambda s, a, u: roles.enable_user(s, a, user=u, reason="r"),
        lambda s, a, u: roles.set_slack_user_id(s, a, user=u, slack_user_id="U123"),
    ],
    ids=["grant", "revoke", "grant-by-email", "disable", "enable", "slack-id"],
)
def test_every_admin_action_needs_an_admin(db_session: Session, reviewer: Actor, member: Actor, action) -> None:
    with pytest.raises(NotAllowed):
        action(db_session, reviewer, member.user)


def test_revoke_role_removes_it_and_audits(db_session: Session, admin: Actor, reviewer: Actor) -> None:
    assert roles.revoke_role(db_session, admin, user=reviewer.user, role=GROUP_REVIEWER, reason="stepped down")
    assert roles.revoke_role(db_session, admin, user=reviewer.user, role=GROUP_REVIEWER) is False

    assert roles.roles_for(db_session, reviewer.user) == frozenset({GROUP_MEMBER})
    (event,) = _events(db_session, roles.AUDIT_ROLE_REVOKE)
    assert event.payload == {"user_id": str(reviewer.user.id), "role": GROUP_REVIEWER}
    assert event.reason == "stepped down"


@pytest.mark.parametrize("role", [GROUP_ADMIN, GROUP_MEMBER])
def test_an_admin_cannot_remove_their_own_admin_or_member_role(
    db_session: Session, admin: Actor, make_actor, role: str
) -> None:
    make_actor(groups=frozenset({GROUP_MEMBER, GROUP_ADMIN}))  # another admin exists, so this is about self

    with pytest.raises(InvalidState, match="your own"):
        roles.revoke_role(db_session, admin, user=admin.user, role=role)


def test_an_admin_can_remove_their_own_reviewer_role(db_session: Session, make_actor) -> None:
    admin = make_actor(groups=frozenset({GROUP_MEMBER, GROUP_REVIEWER, GROUP_ADMIN}))

    assert roles.revoke_role(db_session, admin, user=admin.user, role=GROUP_REVIEWER) is True


@pytest.mark.parametrize("role", [GROUP_ADMIN, GROUP_MEMBER])
def test_the_last_admin_cannot_be_removed(db_session: Session, make_actor, role: str) -> None:
    # The acting admin is disabled-in-waiting: only `target` can still act, so removing either of its
    # roles would leave Krater with no admin at all.
    target = make_actor(groups=frozenset({GROUP_MEMBER, GROUP_ADMIN}))
    acting = make_actor(groups=frozenset({GROUP_MEMBER, GROUP_ADMIN}))
    acting.user.disabled_at = datetime.now(UTC)
    db_session.flush()

    with pytest.raises(InvalidState, match="last Ganymede admin"):
        roles.revoke_role(db_session, acting, user=target.user, role=role)


def test_an_admin_can_be_removed_while_another_remains(db_session: Session, admin: Actor, make_actor) -> None:
    other = make_actor(groups=frozenset({GROUP_MEMBER, GROUP_ADMIN}))

    assert roles.revoke_role(db_session, admin, user=other.user, role=GROUP_ADMIN) is True


# -- Grant by email ------------------------------------------------------------------------------------


def test_grant_by_email_leaves_a_pending_grant_for_someone_not_signed_in(db_session: Session, admin: Actor) -> None:
    result = roles.grant_role_by_email(db_session, admin, email="  New.Person@Example.COM ", role=GROUP_MEMBER)
    again = roles.grant_role_by_email(db_session, admin, email="new.person@example.com", role=GROUP_MEMBER)

    assert result.user is None and result.created is True
    assert again.created is False
    (grant,) = db_session.scalars(select(PendingRoleGrant)).all()
    assert grant.email == "new.person@example.com"
    assert grant.granted_by_id == admin.user.id
    (event,) = _events(db_session, roles.AUDIT_PENDING_ROLE_GRANT)
    assert event.payload == {"email": "new.person@example.com", "role": GROUP_MEMBER}


def test_grant_by_email_grants_directly_to_a_signed_in_verified_user(
    db_session: Session, admin: Actor, make_user
) -> None:
    user = make_user(email="Known@example.com")

    result = roles.grant_role_by_email(db_session, admin, email="known@example.com", role=GROUP_REVIEWER)

    assert result.user is user
    assert roles.roles_for(db_session, user) == frozenset({GROUP_REVIEWER})
    assert db_session.scalars(select(PendingRoleGrant)).all() == []


def test_grant_by_email_does_not_trust_an_unverified_match(db_session: Session, admin: Actor, make_user) -> None:
    user = make_user(email="claimed@example.com", email_verified=False)

    result = roles.grant_role_by_email(db_session, admin, email="claimed@example.com", role=GROUP_MEMBER)

    assert result.user is None
    assert roles.roles_for(db_session, user) == frozenset()
    assert len(db_session.scalars(select(PendingRoleGrant)).all()) == 1


@pytest.mark.parametrize("email", ["", "not-an-email", "two@@example.com", "spaces in@example.com"])
def test_grant_by_email_validates_the_address(db_session: Session, admin: Actor, email: str) -> None:
    with pytest.raises(ValidationFailed) as exc_info:
        roles.grant_role_by_email(db_session, admin, email=email, role=GROUP_MEMBER)
    assert "email" in exc_info.value.errors


def test_cancel_pending_grant_deletes_and_audits(db_session: Session, admin: Actor) -> None:
    roles.grant_role_by_email(db_session, admin, email="gone@example.com", role=GROUP_MEMBER)
    grant = db_session.scalars(select(PendingRoleGrant)).one()

    roles.cancel_pending_grant(db_session, admin, grant_id=grant.id, reason="typo")

    assert db_session.scalars(select(PendingRoleGrant)).all() == []
    (event,) = _events(db_session, roles.AUDIT_PENDING_ROLE_CANCEL)
    assert event.reason == "typo"
    with pytest.raises(NotFound):
        roles.cancel_pending_grant(db_session, admin, grant_id=grant.id)


# -- Disable / enable ------------------------------------------------------------------------------------


def test_disable_and_enable_need_a_reason_and_are_audited(db_session: Session, admin: Actor, member: Actor) -> None:
    with pytest.raises(ValidationFailed):
        roles.disable_user(db_session, admin, user=member.user, reason="  ")

    roles.disable_user(db_session, admin, user=member.user, reason="left the program")
    assert member.user.is_disabled
    with pytest.raises(InvalidState):
        roles.disable_user(db_session, admin, user=member.user, reason="again")

    with pytest.raises(ValidationFailed):
        roles.enable_user(db_session, admin, user=member.user, reason="")
    roles.enable_user(db_session, admin, user=member.user, reason="back")
    assert not member.user.is_disabled
    with pytest.raises(InvalidState):
        roles.enable_user(db_session, admin, user=member.user, reason="again")

    (disabled,) = _events(db_session, roles.AUDIT_USER_DISABLE)
    (enabled,) = _events(db_session, roles.AUDIT_USER_ENABLE)
    assert disabled.reason == "left the program" and disabled.payload == {"user_id": str(member.user.id)}
    assert enabled.reason == "back"


def test_an_admin_cannot_disable_themselves(db_session: Session, admin: Actor, make_actor) -> None:
    make_actor(groups=frozenset({GROUP_MEMBER, GROUP_ADMIN}))

    with pytest.raises(InvalidState, match="your own"):
        roles.disable_user(db_session, admin, user=admin.user, reason="r")


def test_the_last_admin_cannot_be_disabled(db_session: Session, make_actor) -> None:
    target = make_actor(groups=frozenset({GROUP_MEMBER, GROUP_ADMIN}))
    acting = make_actor(groups=frozenset({GROUP_MEMBER, GROUP_ADMIN}))
    acting.user.disabled_at = datetime.now(UTC)
    db_session.flush()

    with pytest.raises(InvalidState, match="last Ganymede admin"):
        roles.disable_user(db_session, acting, user=target.user, reason="r")


def test_another_admin_can_be_disabled(db_session: Session, admin: Actor, make_actor) -> None:
    other = make_actor(groups=frozenset({GROUP_MEMBER, GROUP_ADMIN}))

    roles.disable_user(db_session, admin, user=other.user, reason="compromised")

    assert other.user.is_disabled


# -- Slack user id ---------------------------------------------------------------------------------------


def test_set_slack_user_id_sets_normalizes_clears_and_audits(db_session: Session, admin: Actor, member: Actor) -> None:
    roles.set_slack_user_id(db_session, admin, user=member.user, slack_user_id=" u0123abcd ", reason="other email")
    assert member.user.slack_user_id == "U0123ABCD"

    roles.set_slack_user_id(db_session, admin, user=member.user, slack_user_id="U0123ABCD")  # no change, no event
    roles.set_slack_user_id(db_session, admin, user=member.user, slack_user_id="")
    assert member.user.slack_user_id is None

    events = _events(db_session, roles.AUDIT_SLACK_USER_ID_SET)
    assert {event.payload["slack_user_id"] for event in events} == {"U0123ABCD", None}
    assert len(events) == 2


@pytest.mark.parametrize("value", ["not-an-id", "C0123ABCD", "U"])
def test_set_slack_user_id_rejects_malformed_ids(db_session: Session, admin: Actor, member: Actor, value: str) -> None:
    with pytest.raises(ValidationFailed):
        roles.set_slack_user_id(db_session, admin, user=member.user, slack_user_id=value)


def test_set_slack_user_id_rejects_an_id_linked_to_someone_else(
    db_session: Session, admin: Actor, member: Actor, make_user
) -> None:
    make_user(display_name="Holder", slack_user_id="UTAKEN1")

    with pytest.raises(ValidationFailed, match="Holder"):
        roles.set_slack_user_id(db_session, admin, user=member.user, slack_user_id="UTAKEN1")


# -- Bootstrap parsing ---------------------------------------------------------------------------------------


def test_parse_bootstrap_admins_splits_and_trims() -> None:
    assert roles.parse_bootstrap_admins(" PWL5A1B2C3D4, ada@example.com ,,") == ["PWL5A1B2C3D4", "ada@example.com"]
    assert roles.parse_bootstrap_admins("") == []
