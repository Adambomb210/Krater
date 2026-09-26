"""Exception types raised by `krater.skypilot`.

Callers branch on these two, mirroring `krater.weave.errors`: one for "couldn't even reach or complete
the round trip with SkyPilot" (network error, timeout, a request stuck non-terminal past our deadline),
one for "SkyPilot understood the request but it failed" (a `FAILED` polled request, a non-2xx response
with a body we can show).
"""

from __future__ import annotations


class SkyPilotError(Exception):
    """Base class for every error raised by `krater.skypilot`."""


class SkyPilotUnavailableError(SkyPilotError):
    """SkyPilot couldn't be reached at all: a network error, a timeout, or a request whose polled
    status never reached SUCCEEDED/FAILED before our deadline."""


class SkyPilotRequestFailedError(SkyPilotError):
    """SkyPilot was reached and understood the request, but it failed. Carries the server's own
    message (from the polled request's `error`, or the HTTP response body) so callers can show it."""
