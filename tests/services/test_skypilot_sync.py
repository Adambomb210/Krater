"""`krater.services.skypilot_sync` against `FakeSkyPilotClient`: workspace provisioning/teardown,
spend snapshots, and budget warning/teardown enforcement.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from krater.models import AuditEvent, BudgetEntryKind, ProjectStatus, ReviewDecision, ReviewSource, SpendSnapshot
from krater.services import budget, projects
from krater.services.actor import Actor
from krater.services.skypilot_sync import (
    AUDIT_BUDGET_TEARDOWN,
    AUDIT_BUDGET_WARNING,
    AUDIT_WORKSPACE_TORN_DOWN,
    current_budget_flag,
    enforce_budgets,
    reconcile,
    sync_spend,
    sync_workspaces,
    workspace_name_for,
)
from krater.skypilot.errors import SkyPilotUnavailableError
from krater.skypilot.fake import FakeSkyPilotClient

WARN_PERCENT = 80


def _approve(session: Session, member: Actor, reviewer: Actor, *, budget_cents: int = 100_000) -> ProjectStatus:
    project = projects.create_project(session, member, title="Rover", write_up="A rover.")
    projects.update_draft(session, member, project=project, budget_requested_cents=budget_cents)
    project = projects.submit(session, member, project=project)
    projects.record_review(
        session, reviewer, revision=project.current_revision, decision=ReviewDecision.APPROVE, source=ReviewSource.WEB
    )
    session.refresh(project)
    assert project.status is ProjectStatus.APPROVED
    return project


@pytest.fixture
def client() -> FakeSkyPilotClient:
    return FakeSkyPilotClient()


# --------------------------------------------------------------------------------------------------
# sync_workspaces
# --------------------------------------------------------------------------------------------------


def test_approved_project_gets_a_workspace(db_session: Session, member: Actor, reviewer: Actor, client) -> None:
    project = _approve(db_session, member, reviewer)

    sync_workspaces(db_session, client)

    expected_name = workspace_name_for(project.id)
    assert project.skypilot_workspace == expected_name
    assert client.workspaces[expected_name] == [member.user.email]


def test_a_team_change_updates_allowed_users(
    db_session: Session, member: Actor, reviewer: Actor, client, make_user
) -> None:
    project = _approve(db_session, member, reviewer)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace

    builder = make_user(email="builder@example.com")
    amendment = projects.start_amendment(db_session, member, project=project)
    projects.update_draft(db_session, member, project=project, credited_builder_ids=[builder.id])
    assert amendment.credited_builder_ids == [builder.id]

    sync_workspaces(db_session, client)

    assert client.workspaces[name] == sorted([member.user.email, builder.email])


def test_a_completed_project_gets_torn_down(db_session: Session, member: Actor, reviewer: Actor, client) -> None:
    project = _approve(db_session, member, reviewer)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    client.add_cluster(name, cost_cents=500)
    client.add_managed_job(name)

    completion = projects.start_completion(db_session, member, project=project)
    projects.submit_completion(db_session, member, project=project)
    projects.record_review(
        db_session, reviewer, revision=completion, decision=ReviewDecision.APPROVE, source=ReviewSource.WEB
    )
    db_session.refresh(project)
    assert project.status is ProjectStatus.COMPLETED

    sync_workspaces(db_session, client)

    assert project.skypilot_workspace is None
    assert name not in client.workspaces
    assert client.list_clusters(name) == []
    event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == AUDIT_WORKSPACE_TORN_DOWN, AuditEvent.project_id == project.id)
    ).one()
    assert event.actor_id is None
    assert event.payload["workspace"] == name


def test_a_withdrawn_project_gets_torn_down_too(db_session: Session, member: Actor, reviewer: Actor, client) -> None:
    project = _approve(db_session, member, reviewer)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace

    projects.withdraw(db_session, member, project=project)
    assert project.status is ProjectStatus.WITHDRAWN

    sync_workspaces(db_session, client)

    assert project.skypilot_workspace is None
    assert name not in client.workspaces


# --------------------------------------------------------------------------------------------------
# sync_spend
# --------------------------------------------------------------------------------------------------


def test_spend_snapshots_are_written_only_on_change(
    db_session: Session, member: Actor, reviewer: Actor, client
) -> None:
    project = _approve(db_session, member, reviewer)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    cluster = client.add_cluster(name, cost_cents=1000)

    sync_spend(db_session, client)
    assert budget.latest_spend_cents(db_session, project) == 1000

    def snapshot_count() -> int:
        return len(db_session.scalars(select(SpendSnapshot).where(SpendSnapshot.project_id == project.id)).all())

    assert snapshot_count() == 1

    # Unchanged spend: no new snapshot.
    sync_spend(db_session, client)
    assert snapshot_count() == 1

    # Spend changes: a new snapshot is written.
    client.set_cluster_cost(cluster, 1500)
    sync_spend(db_session, client)
    assert snapshot_count() == 2
    assert budget.latest_spend_cents(db_session, project) == 1500


# --------------------------------------------------------------------------------------------------
# enforce_budgets
# --------------------------------------------------------------------------------------------------


def test_the_warning_fires_once(db_session: Session, member: Actor, reviewer: Actor, client) -> None:
    project = _approve(db_session, member, reviewer, budget_cents=1000)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    client.add_cluster(name, cost_cents=850)  # 85% >= 80% warn threshold
    sync_spend(db_session, client)

    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)
    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)  # re-run: should not duplicate

    warnings = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == AUDIT_BUDGET_WARNING, AuditEvent.project_id == project.id)
    ).all()
    assert len(warnings) == 1
    assert warnings[0].actor_id is None
    assert current_budget_flag(db_session, project, warn_percent=WARN_PERCENT) == "warning"


def test_teardown_at_100_percent_downs_clusters_and_cancels_jobs(
    db_session: Session, member: Actor, reviewer: Actor, client
) -> None:
    project = _approve(db_session, member, reviewer, budget_cents=1000)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    client.add_cluster(name, cost_cents=1200)  # over budget
    client.add_managed_job(name)
    sync_spend(db_session, client)

    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)

    assert client.list_clusters(name) == []
    assert client.list_managed_jobs(name) == []
    teardown_event = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == AUDIT_BUDGET_TEARDOWN, AuditEvent.project_id == project.id)
    ).one()
    assert teardown_event.actor_id is None
    assert current_budget_flag(db_session, project, warn_percent=WARN_PERCENT) == "teardown"


def test_teardown_does_not_repeat_without_new_clusters(
    db_session: Session, member: Actor, reviewer: Actor, client
) -> None:
    project = _approve(db_session, member, reviewer, budget_cents=1000)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    client.add_cluster(name, cost_cents=1200)
    sync_spend(db_session, client)

    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)
    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)

    events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == AUDIT_BUDGET_TEARDOWN, AuditEvent.project_id == project.id)
    ).all()
    assert len(events) == 1


def test_teardown_repeats_when_a_new_cluster_appears(
    db_session: Session, member: Actor, reviewer: Actor, client
) -> None:
    project = _approve(db_session, member, reviewer, budget_cents=1000)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    client.add_cluster(name, cost_cents=1200)
    sync_spend(db_session, client)
    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)

    # A new cluster shows up after the first teardown (spend/ceiling is still >= 100%).
    client.add_cluster(name, cost_cents=1200)
    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)

    events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == AUDIT_BUDGET_TEARDOWN, AuditEvent.project_id == project.id)
    ).all()
    assert len(events) == 2
    assert client.list_clusters(name) == []


def test_raising_the_ceiling_re_arms_the_warning(db_session: Session, member: Actor, reviewer: Actor, client) -> None:
    project = _approve(db_session, member, reviewer, budget_cents=1000)
    sync_workspaces(db_session, client)
    name = project.skypilot_workspace
    cluster_name = client.add_cluster(name, cost_cents=850)
    sync_spend(db_session, client)
    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)

    warnings_before = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == AUDIT_BUDGET_WARNING, AuditEvent.project_id == project.id)
    ).all()
    assert len(warnings_before) == 1

    # Raise the ceiling, but not enough to drop below the warn threshold, then more spend accrues.
    budget.add_entry(
        db_session, project=project, kind=BudgetEntryKind.ADMIN_ADJUSTMENT, amount_cents=1000, actor=reviewer
    )
    client.set_cluster_cost(cluster_name, 1700)  # 85% of the new 2000 ceiling
    sync_spend(db_session, client)

    enforce_budgets(db_session, client, warn_percent=WARN_PERCENT)

    warnings_after = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == AUDIT_BUDGET_WARNING, AuditEvent.project_id == project.id)
    ).all()
    assert len(warnings_after) == 2


# --------------------------------------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------------------------------------


class _OutageOnceClient(FakeSkyPilotClient):
    """A `FakeSkyPilotClient` whose `cost_report` fails once, to test that an outage in one reconcile
    step doesn't block the others."""

    def __init__(self) -> None:
        super().__init__()
        self.cost_report_calls = 0

    def cost_report(self, days: int):
        self.cost_report_calls += 1
        if self.cost_report_calls == 1:
            raise SkyPilotUnavailableError("simulated outage")
        return super().cost_report(days)


def test_an_outage_in_one_step_does_not_block_the_others(db_session: Session, member: Actor, reviewer: Actor) -> None:
    flaky_client = _OutageOnceClient()
    project = _approve(db_session, member, reviewer)

    # sync_spend (step 2) raises once; sync_workspaces (step 1) and enforce_budgets (step 3) must still
    # run and have their work committed.
    reconcile(db_session, flaky_client, warn_percent=WARN_PERCENT)

    db_session.refresh(project)
    assert project.skypilot_workspace == workspace_name_for(project.id)
    assert flaky_client.cost_report_calls == 1

    # A later reconcile succeeds and the spend step catches up.
    flaky_client.add_cluster(project.skypilot_workspace, cost_cents=42)
    reconcile(db_session, flaky_client, warn_percent=WARN_PERCENT)
    assert budget.latest_spend_cents(db_session, project) == 42


def test_workspace_name_for_is_deterministic_lowercase_hex() -> None:
    project_id = uuid.uuid4()
    name = workspace_name_for(project_id)
    assert name == f"ganymede-{project_id.hex[:12]}"
    assert name.islower()
