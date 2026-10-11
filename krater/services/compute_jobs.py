"""Submitting and cancelling Krater Compute jobs. The scheduler (`krater.compute.scheduler`) does the renting.

Members choose their own target and max prices, within their project's hourly cap. Cancelling only changes the job's
state; the scheduler destroys any machine a finished job still has on its next pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from krater.models import (
    ComputeEscalation,
    ComputeEvent,
    ComputeJob,
    ComputeJobKind,
    ComputeJobState,
    Project,
)
from krater.models.compute_job import FINISHED_JOB_STATES
from krater.services.actor import Actor
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed
from krater.services.launch_policy import LAUNCHABLE_STATUSES

MICROUSD_PER_CENT = 10_000
_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._\-/:@]*$")


@dataclass(frozen=True)
class JobSpec:
    """What a member asks for. Prices are full hourly cost (GPU plus disk), in micro-dollars."""

    name: str
    image: str
    target_microusd: int
    max_microusd: int
    command: str | None = None
    kind: ComputeJobKind = ComputeJobKind.JOB
    gpu_names: tuple[str, ...] = ()
    min_gpu_ram_gb: int | None = None
    num_gpus: int = 1
    disk_gb: int = 32
    interruptible: bool = True
    max_wait_minutes: int = 12 * 60
    on_max_wait: ComputeEscalation = ComputeEscalation.KEEP_WAITING
    max_runtime_minutes: int = 8 * 60


def _can_launch_for(actor: Actor, project: Project) -> bool:
    current = project.current_revision
    return actor.user.id == project.submitter_id or (
        current is not None and actor.user.id in current.credited_builder_ids
    )


def _validate(spec: JobSpec, *, cap_microusd: int) -> None:
    errors: dict[str, str] = {}
    if not spec.name.strip() or len(spec.name) > 200:
        errors["name"] = "Give the job a name of up to 200 characters."
    if not _IMAGE_RE.match(spec.image) or len(spec.image) > 500:
        errors["image"] = "Enter a container image, such as ghcr.io/you/train:v1."
    if spec.kind is ComputeJobKind.JOB and not (spec.command and spec.command.strip()):
        errors["command"] = "Enter the command to run."
    if spec.command is not None and len(spec.command) > 2000:
        errors["command"] = "The command can be up to 2,000 characters; put longer scripts in the image."
    if not 1 <= spec.num_gpus <= 8:
        errors["num_gpus"] = "Choose 1 to 8 GPUs."
    if not 8 <= spec.disk_gb <= 2000:
        errors["disk_gb"] = "Choose 8 to 2,000 GB of disk."
    if not spec.gpu_names and spec.min_gpu_ram_gb is None:
        errors["gpu_names"] = "Choose GPU models, or a minimum amount of GPU memory."
    if spec.min_gpu_ram_gb is not None and not 1 <= spec.min_gpu_ram_gb <= 256:
        errors["min_gpu_ram_gb"] = "Choose 1 to 256 GB of GPU memory."
    if spec.target_microusd <= 0:
        errors["target"] = "Enter a target price above zero."
    elif spec.target_microusd > spec.max_microusd:
        errors["target"] = "The target can't be above the max price."
    if spec.max_microusd > cap_microusd:
        errors["max"] = f"The project's hourly cap is ${cap_microusd / 1_000_000:.2f}/h."
    if not 1 <= spec.max_wait_minutes <= 7 * 24 * 60:
        errors["max_wait_minutes"] = "Choose a max wait of 1 minute to 7 days."
    if not 1 <= spec.max_runtime_minutes <= 7 * 24 * 60:
        errors["max_runtime_minutes"] = "Choose a max runtime of 1 minute to 7 days."
    if errors:
        raise ValidationFailed(errors)


def submit_job(
    session: Session, actor: Actor, *, project: Project, spec: JobSpec, cap_cents: int, now: datetime
) -> ComputeJob:
    """Queue `spec` for `project`. Only the project's submitter and credited builders may, and only while the project
    can launch. `cap_cents` is the project's hourly cap (`launch_policy.hourly_cap_cents`)."""
    if not _can_launch_for(actor, project):
        raise NotAllowed("Only the project's team can run jobs on its budget.")
    if project.status not in LAUNCHABLE_STATUSES:
        raise InvalidState(f"A {project.status.value} project can't run jobs.")
    _validate(spec, cap_microusd=cap_cents * MICROUSD_PER_CENT)
    job = ComputeJob(
        project_id=project.id,
        submitter_id=actor.user.id,
        kind=spec.kind,
        name=spec.name.strip(),
        image=spec.image,
        command=spec.command,
        gpu_names=list(spec.gpu_names),
        min_gpu_ram_gb=spec.min_gpu_ram_gb,
        num_gpus=spec.num_gpus,
        disk_gb=spec.disk_gb,
        target_microusd=spec.target_microusd,
        max_microusd=spec.max_microusd,
        interruptible=spec.interruptible,
        max_wait_minutes=spec.max_wait_minutes,
        on_max_wait=spec.on_max_wait,
        max_runtime_minutes=spec.max_runtime_minutes,
        state=ComputeJobState.QUEUED,
        state_reason="Waiting for an offer at or under its target price.",
        queued_at=now,
    )
    session.add(job)
    session.flush()
    session.add(ComputeEvent(job_id=job.id, kind="queued", message="Queued.", payload={}, at=now))
    session.flush()
    return job


def cancel_job(session: Session, actor: Actor, *, job: ComputeJob, now: datetime) -> ComputeJob:
    """Cancel `job`, by its team or an admin. Its machine, if any, is destroyed on the scheduler's next pass."""
    if not (actor.is_admin or _can_launch_for(actor, job.project)):
        raise NotAllowed("Only the project's team or an admin can cancel this job.")
    if job.state in FINISHED_JOB_STATES:
        raise InvalidState(f"The job is already {job.state.value}.")
    job.state = ComputeJobState.CANCELLED
    job.state_reason = f"Cancelled by {actor.user.display_name}."
    job.ended_at = now
    job.hold_cents = 0
    session.add(
        ComputeEvent(
            job_id=job.id, kind="cancelled", message=job.state_reason, payload={"by": str(actor.user.id)}, at=now
        )
    )
    session.flush()
    return job
