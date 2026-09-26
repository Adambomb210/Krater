"""The SkyPilot launch gate: a pure decision function for the admin-policy endpoint.

Takes a decoded policy request, a `Session` and `Settings`, and returns an allow (optionally mutated)
or a reject with a message the member will see verbatim in their terminal. See
`docs/skypilot-integration.md` section 2 and `docs/dev/skypilot-spike.md` for the design and the wire
facts this enforces. Framework-free like every other service: no FastAPI, no HTTP status codes -- the
route (`krater.web.routers.skypilot_policy`) maps `Reject`/`Allow` onto 400/200.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.config import Settings
from krater.models import Project, ProjectStatus
from krater.services import budget
from krater.skypilot_policy.envelope import PolicyRequest

#: `request_name` values that actually reserve/consume compute, and so are the only ones subject to
#: rejection (missing/unknown workspace, inactive project, exhausted budget). Everything else --
#: currently just `validate`, SkyPilot's pre-flight schema check, run before the real `launch` call --
#: still gets the same cost/autodown mutations applied (so a dry validation reflects what the real
#: launch would do), but is *never* rejected.
#:
#: This matters because of a spike finding (docs/dev/skypilot-spike.md, Surprise #2): a single
#: `sky launch` triggers 2-3 policy calls (`launch` client-side, `validate` server-side, `launch`
#: server-side), and the `validate` call routinely omits `skypilot_config.active_workspace` even when
#: the *actual* `launch` call moments later, from the same invocation, carries it correctly. Rejecting
#: `validate` on "no workspace" would therefore reject every real launch on its very first hop, before
#: the hop with the real workspace ever runs -- and rejecting it on budget would double-count nothing
#: (validate never provisions anything) while still risking that same false rejection. `jobs.launch`,
#: `jobs.launch_controller` and `exec` exist in SkyPilot's `AdminPolicyRequestName` enum but were never
#: observed on the wire in the spike; they're deliberately left out of this set (treated like
#: `validate`, advisory-only) until one is actually seen, rather than guessed at.
ENFORCED_REQUEST_NAMES = frozenset({"launch"})

#: Project statuses in which compute launches are allowed. Draft/pending/changes-requested projects
#: haven't been funded yet; completed/withdrawn ones no longer have live budget to spend.
_ACTIVE_STATUSES = frozenset(
    {ProjectStatus.APPROVED, ProjectStatus.PENDING_COMPLETION_REVIEW, ProjectStatus.COMPLETION_CHANGES_REQUESTED}
)

# SkyPilot 0.13.0's `skypilot_config.to_dict()` only fills in `active_workspace` when the user (or their
# local config) explicitly set one -- see docs/dev/skypilot-spike.md Surprise #2. `-w default` is a
# no-op: `default` isn't a Krater project workspace either, so it gets the same message as "nothing set".
_WORKSPACE_HELP = (
    "Target your project's workspace: run `sky launch -w <your-project-workspace> ...` (the workspace "
    "name is on your project's Krater page), or add `active_workspace: <workspace>` under your "
    "`~/.sky/config.yaml`."
)


@dataclass(frozen=True)
class Allow:
    """Allow the launch, with `task` mutated (autodown forced, cost capped) and `skypilot_config` as-is."""

    task: dict[str, Any]
    skypilot_config: dict[str, Any]


@dataclass(frozen=True)
class Reject:
    """Reject the launch. `message` is shown to the member verbatim -- keep it clear and actionable."""

    message: str


PolicyDecision = Allow | Reject


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _resource_items(task: dict[str, Any]) -> list[dict[str, Any]]:
    """`task["resources"]`, normalized to a list of the dicts to mutate in place.

    A SkyPilot task's `resources:` can be a single mapping or a list of candidate mappings (`any_of`/
    `ordered`); either way, the returned dicts are the same objects nested in `task`, so mutating them
    mutates `task` too.
    """
    resources = task.setdefault("resources", {})
    if isinstance(resources, list):
        return [item for item in resources if isinstance(item, dict)]
    if isinstance(resources, dict):
        return [resources]
    return []


def _apply_mutations(task: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Force autodown and cap `max_hourly_cost` on every resource candidate in `task`, in place."""
    cap_dollars = settings.skypilot_max_hourly_cost_cents / 100
    for resource in _resource_items(task):
        existing_cost = resource.get("max_hourly_cost")
        resource["max_hourly_cost"] = (
            min(existing_cost, cap_dollars) if isinstance(existing_cost, int | float) else cap_dollars
        )

        autostop = resource.get("autostop")
        user_is_stricter = (
            isinstance(autostop, dict)
            and autostop.get("down") is True
            and isinstance(autostop.get("idle_minutes"), int | float)
            and autostop["idle_minutes"] < settings.skypilot_autodown_idle_minutes
        )
        if not user_is_stricter:
            resource["autostop"] = {"idle_minutes": settings.skypilot_autodown_idle_minutes, "down": True}
    return task


def decide(request: PolicyRequest, session: Session, settings: Settings) -> PolicyDecision:
    """Decide whether to allow `request`'s launch.

    Only `request_name`s in `ENFORCED_REQUEST_NAMES` can be rejected -- see that constant's docstring.
    Every request (enforced or not) that isn't rejected gets `task` mutated the same way: autodown
    forced after `settings.skypilot_autodown_idle_minutes` (unless the user's own `autostop` is already
    stricter), and every resource's `max_hourly_cost` capped at
    `min(user's value, settings.skypilot_max_hourly_cost_cents / 100)`. `skypilot_config` is returned
    unchanged: the mutation points here are `resources`-level task fields (see the spike), not config.

    Does at most three simple, indexed reads (the project lookup, plus `budget.remaining_cents`'s two
    selects) and never writes -- this is called on the hot path of every `sky launch`.
    """
    workspace = request.skypilot_config.get("active_workspace")

    if request.request_name in ENFORCED_REQUEST_NAMES:
        if not workspace or workspace == "default":
            return Reject(f"No Ganymede project workspace selected. {_WORKSPACE_HELP}")

        project = session.scalar(sa.select(Project).where(Project.skypilot_workspace == workspace))
        if project is None:
            return Reject(f"Workspace '{workspace}' isn't a Ganymede project on Krater. {_WORKSPACE_HELP}")

        if project.status not in _ACTIVE_STATUSES:
            return Reject(
                f"Project '{project.title}' isn't active on Krater right now (status: {project.status.value}). "
                "Compute launches are only allowed while a project is approved or in its completion review."
            )

        remaining = budget.remaining_cents(session, project)
        if remaining <= 0:
            ceiling = budget.ceiling_cents(session, project)
            spend = budget.latest_spend_cents(session, project)
            return Reject(
                f"Project '{project.title}' has used its full compute budget "
                f"({_dollars(spend)} of {_dollars(ceiling)}). Launches are blocked until the budget "
                "changes -- ask a Ganymede admin to review it."
            )

    task = _apply_mutations(copy.deepcopy(request.task), settings)
    return Allow(task=task, skypilot_config=request.skypilot_config)


__all__ = ["Allow", "ENFORCED_REQUEST_NAMES", "PolicyDecision", "Reject", "decide"]
