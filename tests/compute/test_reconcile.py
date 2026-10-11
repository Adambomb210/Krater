"""Hourly reconciliation of the spend estimate with Vast's charges."""

from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa

from krater.compute import reconcile
from krater.models import AuditEvent, QuiltOutbox, SpendReconciliation, SpendSnapshot
from krater.services import budget
from krater.vast import CreateInstance
from tests.compute.test_scheduler import NOW, SETTINGS, _beat, _instance, _pass, _runner_starts, _submit


def _ran_an_hour(db_session, member, project, client, fake):  # noqa: F811
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)
    fake.set_status(row.vast_instance_id, "running")
    _pass(db_session, client, NOW + timedelta(minutes=4))
    _runner_starts(db_session, fake, row, NOW + timedelta(minutes=4))
    _beat(db_session, row, NOW + timedelta(minutes=63))
    _pass(db_session, client, NOW + timedelta(minutes=64))
    return job, row


def _reconcile(db_session, client, minutes=65):
    return reconcile.run(db_session, client, SETTINGS, now=NOW + timedelta(minutes=minutes))


def test_vasts_charges_replace_the_estimate(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _ran_an_hour(db_session, member, project, client, fake)
    estimate = row.accrued_microusd  # about $0.0644
    fake.add_charge(row.vast_instance_id, gpu="0.058", disk="0.009")  # 4% over the estimate

    result = _reconcile(db_session, client)

    assert result.projects == 1 and result.alerts == 0
    assert row.charged_microusd == 67_000
    assert row.accrued_microusd == 67_000
    check = db_session.scalars(sa.select(SpendReconciliation).where(SpendReconciliation.project_id == project.id)).one()
    assert check.estimate_microusd == estimate and check.actual_microusd == 67_000 and not check.alerted
    assert check.details[0]["vast_instance_id"] == row.vast_instance_id


def test_a_big_difference_alerts_admins_and_raises_the_spend(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _ran_an_hour(db_session, member, project, client, fake)
    fake.add_charge(row.vast_instance_id, gpu="0.090", disk="0.010")  # $0.10 against about $0.064

    result = _reconcile(db_session, client)

    assert result.alerts == 1
    event = db_session.scalars(
        sa.select(AuditEvent).where(AuditEvent.action == reconcile.AUDIT_RECONCILE_MISMATCH)
    ).one()
    assert event.project_id == project.id and event.payload["actual_microusd"] == 100_000
    assert job.spend_cents == 10
    assert budget.latest_spend_cents(db_session, project) == 10


def test_a_lower_charge_never_lowers_a_live_instances_estimate(db_session, member, project, client, fake) -> None:  # noqa: F811
    """Charges for a running machine may lag: correcting downwards would make the budget stop late."""
    job, row = _ran_an_hour(db_session, member, project, client, fake)
    estimate = row.accrued_microusd
    fake.add_charge(row.vast_instance_id, gpu="0.010", disk="0.001")

    _reconcile(db_session, client)

    assert row.charged_microusd == 11_000
    assert row.accrued_microusd == estimate


def test_a_destroyed_instance_takes_vasts_lower_figure_but_quilt_never_goes_down(
    db_session,
    member,
    project,
    client,
    fake,  # noqa: F811
) -> None:
    from krater.services import compute_jobs

    job, row = _ran_an_hour(db_session, member, project, client, fake)
    compute_jobs.cancel_job(db_session, member, job=job, now=NOW + timedelta(minutes=64))
    _pass(db_session, client, NOW + timedelta(minutes=65))
    assert row.destroyed_at is not None
    before = budget.latest_spend_cents(db_session, project)
    snapshots_before = db_session.scalar(sa.select(sa.func.count()).select_from(SpendSnapshot))
    outbox_before = db_session.scalar(sa.select(sa.func.count()).select_from(QuiltOutbox))
    fake.add_charge(row.vast_instance_id, gpu="0.010", disk="0.001")

    _reconcile(db_session, client, minutes=70)

    assert row.accrued_microusd == 11_000
    assert job.spend_cents == 2  # Krater's own records take Vast's figure...
    assert budget.latest_spend_cents(db_session, project) == before  # ...the snapshot and Quilt don't go down
    assert db_session.scalar(sa.select(sa.func.count()).select_from(SpendSnapshot)) == snapshots_before
    assert db_session.scalar(sa.select(sa.func.count()).select_from(QuiltOutbox)) == outbox_before


def test_an_instance_with_no_charges_yet_keeps_its_estimate(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _ran_an_hour(db_session, member, project, client, fake)
    estimate = row.accrued_microusd

    result = _reconcile(db_session, client)

    assert result.projects == 0
    assert row.accrued_microusd == estimate and row.charged_microusd is None


def test_charges_for_an_unknown_krater_instance_are_flagged(db_session, client, fake) -> None:
    fake.add_offer(3001, 3001, "RTX 3060", dph_base=0.1, min_bid=0.05, storage_cost=0.1, gpu_ram_mb=12288)
    stray = client.create_instance(3001, CreateInstance(image="x", disk_gb=8, label="krater-job-ffffffffffff-1"))
    fake.add_charge(stray, gpu="0.010")

    result = _reconcile(db_session, client)

    assert result.unknown_sources == [f"instance-{stray}"]
    assert db_session.scalars(sa.select(AuditEvent).where(AuditEvent.action == reconcile.AUDIT_UNKNOWN_CHARGES)).one()


def test_unknown_charges_are_flagged_once(db_session, client, fake) -> None:
    fake.add_offer(3002, 3002, "RTX 3060", dph_base=0.1, min_bid=0.05, storage_cost=0.1, gpu_ram_mb=12288)
    stray = client.create_instance(3002, CreateInstance(image="x", disk_gb=8, label="krater-job-eeeeeeeeeeee-1"))
    fake.add_charge(stray, gpu="0.010")

    _reconcile(db_session, client)
    _reconcile(db_session, client, minutes=125)

    events = db_session.scalars(sa.select(AuditEvent).where(AuditEvent.action == reconcile.AUDIT_UNKNOWN_CHARGES)).all()
    assert len(events) == 1
