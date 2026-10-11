"""What the runner on a rented machine may ask Krater, and nothing more.

The runner authenticates with its instance's token (`KRATER_JOB_TOKEN`, generated when the instance is rented;
Krater keeps only its sha256). A token works only while its instance is live and not being destroyed, so a moved
job's old machine is locked out. With it the runner can:

- `start`: say it started, and learn its command, whether this is a restart, and its timings;
- `heartbeat`: report GPU use and open sessions, and learn whether to stop;
- `exit`: report the command's exit code (the scheduler destroys the machine on its next pass);
- `checkpoints`: list, download and upload its own job's checkpoints and outputs, through presigned URLs.

Framework-free; `krater.web.routers.runner` maps it onto HTTP.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.models import ComputeEvent, ComputeInstance, ComputeJob, ComputeJobState
from krater.models.compute_job import ACTIVE_JOB_STATES
from krater.services.errors import NotAllowed, ValidationFailed
from krater.storage import ObjectStore

#: The runner's timings, sent with `start` so they can change without a new runner build.
HEARTBEAT_SECONDS = 60
CHECKPOINT_SYNC_SECONDS = 600
#: Uploads at most this big go up in one PUT; bigger ones in parts of this size. Under Cloudflare's 100 MB cap on a
#: proxied request body.
PART_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_BYTES = 500 * 1024**3
#: Presigned URLs live this long: a 64 MB part on a slow link still makes it.
URL_EXPIRES_SECONDS = 3600

AREAS = ("checkpoints", "outputs")
_NAME_RE = re.compile(r"^[A-Za-z0-9._\-][A-Za-z0-9._\-/]{0,511}$")


def new_token() -> tuple[str, str]:
    """A fresh runner token and the hash Krater stores."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate(session: Session, token: str) -> ComputeInstance:
    """The live instance `token` belongs to. Raises `NotAllowed` for anything else (unknown, destroyed, being
    destroyed, or its job finished)."""
    if not token or len(token) > 200:
        raise NotAllowed("No runner token.")
    instance = session.scalar(sa.select(ComputeInstance).where(ComputeInstance.runner_token_hash == hash_token(token)))
    if instance is None or not instance.is_live or instance.destroy_requested_at is not None:
        raise NotAllowed("This machine is no longer Krater's to run.")
    return instance


def _event(session: Session, instance: ComputeInstance, kind: str, message: str, *, now: datetime, **payload):
    session.add(
        ComputeEvent(
            job_id=instance.job_id, instance_id=instance.id, kind=kind, message=message, payload=payload, at=now
        )
    )


@dataclass(frozen=True)
class StartInfo:
    job_id: str
    attempt: int
    resumed: bool
    command: str | None
    kind: str
    heartbeat_seconds: int = HEARTBEAT_SECONDS
    checkpoint_sync_seconds: int = CHECKPOINT_SYNC_SECONDS


def start(session: Session, instance: ComputeInstance, *, now: datetime) -> StartInfo:
    """The runner started (again). A restart on the same machine (Vast restarts the container after a pause) or a
    later attempt on another one is a resume: the member's code should restore its last checkpoint."""
    job = instance.job
    if job.state not in ACTIVE_JOB_STATES:
        raise NotAllowed("This job has ended.")
    instance.runner_starts += 1
    instance.runner_started_at = now
    instance.last_heartbeat_at = now
    resumed = instance.attempt > 1 or instance.runner_starts > 1
    _event(
        session,
        instance,
        "runner_started",
        "The runner started." + (" Resuming." if resumed else ""),
        now=now,
        runner_starts=instance.runner_starts,
    )
    session.flush()
    return StartInfo(
        job_id=str(job.id), attempt=instance.attempt, resumed=resumed, command=job.command, kind=job.kind.value
    )


_HEARTBEAT_FIELDS = {"gpu_util": (int, float), "cpu_util": (int, float), "sessions": int, "phase": str}


def heartbeat(session: Session, instance: ComputeInstance, payload: dict[str, Any], *, now: datetime) -> bool:
    """Record a heartbeat. Returns whether the runner should stop (its job has ended)."""
    kept = {k: v for k, v in payload.items() if k in _HEARTBEAT_FIELDS and isinstance(v, _HEARTBEAT_FIELDS[k])}
    if isinstance(kept.get("phase"), str):
        kept["phase"] = kept["phase"][:32]
    instance.last_heartbeat_at = now
    instance.last_heartbeat = kept
    session.flush()
    return instance.job.state not in ACTIVE_JOB_STATES


def report_exit(session: Session, instance: ComputeInstance, exit_code: int, *, now: datetime) -> ComputeJob:
    """The member's command exited. Idempotent: only the first report counts. The scheduler destroys the machine."""
    job = instance.job
    if job.state in ACTIVE_JOB_STATES:
        job.exit_code = exit_code
        job.state = ComputeJobState.SUCCEEDED if exit_code == 0 else ComputeJobState.FAILED
        job.state_reason = "Finished." if exit_code == 0 else f"The command exited with code {exit_code}."
        job.ended_at = now
        job.hold_cents = 0
        _event(session, instance, "exited", job.state_reason, now=now, exit_code=exit_code)
        session.flush()
    return job


# -- Checkpoints and outputs -------------------------------------------------------------------------------


def _prefix(instance: ComputeInstance, area: str) -> str:
    if area not in AREAS:
        raise ValidationFailed({"area": f"area must be one of {', '.join(AREAS)}"})
    job = instance.job
    return f"projects/{job.project_id}/jobs/{job.id}/{area}/"


def _key(instance: ComputeInstance, area: str, name: str) -> str:
    if not _NAME_RE.match(name) or ".." in name.split("/") or "//" in name or name.endswith("/"):
        raise ValidationFailed({"name": "Use a relative path of letters, digits, '.', '_', '-' and '/'."})
    return _prefix(instance, area) + name


def list_files(store: ObjectStore, instance: ComputeInstance, area: str) -> list[dict[str, Any]]:
    prefix = _prefix(instance, area)
    return [
        {
            "name": item.key[len(prefix) :],
            "size": item.size_bytes,
            "url": store.presign_download(item.key, expires=URL_EXPIRES_SECONDS),
        }
        for item in store.list_prefix(prefix)
    ]


def begin_upload(store: ObjectStore, instance: ComputeInstance, area: str, name: str, size: int) -> dict[str, Any]:
    """One presigned PUT for a small file; for a big one, a multipart upload with a presigned URL per part."""
    key = _key(instance, area, name)
    if not 0 <= size <= MAX_UPLOAD_BYTES:
        raise ValidationFailed({"size": f"Files can be up to {MAX_UPLOAD_BYTES // 1024**3} GB."})
    if size <= PART_BYTES:
        return {"name": name, "url": store.presign_put(key, expires=URL_EXPIRES_SECONDS)}
    upload_id = store.create_multipart_upload(key)
    parts = math.ceil(size / PART_BYTES)
    return {
        "name": name,
        "upload_id": upload_id,
        "part_bytes": PART_BYTES,
        "parts": [
            store.presign_upload_part(key, upload_id=upload_id, part_number=n, expires=URL_EXPIRES_SECONDS)
            for n in range(1, parts + 1)
        ],
    }


def complete_upload(
    store: ObjectStore, instance: ComputeInstance, area: str, name: str, upload_id: str, etags: list[str]
) -> None:
    key = _key(instance, area, name)
    if not upload_id or not etags or not all(isinstance(e, str) and e for e in etags):
        raise ValidationFailed({"etags": "Send the ETag of every part, in order."})
    store.complete_multipart_upload(key, upload_id=upload_id, parts=list(enumerate(etags, start=1)))
