"""Members' names and emails, read from Weave when Krater shows or uses them.

Weave stores member data; Krater keeps only the Patchwork ID (`User.weave_sub`) and asks the directory
when a page needs a name (docs/weave-integration.md, patchworklabsorg/krater#7). The directory client
caches answers for a short time, so a page listing a few people costs a few lookups at most.

A name is for display, so a lookup that fails (Weave down, or the account gone) falls back to the
Patchwork ID instead of failing the page. Nothing here authorizes anything: that is
`krater.services.users.authorize`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import GalleryMinorNames, User
from krater.weave import WeaveClient, WeaveUnavailableError, WeaveUser

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Person:
    """What Weave says about one member right now. `email` is `None` when Weave didn't answer."""

    sub: str
    name: str
    email: str | None = None
    #: What Weave says about their age: `True` under 18, `None` when it didn't say (or didn't answer).
    minor: bool | None = None
    #: Whether Weave returned a record (with a name). When not, `name` is just the Patchwork ID.
    known: bool = True

    @property
    def label(self) -> str:
        return self.name or self.sub


def lookup(weave_client: WeaveClient, sub: str) -> Person:
    """The member with Patchwork ID `sub`, or just the ID when Weave can't say."""
    try:
        record = weave_client.get_user(sub)
    except WeaveUnavailableError:
        logger.warning("Weave unavailable; showing %s by Patchwork ID", sub)
        record = None
    if record is None:
        return Person(sub=sub, name=sub, known=False)
    return Person(
        sub=sub, name=record.name or sub, email=record.email or None, minor=record.minor, known=bool(record.name)
    )


def names(weave_client: WeaveClient, users: Iterable[User]) -> dict[uuid.UUID, str]:
    """`User.id` -> display name, for every user in `users`."""
    return {user.id: lookup(weave_client, user.weave_sub).label for user in users}


def public_names(
    weave_client: WeaveClient, users: Iterable[User], *, minors: GalleryMinorNames
) -> tuple[list[str], int]:
    """How the public gallery names `users`: (the names it shows, how many it leaves unnamed). Anyone Weave doesn't
    say is an adult counts as under 18 here, since this is public: a first name is all that unknown age costs. Someone
    Weave doesn't return (gone, consent withdrawn, or Weave down) is left unnamed: the fallback label is their
    Patchwork ID, which never goes on a public page."""
    shown: list[str] = []
    unnamed = 0
    for user in users:
        person = lookup(weave_client, user.weave_sub)
        if not person.known:
            unnamed += 1
        elif person.minor is False or minors is GalleryMinorNames.FULL:
            shown.append(person.label)
        elif minors is GalleryMinorNames.FIRST:
            shown.append(person.label.split()[0] if person.label.strip() else person.label)
        else:
            unnamed += 1
    return shown, unnamed


#: What a teammate search by email says. Weave's directory has no search by email, and with no member role in Weave
#: there is no list of members to search, so Krater finds people by Patchwork ID only.
NO_EMAIL_SEARCH = (
    "Krater can't find people by email. Ask them for their Patchwork ID: it's the Weave ID on their Weave "
    "account page (Who am I)."
)


def is_email(query: str) -> bool:
    return "@" in query


def find_member(weave_client: WeaveClient, query: str) -> WeaveUser | None:
    """The active Patchwork member whose Patchwork ID is `query`, or `None` (also for an email: see
    `NO_EMAIL_SEARCH`, which callers show instead of "not found").

    The directory returns only people Weave lets use Krater, and they are all Ganymede members. Raises
    `WeaveUnavailableError` if Weave can't answer: callers show an error rather than "not found".
    """
    query = query.strip()
    if not query or is_email(query):
        return None
    record = weave_client.get_user(query.upper(), fresh=True)
    return record if record is not None and record.active else None


def user_for(session: Session, record: WeaveUser) -> User:
    """The Krater user row for a Weave member, created (holding only the Patchwork ID) if they have never
    signed in to Krater. Flushes, doesn't commit."""
    user = session.scalar(sa.select(User).where(User.weave_sub == record.sub))
    if user is None:
        user = User(weave_sub=record.sub, roles_cached=sorted(record.roles))
        session.add(user)
        session.flush()
    return user


__all__ = ["NO_EMAIL_SEARCH", "Person", "find_member", "is_email", "lookup", "names", "public_names", "user_for"]
