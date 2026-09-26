"""The public gallery of completed projects: `/gallery`, `/gallery/{id}`. No auth -- anything not
`completed` 404s, so an unfinished project's details never leak here."""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.models import Project, ProjectStatus, User
from krater.services import budget
from krater.services import projects as project_service
from krater.services.errors import NotFound
from krater.web.templates import templates

router = APIRouter()


@router.get("/gallery")
def gallery_index(request: Request, db_session: Annotated[Session, Depends(get_session)]):
    completed = list(
        db_session.scalars(
            sa.select(Project).where(Project.status == ProjectStatus.COMPLETED).order_by(Project.updated_at.desc())
        )
    )
    entries = [
        {
            "project": project,
            "revision": project.current_revision,
            "spend_cents": budget.latest_spend_cents(db_session, project),
        }
        for project in completed
    ]
    return templates.TemplateResponse(request, "gallery/index.html", {"entries": entries})


@router.get("/gallery/{project_id}")
def gallery_detail(request: Request, project_id: uuid.UUID, db_session: Annotated[Session, Depends(get_session)]):
    project = project_service.get_project(db_session, project_id=project_id)
    if project.status is not ProjectStatus.COMPLETED:
        raise NotFound(f"No completed project with id {project_id}.")

    # A completed project's `current_revision` is its approved completion revision: approving a
    # completion moves the project straight to `completed` without opening another draft.
    revision = project.current_revision
    builders = []
    if revision is not None and revision.credited_builder_ids:
        builders = list(db_session.scalars(sa.select(User).where(User.id.in_(revision.credited_builder_ids))))

    spend_cents = budget.latest_spend_cents(db_session, project)
    return templates.TemplateResponse(
        request,
        "gallery/detail.html",
        {"project": project, "revision": revision, "builders": builders, "spend_cents": spend_cents},
    )


__all__ = ["router"]
