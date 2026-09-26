"""Processing a Slack Approve/Reject interaction (`krater.services.slack_reviews`)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from krater.models import ProjectStatus, ReviewDecision, ReviewSource, RevisionOutcome
from krater.services import projects, slack_reviews
from krater.services.actor import GROUP_MEMBER, GROUP_REVIEWER, Actor
from krater.slack.fake import FakeSlackClient
from krater.weave.types import WeaveUser


@dataclass
class _SlackWeaveClient:
    """A minimal `WeaveClient` double: `get_user_by_slack_id` from a fixed `slack_id -> WeaveUser` map."""

    by_slack_id: dict[str, WeaveUser]

    def get_user(self, sub: str) -> WeaveUser | None:
        return next((user for user in self.by_slack_id.values() if user.sub == sub), None)

    def get_user_by_slack_id(self, slack_id: str) -> WeaveUser | None:
        return self.by_slack_id.get(slack_id)

    def list_users_in_group(self, group: str) -> list[WeaveUser]:
        return [user for user in self.by_slack_id.values() if group in user.groups]


def _weave_user(actor: Actor, *, slack_id: str, active: bool = True) -> WeaveUser:
    return WeaveUser(
        sub=actor.user.weave_sub,
        name=actor.user.display_name,
        email=actor.user.email,
        slack_id=slack_id,
        groups=actor.groups,
        active=active,
    )


def _submitted_revision(db_session: Session, member: Actor):
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project)
    # Mirrors production: the submitting web request commits before the Slack job (which may itself
    # roll back on a domain error) ever runs, in a fresh session of its own.
    db_session.commit()
    return project, project.current_revision


def test_approve_by_a_reviewer_records_a_slack_review(db_session: Session, member: Actor, reviewer: Actor) -> None:
    _project, revision = _submitted_revision(db_session, member)
    weave_client = _SlackWeaveClient({"U_REVIEWER": _weave_user(reviewer, slack_id="U_REVIEWER")})
    slack_client = FakeSlackClient()

    slack_reviews.process_approve(
        db_session,
        slack_client,
        weave_client,
        revision_id=revision.id,
        slack_user_id="U_REVIEWER",
        response_url="https://hooks.example/1",
    )

    db_session.refresh(revision)
    assert revision.outcome is RevisionOutcome.APPROVED
    assert revision.reviews[0].source is ReviewSource.SLACK
    assert slack_client.ephemeral_messages == []


def test_approve_by_the_submitter_gets_an_ephemeral_error(db_session: Session, make_actor) -> None:
    # The submitter also happens to be a reviewer, so `record_review`'s self-review check (rather than
    # its reviewer-only check) is the one that fires.
    submitter_reviewer = make_actor(groups=frozenset({GROUP_MEMBER, GROUP_REVIEWER}))
    _project, revision = _submitted_revision(db_session, submitter_reviewer)
    weave_client = _SlackWeaveClient({"U_MEMBER": _weave_user(submitter_reviewer, slack_id="U_MEMBER")})
    slack_client = FakeSlackClient()

    slack_reviews.process_approve(
        db_session,
        slack_client,
        weave_client,
        revision_id=revision.id,
        slack_user_id="U_MEMBER",
        response_url="https://hooks.example/1",
    )

    db_session.refresh(revision)
    assert revision.outcome is RevisionOutcome.PENDING
    assert len(slack_client.ephemeral_messages) == 1
    response_url, text = slack_client.ephemeral_messages[0]
    assert response_url == "https://hooks.example/1"
    assert "own" in text.lower()


def test_unknown_slack_user_gets_an_ephemeral_error(db_session: Session, member: Actor) -> None:
    _project, revision = _submitted_revision(db_session, member)
    weave_client = _SlackWeaveClient({})
    slack_client = FakeSlackClient()

    slack_reviews.process_approve(
        db_session,
        slack_client,
        weave_client,
        revision_id=revision.id,
        slack_user_id="U_UNKNOWN",
        response_url="https://hooks.example/1",
    )

    db_session.refresh(revision)
    assert revision.outcome is RevisionOutcome.PENDING
    assert len(slack_client.ephemeral_messages) == 1
    assert "linked" in slack_client.ephemeral_messages[0][1].lower()


def test_inactive_weave_user_gets_an_ephemeral_error(db_session: Session, member: Actor, reviewer: Actor) -> None:
    _project, revision = _submitted_revision(db_session, member)
    weave_client = _SlackWeaveClient({"U_REVIEWER": _weave_user(reviewer, slack_id="U_REVIEWER", active=False)})
    slack_client = FakeSlackClient()

    slack_reviews.process_approve(
        db_session,
        slack_client,
        weave_client,
        revision_id=revision.id,
        slack_user_id="U_REVIEWER",
        response_url="https://hooks.example/1",
    )

    db_session.refresh(revision)
    assert revision.outcome is RevisionOutcome.PENDING
    assert len(slack_client.ephemeral_messages) == 1


def test_reject_records_the_rejection_with_its_reason(db_session: Session, member: Actor, reviewer: Actor) -> None:
    _project, revision = _submitted_revision(db_session, member)
    weave_client = _SlackWeaveClient({"U_REVIEWER": _weave_user(reviewer, slack_id="U_REVIEWER")})
    slack_client = FakeSlackClient()

    slack_reviews.process_reject(
        db_session,
        slack_client,
        weave_client,
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

    weave_client = _SlackWeaveClient({"U_REVIEWER": _weave_user(reviewer, slack_id="U_REVIEWER")})
    slack_reviews.process_approve(
        db_session,
        slack_client,
        weave_client,
        revision_id=completion_revision.id,
        slack_user_id="U_REVIEWER",
        response_url="https://hooks.example/1",
    )

    db_session.refresh(project)
    assert project.status is ProjectStatus.COMPLETED
    assert project.slack_channel_archived is True
    assert slack_client.channels[project.slack_channel_id]["archived"] is True
