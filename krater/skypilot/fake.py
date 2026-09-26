"""`FakeSkyPilotClient`: an in-memory `SkyPilotClient` for `KRATER_SKYPILOT_MODE=fake` (development and
tests). Makes no network calls. Test helpers (`add_cluster`, `add_managed_job`) let a test set up
billing/queue state that a real SkyPilot server would otherwise report via `cost_report`/`status`/
`jobs/queue`, without standing one up.
"""

from __future__ import annotations

import itertools

from krater.skypilot.types import ClusterInfo, CostReportRow, ManagedJobInfo


class FakeSkyPilotClient:
    """A `SkyPilotClient` backed by plain Python dicts. `workspaces` maps name -> sorted allowed_users,
    for tests that want to assert on membership directly."""

    def __init__(self) -> None:
        self.workspaces: dict[str, list[str]] = {}
        self._clusters: dict[str, ClusterInfo] = {}
        # Cost history, keyed by cluster name -- separate from `_clusters` (the *live* set) because
        # real SkyPilot's `cost_report` reads `cluster_history`, which keeps a torn-down cluster's
        # final cost around after `down` removes it from the live listing (`/status`). `down_cluster`
        # below only pops `_clusters`, never this, so a workspace's last `cost_report` total still
        # includes clusters just torn down in the same reconcile pass (see `sync_workspaces`'s
        # final-spend-before-teardown step).
        self._cost_history: dict[str, tuple[str, int]] = {}  # name -> (workspace, cost_cents)
        self._jobs: dict[int, ManagedJobInfo] = {}
        self._job_id_seq = itertools.count(1)
        # A monotonic counter, not `len(self._clusters)`: a cluster added after an earlier one was
        # torn down (e.g. by `down_cluster`) must get a name that was never used before, or a caller
        # tracking "which clusters have I already seen" (like the budget-teardown re-arm check in
        # `krater.services.skypilot_sync.enforce_budgets`) would wrongly treat it as the same cluster.
        self._cluster_name_seq = itertools.count(1)

    # -- SkyPilotClient protocol -----------------------------------------------------------------

    def create_workspace(self, name: str, *, allowed_users: list[str]) -> None:
        self.workspaces[name] = sorted(allowed_users)

    def update_workspace(self, name: str, *, allowed_users: list[str]) -> None:
        self.workspaces[name] = sorted(allowed_users)

    def delete_workspace(self, name: str) -> None:
        self.workspaces.pop(name, None)

    def list_workspaces(self) -> list[str]:
        return list(self.workspaces.keys())

    def cost_report(self, days: int) -> list[CostReportRow]:
        del days  # the fake has no notion of time; it just reports whatever's been added
        return [
            CostReportRow(workspace=workspace, cluster_name=name, total_cost_cents=cost_cents)
            for name, (workspace, cost_cents) in self._cost_history.items()
        ]

    def list_clusters(self, workspace: str) -> list[ClusterInfo]:
        return [cluster for cluster in self._clusters.values() if cluster.workspace == workspace]

    def list_managed_jobs(self, workspace: str) -> list[ManagedJobInfo]:
        return [job for job in self._jobs.values() if job.workspace == workspace]

    def down_cluster(self, name: str) -> None:
        self._clusters.pop(name, None)

    def cancel_managed_jobs(self, workspace: str) -> None:
        for job_id in [job.job_id for job in self._jobs.values() if job.workspace == workspace]:
            del self._jobs[job_id]

    # -- Test helpers ------------------------------------------------------------------------------

    def add_cluster(self, workspace: str, cost_cents: int, *, name: str | None = None, status: str = "UP") -> str:
        """Add a cluster in `workspace` with the given cost estimate, returning its name."""
        name = name or f"{workspace}-cluster-{next(self._cluster_name_seq)}"
        self._clusters[name] = ClusterInfo(name=name, workspace=workspace, status=status)
        self._cost_history[name] = (workspace, cost_cents)
        return name

    def set_cluster_cost(self, name: str, cost_cents: int) -> None:
        """Change an existing (or already torn-down) cluster's cost estimate, simulating time passing
        / the meter running."""
        workspace, _ = self._cost_history[name]
        self._cost_history[name] = (workspace, cost_cents)

    def add_managed_job(self, workspace: str, *, name: str | None = None, status: str = "RUNNING") -> int:
        """Add a managed job in `workspace`, returning its job id."""
        job_id = next(self._job_id_seq)
        self._jobs[job_id] = ManagedJobInfo(job_id=job_id, name=name, workspace=workspace, status=status)
        return job_id


__all__ = ["FakeSkyPilotClient"]
