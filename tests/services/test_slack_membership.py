"""The interim Slack membership gate (`docs/SPEC.md` "Roles & authentication")."""

from __future__ import annotations

from sqlalchemy.orm import Session

from krater.models import User
from krater.services.slack_membership import is_full_slack_member
from krater.slack.fake import FakeSlackClient


def test_known_slack_id_defaults_to_a_full_member(db_session: Session, make_user) -> None:
    user: User = make_user()
    user.slack_user_id = "U0001"
    slack_client = FakeSlackClient()

    assert is_full_slack_member(db_session, slack_client, user) is True


def test_no_slack_link_and_no_email_match_fails(db_session: Session, make_user) -> None:
    user: User = make_user(email="nobody@example.com")
    slack_client = FakeSlackClient()

    assert is_full_slack_member(db_session, slack_client, user) is False


def test_resolved_by_email_lookup_passes_and_caches_the_id(db_session: Session, make_user) -> None:
    user: User = make_user(email="found@example.com")
    slack_client = FakeSlackClient()
    slack_client.register_email("found@example.com", "U0999")

    assert is_full_slack_member(db_session, slack_client, user) is True
    assert user.slack_user_id == "U0999"


def test_restricted_guest_fails(db_session: Session, make_user) -> None:
    user: User = make_user()
    user.slack_user_id = "U0002"
    slack_client = FakeSlackClient()
    slack_client.set_user_info("U0002", is_restricted=True)

    assert is_full_slack_member(db_session, slack_client, user) is False


def test_ultra_restricted_guest_fails(db_session: Session, make_user) -> None:
    user: User = make_user()
    user.slack_user_id = "U0003"
    slack_client = FakeSlackClient()
    slack_client.set_user_info("U0003", is_ultra_restricted=True)

    assert is_full_slack_member(db_session, slack_client, user) is False


def test_deleted_account_fails(db_session: Session, make_user) -> None:
    user: User = make_user()
    user.slack_user_id = "U0004"
    slack_client = FakeSlackClient()
    slack_client.set_user_info("U0004", deleted=True)

    assert is_full_slack_member(db_session, slack_client, user) is False


def test_slack_id_unknown_to_slack_fails(db_session: Session, make_user) -> None:
    user: User = make_user()
    user.slack_user_id = "UGHOST"
    slack_client = FakeSlackClient()
    slack_client.remove_user_info("UGHOST")

    assert is_full_slack_member(db_session, slack_client, user) is False
