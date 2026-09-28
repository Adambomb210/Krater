"""User: a Weave identity (from sign-in) plus Krater's own account state. Roles live in `user_roles`
(`krater.models.user_role`), managed by `krater.services.roles`."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base
from krater.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from krater.models.audit_event import AuditEvent
    from krater.models.budget_entry import BudgetEntry
    from krater.models.project import Project
    from krater.models.review import Review


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    weave_sub: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    email: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    # Whether Weave said `email` was verified at the user's latest sign-in. Anything that trusts the
    # email as proof of identity (pending grants, bootstrap admins, Slack email matching) checks this.
    email_verified: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False, server_default=sa.false())
    # Unique so a Slack click resolves to exactly one Krater user.
    slack_user_id: Mapped[str | None] = mapped_column(sa.String(64), unique=True, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # Set by a Ganymede admin. A disabled user can't sign in and fails every authorization check.
    disabled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    projects: Mapped[list[Project]] = relationship(foreign_keys="Project.submitter_id", back_populates="submitter")
    reviews: Mapped[list[Review]] = relationship(back_populates="reviewer")
    budget_entries: Mapped[list[BudgetEntry]] = relationship(back_populates="actor")
    audit_events: Mapped[list[AuditEvent]] = relationship(back_populates="actor")

    @property
    def is_disabled(self) -> bool:
        return self.disabled_at is not None

    def __repr__(self) -> str:
        return f"<User {self.weave_sub} {self.display_name!r}>"
