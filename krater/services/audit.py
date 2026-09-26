"""Audit logging: one immutable `AuditEvent` row per admin override (and other notable actions)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from krater.models import AuditEvent, Project
from krater.services.actor import Actor


def record(
    session: Session,
    actor: Actor,
    action: str,
    *,
    project: Project | None = None,
    payload: dict | None = None,
    reason: str | None = None,
) -> AuditEvent:
    """Write an `AuditEvent` for `action`, performed by `actor`, and return it.

    `project` is optional (some actions aren't project-scoped). `payload` is a free-form JSON-able
    dict of extra context (e.g. amounts, decision, revision id). Flushes, doesn't commit.
    """
    event = AuditEvent(
        actor_id=actor.user.id,
        action=action,
        project_id=project.id if project is not None else None,
        payload=payload or {},
        reason=reason,
    )
    session.add(event)
    session.flush()
    return event
