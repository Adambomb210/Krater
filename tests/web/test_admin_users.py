"""HTTP-level tests for admin user management, `/admin/users`: every route's happy path, plus the
sign-in, admin-only and CSRF gates. The rules themselves are unit-tested in tests/services/test_roles.py."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from krater.models import AuditEvent, PendingRoleGrant, User, UserRole
from krater.services import roles
from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER
from tests.conftest import ADMIN_SUB, MEMBER_SUB, NON_MEMBER_SUB, REVIEWER_SUB, get_csrf_token


def _csrf(client: TestClient) -> str:
    return get_csrf_token(client.get("/").text)


def _post(client: TestClient, path: str, **data: str):
    return client.post(path, data={"csrf_token": _csrf(client), **data}, follow_redirects=False)


def _flash_after(client: TestClient, response) -> str:
    assert response.status_code == 303, response.text
    return client.get(response.headers["location"]).text


@pytest.fixture
def signed_in_admin(login_as, client: TestClient) -> User:
    return login_as(ADMIN_SUB)


@pytest.fixture
def some_member(login_as) -> User:
    """A signed-in-at-some-point member, created through the real stub flow (so the admin's own sign-in
    afterwards is the current session)."""
    return login_as(MEMBER_SUB)


# -- Gates, for every route --------------------------------------------------------------------------------

_POST_ROUTES = [
    "/admin/users/{user_id}/roles/grant",
    "/admin/users/{user_id}/roles/revoke",
    "/admin/users/{user_id}/disable",
    "/admin/users/{user_id}/enable",
    "/admin/users/{user_id}/slack-id",
    "/admin/pending-grants",
    "/admin/pending-grants/{grant_id}/cancel",
]


def _fill(path: str, user: User, grant_id: str = "00000000-0000-0000-0000-000000000000") -> str:
    return path.format(user_id=user.id, grant_id=grant_id)


@pytest.mark.parametrize("path", ["/admin/users", "/admin/users/{user_id}"])
def test_pages_require_sign_in(client: TestClient, login_as, path: str) -> None:
    member = login_as(MEMBER_SUB)
    client.cookies.clear()

    response = client.get(_fill(path, member), follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


@pytest.mark.parametrize("path", ["/admin/users", "/admin/users/{user_id}"])
def test_pages_are_admin_only(client: TestClient, login_as, path: str) -> None:
    reviewer = login_as(REVIEWER_SUB)

    assert client.get(_fill(path, reviewer)).status_code == 403


@pytest.mark.parametrize("path", _POST_ROUTES)
def test_posts_are_admin_only(client: TestClient, login_as, db_session: Session, path: str) -> None:
    member = login_as(MEMBER_SUB)
    login_as(REVIEWER_SUB)

    response = _post(client, _fill(path, member), role=GROUP_ADMIN, reason="r", email="x@example.com")

    assert response.status_code == 403
    assert roles.roles_for(db_session, member) == frozenset({GROUP_MEMBER})


@pytest.mark.parametrize("path", _POST_ROUTES)
def test_posts_require_a_csrf_token(client: TestClient, signed_in_admin: User, db_session: Session, path: str) -> None:
    response = client.post(
        _fill(path, signed_in_admin), data={"role": GROUP_REVIEWER, "reason": "r"}, follow_redirects=False
    )

    assert response.status_code == 403


def test_a_disabled_admin_is_refused(client: TestClient, login_as, db_session: Session) -> None:
    other_admin = login_as(REVIEWER_SUB)
    db_session.add(UserRole(user_id=other_admin.id, role=GROUP_ADMIN))
    db_session.flush()
    admin = login_as(ADMIN_SUB)
    assert client.get("/admin/users").status_code == 200

    roles.disable_user(db_session, roles.authorize(db_session, other_admin), user=admin, reason="test")
    db_session.flush()

    assert client.get("/admin/users").status_code == 403


# -- Happy paths -------------------------------------------------------------------------------------------


def test_users_page_lists_users_roles_and_pending_grants(
    client: TestClient, some_member: User, signed_in_admin: User, db_session: Session
) -> None:
    db_session.add(PendingRoleGrant(email="pending@example.com", role=GROUP_REVIEWER))
    db_session.flush()

    page = client.get("/admin/users")

    assert page.status_code == 200
    assert "Mia Member" in page.text
    assert "Ana Admin" in page.text
    assert GROUP_ADMIN in page.text
    assert "pending@example.com" in page.text


def test_user_page_shows_roles_and_history(client: TestClient, some_member: User, signed_in_admin: User) -> None:
    page = client.get(f"/admin/users/{some_member.id}")

    assert page.status_code == 200
    assert "Mia Member" in page.text
    assert some_member.weave_sub in page.text
    assert "role_grant" in page.text  # stub sign-in seeded the member role, audited


def test_user_page_404s_for_an_unknown_user(client: TestClient, signed_in_admin: User) -> None:
    assert client.get("/admin/users/00000000-0000-0000-0000-000000000000").status_code == 404


def test_grant_and_revoke_a_role(
    client: TestClient, some_member: User, signed_in_admin: User, db_session: Session
) -> None:
    text = _flash_after(
        client, _post(client, f"/admin/users/{some_member.id}/roles/grant", role=GROUP_REVIEWER, reason="trusted")
    )
    assert "Granted" in text
    assert roles.roles_for(db_session, some_member) == frozenset({GROUP_MEMBER, GROUP_REVIEWER})

    text = _flash_after(client, _post(client, f"/admin/users/{some_member.id}/roles/revoke", role=GROUP_REVIEWER))
    assert "Removed" in text
    assert roles.roles_for(db_session, some_member) == frozenset({GROUP_MEMBER})

    actions = db_session.scalars(
        select(AuditEvent.action).where(AuditEvent.actor_id == signed_in_admin.id).order_by(AuditEvent.action)
    ).all()
    assert actions == [roles.AUDIT_ROLE_GRANT, roles.AUDIT_ROLE_REVOKE]


def test_granting_an_unknown_role_is_refused_with_a_message(
    client: TestClient, some_member: User, signed_in_admin: User
) -> None:
    text = _flash_after(client, _post(client, f"/admin/users/{some_member.id}/roles/grant", role="weave:admin"))

    assert "Unknown role" in text


def test_an_admin_cannot_remove_their_own_admin_role(
    client: TestClient, signed_in_admin: User, db_session: Session
) -> None:
    text = _flash_after(client, _post(client, f"/admin/users/{signed_in_admin.id}/roles/revoke", role=GROUP_ADMIN))

    assert "your own" in text
    assert GROUP_ADMIN in roles.roles_for(db_session, signed_in_admin)


def test_disable_then_enable_a_user(
    client: TestClient, some_member: User, signed_in_admin: User, db_session: Session
) -> None:
    text = _flash_after(client, _post(client, f"/admin/users/{some_member.id}/disable", reason="left"))
    assert "Disabled Mia Member" in text
    assert some_member.is_disabled

    text = _flash_after(client, _post(client, f"/admin/users/{some_member.id}/enable", reason="back"))
    assert "Enabled Mia Member" in text
    assert not some_member.is_disabled


def test_disabling_without_a_reason_is_refused(client: TestClient, some_member: User, signed_in_admin: User) -> None:
    text = _flash_after(client, _post(client, f"/admin/users/{some_member.id}/disable", reason=""))

    assert "reason is required" in text
    assert not some_member.is_disabled


def test_an_admin_cannot_disable_themselves(client: TestClient, signed_in_admin: User) -> None:
    text = _flash_after(client, _post(client, f"/admin/users/{signed_in_admin.id}/disable", reason="oops"))

    assert "your own account" in text
    assert not signed_in_admin.is_disabled


def test_a_disabled_user_is_refused_on_their_next_request(client: TestClient, login_as, db_session: Session) -> None:
    admin = login_as(ADMIN_SUB)
    member = login_as(MEMBER_SUB)
    assert client.get("/projects/new").status_code == 200

    roles.disable_user(db_session, roles.authorize(db_session, admin), user=member, reason="test")
    db_session.flush()

    response = client.get("/projects/new")
    assert response.status_code == 403
    assert "disabled" in response.text


def test_set_and_clear_a_slack_user_id(
    client: TestClient, some_member: User, signed_in_admin: User, db_session: Session
) -> None:
    text = _flash_after(client, _post(client, f"/admin/users/{some_member.id}/slack-id", slack_user_id="u0777other"))
    assert "U0777OTHER" in text
    assert some_member.slack_user_id == "U0777OTHER"

    _flash_after(client, _post(client, f"/admin/users/{some_member.id}/slack-id", slack_user_id=""))
    assert some_member.slack_user_id is None


def test_a_malformed_slack_user_id_is_refused(client: TestClient, some_member: User, signed_in_admin: User) -> None:
    text = _flash_after(client, _post(client, f"/admin/users/{some_member.id}/slack-id", slack_user_id="nope"))

    assert "look like" in text
    assert some_member.slack_user_id == "U0001MEMBER"


def test_grant_by_email_for_someone_not_signed_in_then_cancel(
    client: TestClient, signed_in_admin: User, db_session: Session
) -> None:
    text = _flash_after(client, _post(client, "/admin/pending-grants", email="Newbie@Example.com", role=GROUP_MEMBER))
    assert "will be granted when newbie@example.com signs in" in text
    grant = db_session.scalars(select(PendingRoleGrant)).one()

    text = _flash_after(client, _post(client, f"/admin/pending-grants/{grant.id}/cancel"))
    assert "cancelled" in text
    assert db_session.scalars(select(PendingRoleGrant)).all() == []


def test_grant_by_email_for_a_signed_in_user_grants_right_away(
    client: TestClient, signed_in_admin: User, db_session: Session
) -> None:
    # Someone Weave signed in who was refused as a non-member: their row, with a verified email, is kept.
    nonmember = User(weave_sub=NON_MEMBER_SUB, display_name="Nico", email="Nico@example.com", email_verified=True)
    db_session.add(nonmember)
    db_session.flush()

    text = _flash_after(client, _post(client, "/admin/pending-grants", email="nico@example.com", role=GROUP_MEMBER))

    assert "has already signed in" in text
    assert roles.roles_for(db_session, nonmember) == frozenset({GROUP_MEMBER})


def test_grant_by_email_rejects_a_bad_address(client: TestClient, signed_in_admin: User) -> None:
    text = _flash_after(client, _post(client, "/admin/pending-grants", email="not-an-email", role=GROUP_MEMBER))

    assert "valid email" in text


def test_cancelling_an_unknown_pending_grant_404s(client: TestClient, signed_in_admin: User) -> None:
    response = _post(client, "/admin/pending-grants/00000000-0000-0000-0000-000000000000/cancel")

    assert response.status_code == 404
