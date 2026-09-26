"""Tests for `krater.services.launch_policy`: the SkyPilot launch gate's decision function."""

from __future__ import annotations

from sqlalchemy.orm import Session

from krater.config import Settings
from krater.models import BudgetEntryKind, Project, ProjectStatus, SpendSnapshot, SpendSource
from krater.services import budget, launch_policy
from krater.services.actor import Actor
from krater.skypilot_policy.envelope import PolicyRequest

SETTINGS = Settings(skypilot_autodown_idle_minutes=30, skypilot_max_hourly_cost_cents=500)


def _request(
    *,
    task: dict | None = None,
    workspace: str | None = "ganymede-test",
    request_name: str = "launch",
) -> PolicyRequest:
    skypilot_config: dict = {}
    if workspace is not None:
        skypilot_config["active_workspace"] = workspace
    return PolicyRequest(
        task=task if task is not None else {"resources": {"infra": "vast", "accelerators": {"A100": 1}}},
        skypilot_config=skypilot_config,
        request_name=request_name,
        request_options={"cluster_name": "test", "dryrun": True},
        at_client_side=True,
        user=None,
        client_api_version=None,
        client_version=None,
    )


def _make_project(
    db_session: Session, member: Actor, *, status: ProjectStatus, workspace: str = "ganymede-test"
) -> Project:
    project = Project(
        title="Launch Policy Project", submitter_id=member.user.id, status=status, skypilot_workspace=workspace
    )
    db_session.add(project)
    db_session.flush()
    return project


# --------------------------------------------------------------------------------------------------
# Rejections
# --------------------------------------------------------------------------------------------------


def test_rejects_when_workspace_is_missing(db_session: Session) -> None:
    decision = launch_policy.decide(_request(workspace=None), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Reject)
    assert "workspace" in decision.message.lower()
    assert "sky launch -w" in decision.message


def test_rejects_when_workspace_is_default(db_session: Session) -> None:
    decision = launch_policy.decide(_request(workspace="default"), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Reject)
    assert "sky launch -w" in decision.message


def test_rejects_when_workspace_is_not_a_krater_project(db_session: Session) -> None:
    decision = launch_policy.decide(_request(workspace="ganymede-nonexistent"), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Reject)
    assert "ganymede-nonexistent" in decision.message


def test_rejects_when_project_is_not_active(db_session: Session, member: Actor) -> None:
    _make_project(db_session, member, status=ProjectStatus.COMPLETED)

    decision = launch_policy.decide(_request(), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Reject)
    assert "completed" in decision.message.lower()


def test_rejects_when_project_is_still_in_review(db_session: Session, member: Actor) -> None:
    _make_project(db_session, member, status=ProjectStatus.PENDING_REVIEW)

    decision = launch_policy.decide(_request(), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Reject)


def test_rejects_when_budget_is_exhausted(db_session: Session, member: Actor) -> None:
    project = _make_project(db_session, member, status=ProjectStatus.APPROVED)
    budget.add_entry(
        db_session, project=project, kind=BudgetEntryKind.INITIAL_APPROVAL, amount_cents=1_000, actor=member
    )
    db_session.add(
        SpendSnapshot(project_id=project.id, estimated_spend_cents=1_000, source=SpendSource.SKYPILOT_COST_REPORT)
    )
    db_session.flush()

    decision = launch_policy.decide(_request(), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Reject)
    assert "$10.00" in decision.message


# --------------------------------------------------------------------------------------------------
# Allow + mutations
# --------------------------------------------------------------------------------------------------


def _approved_project(db_session: Session, member: Actor, ceiling_cents: int = 100_000) -> Project:
    project = _make_project(db_session, member, status=ProjectStatus.APPROVED)
    budget.add_entry(
        db_session, project=project, kind=BudgetEntryKind.INITIAL_APPROVAL, amount_cents=ceiling_cents, actor=member
    )
    return project


def test_allows_and_forces_autodown_when_user_specified_none(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)

    decision = launch_policy.decide(_request(), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["autostop"] == {"idle_minutes": 30, "down": True}


def test_allows_and_caps_max_hourly_cost_to_the_global_default(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    task = {"resources": {"infra": "vast", "max_hourly_cost": 999.0}}

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["max_hourly_cost"] == 5.0  # 500 cents


def test_allows_and_keeps_the_users_lower_max_hourly_cost(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    task = {"resources": {"infra": "vast", "max_hourly_cost": 1.5}}

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["max_hourly_cost"] == 1.5


def test_allows_and_keeps_the_users_stricter_autostop(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    task = {"resources": {"infra": "vast", "autostop": {"idle_minutes": 5, "down": True}}}

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["autostop"] == {"idle_minutes": 5, "down": True}


def test_allows_and_overrides_a_looser_user_autostop(db_session: Session, member: Actor) -> None:
    _approved_project(db_session, member)
    task = {"resources": {"infra": "vast", "autostop": {"idle_minutes": 120, "down": True}}}

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["autostop"] == {"idle_minutes": 30, "down": True}


def test_allows_and_overrides_autostop_with_down_false(db_session: Session, member: Actor) -> None:
    """A user who asked for `down: false` (never autodown) isn't "stricter" -- Krater's cap still applies."""
    _approved_project(db_session, member)
    task = {"resources": {"infra": "vast", "autostop": {"idle_minutes": 1, "down": False}}}

    decision = launch_policy.decide(_request(task=task), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["autostop"] == {"idle_minutes": 30, "down": True}


def test_pending_completion_review_and_completion_changes_requested_are_active(
    db_session: Session, member: Actor
) -> None:
    for status in (ProjectStatus.PENDING_COMPLETION_REVIEW, ProjectStatus.COMPLETION_CHANGES_REQUESTED):
        project = _make_project(db_session, member, status=status, workspace=f"ganymede-{status.value}")
        budget.add_entry(
            db_session, project=project, kind=BudgetEntryKind.INITIAL_APPROVAL, amount_cents=1_000, actor=member
        )

        decision = launch_policy.decide(_request(workspace=f"ganymede-{status.value}"), db_session, SETTINGS)

        assert isinstance(decision, launch_policy.Allow), status


# --------------------------------------------------------------------------------------------------
# `validate` (and other unenforced request names): mutations yes, rejection never
# --------------------------------------------------------------------------------------------------


def test_validate_is_never_rejected_for_missing_workspace(db_session: Session) -> None:
    decision = launch_policy.decide(_request(workspace=None, request_name="validate"), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)


def test_validate_is_never_rejected_for_unknown_workspace(db_session: Session) -> None:
    decision = launch_policy.decide(
        _request(workspace="ganymede-nonexistent", request_name="validate"), db_session, SETTINGS
    )

    assert isinstance(decision, launch_policy.Allow)


def test_validate_is_never_rejected_for_exhausted_budget(db_session: Session, member: Actor) -> None:
    project = _make_project(db_session, member, status=ProjectStatus.APPROVED)
    budget.add_entry(
        db_session, project=project, kind=BudgetEntryKind.INITIAL_APPROVAL, amount_cents=1_000, actor=member
    )
    db_session.add(
        SpendSnapshot(project_id=project.id, estimated_spend_cents=1_000, source=SpendSource.SKYPILOT_COST_REPORT)
    )
    db_session.flush()

    decision = launch_policy.decide(_request(request_name="validate"), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)


def test_validate_still_gets_the_same_mutations(db_session: Session) -> None:
    task = {"resources": {"infra": "vast", "max_hourly_cost": 999.0}}

    decision = launch_policy.decide(_request(task=task, workspace=None, request_name="validate"), db_session, SETTINGS)

    assert isinstance(decision, launch_policy.Allow)
    assert decision.task["resources"]["max_hourly_cost"] == 5.0
    assert decision.task["resources"]["autostop"] == {"idle_minutes": 30, "down": True}
