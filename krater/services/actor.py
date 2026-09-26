"""Who is performing an action, and what Weave says they may do.

Built by the web layer (or Slack handlers) from a fresh WeaveClient lookup, then passed explicitly into services. Services
authorize against `groups` here and never read `User.groups_cached`.
"""

from __future__ import annotations

from dataclasses import dataclass

from krater.models import User

GROUP_MEMBER = "ganymede:member"
GROUP_REVIEWER = "ganymede:reviewer"
GROUP_ADMIN = "ganymede:admin"


@dataclass(frozen=True)
class Actor:
    user: User
    groups: frozenset[str]

    def in_group(self, group: str) -> bool:
        return group in self.groups

    @property
    def is_member(self) -> bool:
        return GROUP_MEMBER in self.groups

    @property
    def is_reviewer(self) -> bool:
        return GROUP_REVIEWER in self.groups

    @property
    def is_admin(self) -> bool:
        return GROUP_ADMIN in self.groups
