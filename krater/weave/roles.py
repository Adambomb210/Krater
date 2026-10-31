"""How Weave's roles and groups become Krater's roles. The only place that knows this mapping.

Every Patchwork member Weave lets use Krater is a Ganymede member: Weave decides that with the Krater
app's access policy, so a user who signs in, or whom the directory returns, holds `ganymede:member`.
There is no member role in Weave.

Weave owns the other roles. Krater is an OAuth app in Weave with two app-defined role keys (by default
`reviewer` and `admin`), usually given to a group on the app page. Weave sends the keys a user holds
for Krater in the `roles` claim and in each directory record. That list is the source of truth.

If Weave sends no `roles` field at all (absent, not empty), Krater falls back to the group slugs
linked to the Krater app (by default `krater-reviewers`, `krater-admins`).

Inside Krater, roles keep their `ganymede:*` names (`krater.services.actor`), so the `Actor`, approval
policies and review snapshots don't change. The translation happens here, at the `krater.weave`
boundary, and nothing outside this package sees Weave's keys or slugs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from krater.config import Settings
from krater.services.actor import GROUP_ADMIN, GROUP_MEMBER, GROUP_REVIEWER


@dataclass(frozen=True)
class RoleMapping:
    """Krater role name -> Weave role key, and Krater role name -> Weave group slug, for the roles Weave
    gives (`ganymede:reviewer`, `ganymede:admin`)."""

    role_keys: Mapping[str, str]
    group_slugs: Mapping[str, str]

    @classmethod
    def from_settings(cls, settings: Settings) -> RoleMapping:
        return cls(
            role_keys={
                GROUP_REVIEWER: settings.weave_role_reviewer,
                GROUP_ADMIN: settings.weave_role_admin,
            },
            group_slugs={
                GROUP_REVIEWER: settings.weave_group_reviewer,
                GROUP_ADMIN: settings.weave_group_admin,
            },
        )

    @classmethod
    def default(cls) -> RoleMapping:
        """The mapping with every setting at its default."""
        return cls.from_settings(Settings(_env_file=None))

    def krater_roles(self, *, roles: object, groups: object) -> frozenset[str]:
        """Krater's role names for a user Weave let use Krater, from Weave's `roles` and `groups` values.

        Always holds `ganymede:member`. `roles` is `None` when Weave didn't send the field at all; only
        then are `groups` used. A value that isn't a list counts as holding no Weave role, so a
        malformed answer fails closed. Unknown keys and slugs, and non-string entries, are ignored.
        """
        given = self._match(self.role_keys, roles) if roles is not None else self._match(self.group_slugs, groups)
        return given | {GROUP_MEMBER}

    def weave_role_key(self, krater_role: str) -> str:
        """The Weave role key for a Krater role name, e.g. `ganymede:reviewer` -> `reviewer`. `ganymede:member` has
        none: every user Weave lets use Krater holds it."""
        try:
            return self.role_keys[krater_role]
        except KeyError:
            raise ValueError(f"no Weave role key for Krater role {krater_role!r}") from None

    @staticmethod
    def _match(table: Mapping[str, str], values: object) -> frozenset[str]:
        held = _string_set(values)
        return frozenset(krater_role for krater_role, weave_name in table.items() if weave_name in held)


def _string_set(values: object) -> frozenset[str]:
    if not isinstance(values, Iterable) or isinstance(values, str | bytes | Mapping):
        return frozenset()
    return frozenset(value for value in values if isinstance(value, str))


__all__ = ["RoleMapping"]
