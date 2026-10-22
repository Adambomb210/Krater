"""The review queue: `/reviews`, reviewers only."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from krater.db import get_session
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
    # Whether every team member is a verified US resident, for picking OSPool projects
    # (`krater.services.residency`). `None` when Weave couldn't say.
    residency_by_project = {
        revision.project_id: residency.team_status_for_display(db_session, weave_client, revision.project)
        for revision in queue
    }
    if us_residents:
        queue = [
            revision
            for revision in queue
            if (status := residency_by_project[revision.project_id]) is not None and status.all_residents
        ]
    return templates.TemplateResponse(
        request,
        "reviews/queue.html",
        {"queue": queue, "residency_by_project": residency_by_project, "us_residents": us_residents},
    )


__all__ = ["router"]
