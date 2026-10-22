"""The review queue: `/reviews`, reviewers only."""

from __future__ import annotations

from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.models import Project, ProjectStatus
from krater.services import projects as project_service
from krater.services import residency
from krater.services.actor import Actor
from krater.weave import WeaveClient, get_weave_client
from krater.web.deps import require_reviewer
from krater.web.templates import templates

router = APIRouter()


@router.get("/reviews")
def review_queue(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_reviewer)],
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
    us_residents: bool = False,
):
    queue = project_service.review_queue(db_session, actor)
    context = {"queue": queue, "us_residents": us_residents, "residency_unavailable": False, "approved_residents": []}
    if us_residents:
        # Picking OSPool projects (`krater.services.residency`): the pending queue and the approved projects, each
        # kept only when the whole team is verified US residents. The lookups run only with the filter on.
        approved = list(
            db_session.scalars(
                sa.select(Project).where(Project.status == ProjectStatus.APPROVED).order_by(Project.created_at.desc())
            )
        )
        by_id = {revision.project_id: revision for revision in queue}
        kept, unavailable = residency.resident_teams(
            db_session, weave_client, [revision.project for revision in queue] + approved
        )
        kept_ids = {project.id for project in kept}
        context.update(
            queue=[revision for revision in queue if revision.project_id in kept_ids],
            approved_residents=[project for project in approved if project.id in kept_ids and project.id not in by_id],
            residency_unavailable=unavailable,
        )
    return templates.TemplateResponse(request, "reviews/queue.html", context)


__all__ = ["router"]
