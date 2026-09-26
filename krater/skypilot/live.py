"""`LiveSkyPilotClient`: plain REST (via `httpx`) against a real SkyPilot API server.

Deliberately **not** built on the `skypilot` Python package -- see `docs/dev/skypilot-spike.md` section
6 ("SDK vs REST") for why: no SDK exists for workspaces at all in 0.13.0, and the package pulls in a
453MB dependency tree (a second SQLAlchemy, two Postgres drivers, pandas, numpy, grpc...) that risks
colliding with Krater's own pinned stack, just to POST JSON and poll a request id.

Auth is a single bearer header (`Authorization: Bearer <service-account token>`). Most admin-plane
endpoints are async: a `POST`/`GET` returns `200` with a `null` body and an `x-skypilot-request-id`
header immediately, and the real result comes from polling `GET /api/get?request_id=...` until
`status` is `SUCCEEDED` or `FAILED` (spike section 2, "Workspaces via the API"). A few calls fail
*synchronously* instead -- no request id is even issued -- e.g. a 403 on an unauthorized workspace
update (spike fixture `workspaces_update_forbidden_nonmember_response.json`); `_finish_async` handles
both shapes uniformly.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from krater.config import Settings
from krater.skypilot.errors import SkyPilotRequestFailedError, SkyPilotUnavailableError
from krater.skypilot.types import ClusterInfo, CostReportRow, ManagedJobInfo

#: How long to keep polling a request id before giving up and treating SkyPilot as unavailable.
DEFAULT_POLL_TIMEOUT_SECONDS = 30.0
#: Poll backoff: start at this interval, double each miss, capped at `MAX_POLL_INTERVAL_SECONDS`.
DEFAULT_POLL_INTERVAL_SECONDS = 0.25
MAX_POLL_INTERVAL_SECONDS = 2.0
POLL_BACKOFF_FACTOR = 2.0

# There's no per-workspace cloud *allowlist* in SkyPilot (a workspace-level `allowed_clouds` key fails
# schema validation -- "did you mean `allowed_users`?", per the spike). Restricting a workspace to Vast
# means denying every other cloud individually. List taken verbatim from
# `tests/fixtures/skypilot/workspaces_disable_all_clouds_except_vast_request.json`.
_CLOUDS_TO_DISABLE = [
    "aws", "azure", "cudo", "do", "fluidstack", "gcp", "hyperbolic", "ibm", "kubernetes", "lambda",
    "mithril", "nebius", "oci", "paperspace", "primeintellect", "runpod", "scp", "seeweb", "shadeform",
    "ssh", "vsphere", "yotta",
]  # fmt: skip


def _decode_return_value(raw: Any) -> Any:
    """Decode a polled request's `return_value`.

    The spike's fixtures show this inconsistently: `cost_report`'s `return_value` is itself a
    JSON-encoded *string* (`"[]"`), while the `workspaces` listing fixture shows a plain JSON object.
    Handling both means: pass through anything that isn't a string, and `json.loads` anything that is
    (falling back to the raw string if it doesn't parse, e.g. `null`/plain text).
    """
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


class LiveSkyPilotClient:
    """A `SkyPilotClient` backed by a real SkyPilot API server over HTTP. `http_client` is injectable
    for tests (`httpx.MockTransport`); production code leaves it out and gets a real `httpx.Client`."""

    def __init__(
        self,
        settings: Settings,
        *,
        http_client: httpx.Client | None = None,
        poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._settings = settings
        self._base_url = settings.skypilot_api_url.rstrip("/")
        self._http = http_client if http_client is not None else httpx.Client(timeout=10.0)
        self._poll_timeout_seconds = poll_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds

    # -- Workspaces --------------------------------------------------------------------------------

    def create_workspace(self, name: str, *, allowed_users: list[str]) -> None:
        self._post_async(
            "/workspaces/create", {"workspace_name": name, "config": self._vast_only_config(allowed_users)}
        )

    def update_workspace(self, name: str, *, allowed_users: list[str]) -> None:
        self._post_async(
            "/workspaces/update", {"workspace_name": name, "config": self._vast_only_config(allowed_users)}
        )

    def delete_workspace(self, name: str) -> None:
        self._post_async("/workspaces/delete", {"workspace_name": name})

    def list_workspaces(self) -> list[str]:
        result = self._get_async("/workspaces") or {}
        return list(result.keys())

    @staticmethod
    def _vast_only_config(allowed_users: list[str]) -> dict:
        config: dict[str, Any] = {"private": True, "allowed_users": list(allowed_users)}
        for cloud in _CLOUDS_TO_DISABLE:
            config[cloud] = {"disabled": True}
        return config

    # -- Spend ---------------------------------------------------------------------------------------

    def cost_report(self, days: int) -> list[CostReportRow]:
        rows = self._post_async("/cost_report", {"days": days}) or []
        return [
            CostReportRow(
                workspace=row.get("workspace") or "default",
                cluster_name=row["name"],
                # `total_cost` is a float dollar estimate (`resources.get_cost(duration) * num_nodes`,
                # per the spike); see `CostReportRow`'s docstring for the rounding rationale.
                total_cost_cents=round(float(row.get("total_cost", 0.0)) * 100),
            )
            for row in rows
        ]

    # -- Clusters and managed jobs --------------------------------------------------------------------

    def list_clusters(self, workspace: str) -> list[ClusterInfo]:
        rows = (
            self._post_async(
                "/status",
                {
                    "cluster_names": None,
                    "refresh": False,
                    "all_users": True,
                    # `/status` has no `workspace` field; scoping is via the request's active-workspace
                    # context (spike section 5). We also filter defensively below, since cost_report's row
                    # shape does carry an explicit `workspace` and the spike flags `active_workspace` as
                    # unreliable in at least one other context (the admin-policy payload).
                    "override_skypilot_config": {"active_workspace": workspace},
                },
            )
            or []
        )
        return [
            ClusterInfo(name=row["name"], workspace=row.get("workspace", workspace), status=row.get("status"))
            for row in rows
            if row.get("workspace", workspace) == workspace
        ]

    def list_managed_jobs(self, workspace: str) -> list[ManagedJobInfo]:
        rows = (
            self._post_async(
                "/jobs/queue",
                {
                    "refresh": False,
                    "skip_finished": True,
                    "all_users": True,
                    "override_skypilot_config": {"active_workspace": workspace},
                },
            )
            or []
        )
        return [
            ManagedJobInfo(
                job_id=row["job_id"],
                name=row.get("job_name") or row.get("name"),
                workspace=row.get("workspace", workspace),
                status=row.get("status"),
            )
            for row in rows
            if row.get("workspace", workspace) == workspace
        ]

    def down_cluster(self, name: str) -> None:
        # `purge=True`: a cluster SkyPilot can't cleanly reach (already gone on Vast, say) is still
        # removed from SkyPilot's own bookkeeping rather than blocking budget enforcement.
        self._post_async("/down", {"cluster_name": name, "purge": True, "graceful": False})

    def cancel_managed_jobs(self, workspace: str) -> None:
        self._post_async(
            "/jobs/cancel",
            {"all": True, "all_users": True, "override_skypilot_config": {"active_workspace": workspace}},
        )

    # -- Transport: request + async request-id polling ------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.skypilot_service_token}"}

    def _post_async(self, path: str, body: dict) -> Any:
        return self._finish_async(self._request("POST", path, json=body))

    def _get_async(self, path: str) -> Any:
        return self._finish_async(self._request("GET", path))

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, f"{self._base_url}{path}", headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise SkyPilotUnavailableError(f"could not reach SkyPilot at {path}: {exc}") from exc

    def _finish_async(self, response: httpx.Response) -> Any:
        """Handle a request that may fail synchronously (no request id issued, e.g. a 403) or that
        was scheduled and must be polled for its real result."""
        self._raise_for_status(response)
        request_id = response.headers.get("x-skypilot-request-id")
        if not request_id:
            # A handful of endpoints (not used above, but the spike found some) answer directly.
            return response.json() if response.content else None
        return self._poll(request_id)

    def _poll(self, request_id: str) -> Any:
        deadline = time.monotonic() + self._poll_timeout_seconds
        interval = self._poll_interval_seconds
        while True:
            response = self._request("GET", "/api/get", params={"request_id": request_id})
            self._raise_for_status(response)
            data = response.json()
            status = data.get("status")
            if status == "SUCCEEDED":
                return _decode_return_value(data.get("return_value"))
            if status == "FAILED":
                raise SkyPilotRequestFailedError(str(data.get("error") or "SkyPilot request failed"))
            if time.monotonic() >= deadline:
                raise SkyPilotUnavailableError(
                    f"SkyPilot request {request_id} did not complete within {self._poll_timeout_seconds}s "
                    f"(last status: {status!r})"
                )
            time.sleep(interval)
            interval = min(interval * POLL_BACKOFF_FACTOR, MAX_POLL_INTERVAL_SECONDS)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code >= 500:
            raise SkyPilotUnavailableError(f"SkyPilot returned {response.status_code} for {response.request.url}")
        if response.status_code >= 400:
            raise SkyPilotRequestFailedError(LiveSkyPilotClient._error_message(response))

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text or f"HTTP {response.status_code}"
        if isinstance(body, dict):
            return str(body.get("detail") or body.get("error") or body)
        return str(body)


__all__ = ["LiveSkyPilotClient"]
