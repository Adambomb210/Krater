"""Processing a Slack Approve/Reject interaction (`krater.services.slack_reviews`): the clicker is found
in Krater's own users (by stored Slack id, else by the verified email on their Slack profile), and their
roles and disabled flag are read fresh from Krater's database."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from krater.models import ProjectStatus, ReviewDecision, ReviewSource, RevisionOutcome
from krater.services import projects, slack_reviews
from krater.services.actor import GROUP_MEMBER, GROUP_REVIEWER, Actor
from krater.slack.fake import FakeSlackClient


def _submitted_revision(db_session: Session, member: Actor):
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project)
    # Mirrors production: the submitting web request commits before the Slack job (which may itself
    # roll back on a domain error) ever runs, in a fresh session of its own.
    db_session.commit()
    return project, project.current_revision


def _approve(db_session: Session, slack_client: FakeSlackClient, revision, slack_user_id: str) -> None:
    slack_reviews.process_approve(
        db_session,
        slack_client,
        revision_id=revision.id,
        slack_user_id=slack_user_id,
        response_url="https://hooks.example/1",
    )


def test_approve_by_a_linked_reviewer_records_a_slack_review(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    reviewer.user.slack_user_id = "U_REVIEWER"
    _project, revision = _submitted_revision(db_session, member)
    slack_client = FakeSlackClient()

    _approve(db_session, slack_client, revision, "U_REVIEWER")

    db_session.refresh(revision)
    assert revision.outcome is RevisionOutcome.APPROVED
    assert revision.reviews[0].source is ReviewSource.SLACK
    assert revision.reviews[0].reviewer_id == reviewer.user.id
    assert slack_client.ephemeral_messages == []


def test_an_unlinked_clicker_is_matched_by_the_verified_email_on_their_slack_profile(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    _project, revision = _submitted_revision(db_session, member)
    slack_client = FakeSlackClient()
    slack_client.register_email(reviewer.user.email.upper(), "U_BY_EMAIL")

    _approve(db_session, slack_client, revision, "U_BY_EMAIL")

    db_session.refresh(revision)
    assert revision.outcome is RevisionOutcome.APPROVED
    assert reviewer.user.slack_user_id == "U_BY_EMAIL"


def test_an_unverified_email_is_not_matched(db_session: Session, member: Actor, make_actor) -> None:
    unverified = make_actor(groups=frozenset({GROUP_MEMBER, GROUP_REVIEWER}), email_verified=False)
    _project, revision = _submitted_revision(db_session, member)
    slack_client = FakeSlackClient()
    slack_client.register_email(unverified.user.email, "U_UNVERIFIED")

    _approve(db_session, slack_client, revision, "U_UNVERIFIED")

    db_session.refresh(revision)
    assert revision.outcome is RevisionOutcome.PENDING
    assert "linked" in slack_client.ephemeral_messages[0][1].lower()
    assert unverified.user.slack_user_id is None


def test_approve_by_the_submitter_gets_an_ephemeral_error(db_session: Session, make_actor) -> None:
    # The submitter also happens to be a reviewer, so `record_review`'s self-review check (rather than
    # its reviewer-only check) is the one that fires.
    submitter_reviewer = make_actor(groups=frozenset({GROUP_MEMBER, GROUP_REVIEWER}), slack_user_id="U_MEMBER")
    _project, revision = _submitted_revision(db_session, submitter_reviewer)
    slack_client = FakeSlackClient()

    _approve(db_session, slack_client, revision, "U_MEMBER")

    db_session.refresh(revision)
    assert revision.outcome is RevisionOutcome.PENDING
    assert len(slack_client.ephemeral_messages) == 1
    response_url, text = slack_client.ephemeral_messages[0]
    assert response_url == "https://hooks.example/1"
    assert "own" in text.lower()


def test_unknown_slack_user_gets_an_ephemeral_error(db_session: Session, member: Actor) -> None:
    _project, revision = _submitted_revision(db_session, member)
    slack_client = FakeSlackClient()

    _approve(db_session, slack_client, revision, "U_UNKNOWN")

    db_session.refresh(revision)
    assert revision.outcome is RevisionOutcome.PENDING
    assert len(slack_client.ephemeral_messages) == 1
    assert "linked" in slack_client.ephemeral_messages[0][1].lower()


def test_a_disabled_reviewer_gets_an_ephemeral_error(db_session: Session, member: Actor, reviewer: Actor) -> None:
    reviewer.user.slack_user_id = "U_REVIEWER"
    reviewer.user.disabled_at = datetime.now(UTC)
    _project, revision = _submitted_revision(db_session, member)
    slack_client = FakeSlackClient()

    _approve(db_session, slack_client, revision, "U_REVIEWER")

    db_session.refresh(revision)
    assert revision.outcome is RevisionOutcome.PENDING
    assert slack_client.ephemeral_messages[0][1] == "Your Krater account has been disabled."


def test_a_linked_non_member_gets_an_ephemeral_error(db_session: Session, member: Actor, make_actor) -> None:
    make_actor(groups=frozenset({GROUP_REVIEWER}), slack_user_id="U_EX_MEMBER")
    _project, revision = _submitted_revision(db_session, member)
    slack_client = FakeSlackClient()

    _approve(db_session, slack_client, revision, "U_EX_MEMBER")

    db_session.refresh(revision)
    assert revision.outcome is RevisionOutcome.PENDING
    assert "not a Ganymede member" in slack_client.ephemeral_messages[0][1]


def test_a_member_without_the_reviewer_role_is_refused_by_record_review(
    db_session: Session, member: Actor, make_actor
) -> None:
    make_actor(groups=frozenset({GROUP_MEMBER}), slack_user_id="U_PLAIN")
    _project, revision = _submitted_revision(db_session, member)
    slack_client = FakeSlackClient()

    _approve(db_session, slack_client, revision, "U_PLAIN")

    db_session.refresh(revision)
    assert revision.outcome is RevisionOutcome.PENDING
    assert len(slack_client.ephemeral_messages) == 1


def test_reject_records_the_rejection_with_its_reason(db_session: Session, member: Actor, reviewer: Actor) -> None:
    reviewer.user.slack_user_id = "U_REVIEWER"
    _project, revision = _submitted_revision(db_session, member)
    slack_client = FakeSlackClient()

    slack_reviews.process_reject(
        db_session,
        slack_client,
        revision_id=revision.id,
        slack_user_id="U_REVIEWER",
        reason="Needs more detail.",
        response_url="https://hooks.example/1",
    )

    db_session.refresh(revision)
    assert revision.outcome is RevisionOutcome.REJECTED
    assert revision.reviews[0].reason == "Needs more detail."
    assert revision.reviews[0].source is ReviewSource.SLACK


def test_approve_that_completes_the_project_archives_the_channel(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    reviewer.user.slack_user_id = "U_REVIEWER"
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project)
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
    )
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED

    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    projects.start_completion(db_session, member, project=project)
    project = projects.submit_completion(db_session, member, project=project)
    completion_revision = project.current_revision

    _approve(db_session, slack_client, completion_revision, "U_REVIEWER")

    db_session.refresh(project)
    assert project.status is ProjectStatus.COMPLETED
    assert project.slack_channel_archived is True
    assert slack_client.channels[project.slack_channel_id]["archived"] is True
