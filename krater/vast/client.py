"""`VastClient`: the only code that talks to Vast.ai's REST API.

Rules learned on the live API (2026-10-11 spike; see docs/compute/vast-api.md), all enforced here:

- Every path ends with a slash. Without it Vast answers **308 and does nothing**, even for DELETE, so redirects are
  never followed and any 3xx is an error (`VastRedirectError`).
- Instances are listed with `GET /api/v1/instances/` (25 per page, `next_token` -> `after_token`); the v0 list is
  gone (410). `GET /api/v0/instances/{id}/` still works and answers `{"instances": null}` for an unknown id.
- A create response carries `instance_api_key`, the new container's own key (it can start, stop and destroy that
  instance). `create_instance` returns the id alone and no response body ever reaches a log or an exception.
- Log downloads (`result_url`) live on another host: they're fetched **without** the Authorization header.
- The org key is sent only as `Authorization: Bearer`, never in a URL.

Only Krater's worker builds one of these: the portal never holds the Vast key.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from krater.vast.errors import (
    VastRateLimitedError,
    VastRedirectError,
    VastRequestFailedError,
    VastUnavailableError,
)
from krater.vast.types import Charge, CreateInstance, Instance, Offer, OfferQuery

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://console.vast.ai"
#: Vast's page size for the v1 instance list is at most 25.
INSTANCE_PAGE_SIZE = 25
#: Vast's page size for charges is at most 500.
CHARGES_PAGE_SIZE = 500


class VastClient:
    """A typed client for the Vast calls Krater Compute needs. `http_client` (and `logs_http_client`, for log
    downloads) are injectable for tests, e.g. backed by `krater.vast.fake.FakeVast`."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        http_client: httpx.Client | None = None,
        logs_http_client: httpx.Client | None = None,
        timeout_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("VastClient needs an API key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http = http_client if http_client is not None else httpx.Client(timeout=timeout_seconds)
        # A separate client so nothing configured on the API client (headers, auth) can reach the log host.
        self._logs_http = logs_http_client if logs_http_client is not None else httpx.Client(timeout=timeout_seconds)
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"VastClient(base_url={self._base_url!r})"

    # -- Offers ----------------------------------------------------------------------------------------

    def search_offers(self, query: OfferQuery) -> list[Offer]:
        """Offers matching `query`, cheapest `dph_total` first. Vast returns at most 512 per search."""
        data = self._request("POST", "/api/v0/bundles/", json=query.to_body())
        return [Offer.from_json(raw) for raw in data.get("offers") or ()]

    # -- Instances -------------------------------------------------------------------------------------

    def create_instance(self, offer_id: int, request: CreateInstance) -> int:
        """Rent `offer_id` and start `request` on it. Returns the new instance id; record it before anything
        else can fail. The response's `instance_api_key` is dropped here and never stored or logged."""
        data = self._request("PUT", f"/api/v0/asks/{int(offer_id)}/", json=request.to_body())
        instance_id = data.get("new_contract")
        if not isinstance(instance_id, int):
            raise VastRequestFailedError("Vast's create response had no new_contract id")
        return instance_id

    def show_instance(self, instance_id: int) -> Instance | None:
        """The instance, or `None` when Vast says the account has no such instance (destroyed, or never ours)."""
        data = self._request("GET", f"/api/v0/instances/{int(instance_id)}/", params={"owner": "me"})
        raw = data.get("instances")
        return Instance.from_json(raw) if isinstance(raw, dict) else None

    def list_instances(self, *, label_prefix: str | None = None) -> list[Instance]:
        """Every instance on the account (all pages). `label_prefix` filters client-side."""
        instances: list[Instance] = []
        after: str | None = None
        while True:
            params: dict[str, Any] = {"limit": INSTANCE_PAGE_SIZE}
            if after:
                params["after_token"] = after
            data = self._request("GET", "/api/v1/instances/", params=params)
            for raw in data.get("instances") or ():
                instance = Instance.from_json(raw)
                if label_prefix is None or (instance.label or "").startswith(label_prefix):
                    instances.append(instance)
            after = data.get("next_token")
            if not after:
                return instances

    def change_bid(self, instance_id: int, price: Decimal) -> None:
        """Set an interruptible instance's bid, in dollars per hour."""
        self._request(
            "PUT", f"/api/v0/instances/bid_price/{int(instance_id)}/", json={"client_id": "me", "price": float(price)}
        )

    def stop_instance(self, instance_id: int) -> None:
        """Stop the container. The GPU stops billing; the disk keeps billing. A member can start a stopped
        instance again with the container's own key, so budget and runtime stops must `destroy_instance`."""
        self._request("PUT", f"/api/v0/instances/{int(instance_id)}/", json={"state": "stopped"})

    def start_instance(self, instance_id: int) -> None:
        """Start a stopped instance. Vast restarts it only if the machine's GPU is still free."""
        self._request("PUT", f"/api/v0/instances/{int(instance_id)}/", json={"state": "running"})

    def destroy_instance(self, instance_id: int) -> bool:
        """Destroy the instance and its disk. Returns `False` if Vast says it doesn't exist (already gone).
        Callers confirm with `show_instance` that it really is gone."""
        try:
            self._request("DELETE", f"/api/v0/instances/{int(instance_id)}/")
        except VastRequestFailedError as exc:
            if exc.status_code == 404 or exc.error == "no_such_instance":
                return False
            raise
        return True

    def request_logs(self, instance_id: int, *, tail: int = 200, attempts: int = 6, wait_seconds: float = 3.0) -> str:
        """The container's last `tail` log lines. Vast writes them to a URL on another host a moment after the
        request; that URL is fetched without Krater's Authorization header. The text is the member's own
        output: show it to them, don't log it."""
        data = self._request("PUT", f"/api/v0/instances/request_logs/{int(instance_id)}/", json={"tail": str(tail)})
        url = data.get("result_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise VastRequestFailedError("Vast's request_logs response had no https result_url")
        for attempt in range(attempts):
            try:
                response = self._logs_http.get(url, follow_redirects=False)
            except httpx.HTTPError as exc:
                raise VastUnavailableError(f"could not fetch instance logs: {type(exc).__name__}") from exc
            if response.status_code == 200 and response.text.strip():
                return response.text
            if attempt + 1 < attempts:
                self._sleep(wait_seconds)
        return ""

    # -- Billing ---------------------------------------------------------------------------------------

    def charges(self, since: datetime, until: datetime) -> list[Charge]:
        """Instance charges for the days from `since` to `until` (all pages). Fresh within about a minute."""
        filters = {
            "day": {"gte": int(since.timestamp()), "lte": int(until.timestamp())},
            "type": {"in": ["instance"]},
        }
        charges: list[Charge] = []
        after: str | None = None
        while True:
            params: dict[str, Any] = {"select_filters": json.dumps(filters), "limit": CHARGES_PAGE_SIZE}
            if after:
                params["after_token"] = after
            data = self._request("GET", "/api/v0/charges/", params=params)
            charges.extend(Charge.from_json(raw) for raw in data.get("results") or ())
            after = data.get("next_token")
            if not after:
                return charges

    # -- Plumbing --------------------------------------------------------------------------------------

    def _request(
        self, method: str, path: str, *, json: dict | None = None, params: dict | None = None
    ) -> dict[str, Any]:
        if not path.endswith("/"):
            # Vast would answer 308 and do nothing; refuse before sending.
            raise ValueError(f"Vast paths must end with a slash: {path}")
        try:
            response = self._http.request(
                method,
                f"{self._base_url}{path}",
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"},
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise VastUnavailableError(f"Vast timed out on {method} {path}") from exc
        except httpx.HTTPError as exc:
            raise VastUnavailableError(f"could not reach Vast for {method} {path}: {type(exc).__name__}") from exc

        status = response.status_code
        if 300 <= status < 400:
            raise VastRedirectError(
                f"Vast redirected {method} {path} ({status}); the request did nothing", status_code=status
            )
        if status == 429:
            raise VastRateLimitedError(f"Vast rate-limited {method} {path}")
        if status >= 500:
            raise VastUnavailableError(f"Vast answered {status} to {method} {path}")

        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise VastRequestFailedError(
                f"Vast answered {status} to {method} {path} without a JSON object", status_code=status
            )
        if status >= 400 or data.get("success") is False:
            error = data.get("error") if isinstance(data.get("error"), str) else None
            message = data.get("msg") or data.get("detail") or error or "no message"
            raise VastRequestFailedError(
                f"Vast refused {method} {path} ({status}): {str(message)[:300]}", status_code=status, error=error
            )
        return data
