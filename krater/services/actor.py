"""Who is performing an action, and which Krater roles they hold.

Built by the web layer (or Slack handlers) from a fresh read of Krater's `user_roles` table (see
`krater.services.roles.authorize`), then passed explicitly into services. Services authorize against
`groups` here. The role identifiers keep the `ganymede:*` names they had as Weave groups, so review
snapshots (`Review.reviewer_groups`) and approval policies (`ApprovalPolicy.required_group`) read the same.
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
