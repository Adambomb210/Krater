"""User: a cache of the Weave identity. Holds no role/authorization data (see CLAUDE.md)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
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
    slack_user_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    # Last-seen `groups` claim from Weave. Display only -- authorization always re-checks via WeaveClient.
    groups_cached: Mapped[list[str]] = mapped_column(
        ARRAY(sa.String), nullable=False, default=list, server_default="{}"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    projects: Mapped[list[Project]] = relationship(foreign_keys="Project.submitter_id", back_populates="submitter")
    reviews: Mapped[list[Review]] = relationship(back_populates="reviewer")
    budget_entries: Mapped[list[BudgetEntry]] = relationship(back_populates="actor")
    audit_events: Mapped[list[AuditEvent]] = relationship(back_populates="actor")

    def __repr__(self) -> str:
        return f"<User {self.weave_sub} {self.display_name!r}>"
