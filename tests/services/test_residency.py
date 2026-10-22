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
    # A team member may read the message, so it names nobody.
    assert "Bea Builder" not in str(exc_info.value)
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


def test_a_pending_invitee_does_not_count(db_session: Session, lead: Actor, builder: Actor, weave) -> None:
    project = _project(db_session, lead)
    add_to_team(db_session, project, builder, accepted=False)

    residency.require_us_residents(db_session, weave, project)


class _SpyWeave:
    """Records the `fresh` flag of every directory lookup, then asks the real stub."""

    def __init__(self, inner: StubWeaveClient) -> None:
        self.inner = inner
        self.fresh_flags: list[bool] = []

    def get_user(self, sub: str, *, fresh: bool = False):
        self.fresh_flags.append(fresh)
        return self.inner.get_user(sub, fresh=fresh)


def test_the_gate_asks_weave_fresh_and_pages_use_the_cache(db_session: Session, lead: Actor, weave) -> None:
    project = _project(db_session, lead)
    spy = _SpyWeave(weave)

    residency.require_us_residents(db_session, spy, project)
    assert spy.fresh_flags == [True]

    spy.fresh_flags.clear()
    residency.team_status_for_display(db_session, spy, project)
    residency.resident_teams(db_session, spy, [project])
    assert spy.fresh_flags == [False, False]


def test_resident_teams_looks_each_member_up_once_and_stops_when_weave_is_down(
    db_session: Session, lead: Actor, builder: Actor, weave
) -> None:
    weave.set_us_resident(builder.user.weave_sub, True)
    first = _project(db_session, lead, builder)
    second = _project(db_session, lead)
    spy = _SpyWeave(weave)

    kept, unavailable = residency.resident_teams(db_session, spy, [first, second])

    assert kept == [first, second]
    assert unavailable is False
    assert len(spy.fresh_flags) == 2
    assert residency.resident_teams(db_session, _DownWeave(), [first, second]) == ([], True)
