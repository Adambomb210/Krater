"""The Krater Compute scheduler: one pass a minute, from the worker. It is the only code that rents or destroys.

Each pass, in order:

1. **List** every instance on the Vast account once.
2. **Settle lost creates.** A create whose response was lost (a timeout) may still have rented a machine. Its
   instance row was committed *before* the call, with a unique label, so the listing finds it by label. Not found
   after `CREATE_SETTLE`, it's marked failed and the job goes back to the queue.
3. **Sweep.** Any instance carrying Krater's label prefix that Krater has no live row for is destroyed. That covers
   a create that appeared too late, a destroy that didn't take, and a member restarting their own machine with the
   container's key.
4. **Watch** each live instance: pulling -> running, running -> paused (outbid), and the pause policy (wait the
   grace, raise the bid in steps up to the job's max, then move the job to another machine). It enforces the
   max runtime and destroys the instances of finished jobs.
5. **Start** queued jobs, oldest first: search offers for the job's shape, choose the cheapest at or under its
   target (or its max, after `max_wait_minutes`, as the member chose), hold its budget under the project's row
   lock, then rent.

Every destroy is confirmed with `show_instance(...) is None`; until then the row keeps `destroy_requested_at` and
the next pass tries again. The pass commits as it goes (a rental's row must be durable before Vast is called), so
call it with a session of its own. The worker runs it under a procrastinate lock, so passes never overlap.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from krater.compute import launch, offers, runner_api, spend
from krater.config import Settings
from krater.models import (
    ComputeEscalation,
    ComputeEvent,
    ComputeInstance,
    ComputeInstanceState,
    ComputeJob,
    ComputeJobKind,
    ComputeJobState,
    Project,
)
from krater.models.compute_job import ACTIVE_JOB_STATES, FINISHED_JOB_STATES
from krater.services import budget, budget_alerts, compute_settings
from krater.services.compute_settings import ComputePolicy
from krater.services.launch_rules import LAUNCHABLE_STATUSES, hourly_cap_cents
from krater.vast import (
    Instance,
    Offer,
    OfferQuery,
    RentalType,
    VastClient,
    VastError,
    VastRedirectError,
    VastRequestFailedError,
    VastUnavailableError,
    microusd_to_usd,
    usd_to_microusd,
)

logger = logging.getLogger(__name__)

#: How long a create with no Vast id may stay unresolved before Krater decides it never happened.
CREATE_SETTLE = timedelta(minutes=5)
#: How long a machine may take from create to running (the image pull bills disk) before Krater gives up on it.
PROVISION_TIMEOUT = timedelta(minutes=30)
MICROUSD_PER_CENT = 10_000
#: A box is idle while its GPU use stays under this (percent), and its CPU use, when reported, under that.
IDLE_GPU_PERCENT = 5
IDLE_CPU_PERCENT = 10
#: The warning before a stopped box is destroyed.
DESTROY_WARNING = timedelta(hours=1)
#: `destroy_reason` of a box the member asked to start again elsewhere.
MOVED_BY_MEMBER = "moved by member"


@dataclass
class PassResult:
    rented: list[str] = field(default_factory=list)  # labels
    destroyed: list[int] = field(default_factory=list)  # Vast ids
    swept: list[int] = field(default_factory=list)  # Vast ids destroyed without a Krater record
    errors: int = 0


def _event(session: Session, job: ComputeJob, kind: str, message: str, *, now: datetime, instance=None, **payload):
    session.add(
        ComputeEvent(
            job_id=job.id,
            instance_id=instance.id if instance is not None else None,
            kind=kind,
            message=message,
            payload=payload,
            at=now,
        )
    )


def _dollars(microusd: int) -> str:
    return f"${microusd_to_usd(microusd).quantize(Decimal('0.0001'))}/h"


def _requeue(session: Session, job: ComputeJob, reason: str, *, now: datetime) -> None:
    job.state = ComputeJobState.QUEUED
    job.state_reason = reason
    job.queued_at = now
    job.hold_cents = 0


def _finish(job: ComputeJob, state: ComputeJobState, reason: str, *, now: datetime) -> None:
    job.state = state
    job.state_reason = reason
    job.ended_at = now
    job.hold_cents = 0


class Scheduler:
    def __init__(
        self, session: Session, client: VastClient, settings: Settings, *, now: datetime, runner_sha256: str | None
    ) -> None:
        self.session = session
        self.client = client
        self.settings = settings
        self.now = now
        # `None` when the runner binary isn't there to serve: nothing new is rented (it couldn't start).
        self.runner_sha256 = runner_sha256
        self.runner_url = settings.compute_runner_url or settings.base_url
        self.prefix = settings.compute_label_prefix
        self.result = PassResult()
        self._policies: dict = {}
        self._ssh_keys_on_account: bool | None = None

    # -- Entry point -----------------------------------------------------------------------------------

    def run(self) -> PassResult:
        # Spend first, at the states instances were in since the last pass, then stop anything over budget.
        changed = spend.accrue(self.session, now=self.now)
        spend.record_snapshots(self.session, changed, now=self.now)
        self.session.commit()
        self._enforce_budgets()

        listing = {i.id: i for i in self.client.list_instances(label_prefix=self.prefix)}
        by_label = {i.label: i for i in listing.values() if i.label}
        self._settle_creates(by_label)
        self._sweep(listing)
        self._watch(listing)
        if self.runner_sha256 is None:
            logger.error(
                "krater.compute: no runner binary at %s; not renting anything", self.settings.compute_runner_binary_path
            )
        else:
            self._start_queued()
        self.session.commit()
        return self.result

    def _policy(self, project: Project) -> ComputePolicy:
        if project.id not in self._policies:
            self._policies[project.id] = compute_settings.effective_policy(self.session, project)
        return self._policies[project.id]

    # -- 1. Budgets ------------------------------------------------------------------------------------

    def _enforce_budgets(self) -> None:
        """Warn at the warn percentage; at the ceiling, projected one minute ahead, stop every machine the project has.
        Runs every pass, so a project that stays over budget can't start new work either (no budget to hold)."""
        stmt = (
            sa.select(Project)
            .join(ComputeJob, ComputeJob.project_id == Project.id)
            .where(ComputeJob.state.in_(ACTIVE_JOB_STATES))
            .distinct()
        )
        for project in list(self.session.scalars(stmt)):
            ceiling = budget.ceiling_cents(self.session, project)
            spent = budget.latest_spend_cents(self.session, project)
            budget_alerts.warn_if_due(
                self.session,
                project,
                ceiling_cents=ceiling,
                spend_cents=spent,
                warn_percent=self.settings.budget_warn_percent,
            )
            projected = spent + spend.next_minute_cents(self.session, project)
            if budget_alerts.budget_percent(ceiling, projected) >= 100.0:
                self._stop_project(project, ceiling_cents=ceiling, spend_cents=spent)
            self.session.commit()

    def _stop_project(self, project: Project, *, ceiling_cents: int, spend_cents: int) -> None:
        jobs = self.session.scalars(
            sa.select(ComputeJob).where(ComputeJob.project_id == project.id, ComputeJob.state.in_(ACTIVE_JOB_STATES))
        )
        stopped: list[int] = []
        for job in list(jobs):
            _finish(job, ComputeJobState.FAILED, "Stopped: the project reached its budget.", now=self.now)
            _event(self.session, job, "budget_stop", "Stopped: the project reached its budget.", now=self.now)
            for row in job.instances:
                if row.is_live and row.vast_instance_id is not None:
                    try:
                        self._destroy(row, "budget reached")
                    except VastError:
                        # The job is finished, so the next pass's watch retries the destroy.
                        logger.exception("krater.compute: budget stop of instance %s failed", row.vast_instance_id)
                        self.result.errors += 1
                    stopped.append(row.vast_instance_id)
        budget_alerts.record_teardown(
            self.session,
            project,
            ceiling_cents=ceiling_cents,
            spend_cents=spend_cents,
            details={"vast_instance_ids": stopped},
        )

    # -- 2. Lost creates -------------------------------------------------------------------------------

    def _settle_creates(self, by_label: dict[str, Instance]) -> None:
        stmt = sa.select(ComputeInstance).where(ComputeInstance.state == ComputeInstanceState.CREATING)
        for row in self.session.scalars(stmt):
            found = by_label.get(row.label)
            job = row.job
            if found is not None:
                row.vast_instance_id = found.id
                row.state = ComputeInstanceState.PROVISIONING
                row.machine_id = found.machine_id
                _event(
                    self.session,
                    job,
                    "rented",
                    f"Found instance {found.id} after an unconfirmed create.",
                    now=self.now,
                    instance=row,
                    vast_instance_id=found.id,
                )
            elif self.now - row.created_at >= CREATE_SETTLE:
                row.state = ComputeInstanceState.CREATE_FAILED
                _event(
                    self.session,
                    job,
                    "create_failed",
                    "The machine never appeared on Vast.",
                    now=self.now,
                    instance=row,
                )
                if job.state is ComputeJobState.STARTING:
                    _requeue(
                        self.session, job, "The last machine never started; waiting for another offer.", now=self.now
                    )
        self.session.commit()

    # -- 3. Sweep --------------------------------------------------------------------------------------

    def _sweep(self, listing: dict[int, Instance]) -> None:
        live_labels = set(
            self.session.scalars(
                sa.select(ComputeInstance.label).where(
                    ComputeInstance.state.not_in([ComputeInstanceState.DESTROYED, ComputeInstanceState.CREATE_FAILED])
                )
            )
        )
        for instance in listing.values():
            if instance.label in live_labels:
                continue
            logger.warning(
                "krater.compute: destroying instance %s (%s): Krater has no live record of it",
                instance.id,
                instance.label,
            )
            try:
                if self._destroy_and_confirm(instance.id):
                    self.result.swept.append(instance.id)
                    row = self.session.scalar(sa.select(ComputeInstance).where(ComputeInstance.label == instance.label))
                    if row is not None:
                        _event(
                            self.session,
                            row.job,
                            "swept",
                            f"Destroyed instance {instance.id}, which Krater no longer expected.",
                            now=self.now,
                            instance=row,
                            vast_instance_id=instance.id,
                        )
            except VastError:
                logger.exception("krater.compute: sweeping instance %s failed", instance.id)
                self.result.errors += 1
        self.session.commit()

    def _destroy_and_confirm(self, vast_instance_id: int) -> bool:
        """Destroy and check it's gone. `True` once Vast no longer shows it."""
        self.client.destroy_instance(vast_instance_id)
        return self.client.show_instance(vast_instance_id) is None

    # -- 4. Watch --------------------------------------------------------------------------------------

    def _watch(self, listing: dict[int, Instance]) -> None:
        stmt = sa.select(ComputeInstance).where(
            ComputeInstance.state.in_(
                [
                    ComputeInstanceState.PROVISIONING,
                    ComputeInstanceState.RUNNING,
                    ComputeInstanceState.PAUSED,
                    ComputeInstanceState.STOPPED,
                ]
            ),
            ComputeInstance.vast_instance_id.is_not(None),
        )
        for row in list(self.session.scalars(stmt)):
            try:
                self._watch_one(row, listing.get(row.vast_instance_id))
            except VastError:
                logger.exception("krater.compute: watching instance %s failed", row.vast_instance_id)
                self.result.errors += 1
            self.session.commit()

    def _destroy(self, row: ComputeInstance, reason: str) -> bool:
        if row.destroy_requested_at is None:
            row.destroy_requested_at = self.now
            row.destroy_reason = reason
        assert row.vast_instance_id is not None
        if self._destroy_and_confirm(row.vast_instance_id):
            row.state = ComputeInstanceState.DESTROYED
            row.destroyed_at = self.now
            self.result.destroyed.append(row.vast_instance_id)
            _event(
                self.session,
                row.job,
                "destroyed",
                f"Destroyed instance {row.vast_instance_id}: {reason}",
                now=self.now,
                instance=row,
                vast_instance_id=row.vast_instance_id,
                reason=reason,
            )
            return True
        _event(
            self.session,
            row.job,
            "destroy_pending",
            f"Instance {row.vast_instance_id} is still listed after destroy; retrying next pass.",
            now=self.now,
            instance=row,
        )
        return False

    def _watch_one(self, row: ComputeInstance, seen: Instance | None) -> None:
        job = row.job
        if seen is None:
            seen = self.client.show_instance(row.vast_instance_id)  # the list may lag; ask directly
        if seen is None:
            row.state = ComputeInstanceState.DESTROYED
            row.destroyed_at = self.now
            if row.destroy_requested_at is None and job.is_active:
                _event(
                    self.session,
                    job,
                    "lost",
                    f"Instance {row.vast_instance_id} disappeared from Vast.",
                    now=self.now,
                    instance=row,
                )
                _requeue(self.session, job, "The machine disappeared; waiting for another offer.", now=self.now)
            return

        previously_seen = row.last_seen_at
        row.last_seen_at = self.now
        row.last_vast_status = seen.actual_status
        self._record_ssh(row, seen)

        if row.destroy_requested_at is not None:
            moving = job.state is ComputeJobState.PAUSED or row.destroy_reason == MOVED_BY_MEMBER
            if self._destroy(row, row.destroy_reason or "requested") and moving:
                self._requeue_moved(job)
            return
        if job.state in FINISHED_JOB_STATES:
            self._destroy(row, f"job {job.state.value}")
            return
        if job.project.status not in LAUNCHABLE_STATUSES:
            # Withdrawn or completed: its remaining budget may already be reclaimed, so stop billing now.
            _finish(job, ComputeJobState.CANCELLED, f"The project is {job.project.status.value}.", now=self.now)
            _event(
                self.session,
                job,
                "cancelled",
                "Cancelled: the project can't launch any more.",
                now=self.now,
                instance=row,
            )
            self._destroy(row, f"project {job.project.status.value}")
            return

        policy = self._policy(job.project)
        if row.state is ComputeInstanceState.STOPPED:
            self._watch_stopped(row, job, seen, policy, previously_seen=previously_seen)
            return
        if (
            row.state is ComputeInstanceState.PROVISIONING
            and not seen.is_running
            and self.now - row.created_at >= PROVISION_TIMEOUT
        ):
            # Finished first: if Vast doesn't confirm the destroy, the next pass retries it for the finished job.
            _finish(
                job,
                ComputeJobState.FAILED,
                "The machine didn't start within 30 minutes. Check that the image exists and can be pulled.",
                now=self.now,
            )
            self._destroy(row, "never started")
            return
        if seen.is_running:
            if row.state is ComputeInstanceState.PROVISIONING:
                row.state = ComputeInstanceState.RUNNING
                row.running_since = self.now
                row.entered_running_at = self.now
                job.state = ComputeJobState.RUNNING
                job.state_reason = None
                job.started_at = job.started_at or self.now
                _event(self.session, job, "running", "The machine is running.", now=self.now, instance=row)
            elif row.state is ComputeInstanceState.PAUSED:
                row.state = ComputeInstanceState.RUNNING
                row.paused_since = None
                row.entered_running_at = self.now  # Vast restarted the container: the runner starts again
                job.state = ComputeJobState.RUNNING
                job.state_reason = None
                _event(self.session, job, "resumed", "The machine is running again.", now=self.now, instance=row)
            # Time actually running (paused and stopped time don't count), added up by the spend accrual.
            over_runtime = row.run_seconds >= job.max_runtime_minutes * 60
            if over_runtime and job.kind is ComputeJobKind.BOX:
                # Never destroy a box's disk on its own: stop it, and the member can resume it for another run.
                if row.stop_requested_at is None:
                    self.request_stop(row, job, f"max runtime of {job.max_runtime_minutes} minutes reached")
                self._finish_stopping(row, job, policy)
                return
            if over_runtime:
                _finish(
                    job,
                    ComputeJobState.FAILED,
                    f"Stopped at its max runtime of {job.max_runtime_minutes} minutes.",
                    now=self.now,
                )
                self._destroy(row, "max runtime reached")
                return
            if job.kind is ComputeJobKind.BOX and row.stop_requested_at is not None:
                self._finish_stopping(row, job, policy)
                return
            self._check_runner(row, job, policy)
            if job.kind is ComputeJobKind.BOX and job.state is ComputeJobState.RUNNING:
                self._check_idle(row, job, policy)
            return

        if row.state is ComputeInstanceState.RUNNING:
            # It was running and isn't now, without Krater stopping it: outbid (or the host went away).
            row.state = ComputeInstanceState.PAUSED
            row.paused_since = self.now
            # A pause kills the container's processes and Vast restarts it on resume: the runner must start again.
            row.runner_starts_needed = row.runner_starts + 1
            job.state = ComputeJobState.PAUSED
            job.state_reason = "Outbid: the machine is paused."
            _event(
                self.session,
                job,
                "paused",
                "The machine was paused (outbid).",
                now=self.now,
                instance=row,
                vast_status=seen.actual_status,
            )
            return
        if row.state is ComputeInstanceState.PAUSED and row.paused_since is not None:
            paused_for = self.now - row.paused_since
            if paused_for >= timedelta(minutes=policy.move_after_minutes):
                if self._destroy(row, f"paused for {policy.move_after_minutes} minutes"):
                    self._requeue_moved(job)
            elif paused_for >= timedelta(minutes=policy.pause_grace_minutes) and row.interruptible:
                self._raise_bid(row, job, policy)

    def _check_runner(self, row: ComputeInstance, job: ComputeJob, policy: ComputePolicy) -> None:
        """A running machine's runner must start within the start deadline of the container (re)starting, and keep
        sending heartbeats. Otherwise the machine is billing for nothing Krater can see or stop cleanly."""
        since = row.entered_running_at or row.running_since
        if since is None:
            return
        if row.runner_starts < row.runner_starts_needed:
            if self.now - since >= timedelta(minutes=policy.runner_start_minutes):
                if job.kind is ComputeJobKind.BOX and row.runner_starts > 0:
                    # It ran before: its disk may hold work newer than the last sync. Stop it, don't destroy it.
                    self._stop_box(row, job, "the runner didn't start again")
                    return
                _finish(
                    job,
                    ComputeJobState.FAILED,
                    "Krater's runner never started on the machine. The image needs a POSIX shell and one of curl, "
                    "wget or Python.",
                    now=self.now,
                )
                self._destroy(row, "runner never started")
            return
        last = row.last_heartbeat_at or row.runner_started_at
        if self.now - last >= timedelta(minutes=policy.heartbeat_timeout_minutes):
            if job.kind is ComputeJobKind.BOX:
                self._stop_box(row, job, "the runner stopped reporting")
                return
            _finish(
                job,
                ComputeJobState.FAILED,
                f"The runner stopped reporting for {policy.heartbeat_timeout_minutes} minutes.",
                now=self.now,
            )
            self._destroy(row, "runner stopped reporting")

    # -- Boxes -----------------------------------------------------------------------------------------

    def _record_ssh(self, row: ComputeInstance, seen: Instance) -> None:
        """Where the member's SSH client should connect: the direct port when Vast mapped one, else Vast's proxy."""
        mapped = (seen.ports or {}).get("22/tcp") or []
        if seen.public_ipaddr and mapped and isinstance(mapped[0], dict) and mapped[0].get("HostPort"):
            row.ssh_host, row.ssh_port = seen.public_ipaddr, int(mapped[0]["HostPort"])
        elif seen.ssh_host and seen.ssh_port:
            row.ssh_host, row.ssh_port = seen.ssh_host, seen.ssh_port

    def _check_idle(self, row: ComputeInstance, job: ComputeJob, policy: ComputePolicy) -> None:
        """A box is idle while its runner reports low GPU (and CPU) use and no open session. Idle for the box idle
        time, Krater starts stopping it."""
        beat = row.last_heartbeat or {}
        if "gpu_util" not in beat and "sessions" not in beat:
            return  # nothing to judge by; the heartbeat timeout covers a silent runner
        idle = beat.get("gpu_util", 0) < IDLE_GPU_PERCENT and beat.get("sessions", 0) == 0
        if "cpu_util" in beat:
            idle = idle and beat["cpu_util"] < IDLE_CPU_PERCENT
        if not idle:
            row.idle_since = None
            return
        if row.idle_since is None:
            row.idle_since = self.now
        elif self.now - row.idle_since >= timedelta(minutes=policy.box_idle_minutes):
            self.request_stop(row, job, f"idle for {policy.box_idle_minutes} minutes")

    def request_stop(self, row: ComputeInstance, job: ComputeJob, reason: str) -> None:
        """Step one of stopping a box: the next heartbeat asks the runner to save /workspace."""
        if row.stop_requested_at is not None:
            return
        row.stop_requested_at = self.now
        row.stop_reason = reason
        job.state_reason = f"Stopping ({reason}): saving /workspace first."
        _event(self.session, job, "stop_requested", f"Stopping the box: {reason}.", now=self.now, instance=row)

    def _finish_stopping(self, row: ComputeInstance, job: ComputeJob, policy: ComputePolicy) -> None:
        """Step two: stop once the runner confirmed the sync, or the sync deadline passed."""
        synced = row.last_synced_at is not None and row.last_synced_at >= row.stop_requested_at
        if synced or self.now - row.stop_requested_at >= timedelta(minutes=policy.box_sync_deadline_minutes):
            self._stop_box(row, job, row.stop_reason or "requested", synced=synced)

    def _stop_box(self, row: ComputeInstance, job: ComputeJob, reason: str, *, synced: bool = False) -> None:
        self.client.stop_instance(row.vast_instance_id)
        row.state = ComputeInstanceState.STOPPED
        row.stopped_at = self.now
        row.stop_requested_at = None
        row.start_requested_at = None
        row.idle_since = None
        # Vast restarts the container on resume: the runner must start again.
        row.runner_starts_needed = row.runner_starts + 1
        job.state = ComputeJobState.STOPPED
        saved = "saved" if synced else "not confirmed saved"
        job.state_reason = f"Stopped ({reason}); /workspace {saved}. Resume it to carry on."
        _event(self.session, job, "stopped", f"Stopped the box: {reason}.", now=self.now, instance=row, synced=synced)

    def _watch_stopped(
        self,
        row: ComputeInstance,
        job: ComputeJob,
        seen: Instance,
        policy: ComputePolicy,
        *,
        previously_seen: datetime | None,
    ) -> None:
        if row.start_requested_at is not None:
            if seen.is_running:
                row.state = ComputeInstanceState.RUNNING
                row.entered_running_at = self.now
                row.run_seconds = 0  # a resumed box starts a new run
                row.start_requested_at = None
                row.stopped_at = None
                job.state = ComputeJobState.RUNNING
                job.state_reason = None
                _event(self.session, job, "resumed", "The box is running again.", now=self.now, instance=row)
            elif self.now - row.start_requested_at >= timedelta(minutes=policy.box_resume_timeout_minutes):
                # Vast keeps trying while the instance's intended state is running; take that back.
                self.client.stop_instance(row.vast_instance_id)
                row.start_requested_at = None
                synced = row.last_synced_at.strftime("%Y-%m-%d %H:%M UTC") if row.last_synced_at else "never"
                job.state_reason = (
                    "Couldn't resume: the machine's GPU is taken. Start it elsewhere from the workspace saved at "
                    f"{synced}, or try again later."
                )
                _event(self.session, job, "resume_blocked", job.state_reason, now=self.now, instance=row)
            else:
                self.client.start_instance(row.vast_instance_id)
            return
        if seen.is_running and "running" in (seen.intended_status, seen.cur_state):
            # Running and meant to be, without Krater starting it: a member can start their own instance with the
            # container's key. (Right after Krater's own stop, Vast still reports `running` for a while, but its
            # intended state is already `stopped`: that's lag, not a restart.)
            # Bill it at the full rate for the time since Krater last looked, and stop it again.
            if previously_seen is not None:
                extra = round(row.gpu_microusd * (self.now - previously_seen).total_seconds() / 3600)
                row.accrued_microusd += extra
            self.client.stop_instance(row.vast_instance_id)
            _event(
                self.session,
                job,
                "unexpected_start",
                "The stopped box was started outside Krater; stopped it again.",
                now=self.now,
                instance=row,
            )
            return
        stopped_for = self.now - (row.stopped_at or self.now)
        destroy_after = timedelta(hours=policy.box_destroy_hours)
        if stopped_for >= destroy_after:
            _finish(
                job,
                ComputeJobState.CANCELLED,
                f"Destroyed after {policy.box_destroy_hours} hours stopped. Its last saved workspace is kept.",
                now=self.now,
            )
            self._destroy(row, "stopped too long")
        elif stopped_for >= destroy_after - DESTROY_WARNING and not self._has_event(job, "destroy_soon"):
            _event(
                self.session,
                job,
                "destroy_soon",
                "This stopped box will be destroyed within the hour unless it's resumed.",
                now=self.now,
                instance=row,
            )

    def _has_event(self, job: ComputeJob, kind: str) -> bool:
        return (
            self.session.scalar(
                sa.select(sa.func.count())
                .select_from(ComputeEvent)
                .where(ComputeEvent.job_id == job.id, ComputeEvent.kind == kind)
            )
            > 0
        )

    def _account_has_ssh_keys(self) -> bool:
        """Checked once per pass: any SSH key on the Vast account may be installed in every instance, which would let
        its holder into every member's box. Krater refuses to start boxes while there are any."""
        if self._ssh_keys_on_account is None:
            try:
                self._ssh_keys_on_account = bool(self.client.list_ssh_keys())
            except VastError:
                # Can't tell: refuse boxes this pass, but keep starting jobs.
                logger.warning("krater.compute: could not check the Vast account's SSH keys; no boxes this pass")
                self._ssh_keys_on_account = True
        return self._ssh_keys_on_account

    def _requeue_moved(self, job: ComputeJob) -> None:
        _requeue(
            self.session,
            job,
            "Moved: paused too long. Waiting for another offer; it resumes from its last checkpoint.",
            now=self.now,
        )

    def _raise_bid(self, row: ComputeInstance, job: ComputeJob, policy: ComputePolicy) -> None:
        max_gpu = job.max_microusd - row.disk_microusd
        if row.gpu_microusd >= max_gpu:
            return
        step = max(1, row.gpu_microusd * max(policy.bid_margin_percent, 1) // 100)
        new_gpu = min(row.gpu_microusd + step, max_gpu)
        new_bid = microusd_to_usd(new_gpu).quantize(offers.BID_QUANTUM, rounding=ROUND_FLOOR)
        self.client.change_bid(row.vast_instance_id, new_bid)
        _event(
            self.session,
            job,
            "bid_raised",
            f"Raised the bid to ${new_bid}/h.",
            now=self.now,
            instance=row,
            old_gpu_microusd=row.gpu_microusd,
            new_gpu_microusd=usd_to_microusd(new_bid),
        )
        row.gpu_microusd = usd_to_microusd(new_bid)

    # -- 5. Start queued jobs --------------------------------------------------------------------------

    def _start_queued(self) -> None:
        stmt = sa.select(ComputeJob).where(ComputeJob.state == ComputeJobState.QUEUED).order_by(ComputeJob.queued_at)
        searches: dict[tuple, list[Offer]] = {}
        taken: set[int] = set()
        for job in list(self.session.scalars(stmt)):
            try:
                stop = self._start_one(job, searches, taken)
            except VastError:
                logger.exception("krater.compute: starting job %s failed", job.id)
                self.result.errors += 1
                stop = True
            self.session.commit()
            if stop:
                return

    def _plan(self, job: ComputeJob) -> tuple[RentalType, int] | None:
        """(rental type, price limit) for this pass, after any escalation. `None` cancels the job."""
        rental_type: RentalType = "bid" if job.interruptible else "ondemand"
        limit = job.target_microusd
        if self.now - job.queued_at >= timedelta(minutes=job.max_wait_minutes):
            match job.on_max_wait:
                case ComputeEscalation.CANCEL:
                    return None
                case ComputeEscalation.RAISE_BID:
                    limit = job.max_microusd
                case ComputeEscalation.ON_DEMAND:
                    rental_type, limit = "ondemand", job.max_microusd
                case ComputeEscalation.KEEP_WAITING:
                    pass
        return rental_type, limit

    def _start_one(self, job: ComputeJob, searches: dict, taken: set[int]) -> bool:
        """Try to rent for `job`. Returns `True` when the pass should stop starting jobs (Vast unreachable)."""
        project = job.project
        if project.status not in LAUNCHABLE_STATUSES:
            _finish(job, ComputeJobState.CANCELLED, f"The project is {project.status.value}.", now=self.now)
            _event(self.session, job, "cancelled", "Cancelled: the project can't launch any more.", now=self.now)
            return False
        plan = self._plan(job)
        if plan is None:
            _finish(
                job,
                ComputeJobState.CANCELLED,
                f"No offer met the target within {job.max_wait_minutes} minutes.",
                now=self.now,
            )
            _event(self.session, job, "cancelled", "Cancelled after its max wait, as chosen.", now=self.now)
            return False
        rental_type, limit = plan
        cap_microusd = hourly_cap_cents(project, self.settings) * MICROUSD_PER_CENT
        limit = min(limit, cap_microusd)
        policy = self._policy(project)

        query = OfferQuery(
            rental_type=rental_type,
            gpu_names=tuple(job.gpu_names),
            min_gpu_ram_gb=job.min_gpu_ram_gb,
            num_gpus=job.num_gpus,
            disk_gb=job.disk_gb,
            min_reliability=policy.reliability_floor,
            min_direct_ports=1 if job.kind is ComputeJobKind.BOX else 0,
        )
        key = json.dumps(query.to_body(), sort_keys=True)
        if key not in searches:
            searches[key] = self.client.search_offers(query)
        found = searches[key]
        choice = offers.choose(
            found,
            rental_type=rental_type,
            disk_gb=job.disk_gb,
            margin_percent=policy.bid_margin_percent,
            limit_microusd=limit,
            max_microusd=min(job.max_microusd, cap_microusd),
            several_models=len(job.gpu_names) != 1,
            exclude=taken,
        )
        if choice is None:
            low = offers.cheapest(
                [o for o in found if o.id not in taken],
                rental_type=rental_type,
                disk_gb=job.disk_gb,
                margin_percent=policy.bid_margin_percent,
            )
            kind = "bid" if rental_type == "bid" else "on-demand"
            job.state_reason = f"Waiting for a {kind} offer at or under {_dollars(limit)}" + (
                f" (cheapest now {_dollars(low)})." if low is not None else " (no matching offers now)."
            )
            return False

        # Budget: under the project's row lock, so two jobs can't hold the same dollars.
        self.session.execute(sa.select(Project.id).where(Project.id == project.id).with_for_update())
        available = budget.remaining_cents(self.session, project) - self._held_cents(project, exclude=job)
        hour_cents = math.ceil(choice.full_microusd / MICROUSD_PER_CENT)
        if available < hour_cents:
            job.state_reason = (
                f"Waiting for budget: one hour at {_dollars(choice.full_microusd)} needs ${hour_cents / 100:.2f}, "
                f"${max(available, 0) / 100:.2f} is free."
            )
            return False
        run_cents = math.ceil(choice.full_microusd * job.max_runtime_minutes / 60 / MICROUSD_PER_CENT)
        return self._rent(job, choice, hold_cents=min(run_cents, available), taken=taken)

    def _held_cents(self, project: Project, *, exclude: ComputeJob) -> int:
        """Budget other active jobs of `project` still expect to spend."""
        rows = self.session.execute(
            sa.select(ComputeJob.hold_cents, ComputeJob.spend_cents).where(
                ComputeJob.project_id == project.id,
                ComputeJob.state.in_(ACTIVE_JOB_STATES),
                ComputeJob.id != exclude.id,
            )
        )
        return sum(max(0, hold - spent) for hold, spent in rows)

    def _rent(self, job: ComputeJob, choice: offers.Choice, *, hold_cents: int, taken: set[int]) -> bool:
        if job.kind is ComputeJobKind.BOX and self._account_has_ssh_keys():
            job.state_reason = (
                "Krater doesn't start boxes while the Vast account has SSH keys: they may reach every machine. "
                "An admin must remove them."
            )
            return False
        attempt = job.attempts + 1
        token, token_hash = runner_api.new_token()
        label = f"{self.prefix}{job.id.hex[:12]}-{attempt}"
        row = ComputeInstance(
            job_id=job.id,
            attempt=attempt,
            label=label,
            state=ComputeInstanceState.CREATING,
            offer_id=choice.offer.id,
            machine_id=choice.offer.machine_id,
            gpu_name=choice.offer.gpu_name,
            interruptible=choice.rental_type == "bid",
            gpu_microusd=choice.gpu_microusd,
            disk_microusd=choice.disk_microusd,
            runner_token_hash=token_hash,
            # The pass's clock, not the database's: the settle and provision timeouts compare with it.
            created_at=self.now,
        )
        self.session.add(row)
        job.attempts = attempt
        job.state = ComputeJobState.STARTING
        job.state_reason = f"Renting a {choice.offer.gpu_name} at {_dollars(choice.full_microusd)}."
        job.hold_cents = hold_cents
        taken.add(choice.offer.id)
        _event(
            self.session,
            job,
            "renting",
            job.state_reason,
            now=self.now,
            instance=row,
            offer_id=choice.offer.id,
            full_microusd=choice.full_microusd,
            bid=str(choice.bid) if choice.bid is not None else None,
        )
        # Durable before Vast is called: if the response is lost, the next pass finds the machine by this label.
        self.session.commit()

        request = launch.create_request(
            job,
            label=label,
            bid_price=choice.bid,
            env=launch.job_env(job, attempt, runner_url=self.runner_url, token=token),
            runner_sha256=self.runner_sha256,
        )
        try:
            vast_id = self.client.create_instance(choice.offer.id, request)
        except VastUnavailableError:
            logger.warning("krater.compute: create for %s unconfirmed; the next pass settles it by label", label)
            _event(
                self.session,
                job,
                "create_unconfirmed",
                "Vast didn't confirm the rental; checking next pass.",
                now=self.now,
                instance=row,
            )
            return True
        except VastRequestFailedError as exc:
            if (
                exc.status_code is not None
                and 200 <= exc.status_code < 300
                and not isinstance(exc, VastRedirectError)
            ):
                # A 200 that says `success: false`: Vast has rented the machine anyway (staging, 2026-10-11), so
                # treat it like a lost answer and let the next pass find the machine by its label.
                logger.warning("krater.compute: create for %s answered 200 without success; settling by label", label)
                _event(
                    self.session,
                    job,
                    "create_unconfirmed",
                    f"Vast didn't confirm the rental ({exc}); checking next pass.",
                    now=self.now,
                    instance=row,
                    error=exc.error,
                )
                return True
            row.state = ComputeInstanceState.CREATE_FAILED
            queued_at = job.queued_at
            _requeue(self.session, job, "The offer was taken before Krater could rent it; trying again.", now=self.now)
            job.queued_at = queued_at  # keep its place in the queue and its wait: it never ran
            _event(
                self.session,
                job,
                "create_refused",
                f"Vast refused the rental: {exc}",
                now=self.now,
                instance=row,
                error=exc.error,
            )
            return False
        row.vast_instance_id = vast_id
        row.state = ComputeInstanceState.PROVISIONING
        _event(
            self.session,
            job,
            "rented",
            f"Rented instance {vast_id}.",
            now=self.now,
            instance=row,
            vast_instance_id=vast_id,
        )
        self.result.rented.append(label)
        return False


def run_pass(
    session: Session, client: VastClient, settings: Settings, *, now: datetime, runner_sha256: str | None
) -> PassResult:
    return Scheduler(session, client, settings, now=now, runner_sha256=runner_sha256).run()
