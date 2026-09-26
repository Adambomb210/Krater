"""Channel/message upkeep (`krater.services.slack_notify`), against `FakeSlackClient` -- creation,
invites, idempotency, decision updates, admin overrides, archiving and the periodic reconcile steps.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from krater.models import AuditEvent, ProjectStatus, ReviewDecision, ReviewSource, RevisionOutcome
from krater.services import projects, slack_notify
from krater.services.actor import Actor
from krater.slack.fake import FakeSlackClient
from krater.weave.types import WeaveUser


@dataclass
class _ListWeaveClient:
    """A minimal `WeaveClient` double: answers `list_users_in_group`/`get_user_by_slack_id` from a
    fixed list of `WeaveUser`s, built from the test's own `Actor`s."""

    users: list[WeaveUser]

    def get_user(self, sub: str) -> WeaveUser | None:
        return next((user for user in self.users if user.sub == sub), None)

    def get_user_by_slack_id(self, slack_id: str) -> WeaveUser | None:
        return next((user for user in self.users if user.slack_id == slack_id), None)

    def list_users_in_group(self, group: str) -> list[WeaveUser]:
        return [user for user in self.users if group in user.groups]


def _weave_user_for(actor: Actor, *, slack_id: str) -> WeaveUser:
    return WeaveUser(
        sub=actor.user.weave_sub,
        name=actor.user.display_name,
        email=actor.user.email,
        slack_id=slack_id,
        groups=actor.groups,
        active=True,
    )


def test_ensure_channel_creates_and_invites_the_team(db_session: Session, member: Actor, reviewer: Actor) -> None:
    member.user.slack_user_id = "U_MEMBER"
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)
    weave_client = _ListWeaveClient([_weave_user_for(reviewer, slack_id="U_REVIEWER")])
    slack_client = FakeSlackClient()

    channel_id = slack_notify.ensure_channel(db_session, slack_client, weave_client, project=project)

    assert project.slack_channel_id == channel_id
    assert slack_client.channels[channel_id]["name"] == slack_notify.channel_name_for(project)
    assert slack_client.channels[channel_id]["members"] == {"U_MEMBER", "U_REVIEWER"}


def test_ensure_channel_is_idempotent(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=1000)
    weave_client = _ListWeaveClient([_weave_user_for(reviewer, slack_id="U_REVIEWER")])
    slack_client = FakeSlackClient()

    first = slack_notify.ensure_channel(db_session, slack_client, weave_client, project=project)
    second = slack_notify.ensure_channel(db_session, slack_client, weave_client, project=project)

    assert first == second
    assert len(slack_client.channels) == 1


def test_notify_revision_submitted_posts_a_review_message_and_a_feed_line(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    project = projects.create_project(
        db_session, member, title="Rover", write_up="A rover.", budget_requested_cents=5000
    )
    project = projects.submit(db_session, member, project=project)
    revision = project.current_revision
    weave_client = _ListWeaveClient([_weave_user_for(reviewer, slack_id="U_REVIEWER")])
    slack_client = FakeSlackClient()

    slack_notify.notify_revision_submitted(
        db_session, slack_client, weave_client, revision=revision, feed_channel_id="C_FEED"
    )

    assert revision.slack_message_ts is not None
    channel_id = revision.slack_message_channel_id
    assert (channel_id, revision.slack_message_ts) in slack_client.messages
    review_actions = slack_client.messages[(channel_id, revision.slack_message_ts)]["blocks"][-1]
    assert review_actions["elements"][0]["value"] == str(revision.id)

    feed_messages = [msg for (chan, _ts), msg in slack_client.messages.items() if chan == "C_FEED"]
    assert len(feed_messages) == 1
    assert "Rover" in feed_messages[0]["text"]


def test_notify_revision_submitted_is_idempotent(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project)
    revision = project.current_revision
    weave_client = _ListWeaveClient([_weave_user_for(reviewer, slack_id="U_REVIEWER")])
    slack_client = FakeSlackClient()

    slack_notify.notify_revision_submitted(
        db_session, slack_client, weave_client, revision=revision, feed_channel_id="C_FEED"
    )
    slack_notify.notify_revision_submitted(
        db_session, slack_client, weave_client, revision=revision, feed_channel_id="C_FEED"
    )

    assert len(slack_client.channels) == 1
    assert len(slack_client.messages) == 2  # the review message + the one feed line, not doubled


def test_no_feed_line_for_a_resubmission_after_rejection(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project)
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.REJECT,
        reason="no",
        source=ReviewSource.WEB,
    )
    db_session.refresh(project)
    projects.update_draft(db_session, member, project=project, write_up="revised")
    project = projects.submit(db_session, member, project=project)
    revision = project.current_revision
    assert revision.number == 2

    weave_client = _ListWeaveClient([])
    slack_client = FakeSlackClient()
    slack_notify.notify_revision_submitted(
        db_session, slack_client, weave_client, revision=revision, feed_channel_id="C_FEED"
    )

    feed_messages = [msg for (chan, _ts), msg in slack_client.messages.items() if chan == "C_FEED"]
    assert feed_messages == []


def test_no_feed_line_for_an_amendment(db_session: Session, member: Actor, reviewer: Actor) -> None:
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
    projects.start_amendment(db_session, member, project=project)
    project = projects.submit(db_session, member, project=project)
    revision = project.current_revision

    weave_client = _ListWeaveClient([])
    slack_client = FakeSlackClient()
    slack_notify.notify_revision_submitted(
        db_session, slack_client, weave_client, revision=revision, feed_channel_id="C_FEED"
    )

    feed_messages = [msg for (chan, _ts), msg in slack_client.messages.items() if chan == "C_FEED"]
    assert feed_messages == []


def test_notify_decision_updates_the_message_and_drops_the_buttons(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project)
    revision = project.current_revision
    weave_client = _ListWeaveClient([])
    slack_client = FakeSlackClient()
    slack_notify.notify_revision_submitted(
        db_session, slack_client, weave_client, revision=revision, feed_channel_id=None
    )

    projects.record_review(
        db_session, reviewer, revision=revision, decision=ReviewDecision.APPROVE, source=ReviewSource.WEB
    )
    db_session.refresh(revision)
    assert revision.outcome is RevisionOutcome.APPROVED

    slack_notify.notify_decision(db_session, slack_client, revision=revision)

    updated = slack_client.messages[(revision.slack_message_channel_id, revision.slack_message_ts)]
    assert not any(block.get("type") == "actions" for block in updated["blocks"])
    assert "Approved" in updated["text"]


def test_notify_decision_is_a_no_op_while_still_pending(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    project = projects.submit(db_session, member, project=project)
    revision = project.current_revision
    weave_client = _ListWeaveClient([])
    slack_client = FakeSlackClient()
    slack_notify.notify_revision_submitted(
        db_session, slack_client, weave_client, revision=revision, feed_channel_id=None
    )

    before = dict(slack_client.messages)
    slack_notify.notify_decision(db_session, slack_client, revision=revision)

    assert slack_client.messages == before


def test_post_admin_override_posts_to_the_channel(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")

    slack_notify.post_admin_override(
        db_session, slack_client, project=project, action="admin_approve", actor_name="Ana Admin", reason="urgent"
    )

    ((channel_id, _ts), message) = next(iter(slack_client.messages.items()))
    assert channel_id == project.slack_channel_id
    assert "admin_approve" in message["text"]
    assert "Ana Admin" in message["text"]
    assert "urgent" in message["text"]


def test_post_admin_override_is_a_no_op_without_a_channel(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()

    slack_notify.post_admin_override(
        db_session, slack_client, project=project, action="admin_withdraw", actor_name="Ana Admin", reason=None
    )

    assert slack_client.messages == {}


def test_archive_project_channel_is_idempotent(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    project.status = ProjectStatus.WITHDRAWN

    slack_notify.archive_project_channel(db_session, slack_client, project=project)
    assert project.slack_channel_archived is True
    assert slack_client.channels[project.slack_channel_id]["archived"] is True

    # A second call must not re-archive (harmless either way, but proves the guard works).
    slack_client.channels[project.slack_channel_id]["archived"] = False
    slack_notify.archive_project_channel(db_session, slack_client, project=project)
    assert slack_client.channels[project.slack_channel_id]["archived"] is False


def test_archive_project_channel_is_a_no_op_while_not_terminal(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")

    slack_notify.archive_project_channel(db_session, slack_client, project=project)

    assert project.slack_channel_archived is False
    assert slack_client.channels[project.slack_channel_id]["archived"] is False


def test_sync_reviewer_invites_invites_reviewers_to_open_channels(
    db_session: Session, member: Actor, reviewer: Actor
) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    weave_client = _ListWeaveClient([_weave_user_for(reviewer, slack_id="U_NEW_REVIEWER")])

    slack_notify.sync_reviewer_invites(db_session, slack_client, weave_client)

    assert "U_NEW_REVIEWER" in slack_client.channels[project.slack_channel_id]["members"]


def test_sync_reviewer_invites_skips_archived_channels(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    project.slack_channel_archived = True
    weave_client = _ListWeaveClient([_weave_user_for(reviewer, slack_id="U_NEW_REVIEWER")])

    slack_notify.sync_reviewer_invites(db_session, slack_client, weave_client)

    assert "U_NEW_REVIEWER" not in slack_client.channels[project.slack_channel_id]["members"]


def test_sync_budget_notifications_posts_a_warning_once(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    event = AuditEvent(
        actor_id=None,
        action=slack_notify.AUDIT_BUDGET_WARNING,
        project_id=project.id,
        payload={"ceiling_cents": 5000, "spend_cents": 4000, "percent": 80.0},
    )
    db_session.add(event)
    db_session.flush()

    slack_notify.sync_budget_notifications(db_session, slack_client)
    assert len(slack_client.messages) == 1

    # A second reconcile pass must not post it again.
    slack_notify.sync_budget_notifications(db_session, slack_client)
    assert len(slack_client.messages) == 1


def test_sync_missed_archives_archives_finished_projects(db_session: Session, member: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    project.status = ProjectStatus.COMPLETED

    slack_notify.sync_missed_archives(db_session, slack_client)

    assert project.slack_channel_archived is True
    assert slack_client.channels[project.slack_channel_id]["archived"] is True


def test_reconcile_runs_every_step(db_session: Session, member: Actor, reviewer: Actor) -> None:
    project = projects.create_project(db_session, member, title="Rover", write_up="w", budget_requested_cents=5000)
    slack_client = FakeSlackClient()
    project.slack_channel_id = slack_client.create_channel("ganymede-rover-test")
    project.status = ProjectStatus.COMPLETED
    db_session.flush()
    db_session.commit()

    weave_client = _ListWeaveClient([_weave_user_for(reviewer, slack_id="U_REVIEWER")])
    slack_notify.reconcile(db_session, slack_client, weave_client)

    assert slack_client.channels[project.slack_channel_id]["archived"] is True
