"""`StubWeaveClient`: the bundled fixture, and the exchange/lookup behavior other tests rely on."""

from __future__ import annotations

import pytest

from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER
from krater.weave.errors import WeaveAuthError
from krater.weave.stub import StubWeaveClient


@pytest.fixture
def stub_client() -> StubWeaveClient:
    return StubWeaveClient()


def test_bundled_fixture_covers_every_role(stub_client: StubWeaveClient) -> None:
    users = stub_client.list_all_users()
    assert len(users) >= 6

    members = [u for u in users if GROUP_MEMBER in u.groups]
    reviewers = [u for u in users if GROUP_REVIEWER in u.groups]
    admins = [u for u in users if GROUP_ADMIN in u.groups]
    non_members = [u for u in users if not u.groups]
    inactive = [u for u in users if not u.active]

    assert len(members) >= 4  # plain members + reviewers + admin all carry ganymede:member
    assert len(reviewers) >= 2
    assert len(admins) >= 1
    assert len(non_members) >= 1
    assert len(inactive) >= 1


def test_exchange_code_treats_the_code_as_a_sub(stub_client: StubWeaveClient) -> None:
    member = next(u for u in stub_client.list_all_users() if u.groups == frozenset({GROUP_MEMBER}))

    identity = stub_client.exchange_code(
        code=member.sub, code_verifier="unused", redirect_uri="http://testserver/auth/callback", nonce="unused"
    )

    assert identity.sub == member.sub
    assert identity.name == member.name
    assert identity.email == member.email
    assert identity.groups == member.groups


def test_exchange_code_rejects_an_unknown_code(stub_client: StubWeaveClient) -> None:
    with pytest.raises(WeaveAuthError):
        stub_client.exchange_code(
            code="not-a-real-sub", code_verifier="v", redirect_uri="http://testserver/auth/callback", nonce="n"
        )


def test_get_user_returns_none_for_an_unknown_sub(stub_client: StubWeaveClient) -> None:
    assert stub_client.get_user("does-not-exist") is None


def test_get_user_by_slack_id_finds_a_linked_user_and_misses_otherwise(stub_client: StubWeaveClient) -> None:
    linked = next(u for u in stub_client.list_all_users() if u.slack_id is not None)

    assert stub_client.get_user_by_slack_id(linked.slack_id).sub == linked.sub  # type: ignore[union-attr]
    assert stub_client.get_user_by_slack_id("no-such-slack-id") is None


def test_list_users_in_group_filters_correctly(stub_client: StubWeaveClient) -> None:
    reviewers = stub_client.list_users_in_group(GROUP_REVIEWER)
    assert reviewers
    assert all(GROUP_REVIEWER in u.groups for u in reviewers)


def test_inactive_fixture_user_is_flagged(stub_client: StubWeaveClient) -> None:
    inactive_user = next(u for u in stub_client.list_all_users() if not u.active)
    assert stub_client.get_user(inactive_user.sub).active is False  # type: ignore[union-attr]
