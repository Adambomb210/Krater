"""Krater Compute's jobs, the Vast instances rented for them, and their event log.

A `ComputeJob` is a container image, a command and a price rule. The scheduler (`krater.compute.scheduler`) rents a
`ComputeInstance` for it once an offer at or under its target appears; one job can have several instances over its
life (after an interruption moves it). `ComputeEvent` is the append-only log of what happened to a job.

Rates are integer micro-dollars per hour (`*_microusd`): full hourly cost, GPU plus disk, as Krater compares offers.
Budget amounts (`hold_cents`, `spend_cents`) are integer cents.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from krater.db import Base
from krater.models.enums import (
    ComputeEscalation,
    ComputeInstanceState,
    ComputeJobKind,
    ComputeJobState,
    pg_enum,
)
from krater.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from krater.models.project import Project


class ComputeJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "compute_jobs"
    __table_args__ = (
        sa.Index("ix_compute_jobs_state_queued_at", "state", "queued_at"),
        sa.CheckConstraint("target_microusd > 0 AND target_microusd <= max_microusd", name="prices"),
        sa.CheckConstraint("num_gpus >= 1 AND disk_gb >= 1 AND max_runtime_minutes >= 1", name="shape"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False, index=True
    )
    submitter_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False)
    kind: Mapped[ComputeJobKind] = mapped_column(pg_enum(ComputeJobKind, name="compute_job_kind"), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)

    image: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    command: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # Allowed Vast GPU model names (empty = any model with at least `min_gpu_ram_gb`).
    gpu_names: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    min_gpu_ram_gb: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    num_gpus: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    disk_gb: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    # Start when an offer's full hourly cost is at or under the target; never pay more than the max.
    target_microusd: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    max_microusd: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    interruptible: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    max_wait_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    on_max_wait: Mapped[ComputeEscalation] = mapped_column(
        pg_enum(ComputeEscalation, name="compute_escalation"), nullable=False
    )
    max_runtime_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    state: Mapped[ComputeJobState] = mapped_column(pg_enum(ComputeJobState, name="compute_job_state"), nullable=False)
    # Why the job is where it is, for the member: "waiting for an RTX 4090 bid at or under $0.20/h", ...
    state_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    # Budget held for this job while it runs (its expected cost), so two jobs can't spend the same dollars, and
    # what it has spent so far. Both cents. The hold is released (0) when the job ends.
    hold_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    spend_cents: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    project: Mapped[Project] = relationship()
    instances: Mapped[list[ComputeInstance]] = relationship(back_populates="job", order_by="ComputeInstance.attempt")

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_JOB_STATES

    def __repr__(self) -> str:
        return f"<ComputeJob {self.id} {self.state.value} {self.image!r}>"


#: States in which a job holds budget and may have a live instance.
ACTIVE_JOB_STATES = frozenset({ComputeJobState.STARTING, ComputeJobState.RUNNING, ComputeJobState.PAUSED})
FINISHED_JOB_STATES = frozenset({ComputeJobState.SUCCEEDED, ComputeJobState.FAILED, ComputeJobState.CANCELLED})


class ComputeInstance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "compute_instances"
    __table_args__ = (sa.UniqueConstraint("job_id", "attempt", name="job_attempt"),)

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("compute_jobs.id"), nullable=False, index=True
    )
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # The Vast label, unique per instance: how a create whose response was lost is found again.
    label: Mapped[str] = mapped_column(sa.String(100), nullable=False, unique=True)
    vast_instance_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True, unique=True)
    state: Mapped[ComputeInstanceState] = mapped_column(
        pg_enum(ComputeInstanceState, name="compute_instance_state"), nullable=False
    )

    offer_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    machine_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    gpu_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    interruptible: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    # The GPU price per hour Krater rented at (the bid for interruptible), the disk per hour, and their sum.
    gpu_microusd: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    disk_microusd: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)

    last_vast_status: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    running_since: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    paused_since: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # What this instance has cost so far by Krater's estimate (disk from create, GPU only while running), and up to
    # when. Hourly reconciliation with Vast's charges corrects it.
    accrued_microusd: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0, server_default="0")
    accrued_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # Vast's own charges for it at the last hourly reconciliation (`krater.compute.reconcile`).
    charged_microusd: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    charged_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # Set when Krater decides to destroy it; the scheduler retries until Vast confirms it's gone.
    destroy_requested_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    destroy_reason: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    destroyed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    job: Mapped[ComputeJob] = relationship(back_populates="instances")

    @property
    def full_microusd(self) -> int:
        return self.gpu_microusd + self.disk_microusd

    @property
    def is_live(self) -> bool:
        """Whether this instance may exist on Vast (and bill)."""
        return self.state not in (ComputeInstanceState.DESTROYED, ComputeInstanceState.CREATE_FAILED)

    def __repr__(self) -> str:
        return f"<ComputeInstance {self.label} vast={self.vast_instance_id} {self.state.value}>"


class SpendReconciliation(UUIDPrimaryKeyMixin, Base):
    """One project's hourly check of Vast's charges against Krater's estimate. Kept even when the corrected total is
    lower than what Quilt was sent: Quilt takes increases only for now (patchworklabsorg/quilt#17)."""

    __tablename__ = "spend_reconciliations"
    __table_args__ = (sa.Index("ix_spend_reconciliations_project_id_checked_at", "project_id", "checked_at"),)

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    instance_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    estimate_microusd: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    actual_microusd: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    # Whether the difference passed the project's reconciliation alert threshold.
    alerted: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    # Per instance: {"vast_instance_id", "estimate_microusd", "actual_microusd"}.
    details: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    @property
    def difference_microusd(self) -> int:
        return self.actual_microusd - self.estimate_microusd


class ComputeEvent(UUIDPrimaryKeyMixin, Base):
    """Append-only: what happened to a job (rented, paused, bid raised, moved, stopped, destroyed, ...)."""

    __tablename__ = "compute_events"
    __table_args__ = (sa.Index("ix_compute_events_job_id_at", "job_id", "at"),)

    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), sa.ForeignKey("compute_jobs.id"), nullable=False)
    instance_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("compute_instances.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    message: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<ComputeEvent {self.kind} job={self.job_id}>"
