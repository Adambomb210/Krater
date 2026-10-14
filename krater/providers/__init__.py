"""`krater.providers`: where Krater Compute runs members' work, and what each place can do.

Krater Compute rents machines from a provider and runs its runner on them. Vast.ai is the only provider so far; this
package is the seam a second one plugs into (docs/compute/providers.md says how, and what each kind of provider
needs). It holds:

- `Capabilities`: what a provider can do. Krater branches on these, never on a provider's name: a provider without
  SSH gets no boxes, one without bids gets no limit orders, one that doesn't report charges keeps Krater's own
  estimate as the spend.
- `ComputeProvider`: the calls the scheduler, the reconciler and the price history make. A provider's package (like
  `krater.vast`) implements them and is the only code that knows that provider's URLs and wire shapes.
- The registry: `KRATER_COMPUTE_MODE` names the provider in use (`off` for none). Like the Vast key, a provider's
  credentials are read only by the worker.

The shapes the calls take and return (`Offer`, `Instance`, `CreateInstance`, `Charge`) started as Vast's, and live in
`krater.vast.types` until a second provider needs them neutral; docs/compute/providers.md lists what changes then.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from krater.providers.errors import (
    ProviderError,
    ProviderRateLimitedError,
    ProviderRequestFailedError,
    ProviderSearchQuotaError,
    ProviderUnavailableError,
)
from krater.providers.phase import Phase

if TYPE_CHECKING:
    from krater.config import Settings
    from krater.vast import Charge, CreateInstance, Instance, Offer, OfferQuery, SearchQuota

__all__ = [
    "Capabilities",
    "ComputeProvider",
    "Phase",
    "ProviderError",
    "ProviderInfo",
    "ProviderRateLimitedError",
    "ProviderRequestFailedError",
    "ProviderSearchQuotaError",
    "ProviderUnavailableError",
    "active",
    "get_provider",
    "register",
    "registered",
]


@dataclass(frozen=True)
class Capabilities:
    """What a provider can do. Every flag gates something members see, so a provider that can't do a thing never
    offers it."""

    #: Machines at a fixed hourly price that run until Krater ends them.
    on_demand: bool
    #: Cheaper machines the provider can take back (Vast's interruptible, AWS and GCP spot).
    interruptible: bool
    #: The interruptible price is a bid Krater sets and can raise (Vast). Limit orders need it. Without it the
    #: provider sets the price (AWS spot) and Krater can only choose whether to rent.
    bids: bool
    #: An interrupted machine pauses with its disk and resumes when it can (Vast). Without it, an interrupted
    #: machine is gone and the job starts again elsewhere from its last checkpoint in storage.
    outbid_pauses: bool
    #: A machine can be stopped, keeping its disk, and started again: boxes can stop and resume.
    stop_start: bool
    #: Members can SSH into a machine: boxes need it.
    ssh: bool
    #: The runner can stop its own machine when Krater is gone (the dead-man switch, docs/compute/runner.md).
    #: Without it the provider must end abandoned work itself (a batch system's own runtime limit).
    self_stop: bool
    #: The provider reports what it billed per machine, which replaces Krater's estimate (`krater.compute.reconcile`).
    charges: bool
    #: Searches count against a quota the provider reports (`krater.compute.search_quota`).
    search_quota: bool

    @property
    def boxes(self) -> bool:
        """Boxes are machines a member SSHes into, stops and resumes."""
        return self.ssh and self.stop_start


class ComputeProvider(Protocol):
    """The calls Krater Compute makes. Instances are found by the label Krater gives them, so a create whose answer
    was lost is still found (and anything left over is swept); every destroy is confirmed by `show_instance` returning
    `None`. Errors are `krater.providers.errors` subclasses."""

    #: The latest search quota the provider reported, when it has one (`Capabilities.search_quota`).
    search_quota: SearchQuota | None

    def search_offers(self, query: OfferQuery) -> list[Offer]: ...

    def create_instance(self, offer_id: str, request: CreateInstance) -> str: ...

    def show_instance(self, instance_id: str) -> Instance | None: ...

    def list_instances(self, *, label_prefix: str | None = None) -> list[Instance]: ...

    def change_bid(self, instance_id: str, price: Decimal) -> None: ...

    def stop_instance(self, instance_id: str) -> None: ...

    def start_instance(self, instance_id: str) -> None: ...

    def destroy_instance(self, instance_id: str) -> bool: ...

    def charges(self, since: datetime, until: datetime) -> list[Charge]: ...

    def list_ssh_keys(self) -> list[dict[str, Any]]:
        """SSH keys the provider installs in every machine of the account. Krater starts no boxes while there are any:
        their holder could get into every member's box."""
        ...


@dataclass(frozen=True)
class ProviderInfo:
    #: The value of `KRATER_COMPUTE_MODE` that selects it.
    name: str
    #: What members and admins see ("Vast.ai").
    title: str
    capabilities: Capabilities
    #: How the runner stops its own machine when Krater is gone (`StartInfo.self_stop`, docs/compute/runner.md):
    #: `vast_api` (Vast's API with the container's own key) or `shutdown` (a cloud VM whose shutdown behaviour is
    #: stop). `None` with `capabilities.self_stop` off.
    self_stop_method: str | None
    #: The worker's client for these settings, or `None` when Krater has no usable account with the provider. Called
    #: on every pass, so a rotated credential takes effect without a restart; cache inside if building is costly.
    client: Callable[[Settings], ComputeProvider | None]


_REGISTRY: dict[str, ProviderInfo] = {}


def register(info: ProviderInfo) -> None:
    if info.name == "off":
        raise ValueError("`off` means no provider")
    _REGISTRY[info.name] = info


def registered() -> dict[str, ProviderInfo]:
    return dict(_REGISTRY)


def active(settings: Settings) -> ProviderInfo | None:
    """The provider `KRATER_COMPUTE_MODE` selects, or `None` when compute is off."""
    if settings.compute_mode == "off":
        return None
    try:
        return _REGISTRY[settings.compute_mode]
    except KeyError:
        raise ValueError(f"KRATER_COMPUTE_MODE={settings.compute_mode!r} names no registered provider") from None


def get_provider(settings: Settings) -> ComputeProvider | None:
    """The worker's client for the active provider, or `None` when compute is off or the provider is unusable."""
    info = active(settings)
    return info.client(settings) if info is not None else None


# -- Built-in providers ----------------------------------------------------------------------------------------


def _vast_client(settings: Settings) -> ComputeProvider | None:
    # Imported here: `krater.vast` builds on this package's errors.
    from krater.vast import get_vast_client

    del settings  # get_vast_client reads the same settings
    return get_vast_client()


register(
    ProviderInfo(
        name="vast",
        title="Vast.ai",
        capabilities=Capabilities(
            on_demand=True,
            interruptible=True,
            bids=True,
            outbid_pauses=True,
            stop_start=True,
            ssh=True,
            self_stop=True,
            charges=True,
            search_quota=True,
        ),
        self_stop_method="vast_api",
        client=_vast_client,
    )
)
