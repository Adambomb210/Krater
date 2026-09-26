"""Screenshot uploads on a completion draft: presign, confirm, remove.

See `docs/SPEC.md` ("Completion flow & public gallery": "Uploads use presigned URLs, and only objects
from approved completion revisions are served publicly"). The three-step flow this module implements:

1. `presign_screenshot` -- the submitter asks for somewhere to upload. This picks the key (never the
   client) and returns a presigned `POST` policy that pins the object's size and content type into the
   signature, so storage itself rejects an oversized or mislabeled upload.
2. The browser uploads directly to storage, bypassing Krater's own process entirely.
3. `confirm_screenshot` -- the submitter tells Krater the upload finished. This is the only step that
   actually changes `ProjectRevision.screenshot_keys`, and it re-checks the object via `ObjectStore.head`
   before trusting it: the presigned policy is *storage's* enforcement, not Krater's, so a client that
   skips step 1 (or a storage backend that doesn't enforce every policy condition -- see
   `docs/dev/storage.md`) must not be able to sneak an oversized or wrong-type object into the gallery.

No image is ever decoded (no Pillow, no magic-byte sniffing beyond what `head` reports). That's an
acceptable gap for now because: uploads require a signed-in member on their own completion draft, not an
anonymous/public endpoint; a mislabeled object is only ever rendered back as an `<img src>`, which just
fails to display rather than executing anything; and decoding untrusted image bytes server-side is its
own attack surface (image-parser CVEs) not worth adding for this. If screenshots ever accept broader or
more adversarial input, add a decode-and-reencode step before confirming.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from krater.models import Project, ProjectRevision, RevisionKind
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed
from krater.storage.client import ObjectStore
from krater.storage.errors import StorageError
from krater.storage.types import PresignedPost

#: PNG / JPEG / WebP only, per docs/SPEC.md's screenshot upload scope. Maps content type -> the
#: extension used in the generated key (cosmetic only; the type actually stored comes from `head`).
ALLOWED_CONTENT_TYPES: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}

MAX_SCREENSHOTS = 6
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class ScreenshotUpload:
    """What `presign_screenshot` hands back: the server-chosen key, and the presigned POST to upload to."""

    key: str
    post: PresignedPost


def _require_completion_draft(actor: Actor, project: Project) -> ProjectRevision:
    """The project's current, unsubmitted `completion` revision, or raise. Submitter only.

    Shared by all three operations: screenshots only ever belong to the *draft in progress*, never a
    submitted/decided revision (which is immutable review history) or another kind of revision.
    """
    if actor.user.id != project.submitter_id:
        raise NotAllowed("Only the submitter may manage this project's screenshots.")

    draft = project.current_revision
    if draft is None or draft.submitted_at is not None or draft.kind is not RevisionKind.COMPLETION:
        raise InvalidState("Screenshots can only be added to a completion draft.")
    return draft


def _key_prefix(project: Project, draft: ProjectRevision) -> str:
    return f"projects/{project.id}/{draft.id}/"


def presign_screenshot(
    session: Session,
    actor: Actor,
    *,
    project: Project,
    content_type: str,
    store: ObjectStore,
) -> ScreenshotUpload:
    """Issue a presigned upload for a new screenshot on the project's completion draft.

    Validates `content_type` and the current screenshot count (`ValidationFailed` otherwise -- the same
    limit is re-checked in `confirm_screenshot`, since two presigns can be issued before either is
    confirmed). The key is always server-generated
    (`projects/<project_id>/<revision_id>/<uuid>.<ext>`), never taken from the caller.
    """
    draft = _require_completion_draft(actor, project)

    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValidationFailed({"screenshot": "Only PNG, JPEG or WebP screenshots are allowed."})
    if len(draft.screenshot_keys) >= MAX_SCREENSHOTS:
        raise ValidationFailed({"screenshot": f"At most {MAX_SCREENSHOTS} screenshots are allowed."})

    extension = ALLOWED_CONTENT_TYPES[content_type]
    key = f"{_key_prefix(project, draft)}{uuid.uuid4()}.{extension}"
    post = store.presign_upload(key, content_type=content_type, max_bytes=MAX_SCREENSHOT_BYTES)
    return ScreenshotUpload(key=key, post=post)


def confirm_screenshot(
    session: Session,
    actor: Actor,
    *,
    project: Project,
    key: str,
    store: ObjectStore,
) -> ProjectRevision:
    """Confirm a screenshot finished uploading to `key`, and append it to the draft.

    Re-validates everything server-side rather than trusting the browser's earlier presign request:
    `key` must belong to this project's current draft (never an arbitrary, client-chosen key), the
    screenshot limit must still hold, and the object must actually exist in storage with an allowed
    content type and size at or under `MAX_SCREENSHOT_BYTES` (`ValidationFailed` if `head` finds
    anything else, including nothing at all). A rejected object is best-effort deleted from storage so
    it doesn't linger.
    """
    draft = _require_completion_draft(actor, project)

    if not key.startswith(_key_prefix(project, draft)):
        raise NotAllowed("That upload doesn't belong to this project's draft.")
    if key in draft.screenshot_keys:
        return draft  # already confirmed; idempotent
    if len(draft.screenshot_keys) >= MAX_SCREENSHOTS:
        raise ValidationFailed({"screenshot": f"At most {MAX_SCREENSHOTS} screenshots are allowed."})

    meta = store.head(key)
    if meta is None:
        raise ValidationFailed({"screenshot": "Upload not found. Try again."})
    if meta.content_type not in ALLOWED_CONTENT_TYPES or meta.size_bytes > MAX_SCREENSHOT_BYTES:
        # Best-effort: the object is orphaned but harmless, and never gets linked to the draft either way.
        with contextlib.suppress(StorageError):
            store.delete(key)
        raise ValidationFailed({"screenshot": "That upload isn't a valid PNG, JPEG or WebP under 5 MB."})

    draft.screenshot_keys = [*draft.screenshot_keys, key]
    session.flush()
    return draft


def remove_screenshot(
    session: Session,
    actor: Actor,
    *,
    project: Project,
    key: str,
    store: ObjectStore,
) -> ProjectRevision:
    """Remove `key` from the draft's screenshots, and best-effort delete it from storage.

    Raises `InvalidState` if `key` isn't currently one of the draft's screenshots (covers both a typo
    and a double-submit of the remove form).
    """
    draft = _require_completion_draft(actor, project)

    if key not in draft.screenshot_keys:
        raise InvalidState("That screenshot isn't on this draft.")

    draft.screenshot_keys = [existing for existing in draft.screenshot_keys if existing != key]
    session.flush()

    # Best-effort, per docs/SPEC.md/module docstring: the draft's own list is the source of truth.
    with contextlib.suppress(StorageError):
        store.delete(key)

    return draft


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "MAX_SCREENSHOTS",
    "MAX_SCREENSHOT_BYTES",
    "ScreenshotUpload",
    "confirm_screenshot",
    "presign_screenshot",
    "remove_screenshot",
]
