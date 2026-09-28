"""Auth dependencies for HTML routes: who's signed in, and which Krater roles they hold right now.

- `current_user`: the signed-in `User`, or `None`. For pages that render differently either way (e.g. the
  header's "sign in" link vs. the user's name) without requiring sign-in.
- `require_user`: the signed-in `User`, or a redirect to `/login?next=<this page>`. For any page that
  needs *a* signed-in user but doesn't itself gate on roles.
- `session_actor`: an `Actor` with the user's current roles, refusing nobody. **Display and navigation
  only** -- e.g. deciding whether to show a "Review" link. Never use it to authorize an action.
- `fresh_actor`: an `Actor` from `krater.services.roles.authorize`. Refuses (403) if the account is
  disabled or no longer holds `ganymede:member`. **Every state-changing action must use this, or one of
  the two below, instead of `session_actor`.**
- `require_reviewer` / `require_admin`: `fresh_actor`, plus a 403 unless the actor is a reviewer/admin.

Roles and the disabled flag live in Krater's database (`krater.services.roles`), so both actors read them
on every request; there's no Weave call involved.
"""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.models import User
from krater.services import roles
from krater.services.actor import Actor
from krater.services.errors import NotAllowed

SESSION_USER_ID_KEY = "user_id"


def current_user(request: Request, db_session: Annotated[Session, Depends(get_session)]) -> User | None:
    """The signed-in `User`, or `None` if there isn't one (no session, or it points at a deleted user)."""
    raw_user_id = request.session.get(SESSION_USER_ID_KEY)
    if not raw_user_id:
        return None
    try:
        user_id = uuid.UUID(raw_user_id)
    except ValueError:
        return None
    return db_session.get(User, user_id)


def require_user(request: Request, user: Annotated[User | None, Depends(current_user)]) -> User:
    """The signed-in `User`. Redirects (303) to `/login?next=<this request's path>` if signed out."""
    if user is not None:
        return user

    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    raise HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": f"/login?next={quote(next_path, safe='')}"},
    )


def session_actor(
    user: Annotated[User, Depends(require_user)], db_session: Annotated[Session, Depends(get_session)]
) -> Actor:
    """An `Actor` with the user's current roles. Display/navigation only -- see the module docstring."""
    return roles.actor_for(db_session, user)


def fresh_actor(
    user: Annotated[User, Depends(require_user)], db_session: Annotated[Session, Depends(get_session)]
) -> Actor:
    """An `Actor` for authorizing an action. Refuses (403) a disabled account or a non-member. Use this
    (or `require_reviewer`/`require_admin`) for state-changing actions."""
    try:
        return roles.authorize(db_session, user)
    except NotAllowed as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


def require_reviewer(actor: Annotated[Actor, Depends(fresh_actor)]) -> Actor:
    """`fresh_actor`, plus a 403 unless the actor is a Ganymede reviewer."""
    if not actor.is_reviewer:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="reviewer access required")
    return actor


def require_admin(actor: Annotated[Actor, Depends(fresh_actor)]) -> Actor:
    """`fresh_actor`, plus a 403 unless the actor is a Ganymede admin."""
    if not actor.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin access required")
    return actor


__all__ = [
    "current_user",
    "fresh_actor",
    "require_admin",
    "require_reviewer",
    "require_user",
    "session_actor",
]
