"""`krater.weave.roles.RoleMapping`: everyone Weave lets use Krater is a member, the `roles` claim decides the
other roles, and group slugs only count when the `roles` field is absent."""

from __future__ import annotations

import pytest

from krater.config import Settings
from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER
from krater.weave.roles import RoleMapping


@pytest.fixture
def mapping() -> RoleMapping:
    return RoleMapping.default()


def test_role_keys_map_to_krater_role_names(mapping: RoleMapping) -> None:
    assert mapping.krater_roles(roles=["reviewer", "admin"], groups=[]) == frozenset(
        {GROUP_MEMBER, GROUP_REVIEWER, GROUP_ADMIN}
    )


def test_everyone_weave_returns_is_a_member_with_no_role(mapping: RoleMapping) -> None:
    assert mapping.krater_roles(roles=[], groups=[]) == frozenset({GROUP_MEMBER})


def test_unknown_role_keys_are_ignored(mapping: RoleMapping) -> None:
    # `member` is no Weave role any more: an app that still defines it gives nothing extra.
    assert mapping.krater_roles(roles=["member", "owner", 7], groups=[]) == frozenset({GROUP_MEMBER})


def test_the_roles_claim_wins_over_groups(mapping: RoleMapping) -> None:
    assert mapping.krater_roles(roles=["reviewer"], groups=["krater-admins"]) == frozenset(
        {GROUP_MEMBER, GROUP_REVIEWER}
    )


def test_an_empty_roles_claim_does_not_fall_back_to_groups(mapping: RoleMapping) -> None:
    assert mapping.krater_roles(roles=[], groups=["krater-reviewers", "krater-admins"]) == frozenset({GROUP_MEMBER})


def test_an_absent_roles_claim_falls_back_to_group_slugs(mapping: RoleMapping) -> None:
    roles = mapping.krater_roles(roles=None, groups=["krater-reviewers", "other-group"])

    assert roles == frozenset({GROUP_MEMBER, GROUP_REVIEWER})


@pytest.mark.parametrize("bad", ["admin", {"admin": True}, 3])
def test_a_malformed_roles_value_gives_no_weave_role(mapping: RoleMapping, bad: object) -> None:
    assert mapping.krater_roles(roles=bad, groups=["krater-admins"]) == frozenset({GROUP_MEMBER})


def test_settings_change_the_keys_and_slugs() -> None:
    mapping = RoleMapping.from_settings(
        Settings(_env_file=None, weave_role_admin="boss", weave_group_reviewer="judges")
    )

    assert mapping.krater_roles(roles=["boss"], groups=[]) == frozenset({GROUP_MEMBER, GROUP_ADMIN})
    assert mapping.krater_roles(roles=None, groups=["judges"]) == frozenset({GROUP_MEMBER, GROUP_REVIEWER})
    assert mapping.weave_role_key(GROUP_ADMIN) == "boss"


@pytest.mark.parametrize("krater_role", [GROUP_MEMBER, "ganymede:reviewer:senior"])
def test_weave_role_key_rejects_a_role_weave_doesnt_give(mapping: RoleMapping, krater_role: str) -> None:
    with pytest.raises(ValueError):
        mapping.weave_role_key(krater_role)
