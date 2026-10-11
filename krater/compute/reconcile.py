"""Hourly reconciliation of Krater Compute's spend estimate with what Vast actually charged.

Reads `GET /api/v0/charges/` for the days Krater's recent instances ran, sums each instance's charges (a row can span
several days and is rounded to $0.001, so only the instance's cumulative total is compared), replaces the instance's
estimate with Vast's figure, and records the check per project in `spend_reconciliations`. A difference past the
project's reconciliation alert threshold writes an `AuditEvent` for admins.

A corrected total above the project's latest snapshot becomes a new `vast_estimate` snapshot (and a Quilt
`spend.recorded` event). A lower one stays in Krater's records only: Quilt takes increases only until
patchworklabsorg/quilt#17. Charges for an instance with Krater's label prefix that Krater has no row for are logged
as an alert too: something rented outside the scheduler.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.compute import spend
from krater.config import Settings
from krater.models import ComputeInstance, ComputeJob, Project, SpendReconciliation
from krater.services import audit, compute_settings
from krater.vast import VastClient, usd_to_microusd

logger = logging.getLogger(__name__)

AUDIT_RECONCILE_MISMATCH = "compute_reconcile_mismatch"
AUDIT_UNKNOWN_CHARGES = "compute_unknown_charges"
#: Instances destroyed longer ago than this are settled and no longer checked.
SETTLED_AFTER = timedelta(days=3)
#: Differences under a cent are Vast's $0.001 rounding, never worth an alert.
MIN_ALERT_MICROUSD = 10_000


@dataclass
class ReconcileResult:
    projects: int = 0
    alerts: int = 0
    unknown_sources: list[str] = field(default_factory=list)


def run(session: Session, client: VastClient, settings: Settings, *, now: datetime) -> ReconcileResult:
    """One reconciliation. Commits; run it under the scheduler's lock, since both write instance estimates."""
    result = ReconcileResult()
    rows = list(
        session.scalars(
            sa.select(ComputeInstance).where(
                ComputeInstance.vast_instance_id.is_not(None),
                sa.or_(ComputeInstance.destroyed_at.is_(None), ComputeInstance.destroyed_at >= now - SETTLED_AFTER),
            )
        )
    )
    since = min((row.created_at for row in rows), default=now - timedelta(days=1))
    day_start = since.replace(hour=0, minute=0, second=0, microsecond=0)
    charges = client.charges(day_start, now)

    actual_by_instance: dict[int, int] = {}
    labels: dict[int, str | None] = {}
    for charge in charges:
        if charge.instance_id is None:
            continue
        actual_by_instance[charge.instance_id] = actual_by_instance.get(charge.instance_id, 0) + usd_to_microusd(
            charge.amount
        )
        labels[charge.instance_id] = charge.label

    known = set(
        session.scalars(
            sa.select(ComputeInstance.vast_instance_id).where(ComputeInstance.vast_instance_id.is_not(None))
        )
    )
    for instance_id, label in labels.items():
        if instance_id not in known and (label or "").startswith(settings.compute_label_prefix):
            result.unknown_sources.append(f"instance-{instance_id}")
    if result.unknown_sources:
        logger.warning(
            "krater.compute: Vast charged for Krater-labelled instances Krater never rented: %s", result.unknown_sources
        )
        audit.record(session, None, AUDIT_UNKNOWN_CHARGES, payload={"sources": result.unknown_sources})

    by_project: dict[uuid.UUID, list[tuple[ComputeInstance, int, int]]] = {}
    for row in rows:
        actual = actual_by_instance.get(row.vast_instance_id)
        if actual is None:
            continue  # nothing charged yet (just created): keep the estimate
        estimate = row.accrued_microusd
        row.charged_microusd = actual
        row.charged_at = now
        # Vast's figure replaces the estimate; accrual continues from here for a live instance.
        row.accrued_microusd = actual
        if row.destroyed_at is None:
            row.accrued_until = now
        by_project.setdefault(row.job.project_id, []).append((row, estimate, actual))

    for project_id, items in by_project.items():
        project = session.get(Project, project_id)
        estimate_total = sum(estimate for _, estimate, _ in items)
        actual_total = sum(actual for _, _, actual in items)
        policy = compute_settings.effective_policy(session, project)
        difference = abs(actual_total - estimate_total)
        alerted = difference >= MIN_ALERT_MICROUSD and difference * 100 > policy.reconcile_alert_percent * max(
            estimate_total, 1
        )
        session.add(
            SpendReconciliation(
                project_id=project_id,
                checked_at=now,
                instance_count=len(items),
                estimate_microusd=estimate_total,
                actual_microusd=actual_total,
                alerted=alerted,
                details=[
                    {"vast_instance_id": row.vast_instance_id, "estimate_microusd": estimate, "actual_microusd": actual}
                    for row, estimate, actual in items
                ],
            )
        )
        if alerted:
            result.alerts += 1
            audit.record(
                session,
                None,
                AUDIT_RECONCILE_MISMATCH,
                project=project,
                payload={
                    "estimate_microusd": estimate_total,
                    "actual_microusd": actual_total,
                    "threshold_percent": policy.reconcile_alert_percent,
                },
            )
        _resum_jobs(session, {row.job_id for row, _, _ in items})
        result.projects += 1

    session.flush()
    spend.record_snapshots(session, set(by_project), now=now)
    session.commit()
    return result


def _resum_jobs(session: Session, job_ids: set[uuid.UUID]) -> None:
    totals = session.execute(
        sa.select(ComputeInstance.job_id, sa.func.sum(ComputeInstance.accrued_microusd))
        .where(ComputeInstance.job_id.in_(job_ids))
        .group_by(ComputeInstance.job_id)
    )
    for job_id, total in totals:
        session.get(ComputeJob, job_id).spend_cents = math.ceil(int(total or 0) / spend.MICROUSD_PER_CENT)
