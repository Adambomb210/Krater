"""Names and emails come from Weave when Krater needs them; Krater stores only the Patchwork ID
(patchworklabsorg/krater#7)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import AuditEvent, User
from krater.services import audit, people
from krater.services.actor import Actor
from krater.weave import StubWeaveClient, WeaveUnavailableError


class _DownWeave(StubWeaveClient):
    def get_user(self, sub: str, *, fresh: bool = False):
        raise WeaveUnavailableError("down")


def test_lookup_reads_the_name_and_email_from_weave(weave: StubWeaveClient) -> None:
    weave.put_user("PWL1AAAAAAAAA", name="Ada Lovelace", email="ada@example.com", roles=["member"])

    person = people.lookup(weave, "PWL1AAAAAAAAA")

    assert (person.name, person.email, person.label) == ("Ada Lovelace", "ada@example.com", "Ada Lovelace")


def test_lookup_falls_back_to_the_patchwork_id_when_weave_cant_say(weave: StubWeaveClient) -> None:
    assert people.lookup(weave, "PWL1UNKNOWN00").label == "PWL1UNKNOWN00"
    assert people.lookup(_DownWeave(), "PWL1AAAAAAAAA").label == "PWL1AAAAAAAAA"


def test_find_member_by_patchwork_id_with_no_weave_role(weave: StubWeaveClient) -> None:
    weave.put_user("PWL1AAAAAAAAA", name="Ada", email="Ada@Example.com", roles=[])

    assert people.find_member(weave, " pwl1aaaaaaaaa ").sub == "PWL1AAAAAAAAA"
    assert people.find_member(weave, "PWL0NOSUCHONE") is None
    assert people.find_member(weave, "") is None


def test_find_member_finds_nobody_by_email(weave: StubWeaveClient) -> None:
    # Weave's directory can't search by email, and there's no member list to match against.
    weave.put_user("PWL1AAAAAAAAA", name="Ada", email="ada@example.com", roles=[])

    assert people.is_email("ada@example.com")
    assert people.find_member(weave, "ada@example.com") is None


def test_find_member_skips_an_inactive_account(weave: StubWeaveClient) -> None:
    weave.put_user("PWL1LOCKED000", name="Locked", email="locked@example.com", roles=[], active=False)

    assert people.find_member(weave, "PWL1LOCKED000") is None


def test_user_for_creates_a_row_holding_only_the_patchwork_id(db_session: Session, weave: StubWeaveClient) -> None:
    weave.put_user("PWL1NEWCOMER0", name="New", email="new@example.com", roles=[])
    record = people.find_member(weave, "PWL1NEWCOMER0")

    user = people.user_for(db_session, record)

    assert user.weave_sub == "PWL1NEWCOMER0"
    assert people.user_for(db_session, record).id == user.id
    assert db_session.scalar(sa.select(sa.func.count()).select_from(User).where(User.weave_sub == user.weave_sub)) == 1


def test_users_table_has_no_member_data(db_session: Session) -> None:
    columns = {column["name"] for column in sa.inspect(db_session.connection()).get_columns("users")}

    assert not columns & {"display_name", "email", "email_verified"}


def test_audit_events_record_the_actor_patchwork_id(db_session: Session, admin: Actor) -> None:
    event = audit.record(db_session, admin, "admin_approve")
    system = audit.record(db_session, None, "budget_warning")

    assert db_session.get(AuditEvent, event.id).actor_sub == admin.user.weave_sub
    assert system.actor_sub is None
