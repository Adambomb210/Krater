"""Krater's own role assignments: `UserRole` (a role a signed-in user holds) and `PendingRoleGrant` (a
role waiting, by email, for someone who hasn't signed in yet). The rules live in `krater.services.roles`."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base
from krater.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from krater.models.user import User


class UserRole(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "user_roles"
    __table_args__ = (sa.UniqueConstraint("user_id", "role", name="uq_user_roles_user_id_role"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False, index=True
    )
    # A plain string rather than an enum, so reviewer tiers (`ganymede:reviewer:<tier>`) won't need a
    # migration; `krater.services.roles.KNOWN_ROLES` is what admins can grant.
    role: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # Null for grants with no human behind them (bootstrap admins, the groups-claim migration).
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
    )

    user: Mapped[User] = relationship(foreign_keys=[user_id])
    granted_by: Mapped[User | None] = relationship(foreign_keys=[granted_by_id])

    def __repr__(self) -> str:
        return f"<UserRole user_id={self.user_id} role={self.role!r}>"


class PendingRoleGrant(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "pending_role_grants"
    __table_args__ = (sa.UniqueConstraint("email", "role", name="uq_pending_role_grants_email_role"),)

    # Always stored lowercased; matched at sign-in only against an email Weave says is verified.
    email: Mapped[str] = mapped_column(sa.String(255), nullable=False, index=True)
    role: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    granted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True
    )

    granted_by: Mapped[User | None] = relationship(foreign_keys=[granted_by_id])

    def __repr__(self) -> str:
        return f"<PendingRoleGrant email={self.email!r} role={self.role!r}>"
