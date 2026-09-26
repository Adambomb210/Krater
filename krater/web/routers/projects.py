"""Project pages: create, view, edit the draft, and every contextual action form on the detail page.

Routers are thin: every rule (who may do what, from what state) lives in `krater.services.projects`.
This module's job is parsing form input (including the dollars -> cents and email -> user-id
conversions), calling the service, and turning its domain errors into the right response -- see
`docs/SPEC.md` "Error handling" and CLAUDE.md.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from krater.config import get_settings
from krater.db import get_session
from krater.models import (
    Project,
    ProjectRevision,
    ProjectStatus,
    ReviewDecision,
    ReviewSource,
    RevisionKind,
    RevisionOutcome,
    User,
)
from krater.services import projects as project_service
from krater.services import slack_membership
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, NotFound, ValidationFailed
from krater.services.skypilot_sync import current_budget_flag
from krater.slack import get_slack_client
from krater.web.csrf import verify_csrf_token
from krater.web.deps import fresh_actor
from krater.web.flash import flash
from krater.web.forms import UnknownEmails, parse_credited_builder_emails, parse_tags
from krater.web.money import InvalidDollarAmount, cents_to_input, parse_dollars
from krater.web.templates import templates
from krater.worker.app import (
    slack_archive_channel,
    slack_notify_decision,
    slack_notify_revision_submitted,
    slack_post_admin_override,
)

router = APIRouter()

_TERMINAL_STATUSES = (ProjectStatus.COMPLETED, ProjectStatus.WITHDRAWN)

#: `docs/SPEC.md` "Roles & authentication" -- shown when the interim Slack membership gate blocks a
#: submission. Plain text (flash messages aren't rendered as HTML), with the Weave URL spelled out so
#: it still reads as a link.
_SLACK_MEMBERSHIP_REQUIRED_MESSAGE = (
    "Join the Patchwork Labs Slack and accept the code of conduct before you can submit. "
    "Manage your account at {weave_url}, then try again."
)


def _enforce_slack_membership(db_session: Session, actor: Actor) -> str | None:
    """`None` if `actor` passes the interim Slack membership gate (`docs/SPEC.md` "Roles &
    authentication"), else a user-facing error message to flash. Drafts are always allowed; this is
    only called from the submit routes, right before handing off to `project_service`."""
    if slack_membership.is_full_slack_member(db_session, get_slack_client(), actor.user):
        return None
    weave_url = get_settings().weave_issuer or "your Weave profile"
    return _SLACK_MEMBERSHIP_REQUIRED_MESSAGE.format(weave_url=weave_url)


# --------------------------------------------------------------------------------------------------
# Visibility: the submitter, reviewers and admins only -- anyone else gets a 404, not a 403, so a
# project's existence doesn't leak to members who have no relationship to it.
# --------------------------------------------------------------------------------------------------


def _can_view(actor: Actor, project: Project) -> bool:
    return actor.user.id == project.submitter_id or actor.is_reviewer or actor.is_admin


def _get_visible_project(session: Session, actor: Actor, project_id: uuid.UUID) -> Project:
    project = project_service.get_project(session, project_id=project_id)
    if not _can_view(actor, project):
        raise NotFound(f"No project with id {project_id}.")
    return project


def _redirect_to_project(project_id: uuid.UUID) -> RedirectResponse:
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


def _invalid_state_redirect(
    db_session: Session, request: Request, project_id: uuid.UUID, exc: InvalidState
) -> RedirectResponse:
    db_session.rollback()
    flash(request, str(exc), "error")
    return _redirect_to_project(project_id)


def _success_redirect(
    db_session: Session,
    request: Request,
    project_id: uuid.UUID,
    message: str,
    *,
    after_commit: Callable[[], None] | None = None,
) -> RedirectResponse:
    """Commit, run `after_commit` (e.g. deferring a Slack job -- see `docs/SPEC.md` "deferred after the
    web request commits"), flash `message`, and redirect back to the project."""
    db_session.commit()
    if after_commit is not None:
        after_commit()
    flash(request, message, "success")
    return _redirect_to_project(project_id)


# --------------------------------------------------------------------------------------------------
# New project
# --------------------------------------------------------------------------------------------------


def _new_project_values(*, title: str = "", repo_url: str = "", write_up: str = "", budget_requested: str = "") -> dict:
    return {"title": title, "repo_url": repo_url, "write_up": write_up, "budget_requested": budget_requested}


@router.get("/projects/new")
def new_project_form(request: Request, actor: Annotated[Actor, Depends(fresh_actor)]):
    del actor  # membership itself is enforced by `fresh_actor`; the service checks it again
    return templates.TemplateResponse(request, "projects/new.html", {"errors": {}, "values": _new_project_values()})


@router.post("/projects/new", dependencies=[Depends(verify_csrf_token)])
def create_project(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    title: Annotated[str, Form()] = "",
    repo_url: Annotated[str, Form()] = "",
    write_up: Annotated[str, Form()] = "",
    budget_requested: Annotated[str, Form()] = "",
):
    errors: dict[str, str] = {}
    budget_cents = 0
    raw_budget = budget_requested.strip()
    if raw_budget:
        try:
            budget_cents = parse_dollars(raw_budget)
        except InvalidDollarAmount as exc:
            errors["budget_requested_cents"] = str(exc)

    if errors:
        values = _new_project_values(
            title=title, repo_url=repo_url, write_up=write_up, budget_requested=budget_requested
        )
        return templates.TemplateResponse(
            request, "projects/new.html", {"errors": errors, "values": values}, status_code=422
        )

    project = project_service.create_project(
        db_session,
        actor,
        title=title,
        write_up=write_up,
        budget_requested_cents=budget_cents,
        repo_url=repo_url or None,
    )
    db_session.commit()
    flash(request, "Draft created.", "success")
    return RedirectResponse(f"/projects/{project.id}", status_code=303)


# --------------------------------------------------------------------------------------------------
# Detail page: read model + which action forms this viewer may use
# --------------------------------------------------------------------------------------------------


def _build_detail_context(
    session: Session,
    project: Project,
    actor: Actor,
    *,
    errors: dict[str, str] | None = None,
    error_form: str | None = None,
    posted: dict[str, str] | None = None,
) -> dict:
    summary = project_service.project_summary(session, project=project)
    revisions = sorted(project.revisions, key=lambda revision: revision.number, reverse=True)

    reviewer_ids = {review.reviewer_id for revision in revisions for review in revision.reviews}
    builder_ids: set[uuid.UUID] = set()
    for revision in revisions:
        builder_ids.update(revision.credited_builder_ids)
    user_ids = reviewer_ids | builder_ids
    users_by_id = (
        {user.id: user for user in session.scalars(sa.select(User).where(User.id.in_(user_ids)))} if user_ids else {}
    )

    is_submitter = actor.user.id == project.submitter_id
    current = project.current_revision
    has_draft = current is not None and current.submitted_at is None
    current_is_pending = (
        current is not None and current.submitted_at is not None and current.outcome is RevisionOutcome.PENDING
    )
    is_credited_builder = current is not None and actor.user.id in current.credited_builder_ids

    can_review = actor.is_reviewer and not is_submitter and not is_credited_builder and current_is_pending

    skypilot_budget_flag = None
    if project.skypilot_workspace is not None:
        skypilot_budget_flag = current_budget_flag(
            session, project, warn_percent=get_settings().skypilot_budget_warn_percent
        )

    return {
        "project": project,
        "summary": summary,
        "skypilot_budget_flag": skypilot_budget_flag,
        "revisions": revisions,
        "users_by_id": users_by_id,
        "is_submitter": is_submitter,
        "is_admin": actor.is_admin,
        "has_draft": has_draft,
        "can_edit_draft": is_submitter and has_draft,
        "can_submit": is_submitter and has_draft and current.kind is not RevisionKind.COMPLETION,
        "can_submit_completion": is_submitter and has_draft and current.kind is RevisionKind.COMPLETION,
        "can_start_amendment": is_submitter and project.status is ProjectStatus.APPROVED and not has_draft,
        "can_start_completion": is_submitter and project.status is ProjectStatus.APPROVED and not has_draft,
        "can_withdraw": is_submitter and project.status not in _TERMINAL_STATUSES,
        "can_review": can_review,
        "can_admin_decide": actor.is_admin and current_is_pending,
        "can_admin_adjust_budget": actor.is_admin
        and project.status in (ProjectStatus.APPROVED, ProjectStatus.PENDING_COMPLETION_REVIEW),
        "can_admin_reclaim": actor.is_admin and summary.ceiling_cents > 0,
        "can_admin_withdraw": actor.is_admin and project.status not in _TERMINAL_STATUSES,
        "errors": errors or {},
        "error_form": error_form,
        "posted": posted or {},
    }


@router.get("/projects/{project_id}")
def project_detail(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    project = _get_visible_project(db_session, actor, project_id)
    context = _build_detail_context(db_session, project, actor)
    return templates.TemplateResponse(request, "projects/detail.html", context)


# --------------------------------------------------------------------------------------------------
# Edit the draft
# --------------------------------------------------------------------------------------------------


def _edit_form_values(session: Session, project: Project, draft: ProjectRevision) -> dict:
    builder_emails = ""
    if draft.credited_builder_ids:
        users = session.scalars(sa.select(User).where(User.id.in_(draft.credited_builder_ids)))
        builder_emails = ", ".join(sorted(user.email for user in users))
    return {
        "title": project.title,
        "repo_url": project.repo_url or "",
        "write_up": draft.write_up,
        "budget_requested": cents_to_input(draft.budget_requested_cents),
        "demo_url": draft.demo_url or "",
        "tags": ", ".join(draft.tags),
        "credited_builder_emails": builder_emails,
    }


def _require_editable_draft(project: Project) -> ProjectRevision | None:
    """The project's current draft, or `None` if there's nothing to edit right now."""
    draft = project.current_revision
    if draft is None or draft.submitted_at is not None:
        return None
    return draft


@router.get("/projects/{project_id}/edit")
def edit_draft_form(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    project = _get_visible_project(db_session, actor, project_id)
    if actor.user.id != project.submitter_id:
        raise NotAllowed("Only the submitter may edit this project.")

    draft = _require_editable_draft(project)
    if draft is None:
        flash(request, "This project has no draft to edit right now.", "error")
        return _redirect_to_project(project_id)

    values = _edit_form_values(db_session, project, draft)
    return templates.TemplateResponse(
        request, "projects/edit.html", {"project": project, "draft": draft, "errors": {}, "values": values}
    )


@router.post("/projects/{project_id}/edit", dependencies=[Depends(verify_csrf_token)])
def update_draft(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    title: Annotated[str, Form()] = "",
    repo_url: Annotated[str, Form()] = "",
    write_up: Annotated[str, Form()] = "",
    budget_requested: Annotated[str, Form()] = "",
    demo_url: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    credited_builder_emails: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    if actor.user.id != project.submitter_id:
        raise NotAllowed("Only the submitter may edit this project.")

    draft = _require_editable_draft(project)
    if draft is None:
        flash(request, "This project has no draft to edit right now.", "error")
        return _redirect_to_project(project_id)

    errors: dict[str, str] = {}
    budget_cents = draft.budget_requested_cents
    raw_budget = budget_requested.strip()
    if raw_budget:
        try:
            budget_cents = parse_dollars(raw_budget)
        except InvalidDollarAmount as exc:
            errors["budget_requested_cents"] = str(exc)

    builder_ids = list(draft.credited_builder_ids)
    if draft.kind is RevisionKind.COMPLETION:
        try:
            builder_ids = parse_credited_builder_emails(db_session, credited_builder_emails)
        except UnknownEmails as exc:
            errors["credited_builder_emails"] = f"Unknown email(s): {', '.join(exc.emails)}"

    if errors:
        values = {
            "title": title,
            "repo_url": repo_url,
            "write_up": write_up,
            "budget_requested": budget_requested,
            "demo_url": demo_url,
            "tags": tags,
            "credited_builder_emails": credited_builder_emails,
        }
        return templates.TemplateResponse(
            request,
            "projects/edit.html",
            {"project": project, "draft": draft, "errors": errors, "values": values},
            status_code=422,
        )

    update_kwargs: dict = {
        "title": title,
        "repo_url": repo_url or None,
        "write_up": write_up,
        "budget_requested_cents": budget_cents,
    }
    if draft.kind is RevisionKind.COMPLETION:
        update_kwargs.update(demo_url=demo_url or None, tags=parse_tags(tags), credited_builder_ids=builder_ids)

    project_service.update_draft(db_session, actor, project=project, **update_kwargs)
    db_session.commit()
    flash(request, "Draft saved.", "success")
    return RedirectResponse(f"/projects/{project_id}/edit", status_code=303)


# --------------------------------------------------------------------------------------------------
# Submitter actions
# --------------------------------------------------------------------------------------------------


@router.post("/projects/{project_id}/submit", dependencies=[Depends(verify_csrf_token)])
def submit_project(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    project = _get_visible_project(db_session, actor, project_id)

    membership_error = _enforce_slack_membership(db_session, actor)
    if membership_error is not None:
        db_session.rollback()
        flash(request, membership_error, "error")
        return _redirect_to_project(project_id)

    try:
        project_service.submit(db_session, actor, project=project)
    except ValidationFailed as exc:
        db_session.rollback()
        draft = project.current_revision
        values = _edit_form_values(db_session, project, draft) if draft is not None else {}
        return templates.TemplateResponse(
            request,
            "projects/edit.html",
            {"project": project, "draft": draft, "errors": exc.errors, "values": values},
            status_code=422,
        )
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)

    revision_id = project.current_revision_id

    def _notify() -> None:
        slack_notify_revision_submitted.defer(revision_id=str(revision_id))

    return _success_redirect(db_session, request, project_id, "Project submitted for review.", after_commit=_notify)


@router.post("/projects/{project_id}/submit-completion", dependencies=[Depends(verify_csrf_token)])
def submit_completion(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    project = _get_visible_project(db_session, actor, project_id)

    membership_error = _enforce_slack_membership(db_session, actor)
    if membership_error is not None:
        db_session.rollback()
        flash(request, membership_error, "error")
        return _redirect_to_project(project_id)

    try:
        project_service.submit_completion(db_session, actor, project=project)
    except ValidationFailed as exc:
        db_session.rollback()
        draft = project.current_revision
        values = _edit_form_values(db_session, project, draft) if draft is not None else {}
        return templates.TemplateResponse(
            request,
            "projects/edit.html",
            {"project": project, "draft": draft, "errors": exc.errors, "values": values},
            status_code=422,
        )
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)

    revision_id = project.current_revision_id

    def _notify() -> None:
        slack_notify_revision_submitted.defer(revision_id=str(revision_id))

    return _success_redirect(db_session, request, project_id, "Completion submitted for review.", after_commit=_notify)


@router.post("/projects/{project_id}/amend", dependencies=[Depends(verify_csrf_token)])
def start_amendment(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    project = _get_visible_project(db_session, actor, project_id)
    try:
        project_service.start_amendment(db_session, actor, project=project)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)
    db_session.commit()
    return RedirectResponse(f"/projects/{project_id}/edit", status_code=303)


@router.post("/projects/{project_id}/complete", dependencies=[Depends(verify_csrf_token)])
def start_completion(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
):
    project = _get_visible_project(db_session, actor, project_id)
    try:
        project_service.start_completion(db_session, actor, project=project)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)
    db_session.commit()
    return RedirectResponse(f"/projects/{project_id}/edit", status_code=303)


@router.post("/projects/{project_id}/withdraw", dependencies=[Depends(verify_csrf_token)])
def withdraw_project(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    reason: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    is_self = actor.user.id == project.submitter_id
    try:
        project_service.withdraw(db_session, actor, project=project, reason=reason or None)
    except ValidationFailed as exc:
        db_session.rollback()
        context = _build_detail_context(db_session, project, actor, errors=exc.errors, error_form="withdraw")
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)

    def _notify() -> None:
        slack_archive_channel.defer(project_id=str(project.id))
        if not is_self:
            slack_post_admin_override.defer(
                project_id=str(project.id), action="admin_withdraw", actor_name=actor.user.display_name, reason=reason
            )

    return _success_redirect(db_session, request, project_id, "Project withdrawn.", after_commit=_notify)


# --------------------------------------------------------------------------------------------------
# Reviewer action (project-scoped; the review *queue* lives in krater/web/routers/reviews.py)
# --------------------------------------------------------------------------------------------------


@router.post("/projects/{project_id}/review", dependencies=[Depends(verify_csrf_token)])
def record_review(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    decision: Annotated[str, Form()],
    reason: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    try:
        decision_enum = ReviewDecision(decision)
    except ValueError:
        db_session.rollback()
        context = _build_detail_context(
            db_session, project, actor, errors={"decision": "Unknown decision."}, error_form="review"
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)

    revision = project.current_revision
    try:
        project_service.record_review(
            db_session, actor, revision=revision, decision=decision_enum, reason=reason or None, source=ReviewSource.WEB
        )
    except ValidationFailed as exc:
        db_session.rollback()
        context = _build_detail_context(
            db_session, project, actor, errors=exc.errors, error_form="review", posted={"reason": reason}
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)

    revision_id = revision.id

    def _notify() -> None:
        # Both are self-guarding no-ops when they don't apply (still pending / not terminal) -- see
        # `krater.services.slack_notify` -- so it's safe to always defer them after any decision.
        slack_notify_decision.defer(revision_id=str(revision_id))
        slack_archive_channel.defer(project_id=str(project.id))

    return _success_redirect(db_session, request, project_id, "Review recorded.", after_commit=_notify)


# --------------------------------------------------------------------------------------------------
# Admin actions
# --------------------------------------------------------------------------------------------------


@router.post("/projects/{project_id}/admin-decide", dependencies=[Depends(verify_csrf_token)])
def admin_decide(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    decision: Annotated[str, Form()],
    reason: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    try:
        decision_enum = ReviewDecision(decision)
    except ValueError:
        db_session.rollback()
        context = _build_detail_context(
            db_session, project, actor, errors={"decision": "Unknown decision."}, error_form="admin_decide"
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)

    revision = project.current_revision
    try:
        project_service.admin_decide(db_session, actor, revision=revision, decision=decision_enum, reason=reason)
    except ValidationFailed as exc:
        db_session.rollback()
        context = _build_detail_context(
            db_session, project, actor, errors=exc.errors, error_form="admin_decide", posted={"reason": reason}
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)

    revision_id = revision.id
    action = "admin_approve" if decision_enum is ReviewDecision.APPROVE else "admin_reject"

    def _notify() -> None:
        slack_notify_decision.defer(revision_id=str(revision_id))
        slack_archive_channel.defer(project_id=str(project.id))
        slack_post_admin_override.defer(
            project_id=str(project.id), action=action, actor_name=actor.user.display_name, reason=reason
        )

    return _success_redirect(db_session, request, project_id, "Decision recorded.", after_commit=_notify)


@router.post("/projects/{project_id}/admin-budget", dependencies=[Depends(verify_csrf_token)])
def admin_adjust_budget(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    amount: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    errors: dict[str, str] = {}
    try:
        amount_cents = parse_dollars(amount, allow_negative=True) if amount.strip() else 0
    except InvalidDollarAmount as exc:
        amount_cents = 0
        errors["amount_cents"] = str(exc)

    if errors:
        context = _build_detail_context(
            db_session,
            project,
            actor,
            errors=errors,
            error_form="admin_budget",
            posted={"amount": amount, "reason": reason},
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)

    try:
        project_service.admin_adjust_budget(
            db_session, actor, project=project, amount_cents=amount_cents, reason=reason
        )
    except ValidationFailed as exc:
        db_session.rollback()
        context = _build_detail_context(
            db_session,
            project,
            actor,
            errors=exc.errors,
            error_form="admin_budget",
            posted={"amount": amount, "reason": reason},
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)

    def _notify() -> None:
        slack_post_admin_override.defer(
            project_id=str(project.id),
            action="admin_adjust_budget",
            actor_name=actor.user.display_name,
            reason=reason,
            extra=f"Amount: {amount_cents / 100:+.2f}",
        )

    return _success_redirect(db_session, request, project_id, "Budget adjusted.", after_commit=_notify)


@router.post("/projects/{project_id}/admin-reclaim", dependencies=[Depends(verify_csrf_token)])
def admin_reclaim_budget(
    request: Request,
    project_id: uuid.UUID,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(fresh_actor)],
    amount: Annotated[str, Form()] = "",
    reason: Annotated[str, Form()] = "",
):
    project = _get_visible_project(db_session, actor, project_id)
    errors: dict[str, str] = {}
    try:
        amount_cents = parse_dollars(amount) if amount.strip() else 0
    except InvalidDollarAmount as exc:
        amount_cents = 0
        errors["amount_cents"] = str(exc)

    if errors:
        context = _build_detail_context(
            db_session,
            project,
            actor,
            errors=errors,
            error_form="admin_reclaim",
            posted={"amount": amount, "reason": reason},
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)

    try:
        project_service.reclaim_budget(db_session, actor, project=project, amount_cents=amount_cents, reason=reason)
    except ValidationFailed as exc:
        db_session.rollback()
        context = _build_detail_context(
            db_session,
            project,
            actor,
            errors=exc.errors,
            error_form="admin_reclaim",
            posted={"amount": amount, "reason": reason},
        )
        return templates.TemplateResponse(request, "projects/detail.html", context, status_code=422)
    except InvalidState as exc:
        return _invalid_state_redirect(db_session, request, project_id, exc)

    def _notify() -> None:
        slack_post_admin_override.defer(
            project_id=str(project.id),
            action="admin_reclaim_budget",
            actor_name=actor.user.display_name,
            reason=reason,
            extra=f"Reclaimed: {amount_cents / 100:.2f}",
        )

    return _success_redirect(db_session, request, project_id, "Budget reclaimed.", after_commit=_notify)


__all__ = ["router"]
