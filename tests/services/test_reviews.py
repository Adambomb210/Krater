"""Review recording: self-review, double review, non-reviewer, and reject/resubmit."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from krater.models import ApprovalPolicy, ApprovalStage, ProjectStatus, ReviewDecision, ReviewSource, RevisionOutcome
from krater.services import projects
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed


def _submitted_project(db_session: Session, member: Actor, *, budget_requested_cents: int = 10_000):
    project = projects.create_project(
        db_session,
        member,
        title="Test Project",
        write_up="Write-up.",
        budget_requested_cents=budget_requested_cents,
    )
    project = projects.submit(db_session, member, project=project)
    return project


def test_non_reviewer_cannot_record_a_review(db_session: Session, member: Actor, make_actor) -> None:
    project = _submitted_project(db_session, member)
    plain_member = make_actor(groups=frozenset({"ganymede:member"}))

    with pytest.raises(NotAllowed):
        projects.record_review(
            db_session,
            plain_member,
            revision=project.current_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )


def test_submitter_cannot_review_their_own_submission(db_session: Session, make_actor) -> None:
    submitter_and_reviewer = make_actor(groups=frozenset({"ganymede:member", "ganymede:reviewer"}))
    project = _submitted_project(db_session, submitter_and_reviewer)

    with pytest.raises(NotAllowed):
        projects.record_review(
            db_session,
            submitter_and_reviewer,
            revision=project.current_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )


def test_credited_builder_cannot_review(db_session: Session, member: Actor, make_actor) -> None:
    builder = make_actor(groups=frozenset({"ganymede:member", "ganymede:reviewer"}))
    project = _submitted_project(db_session, member)
    project.current_revision.credited_builder_ids = [builder.user.id]
    db_session.flush()

    with pytest.raises(NotAllowed):
        projects.record_review(
            db_session,
            builder,
            revision=project.current_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )


def test_double_review_by_the_same_reviewer_is_blocked(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = _submitted_project(db_session, member)
    # First approval alone would satisfy the default policy and close out the revision, so use a
    # multi-approval policy to keep it open for a second decision attempt.
    db_session.add(ApprovalPolicy(stage=ApprovalStage.PROPOSAL, min_approvals=2))
    db_session.flush()

    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
    )

    with pytest.raises(InvalidState):
        projects.record_review(
            db_session,
            reviewer,
            revision=project.current_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )


def test_reject_requires_a_reason(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = _submitted_project(db_session, member)

    with pytest.raises(ValidationFailed):
        projects.record_review(
            db_session,
            reviewer,
            revision=project.current_revision,
            decision=ReviewDecision.REJECT,
            source=ReviewSource.WEB,
        )


def test_a_single_reject_rejects_the_revision_immediately_and_opens_the_next_draft(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    project = _submitted_project(db_session, member, budget_requested_cents=10_000)
    rejected_revision = project.current_revision

    projects.record_review(
        db_session,
        reviewer,
        revision=rejected_revision,
        decision=ReviewDecision.REJECT,
        reason="Needs more detail.",
        source=ReviewSource.WEB,
    )

    db_session.refresh(project)
    assert project.status is ProjectStatus.CHANGES_REQUESTED
    assert rejected_revision.outcome is RevisionOutcome.REJECTED

    new_draft = project.current_revision
    assert new_draft.id != rejected_revision.id
    assert new_draft.number == rejected_revision.number + 1
    assert new_draft.submitted_at is None
    assert new_draft.budget_requested_cents == 10_000


def test_resubmit_after_reject_does_not_count_old_reviews(db_session: Session, member: Actor, make_actor) -> None:
    """A reviewer's rejection of an earlier revision must not carry over to (or block approval of) the
    resubmitted one -- only the current revision's own reviews count toward the policy."""
    reviewer_a = make_actor(groups=frozenset({"ganymede:member", "ganymede:reviewer"}))
    reviewer_b = make_actor(groups=frozenset({"ganymede:member", "ganymede:reviewer"}))

    project = _submitted_project(db_session, member)
    first_revision = project.current_revision

    projects.record_review(
        db_session,
        reviewer_a,
        revision=first_revision,
        decision=ReviewDecision.REJECT,
        reason="Not ready.",
        source=ReviewSource.WEB,
    )
    db_session.refresh(project)
    assert project.status is ProjectStatus.CHANGES_REQUESTED

    projects.update_draft(db_session, member, project=project, write_up="Revised write-up.")
    project = projects.submit(db_session, member, project=project)
    second_revision = project.current_revision
    assert second_revision.id != first_revision.id

    # The default policy (1 approval, any reviewer) is satisfied by reviewer_b's single approval, even
    # though reviewer_a already rejected the *first* revision.
    projects.record_review(
        db_session, reviewer_b, revision=second_revision, decision=ReviewDecision.APPROVE, source=ReviewSource.WEB
    )
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED
    assert project.approved_revision_id == second_revision.id


def test_cannot_review_a_non_current_revision(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = _submitted_project(db_session, member)
    old_revision = project.current_revision

    projects.record_review(
        db_session,
        reviewer,
        revision=old_revision,
        decision=ReviewDecision.REJECT,
        reason="Needs work.",
        source=ReviewSource.WEB,
    )

    with pytest.raises(InvalidState):
        projects.record_review(
            db_session,
            reviewer,
            revision=old_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )


def test_cannot_review_a_draft_revision(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = projects.create_project(db_session, member, title="T", write_up="W", budget_requested_cents=1_000)

    with pytest.raises(InvalidState):
        projects.record_review(
            db_session,
            reviewer,
            revision=project.current_revision,
            decision=ReviewDecision.APPROVE,
            source=ReviewSource.WEB,
        )
