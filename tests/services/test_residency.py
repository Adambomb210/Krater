"""The OSPool residency rule (`krater.services.residency`): only a team of verified US residents may use OSPool
capacity."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from krater.services import residency
from krater.services.actor import GROUP_MEMBER, Actor
from krater.services.errors import UsResidencyCheckUnavailable, UsResidencyRequired
from krater.weave import StubWeaveClient, WeaveUnavailableError
from tests.factories import new_project
from tests.services.conftest import add_to_team


class _DownWeave:
    """A Weave that can't be reached."""

    def get_user(self, sub: str, *, fresh: bool = False):
        raise WeaveUnavailableError("down")


@pytest.fixture
def lead(make_actor, weave: StubWeaveClient) -> Actor:
    actor = make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Ada Lead")
    weave.set_us_resident(actor.user.weave_sub, True)
    return actor


@pytest.fixture
def builder(make_actor) -> Actor:
    return make_actor(groups=frozenset({GROUP_MEMBER}), display_name="Bea Builder")


def _project(db_session: Session, lead: Actor, *builders: Actor):
    project = new_project(db_session, lead, title="Rover", write_up="A rover.", budget_requested_cents=10_000)
    for member in builders:
        add_to_team(db_session, project, member)
    return project


def test_a_team_of_verified_us_residents_passes(db_session: Session, lead: Actor, builder: Actor, weave) -> None:
    weave.set_us_resident(builder.user.weave_sub, True)
    project = _project(db_session, lead, builder)

    residency.require_us_residents(db_session, weave, project)

    status = residency.team_status_for_display(db_session, weave, project)
    assert status.all_residents
    assert status.team_size == 2


@pytest.mark.parametrize("answer", [False, None])
def test_one_member_without_a_yes_blocks_the_whole_team(
    db_session: Session, lead: Actor, builder: Actor, weave, answer
) -> None:
    weave.set_us_resident(builder.user.weave_sub, answer)
    project = _project(db_session, lead, builder)

    with pytest.raises(UsResidencyRequired) as exc_info:
        residency.require_us_residents(db_session, weave, project)

    assert exc_info.value.members == [(f"Bea Builder ({builder.user.weave_sub})", residency.REASON_NOT_RESIDENT)]
    assert "OSPool is open only to US residents" in str(exc_info.value)
    assert not residency.team_status_for_display(db_session, weave, project).all_residents


def test_a_member_weave_does_not_return_blocks(db_session: Session, lead: Actor, builder: Actor, weave) -> None:
    weave.remove_user(builder.user.weave_sub)
    project = _project(db_session, lead, builder)

    with pytest.raises(UsResidencyRequired) as exc_info:
        residency.require_us_residents(db_session, weave, project)

    assert exc_info.value.members == [(builder.user.weave_sub, residency.REASON_NOT_IN_WEAVE)]


def test_weave_down_fails_closed_and_the_page_shows_nothing(db_session: Session, lead: Actor) -> None:
    project = _project(db_session, lead)

    with pytest.raises(UsResidencyCheckUnavailable):
        residency.require_us_residents(db_session, _DownWeave(), project)
    assert residency.team_status_for_display(db_session, _DownWeave(), project) is None
