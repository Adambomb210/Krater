"""Admin user management: `/admin/users`, admins only. Krater's roles (`ganymede:member`,
`ganymede:reviewer`, `ganymede:admin`), grants by email for people who haven't signed in yet, disabling
accounts, and linking a Slack account by hand. Every rule lives in `krater.services.roles`."""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.models import AuditEvent
from krater.services import roles
from krater.services.actor import Actor
from krater.services.errors import InvalidState, ValidationFailed
from krater.web.csrf import verify_csrf_token
from krater.web.deps import require_admin
from krater.web.flash import flash
from krater.web.templates import templates

router = APIRouter()

_USERS_PATH = "/admin/users"

#: Audit actions shown in a user's history on their admin page.
_USER_AUDIT_ACTIONS = (
    roles.AUDIT_ROLE_GRANT,
    roles.AUDIT_ROLE_REVOKE,
    roles.AUDIT_PENDING_ROLE_APPLY,
    roles.AUDIT_USER_DISABLE,
    roles.AUDIT_USER_ENABLE,
    roles.AUDIT_SLACK_USER_ID_SET,
)


def _user_path(user_id: uuid.UUID) -> str:
    return f"{_USERS_PATH}/{user_id}"


def _done(db_session: Session, request: Request, location: str, message: str) -> RedirectResponse:
    db_session.commit()
    flash(request, message, "success")
    return RedirectResponse(location, status_code=303)


def _refused(
    db_session: Session, request: Request, location: str, exc: InvalidState | ValidationFailed
) -> RedirectResponse:
    db_session.rollback()
    message = "; ".join(exc.errors.values()) if isinstance(exc, ValidationFailed) else str(exc)
    flash(request, message, "error")
    return RedirectResponse(location, status_code=303)


@router.get(_USERS_PATH)
def list_users(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
):
    del actor  # `require_admin` is the gate
    return templates.TemplateResponse(
        request,
        "admin/users.html",
        {
            "users": roles.list_users_with_roles(db_session),
            "pending_grants": roles.list_pending_grants(db_session),
            "known_roles": roles.KNOWN_ROLES,
        },
    )


@router.get(_USERS_PATH + "/{user_id}")
def user_detail(
    request: Request,
    user_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
):
    user = roles.get_user(db_session, user_id=user_id)
    history = list(
        db_session.scalars(
            sa.select(AuditEvent)
            .where(AuditEvent.action.in_(_USER_AUDIT_ACTIONS), AuditEvent.payload["user_id"].astext == str(user.id))
            .order_by(AuditEvent.created_at.desc())
            .limit(50)
        )
    )
    return templates.TemplateResponse(
        request,
        "admin/user.html",
        {
            "subject": user,
            "subject_roles": roles.roles_for(db_session, user),
            "known_roles": roles.KNOWN_ROLES,
            "is_self": user.id == actor.user.id,
            "history": history,
        },
    )


@router.post(_USERS_PATH + "/{user_id}/roles/grant", dependencies=[Depends(verify_csrf_token)])
def grant_role(
    request: Request,
    user_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
    role: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
):
    user = roles.get_user(db_session, user_id=user_id)
    try:
        added = roles.grant_role(db_session, actor, user=user, role=role, reason=reason)
    except ValidationFailed as exc:
        return _refused(db_session, request, _user_path(user_id), exc)
    message = f"Granted {role} to {user.display_name}." if added else f"{user.display_name} already has {role}."
    return _done(db_session, request, _user_path(user_id), message)


@router.post(_USERS_PATH + "/{user_id}/roles/revoke", dependencies=[Depends(verify_csrf_token)])
def revoke_role(
    request: Request,
    user_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
    role: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
):
    user = roles.get_user(db_session, user_id=user_id)
    try:
        removed = roles.revoke_role(db_session, actor, user=user, role=role, reason=reason)
    except (InvalidState, ValidationFailed) as exc:
        return _refused(db_session, request, _user_path(user_id), exc)
    message = f"Removed {role} from {user.display_name}." if removed else f"{user.display_name} didn't have {role}."
    return _done(db_session, request, _user_path(user_id), message)


@router.post(_USERS_PATH + "/{user_id}/disable", dependencies=[Depends(verify_csrf_token)])
def disable_user(
    request: Request,
    user_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
    reason: Annotated[str, Form()] = "",
):
    user = roles.get_user(db_session, user_id=user_id)
    try:
        roles.disable_user(db_session, actor, user=user, reason=reason)
    except (InvalidState, ValidationFailed) as exc:
        return _refused(db_session, request, _user_path(user_id), exc)
    return _done(db_session, request, _user_path(user_id), f"Disabled {user.display_name}.")


@router.post(_USERS_PATH + "/{user_id}/enable", dependencies=[Depends(verify_csrf_token)])
def enable_user(
    request: Request,
    user_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
    reason: Annotated[str, Form()] = "",
):
    user = roles.get_user(db_session, user_id=user_id)
    try:
        roles.enable_user(db_session, actor, user=user, reason=reason)
    except (InvalidState, ValidationFailed) as exc:
        return _refused(db_session, request, _user_path(user_id), exc)
    return _done(db_session, request, _user_path(user_id), f"Enabled {user.display_name}.")


@router.post(_USERS_PATH + "/{user_id}/slack-id", dependencies=[Depends(verify_csrf_token)])
def set_slack_user_id(
    request: Request,
    user_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
    slack_user_id: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
):
    user = roles.get_user(db_session, user_id=user_id)
    try:
        roles.set_slack_user_id(db_session, actor, user=user, slack_user_id=slack_user_id, reason=reason)
    except ValidationFailed as exc:
        return _refused(db_session, request, _user_path(user_id), exc)
    message = f"Slack account for {user.display_name}: {user.slack_user_id or 'none'}."
    return _done(db_session, request, _user_path(user_id), message)


@router.post("/admin/pending-grants", dependencies=[Depends(verify_csrf_token)])
def grant_by_email(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
    email: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
):
    try:
        result = roles.grant_role_by_email(db_session, actor, email=email, role=role, reason=reason)
    except ValidationFailed as exc:
        return _refused(db_session, request, _USERS_PATH, exc)
    if result.user is not None:
        message = f"{result.email} has already signed in: granted {result.role} to {result.user.display_name}."
    elif result.created:
        message = f"{result.role} will be granted when {result.email} signs in with a verified email."
    else:
        message = f"{result.role} for {result.email} was already pending."
    return _done(db_session, request, _USERS_PATH, message)


@router.post("/admin/pending-grants/{grant_id}/cancel", dependencies=[Depends(verify_csrf_token)])
def cancel_pending_grant(
    request: Request,
    grant_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
):
    roles.cancel_pending_grant(db_session, actor, grant_id=grant_id)
    return _done(db_session, request, _USERS_PATH, "Pending grant cancelled.")


__all__ = ["router"]
