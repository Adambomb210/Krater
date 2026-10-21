"""The demographics gate (`krater.services.demographics`): a member answers Weave's demographics form before they
submit a grant application (docs/weave-integration.md "Demographics gate")."""

from __future__ import annotations

import logging

import pytest

from krater.services import demographics
from krater.services.actor import Actor
from krater.services.errors import DemographicsCheckUnavailable, DemographicsRequired
from krater.weave import StubWeaveClient, WeaveUnavailableError


class _DownWeave:
    """A Weave that can't be reached."""

    def get_user(self, sub: str, *, fresh: bool = False):
        raise WeaveUnavailableError("down")


class _SpyWeave:
    """Records the `fresh` flag of every directory lookup, then asks the real stub."""

    def __init__(self, inner: StubWeaveClient) -> None:
        self.inner = inner
        self.fresh_flags: list[bool] = []

    def get_user(self, sub: str, *, fresh: bool = False):
        self.fresh_flags.append(fresh)
        return self.inner.get_user(sub, fresh=fresh)


@pytest.fixture
def demographics_logs(caplog, monkeypatch):
    """Captures the gate's warnings. Alembic's `fileConfig` (run by the `engine` fixture) disables loggers that
    already exist, so turn this one back on."""
    monkeypatch.setattr(demographics.logger, "disabled", False)
    caplog.set_level(logging.WARNING, logger="krater.services.demographics")
    return caplog


def test_a_member_who_answered_passes(member: Actor, weave: StubWeaveClient) -> None:
    weave.set_demographics_answered(member.user.weave_sub, True)

    demographics.require_answered(weave, member.user)


def test_a_member_who_has_not_answered_is_refused(member: Actor, weave: StubWeaveClient) -> None:
    weave.set_demographics_answered(member.user.weave_sub, False)

    with pytest.raises(DemographicsRequired, match="demographics form in Weave"):
        demographics.require_answered(weave, member.user)


def test_a_record_without_the_field_passes_with_a_warning(
    member: Actor, weave: StubWeaveClient, demographics_logs
) -> None:
    # Older Weave versions leave the field out.
    weave.set_demographics_answered(member.user.weave_sub, None)

    demographics.require_answered(weave, member.user)

    assert any(
        record.levelno == logging.WARNING and member.user.weave_sub in record.getMessage()
        for record in demographics_logs.records
    )


def test_a_member_weave_does_not_return_is_refused(member: Actor, weave: StubWeaveClient) -> None:
    weave.remove_user(member.user.weave_sub)

    with pytest.raises(DemographicsRequired):
        demographics.require_answered(weave, member.user)


def test_weave_down_fails_closed(member: Actor) -> None:
    with pytest.raises(DemographicsCheckUnavailable):
        demographics.require_answered(_DownWeave(), member.user)


def test_the_gate_skips_the_directory_cache(member: Actor, weave: StubWeaveClient) -> None:
    weave.set_demographics_answered(member.user.weave_sub, True)
    spy = _SpyWeave(weave)

    demographics.require_answered(spy, member.user)

    assert spy.fresh_flags == [True]
