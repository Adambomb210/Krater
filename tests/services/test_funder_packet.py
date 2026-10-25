"""The funder packet (`krater.services.funder_packet`): which projects an admin can share with OSPool, what a
funder sees of them, and the audit of every export."""

from __future__ import annotations

import dataclasses

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import AuditEvent, GalleryMinorNames, ProjectStatus, ReviewDecision, ReviewSource
from krater.services import funder_packet, projects, site_settings
from krater.services.actor import GROUP_MEMBER, Actor
from krater.services.errors import FunderPacketUnavailable, NotAllowed, ValidationFailed
from krater.weave import StubWeaveClient, WeaveUnavailableError
from tests.factories import estimate, new_project
from tests.services.conftest import add_to_team


class _DownWeave:
    """A Weave that can't be reached."""

    def get_user(self, sub: str, *, fresh: bool = False):
        raise WeaveUnavailableError("down")


@pytest.fixture
def lead(make_actor, weave: StubWeaveClient) -> Actor:
    actor = make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Ada Lead", email="ada@example.com")
    weave.set_us_resident(actor.user.weave_sub, True)
    weave.set_minor(actor.user.weave_sub, False)
    return actor


@pytest.fixture
def builder(make_actor, weave: StubWeaveClient) -> Actor:
    actor = make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Bea Builder")
    weave.set_us_resident(actor.user.weave_sub, True)
    weave.set_minor(actor.user.weave_sub, False)
    return actor


def _submitted(db_session: Session, lead: Actor, *team: Actor, title: str = "Rover", **fields):
    project = new_project(db_session, lead, title=title, write_up="A rover.", budget_requested_cents=1_000, **fields)
    for member in team:
        add_to_team(db_session, project, member)
    return projects.submit(db_session, lead, project=project)


def _approved(db_session: Session, weave, reviewer: Actor, lead: Actor, *team: Actor, **fields):
    project = _submitted(db_session, lead, *team, **fields)
    projects.record_review(
        db_session,
        reviewer,
        revision=project.current_revision,
        decision=ReviewDecision.APPROVE,
        source=ReviewSource.WEB,
        weave_client=weave,
    )
    db_session.refresh(project)
    assert project.status is ProjectStatus.APPROVED
    return project


def test_an_approved_team_of_us_residents_is_in_the_packet(
    db_session: Session, weave, admin: Actor, reviewer: Actor, lead: Actor, builder: Actor
) -> None:
    project = _approved(
        db_session,
        weave,
        reviewer,
        lead,
        builder,
        summary="Maps caves.",
        compute_use="Training a mapping model.",
        budget_estimate=estimate(1_000, name="RTX4090", hours=12.0),
    )

    packet = funder_packet.build(db_session, weave, admin)

    assert packet.funder == "ospool"
    assert packet.left_out == 0
    [row] = packet.rows
    assert row.project_id == project.id
    assert (row.title, row.summary, row.compute_use) == ("Rover", "Maps caves.", "Training a mapping model.")
    assert row.status is ProjectStatus.APPROVED
    assert (row.requested_cents, row.granted_cents) == (1_000, 1_000)
    assert row.gpu.text == "1× RTX4090 × 12h, interruptible"
    assert row.team_names == ["Ada Lead", "Bea Builder"]
    assert (row.unnamed, row.team_size) == (0, 2)


def test_the_packet_holds_no_emails_or_patchwork_ids(
    db_session: Session, weave, admin: Actor, reviewer: Actor, lead: Actor
) -> None:
    _approved(db_session, weave, reviewer, lead)

    [row] = funder_packet.build(db_session, weave, admin).rows

    shared = repr(dataclasses.asdict(row))
    assert "ada@example.com" not in shared
    assert lead.user.weave_sub not in shared


def test_one_member_who_is_not_a_verified_us_resident_keeps_the_project_out(
    db_session: Session, weave, admin: Actor, reviewer: Actor, lead: Actor, builder: Actor
) -> None:
    weave.set_us_resident(builder.user.weave_sub, None)
    _approved(db_session, weave, reviewer, lead, builder)

    packet = funder_packet.build(db_session, weave, admin)

    assert packet.rows == []
    assert packet.left_out == 1


def test_pending_proposals_are_in_only_when_asked_for(db_session: Session, weave, admin: Actor, lead: Actor) -> None:
    project = _submitted(db_session, lead)

    assert funder_packet.build(db_session, weave, admin).rows == []
    [row] = funder_packet.build(db_session, weave, admin, include_pending=True).rows
    assert row.project_id == project.id
    assert row.status_label == "Waiting for review"
    assert row.granted_cents == 0


@pytest.mark.parametrize("status", [ProjectStatus.DRAFT, ProjectStatus.WITHDRAWN, ProjectStatus.COMPLETED])
def test_drafts_withdrawn_and_completed_projects_are_never_in(
    db_session: Session, weave, admin: Actor, lead: Actor, status: ProjectStatus
) -> None:
    project = _submitted(db_session, lead)
    project.status = status
    db_session.flush()

    assert funder_packet.build(db_session, weave, admin, include_pending=True).rows == []


def test_the_admin_can_narrow_the_packet_to_chosen_projects(
    db_session: Session, weave, admin: Actor, reviewer: Actor, lead: Actor
) -> None:
    chosen = _approved(db_session, weave, reviewer, lead, title="Chosen")
    _approved(db_session, weave, reviewer, lead, title="Other")

    packet = funder_packet.build(db_session, weave, admin, project_ids=[chosen.id])

    assert [row.title for row in packet.rows] == ["Chosen"]


def test_an_approved_project_shows_what_was_approved_not_a_pending_amendment(
    db_session: Session, weave, admin: Actor, reviewer: Actor, lead: Actor
) -> None:
    project = _approved(db_session, weave, reviewer, lead)
    amendment = projects.start_amendment(db_session, lead, project=project)
    projects.update_draft(db_session, lead, project=project, budget_requested_cents=5_000)
    assert amendment.budget_requested_cents == 5_000

    [row] = funder_packet.build(db_session, weave, admin).rows

    assert row.requested_cents == 1_000


@pytest.mark.parametrize(
    ("setting", "names", "unnamed"),
    [
        (GalleryMinorNames.FULL, ["Ada Lead", "Bea Builder"], 0),
        (GalleryMinorNames.FIRST, ["Ada Lead", "Bea"], 0),
        (GalleryMinorNames.HIDDEN, ["Ada Lead"], 1),
    ],
)
def test_minors_are_named_as_the_gallery_setting_says(
    db_session: Session,
    weave,
    admin: Actor,
    reviewer: Actor,
    lead: Actor,
    builder: Actor,
    setting: GalleryMinorNames,
    names: list[str],
    unnamed: int,
) -> None:
    weave.set_minor(builder.user.weave_sub, True)
    weave.set_guardian_verified(builder.user.weave_sub, True)
    _approved(db_session, weave, reviewer, lead, builder)
    site_settings.set_gallery_minor_names(db_session, admin, value=setting.value, reason="Test")

    [row] = funder_packet.build(db_session, weave, admin).rows

    assert (row.team_names, row.unnamed) == (names, unnamed)


def test_only_an_admin_may_build_a_packet(db_session: Session, weave, reviewer: Actor) -> None:
    with pytest.raises(NotAllowed):
        funder_packet.build(db_session, weave, reviewer)


def test_the_packet_fails_closed_when_weave_is_down(
    db_session: Session, weave, admin: Actor, reviewer: Actor, lead: Actor
) -> None:
    _approved(db_session, weave, reviewer, lead)

    with pytest.raises(FunderPacketUnavailable):
        funder_packet.build(db_session, _DownWeave(), admin)


def test_an_export_is_audited_with_its_format_and_projects(
    db_session: Session, weave, admin: Actor, reviewer: Actor, lead: Actor
) -> None:
    project = _approved(db_session, weave, reviewer, lead)
    packet = funder_packet.build(db_session, weave, admin)

    funder_packet.record_export(db_session, admin, packet, file_format="pdf")

    event = db_session.scalars(sa.select(AuditEvent).where(AuditEvent.action == funder_packet.AUDIT_EXPORT)).one()
    assert event.actor_id == admin.user.id
    assert event.payload == {
        "funder": "ospool",
        "format": "pdf",
        "include_pending": False,
        "project_ids": [str(project.id)],
    }


def test_an_empty_packet_or_an_unknown_format_is_refused(
    db_session: Session, weave, admin: Actor, reviewer: Actor, lead: Actor
) -> None:
    empty = funder_packet.build(db_session, weave, admin)
    with pytest.raises(ValidationFailed):
        funder_packet.record_export(db_session, admin, empty, file_format="csv")

    _approved(db_session, weave, reviewer, lead)
    packet = funder_packet.build(db_session, weave, admin)
    with pytest.raises(ValidationFailed):
        funder_packet.record_export(db_session, admin, packet, file_format="xlsx")

    assert (
        db_session.scalar(
            sa.select(sa.func.count()).select_from(AuditEvent).where(AuditEvent.action == funder_packet.AUDIT_EXPORT)
        )
        == 0
    )


def test_only_an_admin_may_record_an_export(
    db_session: Session, weave, admin: Actor, reviewer: Actor, lead: Actor
) -> None:
    _approved(db_session, weave, reviewer, lead)
    packet = funder_packet.build(db_session, weave, admin)

    with pytest.raises(NotAllowed):
        funder_packet.record_export(db_session, reviewer, packet, file_format="csv")
