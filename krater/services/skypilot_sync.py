"""SkyPilot provisioning, spend reconciliation and budget enforcement.

Framework-free (no FastAPI, no procrastinate) so it can be unit-tested against `FakeSkyPilotClient` and
driven by the worker's periodic task or the `python -m krater.skypilot.reconcile_once` CLI alike. Every
function here is safe to call repeatedly: re-running `reconcile` after a partial failure (or just on its
normal schedule) should never double up work or double-write ledger/audit rows.

The reconciler acts as a system process with no human behind it, so every `audit.record` call here
passes `actor=None` (see `AuditEvent.actor_id`, nullable for exactly this reason).

See `docs/skypilot-integration.md` sections 1, 3 and 4, and `docs/dev/skypilot-spike.md` for the facts
this was built against (workspace naming, `allowed_users` semantics, the Vast-only cloud denylist, and
`cost_report`'s row shape).
"""

from __future__ import annotations

import logging
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import AuditEvent, Project, ProjectStatus, SpendSnapshot, SpendSource, User
from krater.services import audit, budget
from krater.skypilot.client import SkyPilotClient
from krater.skypilot.errors import SkyPilotError

logger = logging.getLogger(__name__)

#: Statuses whose project should have a live, up-to-date SkyPilot workspace.
_PROVISIONED_STATUSES = (
    ProjectStatus.APPROVED,
    ProjectStatus.PENDING_COMPLETION_REVIEW,
    ProjectStatus.COMPLETION_CHANGES_REQUESTED,
)
#: Statuses whose project's workspace (if any) should be torn down and released.
_TERMINAL_STATUSES = (ProjectStatus.COMPLETED, ProjectStatus.WITHDRAWN)

#: `cost_report`'s window. Deliberately large (not the design doc's illustrative "5 minutes" cadence,
#: which is the *reconcile* interval, not this): a project can run for months, and spend is cumulative
#: since the workspace was created, so a short window (e.g. 30 days) would silently under-report a
#: long-lived project's total once its oldest clusters age out of it.
COST_REPORT_DAYS = 3650

AUDIT_WORKSPACE_TORN_DOWN = "skypilot_workspace_torn_down"
AUDIT_BUDGET_WARNING = "budget_warning"
AUDIT_BUDGET_TEARDOWN = "budget_teardown"


def workspace_name_for(project_id: uuid.UUID) -> str:
    """The workspace name for a project: `ganymede-` plus the first 12 hex characters of its UUID.

    Lowercase hex + hyphens matches the only naming convention the spike's fixtures show in practice
    (e.g. `ganymede-priv-test`); a full UUID would work too, but the shorter form is what this build was
    asked for and is plenty unique for Krater's project volume.
    """
    return f"ganymede-{project_id.hex[:12]}"


def _team_emails(session: Session, project: Project) -> list[str]:
    """The submitter's email plus the emails of the credited builders on the project's latest revision
    (`current_revision`: the newest revision, draft or submitted -- see `krater.services.projects`)."""
    emails = {project.submitter.email}
    revision = project.current_revision
    if revision is not None and revision.credited_builder_ids:
        builders = session.scalars(sa.select(User).where(User.id.in_(revision.credited_builder_ids)))
        emails.update(user.email for user in builders)
    return sorted(emails)


def _active_projects(session: Session) -> list[Project]:
    return list(session.scalars(sa.select(Project).where(Project.status.in_(_PROVISIONED_STATUSES))))


def sync_workspaces(session: Session, client: SkyPilotClient) -> None:
    """Keep every active project's SkyPilot workspace in step, and tear down finished ones.

    - Every `approved`/`pending_completion_review`/`completion_changes_requested` project gets (or
      keeps) a private, Vast-only workspace named `workspace_name_for(project.id)`, with
      `allowed_users` kept equal to `_team_emails`. Always calls `create`/`update` (never skips), so a
      team change is picked up on the very next reconcile -- cheap, and safe to re-run.
    - Every `completed`/`withdrawn` project that still has a workspace gets its clusters downed, its
      managed jobs cancelled, one final `cost_report` total recorded (as a `SpendSnapshot`, if it
      changed, and in the `skypilot_workspace_torn_down` audit event's payload) *before* the workspace
      is deleted and `skypilot_workspace` cleared -- once the workspace is gone, nothing can attribute
      further `cost_report` rows back to this project (see `docs/skypilot-integration.md` section 4,
      "Final spend").
    """
    for project in _active_projects(session):
        allowed_users = _team_emails(session, project)
        if project.skypilot_workspace is None:
            name = workspace_name_for(project.id)
            client.create_workspace(name, allowed_users=allowed_users)
            project.skypilot_workspace = name
        else:
            client.update_workspace(project.skypilot_workspace, allowed_users=allowed_users)
        session.flush()

    stmt = sa.select(Project).where(Project.status.in_(_TERMINAL_STATUSES), Project.skypilot_workspace.is_not(None))
    for project in session.scalars(stmt):
        name = project.skypilot_workspace
        for cluster in client.list_clusters(name):
            client.down_cluster(cluster.name)
        client.cancel_managed_jobs(name)

        final_spend_cents = _workspace_total_cents(client, name)
        if final_spend_cents != budget.latest_spend_cents(session, project):
            session.add(
                SpendSnapshot(
                    project_id=project.id,
                    estimated_spend_cents=final_spend_cents,
                    source=SpendSource.SKYPILOT_COST_REPORT,
                )
            )
            session.flush()

        client.delete_workspace(name)
        project.skypilot_workspace = None
        audit.record(
            session,
            None,
            AUDIT_WORKSPACE_TORN_DOWN,
            project=project,
            payload={"workspace": name, "final_spend_cents": final_spend_cents},
        )
        session.flush()


def _workspace_total_cents(client: SkyPilotClient, workspace: str) -> int:
    """One `cost_report` call, summed to a single workspace's total -- used for the final-spend figure
    captured just before a workspace is torn down."""
    return sum(row.total_cost_cents for row in client.cost_report(days=COST_REPORT_DAYS) if row.workspace == workspace)


def sync_spend(session: Session, client: SkyPilotClient) -> None:
    """Write a `SpendSnapshot` for each project whose SkyPilot-reported total spend has changed.

    One `cost_report` call, summed per workspace, then compared against each project's
    `budget.latest_spend_cents`. Projects whose total is unchanged are skipped, so the table doesn't
    grow every reconcile tick for an idle project.
    """
    totals_by_workspace: dict[str, int] = {}
    for row in client.cost_report(days=COST_REPORT_DAYS):
        totals_by_workspace[row.workspace] = totals_by_workspace.get(row.workspace, 0) + row.total_cost_cents

    stmt = sa.select(Project).where(Project.skypilot_workspace.is_not(None))
    for project in session.scalars(stmt):
        total_cents = totals_by_workspace.get(project.skypilot_workspace, 0)
        if total_cents == budget.latest_spend_cents(session, project):
            continue
        session.add(
            SpendSnapshot(
                project_id=project.id, estimated_spend_cents=total_cents, source=SpendSource.SKYPILOT_COST_REPORT
            )
        )
        session.flush()


def _latest_audit_event(session: Session, project: Project, action: str) -> AuditEvent | None:
    stmt = (
        sa.select(AuditEvent)
        .where(AuditEvent.project_id == project.id, AuditEvent.action == action)
        .order_by(AuditEvent.created_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _budget_percent(ceiling_cents: int, spend_cents: int) -> float:
    if ceiling_cents <= 0:
        return 100.0 if spend_cents > 0 else 0.0
    return (spend_cents / ceiling_cents) * 100.0


def enforce_budgets(session: Session, client: SkyPilotClient, *, warn_percent: int) -> None:
    """Warn once per project at `warn_percent` of the ceiling, and tear down (repeatably, but only
    when new clusters have appeared) once spend reaches or passes 100%.

    - **Warning:** written once per project. Re-armed only if the ceiling is later raised (compared
      against the ceiling recorded on the last warning) and the percentage is crossed again.
    - **Teardown:** downs every current cluster and cancels every managed job in the project's
      workspace, then records which clusters were torn down. A later run only repeats the teardown if
      it finds a cluster that wasn't in that recorded set (i.e. one launched after the last teardown) --
      an idle-but-still-over-budget project isn't re-torn-down every tick.
    """
    stmt = sa.select(Project).where(Project.status.in_(_PROVISIONED_STATUSES), Project.skypilot_workspace.is_not(None))
    for project in session.scalars(stmt):
        workspace = project.skypilot_workspace
        ceiling_cents = budget.ceiling_cents(session, project)
        spend_cents = budget.latest_spend_cents(session, project)
        percent = _budget_percent(ceiling_cents, spend_cents)

        if percent >= warn_percent:
            last_warning = _latest_audit_event(session, project, AUDIT_BUDGET_WARNING)
            already_warned_at_this_ceiling = last_warning is not None and ceiling_cents <= last_warning.payload.get(
                "ceiling_cents", 0
            )
            if not already_warned_at_this_ceiling:
                audit.record(
                    session,
                    None,
                    AUDIT_BUDGET_WARNING,
                    project=project,
                    payload={
                        "ceiling_cents": ceiling_cents,
                        "spend_cents": spend_cents,
                        "percent": round(percent, 1),
                    },
                )
                session.flush()

        if percent >= 100.0:
            clusters = client.list_clusters(workspace)
            current_names = sorted(cluster.name for cluster in clusters)
            last_teardown = _latest_audit_event(session, project, AUDIT_BUDGET_TEARDOWN)
            previously_torn_down = set(last_teardown.payload.get("cluster_names", [])) if last_teardown else set()
            new_clusters_appeared = bool(set(current_names) - previously_torn_down)

            if last_teardown is None or new_clusters_appeared:
                for cluster in clusters:
                    client.down_cluster(cluster.name)
                client.cancel_managed_jobs(workspace)
                audit.record(
                    session,
                    None,
                    AUDIT_BUDGET_TEARDOWN,
                    project=project,
                    payload={
                        "cluster_names": current_names,
                        "ceiling_cents": ceiling_cents,
                        "spend_cents": spend_cents,
                    },
                )
                session.flush()


def current_budget_flag(session: Session, project: Project, *, warn_percent: int) -> str | None:
    """`"teardown"`, `"warning"`, or `None`: whether the project's spend *right now* still meets the
    condition that last triggered a teardown or warning. Used for the project page's warning banner.

    Deliberately re-derived from the live ceiling/spend rather than "does a budget_warning/teardown
    event exist", so raising the ceiling (which re-arms future warnings/teardowns) also immediately
    clears a banner that no longer reflects reality.
    """
    ceiling_cents = budget.ceiling_cents(session, project)
    spend_cents = budget.latest_spend_cents(session, project)
    if spend_cents >= ceiling_cents and _latest_audit_event(session, project, AUDIT_BUDGET_TEARDOWN) is not None:
        return "teardown"
    percent = _budget_percent(ceiling_cents, spend_cents)
    if percent >= warn_percent and _latest_audit_event(session, project, AUDIT_BUDGET_WARNING) is not None:
        return "warning"
    return None


def reconcile(session: Session, client: SkyPilotClient, *, warn_percent: int) -> None:
    """Run `sync_spend`, `enforce_budgets` and `sync_workspaces` in order, committing after each step.

    `sync_spend`/`enforce_budgets` before `sync_workspaces`: active projects get measured and their
    budgets enforced against this pass's numbers before any teardown work happens in the same pass, so
    a project that just went over budget is caught before, not after, whatever else this reconcile
    tick does to it. `sync_workspaces` still takes its own final `cost_report` reading for a
    completed/withdrawn project's teardown, since that project has already dropped out of
    `sync_spend`'s and `enforce_budgets`' scope (they only cover active statuses).

    Committing per step means a SkyPilot outage partway through doesn't lose the other steps' work: if
    one step raises `SkyPilotError`, it's logged and the next step still runs on the next scheduled
    reconcile (this function itself doesn't retry within a single call, since the periodic task is
    already the retry loop).
    """
    steps = (
        ("sync_spend", lambda: sync_spend(session, client)),
        ("enforce_budgets", lambda: enforce_budgets(session, client, warn_percent=warn_percent)),
        ("sync_workspaces", lambda: sync_workspaces(session, client)),
    )
    for name, step in steps:
        try:
            step()
        except SkyPilotError:
            logger.exception("krater.skypilot reconcile step %s failed; continuing", name)
            session.rollback()
        else:
            session.commit()
