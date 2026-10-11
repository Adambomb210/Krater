"""`FakeVast`: an in-memory Vast.ai for dev and tests, served through `httpx.MockTransport`.

Tests drive the real `VastClient` against it, so it behaves like the live API in the ways that cost money to get
wrong (checked in the 2026-10-11 spike):

- a path without its trailing slash gets a 308 and **changes nothing**, DELETE included;
- the v0 instance list answers 410; the v1 list pages 25 at a time;
- showing an unknown id answers `{"instances": null}`;
- any request without the right `Authorization: Bearer` header gets a 401;
- a create response carries an `instance_api_key`, which the client must drop;
- a search returns at most 512 offers, and prices `dph_total` at the search's `allocated_storage`;
- log downloads are served from another host, which records whether an Authorization header reached it.

Test helpers (`add_offer`, `set_status`, `add_charge`, `fail_next`) set up what the live API would report.
"""

from __future__ import annotations

import base64
import itertools
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx

from krater.vast.client import VastClient
from krater.vast.types import HOURS_PER_MONTH, SEARCH_RESULT_CAP

FAKE_API_KEY = "fake-vast-key"
API_HOST = "console.vast.ai"
LOGS_HOST = "logs.fake-vast.test"
#: Vast's default `allocated_storage` when a search leaves it out (measured: about 8.1 GB).
DEFAULT_ALLOCATED_STORAGE = 8.1


def _offer(
    offer_id: int,
    machine_id: int,
    gpu_name: str,
    *,
    dph_base: float,
    min_bid: float,
    storage_cost: float,
    gpu_ram_mb: int,
    reliability: float = 0.99,
    num_gpus: int = 1,
    geolocation: str = "US",
    direct_port_count: int = 20,
) -> dict[str, Any]:
    return {
        "id": offer_id,
        "machine_id": machine_id,
        "gpu_name": gpu_name,
        "num_gpus": num_gpus,
        "gpu_ram": gpu_ram_mb,
        "gpu_frac": 1.0,
        "dph_base": dph_base,
        "min_bid": min_bid,
        "storage_cost": storage_cost,
        "inet_up_cost": 0.002,
        "inet_down_cost": 0.002,
        "reliability": reliability,
        "dlperf_per_dphtotal": 100.0,
        "cuda_max_good": 12.4,
        "direct_port_count": direct_port_count,
        "disk_space": 200.0,
        "geolocation": geolocation,
        "verification": "verified",
        "verified": True,
        "rentable": True,
        "rented": False,
    }


def default_offers() -> list[dict[str, Any]]:
    """A small market: two GPU models, a few hosts each, bid well under on-demand. Dev and tests only."""
    return [
        _offer(1001, 501, "RTX 3060", dph_base=0.060, min_bid=0.050, storage_cost=0.20, gpu_ram_mb=12288),
        _offer(1002, 502, "RTX 3060", dph_base=0.070, min_bid=0.045, storage_cost=0.80, gpu_ram_mb=12288),
        _offer(1003, 503, "RTX 3060", dph_base=0.090, min_bid=0.060, storage_cost=0.15, gpu_ram_mb=12288),
        _offer(1004, 504, "RTX 4090", dph_base=0.350, min_bid=0.200, storage_cost=0.30, gpu_ram_mb=24564),
        _offer(1005, 505, "RTX 4090", dph_base=0.400, min_bid=0.260, storage_cost=0.25, gpu_ram_mb=24564),
        _offer(1006, 506, "RTX 4090", dph_base=0.380, min_bid=0.180, storage_cost=0.40, gpu_ram_mb=24564),
        _offer(1007, 507, "RTX 4090", dph_base=0.300, min_bid=0.150, storage_cost=0.20, gpu_ram_mb=24564, num_gpus=2),
    ]


def _matches(value: Any, condition: Any) -> bool:
    if not isinstance(condition, dict):
        return value == condition
    for op, expected in condition.items():
        if op == "eq" and value != expected:
            return False
        if op == "neq" and value == expected:
            return False
        if op == "in" and value not in expected:
            return False
        if op == "notin" and value in expected:
            return False
        if op in ("gte", "gt", "lte", "lt"):
            if value is None:
                return False
            if op == "gte" and not value >= expected:
                return False
            if op == "gt" and not value > expected:
                return False
            if op == "lte" and not value <= expected:
                return False
            if op == "lt" and not value < expected:
                return False
    return True


#: Search body keys that aren't offer-field filters.
_SEARCH_CONTROL_KEYS = {"limit", "type", "order", "allocated_storage"}


class FakeVast:
    """The fake API's state, plus the `httpx` handler that serves it."""

    def __init__(self, *, api_key: str = FAKE_API_KEY, offers: list[dict[str, Any]] | None = None) -> None:
        self.api_key = api_key
        self.offers: dict[int, dict[str, Any]] = {o["id"]: dict(o) for o in (offers or default_offers())}
        self.instances: dict[int, dict[str, Any]] = {}
        self.charges: list[dict[str, Any]] = []
        self.logs: dict[int, str] = {}
        #: Every request seen: (method, path) on the API host, ("GET", url) on the log host.
        self.requests: list[tuple[str, str]] = []
        #: Whether any log download carried an Authorization header (it must never).
        self.log_fetch_saw_authorization = False
        self.search_bodies: list[dict[str, Any]] = []
        self._ids = itertools.count(55_000_001)
        self._failures: list[int] = []

    # -- Wiring ----------------------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, *, sleep: Callable[[float], None] = lambda _: None) -> VastClient:
        """A real `VastClient` wired to this fake."""
        return VastClient(
            self.api_key,
            base_url=f"https://{API_HOST}",
            http_client=httpx.Client(transport=self.transport()),
            logs_http_client=httpx.Client(transport=self.transport()),
            sleep=sleep,
        )

    # -- Test helpers ----------------------------------------------------------------------------------

    def add_offer(self, offer_id: int, machine_id: int, gpu_name: str, **kwargs: Any) -> dict[str, Any]:
        offer = _offer(offer_id, machine_id, gpu_name, **kwargs)
        self.offers[offer_id] = offer
        return offer

    def set_status(self, instance_id: int, actual_status: str | None, **fields: Any) -> None:
        """What Vast would report next: `running` after the pull, `stopped` when stopped or paused, ..."""
        self.instances[instance_id]["actual_status"] = actual_status
        self.instances[instance_id].update(fields)

    def add_charge(self, instance_id: int, *, gpu: str, disk: str = "0", start: int = 0, end: int = 0) -> None:
        items = [
            {"type": "gpu", "amount": float(gpu), "description": "gpu"},
            {"type": "disk", "amount": float(disk), "description": "disk"},
        ]
        self.charges.append(
            {
                "source": f"instance-{instance_id}",
                "type": "instance",
                "start": start,
                "end": end,
                "amount": float(Decimal(gpu) + Decimal(disk)),
                "metadata": {"label": (self.instances.get(instance_id) or {}).get("label")},
                "items": items,
            }
        )

    def fail_next(self, status_code: int, times: int = 1) -> None:
        """Answer the next `times` API requests with `status_code` (429, 500, ...) and do nothing else."""
        self._failures.extend([status_code] * times)

    # -- The handler -----------------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == LOGS_HOST:
            self.requests.append(("GET", str(request.url)))
            if "authorization" in request.headers:
                self.log_fetch_saw_authorization = True
            instance_id = int(request.url.path.strip("/").split("/")[-1])
            return httpx.Response(200, text=self.logs.get(instance_id, ""))

        path = request.url.path
        self.requests.append((request.method, path))
        if request.headers.get("authorization") != f"Bearer {self.api_key}":
            return httpx.Response(401, json={"success": False, "error": "auth_error", "msg": "unauthorized"})
        if self._failures:
            status = self._failures.pop(0)
            if status == 429:
                return httpx.Response(429, json={"detail": "API requests too frequent"})
            return httpx.Response(status, text="server error")
        if not path.endswith("/"):
            # The live API: 308 to the slashed path, and the request itself does nothing.
            return httpx.Response(308, headers={"Location": f"https://{API_HOST}{path}/"}, text="Permanent Redirect")

        parts = [p for p in path.split("/") if p]
        body: dict[str, Any] = {}
        if request.content:
            body = json.loads(request.content)

        match (request.method, parts):
            case ("POST", ["api", "v0", "bundles"]):
                return self._search(body)
            case ("PUT", ["api", "v0", "asks", offer_id]):
                return self._create(int(offer_id), body)
            case ("GET", ["api", "v0", "instances"]):
                return httpx.Response(410, json={"success": False, "error": "gone", "msg": "use /api/v1/instances/"})
            case ("GET", ["api", "v1", "instances"]):
                return self._list(request)
            case ("GET", ["api", "v0", "instances", instance_id]):
                instance = self.instances.get(int(instance_id))
                return httpx.Response(200, json={"instances": dict(instance) if instance else None})
            case ("PUT", ["api", "v0", "instances", "bid_price", instance_id]):
                return self._change_bid(int(instance_id), body)
            case ("PUT", ["api", "v0", "instances", "request_logs", instance_id]):
                if int(instance_id) not in self.instances:
                    return self._no_such_instance()
                url = f"https://{LOGS_HOST}/logs/{int(instance_id)}"
                return httpx.Response(200, json={"success": True, "result_url": url})
            case ("PUT", ["api", "v0", "instances", instance_id]):
                return self._manage(int(instance_id), body)
            case ("DELETE", ["api", "v0", "instances", instance_id]):
                return self._destroy(int(instance_id))
            case ("GET", ["api", "v0", "charges"]):
                return self._charges(request)
        return httpx.Response(404, json={"success": False, "error": "not_found", "msg": path})

    # -- Endpoints -------------------------------------------------------------------------------------

    def _search(self, body: dict[str, Any]) -> httpx.Response:
        self.search_bodies.append(body)
        storage = float(body.get("allocated_storage", DEFAULT_ALLOCATED_STORAGE))
        results = []
        for offer in self.offers.values():
            if not all(_matches(offer.get(k), v) for k, v in body.items() if k not in _SEARCH_CONTROL_KEYS):
                continue
            priced = dict(offer)
            disk = Decimal(str(offer["storage_cost"])) * Decimal(str(storage)) / HOURS_PER_MONTH
            priced["dph_total"] = float(Decimal(str(offer["dph_base"])) + disk)
            priced["is_bid"] = body.get("type") == "bid"
            results.append(priced)
        results.sort(key=lambda o: o["dph_total"])
        limit = min(int(body.get("limit", 64)), SEARCH_RESULT_CAP)
        return httpx.Response(200, json={"offers": results[:limit]})

    def _create(self, offer_id: int, body: dict[str, Any]) -> httpx.Response:
        offer = self.offers.get(offer_id)
        if offer is None:
            return httpx.Response(404, json={"success": False, "error": "no_such_ask", "msg": "no such offer"})
        if offer["rented"]:
            return httpx.Response(
                400,
                json={"success": False, "error": "invalid_args", "msg": "GPU conflict: requesting already rented gpus"},
            )
        price = body.get("price")
        if price is not None and price < offer["min_bid"]:
            return httpx.Response(400, json={"success": False, "error": "invalid_price", "msg": "bid below min_bid"})
        if not body.get("image"):
            return httpx.Response(400, json={"success": False, "error": "invalid_args", "msg": "image required"})
        offer["rented"] = True
        instance_id = next(self._ids)
        disk = Decimal(str(offer["storage_cost"])) * Decimal(str(body.get("disk", 8))) / HOURS_PER_MONTH
        gpu_price = Decimal(str(price)) if price is not None else Decimal(str(offer["dph_base"]))
        self.instances[instance_id] = {
            "id": instance_id,
            "label": body.get("label"),
            "image": body["image"],
            "env": body.get("env"),
            "onstart": body.get("onstart"),
            "args": body.get("args"),
            "runtype": body.get("runtype"),
            "offer_id": offer_id,
            "actual_status": None,
            "intended_status": "running",
            "cur_state": "running",
            "next_state": "running",
            "status_msg": "pulling image",
            "gpu_util": None,
            "cpu_util": 0.0,
            "dph_base": float(gpu_price),
            "dph_total": float(gpu_price + disk),
            "min_bid": offer["min_bid"],
            "is_bid": price is not None,
            "machine_id": offer["machine_id"],
            "gpu_name": offer["gpu_name"],
            "num_gpus": offer["num_gpus"],
            "disk_space": float(body.get("disk", 8)),
            "start_date": 1_791_684_000.0,
            "ssh_host": "ssh1.vast.ai",
            "ssh_port": 20_000 + instance_id % 1000,
            "public_ipaddr": "203.0.113.10",
            "ports": {},
        }
        api_key = base64.b16encode(instance_id.to_bytes(8, "big")).decode().lower() * 4
        return httpx.Response(200, json={"success": True, "new_contract": instance_id, "instance_api_key": api_key})

    def _list(self, request: httpx.Request) -> httpx.Response:
        ids = sorted(self.instances)
        after = request.url.params.get("after_token")
        start = int(base64.b64decode(after).decode()) if after else 0
        page = ids[start : start + 25]
        next_token = base64.b64encode(str(start + 25).encode()).decode() if start + 25 < len(ids) else None
        return httpx.Response(
            200,
            json={
                "success": True,
                "instances_found": len(page),
                "total_instances": len(ids),
                "next_token": next_token,
                "instances": [dict(self.instances[i]) for i in page],
            },
        )

    def _change_bid(self, instance_id: int, body: dict[str, Any]) -> httpx.Response:
        instance = self.instances.get(instance_id)
        if instance is None:
            return self._no_such_instance()
        price = body.get("price")
        if body.get("client_id") != "me" or not isinstance(price, int | float) or price < 0.001:
            return httpx.Response(400, json={"success": False, "error": "invalid_args", "msg": "bad bid"})
        disk = Decimal(str(instance["dph_total"])) - Decimal(str(instance["dph_base"]))
        instance["dph_base"] = float(price)
        instance["dph_total"] = float(Decimal(str(price)) + disk)
        return httpx.Response(200, json={"success": True})

    def _manage(self, instance_id: int, body: dict[str, Any]) -> httpx.Response:
        instance = self.instances.get(instance_id)
        if instance is None:
            return self._no_such_instance()
        state = body.get("state")
        if state not in ("running", "stopped") and "label" not in body:
            return httpx.Response(400, json={"success": False, "error": "invalid_args", "msg": "bad state"})
        if state is not None:
            instance["intended_status"] = state
            instance["cur_state"] = state
            instance["actual_status"] = "stopped" if state == "stopped" else instance["actual_status"]
        if "label" in body:
            instance["label"] = body["label"]
        return httpx.Response(200, json={"success": True})

    def _destroy(self, instance_id: int) -> httpx.Response:
        instance = self.instances.pop(instance_id, None)
        if instance is None:
            return self._no_such_instance()
        offer = self.offers.get(instance["offer_id"])
        if offer is not None:
            offer["rented"] = False
        return httpx.Response(200, json={"success": True})

    def _charges(self, request: httpx.Request) -> httpx.Response:
        raw_filters = request.url.params.get("select_filters")
        if not raw_filters or "day" not in json.loads(raw_filters):
            return httpx.Response(400, json={"success": False, "error": "invalid_args", "msg": "day is required"})
        limit = min(int(request.url.params.get("limit", 100)), 500)
        after = request.url.params.get("after_token")
        start = int(after) if after else 0
        page = self.charges[start : start + limit]
        next_token = str(start + limit) if start + limit < len(self.charges) else None
        return httpx.Response(
            200,
            json={
                "success": True,
                "count": len(page),
                "total": len(self.charges),
                "next_token": next_token,
                "results": page,
            },
        )

    @staticmethod
    def _no_such_instance() -> httpx.Response:
        return httpx.Response(404, json={"success": False, "error": "no_such_instance", "msg": "no such instance"})
