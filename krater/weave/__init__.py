"""`krater.weave`: the only place that talks to Weave. Krater uses Weave for OIDC sign-in only.

Nothing outside this package should import `httpx` for Weave or know its URLs, scopes or claim shapes --
go through `WeaveClient` (via `get_weave_client`, a FastAPI dependency that tests can override). See
`docs/weave-integration.md` for the contract.
"""

from __future__ import annotations

from functools import lru_cache

from krater.config import get_settings
from krater.weave.client import WeaveClient
from krater.weave.errors import WeaveAuthError, WeaveError, WeaveUnavailableError
from krater.weave.live import LiveWeaveClient
from krater.weave.stub import StubWeaveClient
from krater.weave.types import WeaveIdentity

__all__ = [
    "LiveWeaveClient",
    "StubWeaveClient",
    "WeaveAuthError",
    "WeaveClient",
    "WeaveError",
    "WeaveIdentity",
    "WeaveUnavailableError",
    "get_weave_client",
]


@lru_cache
def get_weave_client() -> WeaveClient:
    """The process-wide `WeaveClient`, chosen by `settings.weave_mode`.

    A FastAPI dependency; override it in tests with `app.dependency_overrides[get_weave_client]`.
    """
    settings = get_settings()
    if settings.weave_mode == "stub":
        return StubWeaveClient(settings.weave_stub_users_file or None)
    return LiveWeaveClient(settings)
