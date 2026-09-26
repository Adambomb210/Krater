"""Data carried out of the SkyPilot adapter. See `docs/dev/skypilot-spike.md` for the wire shapes
these are built from."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostReportRow:
    """One row of `cost_report`'s per-cluster estimate, as attributed to a workspace.

    `total_cost_cents` is `round(total_cost_dollars * 100)`: SkyPilot's own figure is a float dollar
    estimate (`resources.get_cost(duration) * num_nodes`, per the spike), so converting to integer
    cents always takes a last rounding step. Round-half-to-even (Python's `round`), applied once per
    row before summing per workspace -- good enough for a spend *estimate* that the docs already flag
    as approximate and that drifts from the real Vast bill regardless.
    """

    workspace: str
    cluster_name: str
    total_cost_cents: int


@dataclass(frozen=True)
class ClusterInfo:
    """One cluster, as returned by `POST /status`."""

    name: str
    workspace: str
    status: str | None


@dataclass(frozen=True)
class ManagedJobInfo:
    """One managed job, as returned by `POST /jobs/queue`."""

    job_id: int
    name: str | None
    workspace: str
    status: str | None


@dataclass(frozen=True)
class ServiceInfo:
    """One SkyPilot Serve service, as returned by `POST /serve/status` (`sky.serve.server.core.status`:
    each row has a `name`; scoping to a workspace works the same way `list_clusters` does -- there's no
    `workspace` field on the row itself, so it's the caller-supplied workspace, filtered defensively).

    A service owns its own controller cluster (`sky-serve-controller-<name>`) plus one cluster per
    replica; `down_service` tears the whole thing down in one call (unlike clusters, which are downed
    one at a time), so `sync_workspaces`/`enforce_budgets` don't need to know its replica cluster names.
    """

    name: str
    workspace: str
    status: str | None
