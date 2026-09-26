"""`FakeObjectStore`: an in-memory `ObjectStore` for `KRATER_S3_MODE=fake` (development and tests).

Makes no network calls, and doesn't actually store bytes -- just enough bookkeeping (size + content
type per key) for `krater.services.screenshots` to exercise the real confirm/head/delete flow. A test
that wants to simulate "the browser successfully uploaded through the presigned POST" calls `put`
directly, mirroring how `FakeSkyPilotClient.add_cluster` simulates a fact a real server would otherwise
report.
"""

from __future__ import annotations

from krater.storage.types import ObjectMeta, PresignedPost


class FakeObjectStore:
    def __init__(self) -> None:
        self._objects: dict[str, ObjectMeta] = {}

    # -- ObjectStore protocol ------------------------------------------------------------------------

    def presign_upload(self, key: str, *, content_type: str, max_bytes: int, expires: int = 300) -> PresignedPost:
        del expires
        # A fake URL, never actually POSTed to in tests -- callers that need to simulate a completed
        # upload use `put` below instead. The fields still carry the real policy inputs so a test can
        # assert on them if it wants to.
        return PresignedPost(
            url=f"fake://krater-storage/{key}",
            fields={"key": key, "Content-Type": content_type, "x-fake-max-bytes": str(max_bytes)},
        )

    def presign_download(self, key: str, *, expires: int = 3600) -> str:
        return f"fake://krater-storage/{key}?expires={expires}"

    def head(self, key: str) -> ObjectMeta | None:
        return self._objects.get(key)

    def delete(self, key: str) -> None:
        self._objects.pop(key, None)

    # -- Test helpers ----------------------------------------------------------------------------------

    def put(self, key: str, *, content_type: str, size_bytes: int) -> None:
        """Simulate a browser having uploaded `key` through the presigned POST above."""
        self._objects[key] = ObjectMeta(size_bytes=size_bytes, content_type=content_type)


__all__ = ["FakeObjectStore"]
