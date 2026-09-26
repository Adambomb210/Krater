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


#: Confirmed live against a real 0.13.0 server (not documented anywhere): `/jobs/queue` and
#: `/jobs/cancel` both raise `sky.exceptions.ClusterNotUpError("No in-progress managed jobs.")` when
#: the workspace's managed-jobs *controller* cluster doesn't exist yet -- i.e. no managed job has ever
#: been launched there. That's the common case for essentially every Ganymede workspace (most compute
#: is a plain `sky launch`, not `sky jobs launch`), so treating it as a real failure would make
#: `list_managed_jobs`/`cancel_managed_jobs` -- and therefore `sync_workspaces`'s teardown and
#: `enforce_budgets`'s budget-exceeded teardown -- fail on almost every project. It means exactly what
#: an empty managed-jobs queue means, so both methods below treat it as one.
_NO_JOBS_CONTROLLER_MARKERS = ("ClusterNotUpError", "No in-progress managed jobs")


def _is_no_jobs_controller_error(exc: SkyPilotRequestFailedError) -> bool:
    message = str(exc)
    return any(marker in message for marker in _NO_JOBS_CONTROLLER_MARKERS)


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
        # Confirmed live against a real 0.13.0 server: deleting a workspace that doesn't exist is a
        # polled `FAILED` request ("Workspace '<name>' does not exist."), not a no-op -- unlike, say,
        # `down_cluster`'s `purge=True`. The `SkyPilotClient` protocol promises callers this method is
        # "safe to call on a workspace that's already gone" (`sync_workspaces` leans on that for a
        # reconcile pass that crashes between deleting a workspace and clearing
        # `Project.skypilot_workspace`, which would otherwise retry this same delete, and fail closed
        # on it, forever), so swallow exactly that one message here instead of every caller re-deriving
        # it.
        try:
            self._post_async("/workspaces/delete", {"workspace_name": name})
        except SkyPilotRequestFailedError as exc:
            if "does not exist" not in str(exc):
                raise

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
                    # `StatusBody.refresh` is `StatusRefreshMode` ("NONE"/"AUTO"/"FORCE"), not a bool --
                    # a real 0.13.0 server 422s on `false` (confirmed live; see
                    # docs/dev/skypilot-spike.md). "NONE" matches this call's old (fake-client-only)
                    # intent of not forcing a live refresh against the cloud.
                    "refresh": "NONE",
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
        try:
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
        except SkyPilotRequestFailedError as exc:
            if _is_no_jobs_controller_error(exc):
                return []
            raise
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
        try:
            self._post_async(
                "/jobs/cancel",
                {"all": True, "all_users": True, "override_skypilot_config": {"active_workspace": workspace}},
            )
        except SkyPilotRequestFailedError as exc:
            if not _is_no_jobs_controller_error(exc):
                raise

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
            data = self._poll_response_data(response)
            if data is None:
                # Not the polled-request shape at all (either direction) -- a genuine transport/server
                # problem `_raise_for_status` can describe; if it somehow doesn't raise, fall through
                # to unavailable below rather than silently treating an unrecognized 2xx as success.
                self._raise_for_status(response)
                raise SkyPilotUnavailableError(f"SkyPilot returned an unrecognized response for request {request_id}")
            status = data.get("status")
            if status == "SUCCEEDED":
                return _decode_return_value(data.get("return_value"))
            if status == "FAILED":
                raise SkyPilotRequestFailedError(self._poll_failure_message(data))
            if time.monotonic() >= deadline:
                raise SkyPilotUnavailableError(
                    f"SkyPilot request {request_id} did not complete within {self._poll_timeout_seconds}s "
                    f"(last status: {status!r})"
                )
            time.sleep(interval)
            interval = min(interval * POLL_BACKOFF_FACTOR, MAX_POLL_INTERVAL_SECONDS)

    @staticmethod
    def _poll_response_data(response: httpx.Response) -> dict[str, Any] | None:
        """Return `/api/get`'s polled-request dict (the one with `status`/`return_value`/`error`),
        regardless of which of the two shapes a real 0.13.0 server used for it.

        Confirmed live: a request that finishes normally (`SUCCEEDED` or a "clean" `FAILED`, e.g. our
        own admin-policy rejecting something upstream) comes back as HTTP 200 with that dict at the
        body's top level -- but a request that failed because the *handler itself* raised an
        uncaught exception (e.g. `ClusterNotUpError` from `/jobs/queue`/`/jobs/cancel` when a
        workspace's managed-jobs controller doesn't exist yet, see `_is_no_jobs_controller_error`)
        comes back as HTTP **500**, with the identical dict nested one level down, under `detail`.
        Callers must handle both or a routine "this failed" case (never mind our own no-jobs-
        controller handling above it) gets misread as SkyPilot being unreachable. Returns `None` if
        the body matches neither shape, so the caller can fall back to the generic transport-error path.
        """
        try:
            body = response.json()
        except ValueError:
            return None
        if isinstance(body, dict) and "status" in body:
            return body
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict) and "status" in detail:
            return detail
        return None

    @staticmethod
    def _poll_failure_message(data: dict[str, Any]) -> str:
        """`data["error"]` for a FAILED polled request is itself a JSON-encoded string (a pickled
        exception's `type`/`message`/... per the spike's fixtures) -- decode it for a readable
        message, falling back to the raw value for anything that doesn't parse that way."""
        error = data.get("error")
        if isinstance(error, str):
            try:
                parsed = json.loads(error)
            except (TypeError, ValueError):
                return error or "SkyPilot request failed"
            if isinstance(parsed, dict):
                return str(parsed.get("message") or parsed.get("type") or error)
            return error
        return str(error or "SkyPilot request failed")

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
