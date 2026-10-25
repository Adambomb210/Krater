"""The funder packet: the proposals an admin shares with a funder, for now OSPool (docs/SPEC.md "Funder packet").

OSPool approves every project after Patchwork's own review (`krater.services.residency`), so an admin sends it the
projects it could take: those whose whole team is verified US residents. The packet is what leaves Krater, so the
rules here decide what a funder sees.

- **Which projects.** Approved ones (with a budget, until completion is approved), and with `include_pending` the
  proposals waiting for review too. Only teams where every member is a verified US resident in Weave. The admin can
  narrow the packet to the projects they pick.
- **Which fields.** Title, summary, how the team will use the compute, status, the amount requested and granted, the
  GPU estimate, and the team's names. Names follow the public gallery's rule for members under 18
  (`krater.services.people.public_names`). Never emails, Patchwork IDs, demographics, secrets, logs or anything
  else: a field is shared only when it is added here.
- **Fresh or not.** An export checks residency fresh in Weave and fails closed when Weave can't be reached
  (`FunderPacketUnavailable`). Only the admin's page view may use the directory cache.
- **Audit.** Every export writes an `AuditEvent` with the format and the projects in it.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import Project, ProjectRevision, ProjectStatus
from krater.services import audit, budget, people, residency, site_settings
from krater.services.actor import Actor
from krater.services.errors import FunderPacketUnavailable, NotAllowed, ValidationFailed
from krater.services.guardian import team_users
from krater.weave import WeaveClient, WeaveUnavailableError

AUDIT_EXPORT = "funder_packet_export"

FUNDER_OSPOOL = "ospool"
FORMATS = ("csv", "pdf")

#: Projects with a granted budget that is still in use: approved, until their completion is approved.
GRANTED_STATUSES = (
    ProjectStatus.APPROVED,
    ProjectStatus.PENDING_COMPLETION_REVIEW,
    ProjectStatus.COMPLETION_CHANGES_REQUESTED,
)

STATUS_LABELS = {
    ProjectStatus.APPROVED: "Approved",
    ProjectStatus.PENDING_COMPLETION_REVIEW: "Approved, completion in review",
    ProjectStatus.COMPLETION_CHANGES_REQUESTED: "Approved, completion in review",
    ProjectStatus.PENDING_REVIEW: "Waiting for review",
}


@dataclass(frozen=True)
class GpuEstimate:
    """The proposal's compute estimate, as the team made it on the application form."""

    count: int
    name: str
    hours: float
    interruptible: bool

    @property
    def text(self) -> str:
        """`"1× RTX3060 × 10h, interruptible"`."""
        kind = "interruptible" if self.interruptible else "on-demand"
        return f"{self.count}× {self.name} × {self.hours:g}h, {kind}"


@dataclass(frozen=True)
class PacketRow:
    """One project as a funder sees it. Everything here may leave Krater."""

    project_id: uuid.UUID
    title: str
    summary: str
    compute_use: str
    status: ProjectStatus
    requested_cents: int
    granted_cents: int
    gpu: GpuEstimate | None
    team_names: list[str]
    #: Team members the packet doesn't name (Weave didn't return them, or the gallery rule hides minors' names).
    unnamed: int
    team_size: int

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.status]


@dataclass(frozen=True)
class Packet:
    funder: str
    generated_at: datetime
    include_pending: bool
    rows: list[PacketRow]
    #: Candidate projects left out because not every team member is a verified US resident.
    left_out: int


def build(
    session: Session,
    weave_client: WeaveClient,
    actor: Actor,
    *,
    include_pending: bool = False,
    project_ids: Collection[uuid.UUID] | None = None,
    fresh: bool = True,
    now: datetime | None = None,
) -> Packet:
    """The OSPool packet. Admin only. `project_ids` narrows it to those projects (any that no longer qualify are
    left out). `fresh=False` is only for the admin's page view; an export must check residency fresh.

    Raises `FunderPacketUnavailable` when Weave can't be reached."""
    if not actor.is_admin:
        raise NotAllowed("Only an admin may share proposals with funders.")
    statuses = [*GRANTED_STATUSES, *([ProjectStatus.PENDING_REVIEW] if include_pending else [])]
    stmt = sa.select(Project).where(Project.status.in_(statuses)).order_by(Project.created_at)
    if project_ids is not None:
        stmt = stmt.where(Project.id.in_(list(project_ids)))
    candidates = list(session.scalars(stmt))
    minors = site_settings.current(session).gallery_minor_names

    rows: list[PacketRow] = []
    try:
        for project in candidates:
            if residency.non_residents(session, weave_client, project, fresh=fresh):
                continue
            team = team_users(session, project)
            if not team:
                continue
            names, unnamed = people.public_names(weave_client, team, minors=minors)
            rows.append(_row(session, project, team_names=names, unnamed=unnamed, team_size=len(team)))
    except WeaveUnavailableError as exc:
        raise FunderPacketUnavailable() from exc
    return Packet(
        funder=FUNDER_OSPOOL,
        generated_at=now or datetime.now(UTC),
        include_pending=include_pending,
        rows=rows,
        left_out=len(candidates) - len(rows),
    )


def _row(session: Session, project: Project, *, team_names: list[str], unnamed: int, team_size: int) -> PacketRow:
    # An approved project shows what was approved, not an amendment still in review; a pending proposal shows itself.
    revision: ProjectRevision = project.approved_revision or project.current_revision
    return PacketRow(
        project_id=project.id,
        title=project.title,
        summary=revision.summary,
        compute_use=revision.compute_use,
        status=project.status,
        requested_cents=revision.budget_requested_cents,
        granted_cents=budget.ceiling_cents(session, project),
        gpu=_gpu(revision.budget_estimate),
        team_names=team_names,
        unnamed=unnamed,
        team_size=team_size,
    )


def _gpu(estimate: dict | None) -> GpuEstimate | None:
    if not estimate or not estimate.get("accelerator_name"):
        return None
    return GpuEstimate(
        count=int(estimate.get("accelerator_count") or 1),
        name=str(estimate["accelerator_name"]),
        hours=float(estimate.get("hours") or 0),
        interruptible=estimate.get("basis") == "spot",
    )


def record_export(session: Session, actor: Actor, packet: Packet, *, file_format: str) -> None:
    """Audit an export of `packet` as `file_format` (`csv` or `pdf`). Refuses an empty packet: there is nothing to
    send. Flushes, doesn't commit."""
    if not actor.is_admin:
        raise NotAllowed("Only an admin may share proposals with funders.")
    if file_format not in FORMATS:
        raise ValidationFailed({"format": "Choose CSV or PDF."})
    if not packet.rows:
        raise ValidationFailed({"project_id": "Choose at least one OSPool-ready project."})
    audit.record(
        session,
        actor,
        AUDIT_EXPORT,
        payload={
            "funder": packet.funder,
            "format": file_format,
            "include_pending": packet.include_pending,
            "project_ids": [str(row.project_id) for row in packet.rows],
        },
    )


__all__ = [
    "AUDIT_EXPORT",
    "FORMATS",
    "FUNDER_OSPOOL",
    "GRANTED_STATUSES",
    "GpuEstimate",
    "Packet",
    "PacketRow",
    "build",
    "record_export",
]
