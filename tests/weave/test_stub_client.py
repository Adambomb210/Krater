"""`StubWeaveClient`: the bundled fixture, and the exchange behavior other tests rely on."""

from __future__ import annotations

import pytest

from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER
from krater.weave.errors import WeaveAuthError
from krater.weave.stub import StubWeaveClient
from krater.weave.types import WeaveIdentity


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
    unverified = [u for u in users if not u.email_verified]

    assert len(members) >= 4  # plain members + reviewers + admin all carry ganymede:member
    assert len(reviewers) >= 2
    assert len(admins) >= 1
    assert len(non_members) >= 1
    assert len(unverified) >= 1


def test_exchange_code_returns_only_the_standard_oidc_identity(stub_client: StubWeaveClient) -> None:
    member = next(u for u in stub_client.list_all_users() if u.groups == frozenset({GROUP_MEMBER}))

    identity = stub_client.exchange_code(
        code=member.sub, code_verifier="unused", redirect_uri="http://testserver/auth/callback", nonce="unused"
    )

    assert identity == WeaveIdentity(sub=member.sub, name=member.name, email=member.email, email_verified=True)


def test_exchange_code_rejects_an_unknown_code(stub_client: StubWeaveClient) -> None:
    with pytest.raises(WeaveAuthError):
        stub_client.exchange_code(
            code="not-a-real-sub", code_verifier="v", redirect_uri="http://testserver/auth/callback", nonce="n"
        )


def test_stub_user_carries_the_dev_seed_and_misses_otherwise(stub_client: StubWeaveClient) -> None:
    linked = next(u for u in stub_client.list_all_users() if u.slack_id is not None)

    assert stub_client.stub_user(linked.sub) == linked
    assert stub_client.stub_user("does-not-exist") is None


def test_a_minimal_fixture_entry_defaults_to_verified_with_no_roles(tmp_path) -> None:
    fixture = tmp_path / "users.json"
    fixture.write_text('[{"sub": "PWLMIN", "name": "Min", "email": "min@example.com"}]')

    user = StubWeaveClient(fixture).stub_user("PWLMIN")

    assert user is not None
    assert user.email_verified is True
    assert user.slack_id is None
    assert user.groups == frozenset()
