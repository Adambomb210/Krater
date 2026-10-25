"""Sharing proposals with funders: `/admin/funders/ospool`, admins only. The page lists the OSPool-ready projects;
the export gives them as CSV or PDF (`krater.services.funder_packet` has the rules)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from krater.db import get_session
from krater.models import GalleryMinorNames
from krater.services import funder_packet, site_settings
from krater.services.actor import Actor
from krater.services.errors import FunderPacketUnavailable, ValidationFailed
from krater.weave import WeaveClient, get_weave_client
from krater.web import funder_export
from krater.web.csrf import verify_csrf_token
from krater.web.deps import require_admin
from krater.web.flash import flash
from krater.web.templates import templates

router = APIRouter()

PAGE = "/admin/funders/ospool"

_MEDIA_TYPES = {"csv": "text/csv; charset=utf-8", "pdf": "application/pdf"}


@router.get(PAGE)
def ospool_page(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
    include_pending: bool = False,
):
    try:
        # A page view may use the directory cache; the export checks again, fresh.
        packet = funder_packet.build(db_session, weave_client, actor, include_pending=include_pending, fresh=False)
    except FunderPacketUnavailable:
        packet = None
    return templates.TemplateResponse(
        request,
        "admin/funders.html",
        {
            "packet": packet,
            "include_pending": include_pending,
            "names_full": site_settings.current(db_session).gallery_minor_names is GalleryMinorNames.FULL,
            "team_text": funder_export.team_text,
        },
    )


@router.post(f"{PAGE}/export", dependencies=[Depends(verify_csrf_token)])
def ospool_export(
    request: Request,
    db_session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(require_admin)],
    weave_client: Annotated[WeaveClient, Depends(get_weave_client)],
    file_format: Annotated[str, Form(alias="format")] = "",
    project_id: Annotated[list[uuid.UUID] | None, Form()] = None,
    include_pending: Annotated[bool, Form()] = False,
):
    back = f"{PAGE}?include_pending=true" if include_pending else PAGE
    try:
        packet = funder_packet.build(
            db_session, weave_client, actor, include_pending=include_pending, project_ids=project_id or []
        )
        funder_packet.record_export(db_session, actor, packet, file_format=file_format)
    except FunderPacketUnavailable as exc:
        db_session.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(back, status_code=303)
    except ValidationFailed as exc:
        db_session.rollback()
        flash(request, " ".join(exc.errors.values()), "error")
        return RedirectResponse(back, status_code=303)
    # The byte-order mark makes Excel read the CSV as UTF-8; Sheets and Numbers ignore it.
    body = funder_export.to_csv(packet).encode("utf-8-sig") if file_format == "csv" else funder_export.to_pdf(packet)
    db_session.commit()
    return Response(
        content=body,
        media_type=_MEDIA_TYPES[file_format],
        headers={
            "Content-Disposition": f'attachment; filename="{funder_export.filename(packet, file_format)}"',
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router"]
