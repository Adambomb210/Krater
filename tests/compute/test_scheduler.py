"""The Krater Compute scheduler, end to end against FakeVast: start when cheap, budget holds, lost creates, the sweep,
pauses, moves, runtime limits and cancelling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa

from krater.compute import scheduler
from krater.config import Settings
from krater.models import (
    ComputeEscalation,
    ComputeEvent,
    ComputeInstance,
    ComputeInstanceState,
    ComputeJob,
    ComputeJobState,
    ProjectStatus,
)
from krater.services import compute_jobs, projects
from krater.services.compute_jobs import JobSpec
from krater.services.errors import InvalidState, NotAllowed, ValidationFailed
from krater.vast import FakeVast, VastClient

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
SETTINGS = Settings(compute_mode="vast", skypilot_max_hourly_cost_cents=500)
# Disk is 32 GB everywhere. RTX 3060 offers: 1001 bids $0.055 + $0.0088 disk = $0.0638/h (cheapest),
# 1003 $0.066 + $0.0066 = $0.0726/h, 1002 $0.0495 + $0.0351 = $0.0846/h.
CHEAPEST_3060_FULL = 63_767


def _submit(db_session, member, project, *, now=NOW, **overrides) -> ComputeJob:  # noqa: F811
    spec = {
        "name": "train",
        "image": "ghcr.io/alice/train:v3",
        "command": "python train.py",
        "gpu_names": ("RTX 3060",),
        "target_microusd": 70_000,
        "max_microusd": 100_000,
        **overrides,
    }
    return compute_jobs.submit_job(db_session, member, project=project, spec=JobSpec(**spec), cap_cents=500, now=now)


RUNNER_SHA = "ab" * 32


def _pass(db_session, client, now=NOW) -> scheduler.PassResult:
    return scheduler.run_pass(db_session, client, SETTINGS, now=now, runner_sha256=RUNNER_SHA)


def _token(fake, row) -> str:
    """The runner token Krater sent to the machine (it keeps only the hash)."""
    env = fake.instances[row.vast_instance_id]["env"]
    return env.split("KRATER_JOB_TOKEN=")[1].split("'")[0].split(" ")[0]


def _runner_starts(db_session, fake, row, now):
    from krater.compute import runner_api

    runner_api.start(db_session, runner_api.authenticate(db_session, _token(fake, row)), now=now)


def _beat(db_session, row, now):
    from krater.compute import runner_api

    runner_api.heartbeat(db_session, row, {"gpu_util": 90}, now=now)


def _instance(db_session, job) -> ComputeInstance:
    db_session.refresh(job)
    return job.instances[-1]


def _events(db_session, job) -> list[str]:
    return list(
        db_session.scalars(sa.select(ComputeEvent.kind).where(ComputeEvent.job_id == job.id).order_by(ComputeEvent.at))
    )


# -- Start when cheap ---------------------------------------------------------------------------------


def test_a_job_waits_while_every_offer_is_over_its_target(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project, target_microusd=50_000)

    result = _pass(db_session, client)

    assert result.rented == []
    assert job.state is ComputeJobState.QUEUED
    assert "at or under $0.0500/h" in job.state_reason
    assert "cheapest now $0.0638/h" in job.state_reason
    assert fake.instances == {}


def test_a_job_rents_the_cheapest_offer_at_or_under_its_target(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)

    result = _pass(db_session, client)

    row = _instance(db_session, job)
    assert result.rented == [row.label]
    assert row.label == f"krater-job-{job.id.hex[:12]}-1"
    assert row.state is ComputeInstanceState.PROVISIONING
    assert row.offer_id == 1001
    assert row.interruptible is True
    assert row.gpu_microusd == 55_000  # min_bid $0.05 plus the 10% margin
    assert row.full_microusd == CHEAPEST_3060_FULL
    created = fake.instances[row.vast_instance_id]
    assert created["is_bid"] is True
    assert created["label"] == row.label
    # The image's ENTRYPOINT is replaced by sh (in args mode, Vast's onstart is the entrypoint override).
    assert created["onstart"] == "sh"
    assert created["args"][0] == "-c"
    # The bootstrap fetches the runner and checks its sha256; the runner asks Krater for the command.
    assert RUNNER_SHA in created["args"][1]
    assert "python train.py" not in created["args"][1]
    assert created["runtype"] == "args"
    assert "KRATER_JOB_ID=" in created["env"]
    assert "KRATER_RUNNER_URL=http://localhost:8000" in created["env"]
    token = _token(fake, row)
    assert len(token) >= 40 and row.runner_token_hash != token
    assert job.state is ComputeJobState.STARTING
    # Holds its max runtime (8 h) at the rented price: ceil(0.063767 * 8 * 100) cents.
    assert job.hold_cents == 52
    # The search asked for this job's shape at the reliability floor.
    body = fake.search_bodies[-1]
    assert body["allocated_storage"] == 32 and body["reliability"] == {"gte": 0.95} and body["type"] == "bid"


def test_the_job_runs_once_vast_reports_running(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)

    _pass(db_session, client, NOW + timedelta(minutes=1))  # still pulling
    assert job.state is ComputeJobState.STARTING
    fake.set_status(row.vast_instance_id, "running")
    _pass(db_session, client, NOW + timedelta(minutes=5))

    assert row.state is ComputeInstanceState.RUNNING
    assert job.state is ComputeJobState.RUNNING
    assert job.started_at == NOW + timedelta(minutes=5)
    assert _events(db_session, job) == ["queued", "renting", "rented", "running"]


def test_an_on_demand_job_pays_dph_base(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project, interruptible=False, target_microusd=80_000)

    _pass(db_session, client)

    row = _instance(db_session, job)
    assert row.interruptible is False
    assert row.gpu_microusd == 60_000
    assert fake.instances[row.vast_instance_id]["is_bid"] is False


def test_two_jobs_never_rent_the_same_offer(db_session, member, project, client) -> None:  # noqa: F811
    first = _submit(db_session, member, project, target_microusd=90_000)
    second = _submit(db_session, member, project, target_microusd=90_000, now=NOW + timedelta(seconds=1))

    _pass(db_session, client, NOW + timedelta(seconds=2))

    assert {_instance(db_session, first).offer_id, _instance(db_session, second).offer_id} == {1001, 1003}


# -- Budget and caps -----------------------------------------------------------------------------------


def test_no_rental_without_budget_for_one_hour(db_session, member, project, client, fake, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr("krater.compute.scheduler.budget.remaining_cents", lambda session, project: 6)
    job = _submit(db_session, member, project)

    _pass(db_session, client)

    assert job.state is ComputeJobState.QUEUED
    assert job.state_reason.startswith("Waiting for budget")
    assert fake.instances == {}


def test_holds_keep_two_jobs_from_spending_the_same_dollars(db_session, member, project, client, monkeypatch) -> None:  # noqa: F811
    # 60 cents left: the first job holds 52 (8 h at $0.0638/h); the second needs 8 for one hour and has 8.
    monkeypatch.setattr("krater.compute.scheduler.budget.remaining_cents", lambda session, project: 60)
    first = _submit(db_session, member, project, target_microusd=90_000)
    second = _submit(db_session, member, project, target_microusd=90_000, now=NOW + timedelta(seconds=1))
    third = _submit(db_session, member, project, target_microusd=90_000, now=NOW + timedelta(seconds=2))

    _pass(db_session, client, NOW + timedelta(seconds=3))

    assert first.state is ComputeJobState.STARTING and first.hold_cents == 52
    assert second.state is ComputeJobState.STARTING and second.hold_cents == 8  # what was left
    assert third.state is ComputeJobState.QUEUED and third.state_reason.startswith("Waiting for budget")


def test_a_lowered_hourly_cap_wins_over_the_target(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    project.max_hourly_cost_cents = 5  # $0.05/h: under every offer, even at its minimum bid
    db_session.flush()

    _pass(db_session, client)

    assert job.state is ComputeJobState.QUEUED
    assert "at or under $0.0500/h" in job.state_reason
    assert fake.instances == {}


def test_a_finished_project_cancels_its_queued_jobs(db_session, member, project, client) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    project.status = ProjectStatus.WITHDRAWN
    db_session.flush()

    _pass(db_session, client)

    assert job.state is ComputeJobState.CANCELLED


# -- Max wait --------------------------------------------------------------------------------------------


def test_after_max_wait_a_job_can_switch_to_on_demand_up_to_its_max(db_session, member, project, client) -> None:  # noqa: F811
    job = _submit(
        db_session,
        member,
        project,
        target_microusd=40_000,
        max_wait_minutes=60,
        on_max_wait=ComputeEscalation.ON_DEMAND,
    )
    _pass(db_session, client)
    assert job.state is ComputeJobState.QUEUED

    _pass(db_session, client, NOW + timedelta(minutes=61))

    row = _instance(db_session, job)
    assert row.interruptible is False
    assert row.full_microusd <= 100_000


def test_after_max_wait_a_job_can_raise_its_bid_to_its_max(db_session, member, project, client) -> None:  # noqa: F811
    job = _submit(
        db_session,
        member,
        project,
        target_microusd=40_000,
        max_wait_minutes=60,
        on_max_wait=ComputeEscalation.RAISE_BID,
    )

    _pass(db_session, client, NOW + timedelta(minutes=61))

    assert _instance(db_session, job).interruptible is True


def test_after_max_wait_a_job_can_cancel(db_session, member, project, client) -> None:  # noqa: F811
    job = _submit(
        db_session, member, project, target_microusd=40_000, max_wait_minutes=60, on_max_wait=ComputeEscalation.CANCEL
    )

    _pass(db_session, client, NOW + timedelta(minutes=61))

    assert job.state is ComputeJobState.CANCELLED


# -- Lost creates and the sweep ----------------------------------------------------------------------------


def _timing_out_client(fake: FakeVast) -> VastClient:
    """A client whose create reaches Vast but whose response is lost."""

    def handler(request: httpx.Request) -> httpx.Response:
        response = fake.handle(request)
        if request.method == "PUT" and "/asks/" in request.url.path:
            raise httpx.ReadTimeout("response lost")
        return response

    client = fake.client()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_a_create_whose_response_was_lost_is_found_by_its_label(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)

    _pass(db_session, _timing_out_client(fake))
    row = _instance(db_session, job)
    assert row.state is ComputeInstanceState.CREATING and row.vast_instance_id is None
    assert len(fake.instances) == 1  # Vast did rent it

    _pass(db_session, client, NOW + timedelta(minutes=1))

    assert row.state is ComputeInstanceState.PROVISIONING
    assert row.vast_instance_id == next(iter(fake.instances))
    assert len(fake.instances) == 1  # not swept, not rented twice


def test_a_create_that_never_appears_is_given_up_and_requeued(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    _pass(db_session, _timing_out_client(fake))
    row = _instance(db_session, job)
    fake.instances.clear()  # it never really happened

    _pass(db_session, client, NOW + timedelta(minutes=1))
    assert row.state is ComputeInstanceState.CREATING
    fake.offers[1001]["rented"] = False
    _pass(db_session, client, NOW + timedelta(minutes=6))

    assert row.state is ComputeInstanceState.CREATE_FAILED
    # Requeued and, the same pass, rented again as attempt 2.
    assert _instance(db_session, job).attempt == 2
    assert job.state is ComputeJobState.STARTING


def test_the_sweep_destroys_krater_instances_krater_does_not_know(db_session, client, fake) -> None:
    fake.add_offer(2001, 2001, "RTX 3060", dph_base=0.1, min_bid=0.05, storage_cost=0.1, gpu_ram_mb=12288)
    fake.add_offer(2002, 2002, "RTX 3060", dph_base=0.1, min_bid=0.05, storage_cost=0.1, gpu_ram_mb=12288)
    from krater.vast import CreateInstance

    stray = client.create_instance(2001, CreateInstance(image="x", disk_gb=8, label="krater-job-deadbeef0000-1"))
    other = client.create_instance(2002, CreateInstance(image="x", disk_gb=8, label="krater-spike-123-A"))

    result = _pass(db_session, client)

    assert result.swept == [stray]
    assert stray not in fake.instances
    assert other in fake.instances  # not Krater's label prefix: never touched


def test_a_refused_create_puts_the_job_back_in_its_place(db_session, member, project, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and "/asks/" in request.url.path:
            # Someone else rented it between the search and the create.
            return httpx.Response(400, json={"success": False, "error": "invalid_args", "msg": "GPU conflict"})
        return fake.handle(request)

    client = fake.client()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))

    _pass(db_session, client)

    assert _instance(db_session, job).state is ComputeInstanceState.CREATE_FAILED
    assert job.state is ComputeJobState.QUEUED
    assert job.queued_at == NOW
    assert job.hold_cents == 0
    assert fake.instances == {}


# -- Pauses, moves, limits ----------------------------------------------------------------------------------


def _running_job(db_session, member, project, client, fake, **overrides):  # noqa: F811
    job = _submit(db_session, member, project, **overrides)
    _pass(db_session, client)
    row = _instance(db_session, job)
    fake.set_status(row.vast_instance_id, "running")
    _pass(db_session, client, NOW + timedelta(minutes=5))
    _runner_starts(db_session, fake, row, NOW + timedelta(minutes=5))
    return job, row


def test_an_outbid_job_waits_raises_its_bid_then_moves(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    vast_id = row.vast_instance_id
    fake.set_status(vast_id, "stopped")

    _pass(db_session, client, NOW + timedelta(minutes=6))
    assert job.state is ComputeJobState.PAUSED and row.state is ComputeInstanceState.PAUSED

    _pass(db_session, client, NOW + timedelta(minutes=8))  # inside the 5-minute grace
    assert row.gpu_microusd == 55_000

    _pass(db_session, client, NOW + timedelta(minutes=12))  # grace over: bid +10%
    assert row.gpu_microusd == 60_500
    assert fake.instances[vast_id]["dph_base"] == pytest.approx(0.0605)

    _pass(db_session, client, NOW + timedelta(minutes=37))  # paused 31 minutes: move
    assert row.state is ComputeInstanceState.DESTROYED
    assert vast_id not in fake.instances
    assert "bid_raised" in _events(db_session, job)
    # Requeued, and in the same pass rented again elsewhere as attempt 2, which resumes.
    new = _instance(db_session, job)
    assert new.attempt == 2 and new.vast_instance_id != vast_id
    assert "KRATER_RESUMED=1" in fake.instances[new.vast_instance_id]["env"]


def test_a_paused_job_that_resumes_runs_again(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    fake.set_status(row.vast_instance_id, "stopped")
    _pass(db_session, client, NOW + timedelta(minutes=6))

    fake.set_status(row.vast_instance_id, "running")
    _pass(db_session, client, NOW + timedelta(minutes=7))

    assert job.state is ComputeJobState.RUNNING and row.paused_since is None


def test_the_bid_never_goes_over_the_jobs_max(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake, max_microusd=70_000)
    fake.set_status(row.vast_instance_id, "stopped")
    for minute in range(6, 29):
        _pass(db_session, client, NOW + timedelta(minutes=minute))

    assert row.full_microusd <= 70_000


def test_max_runtime_destroys_the_machine_and_fails_the_job(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake, max_runtime_minutes=60)

    _pass(db_session, client, NOW + timedelta(minutes=66))

    assert job.state is ComputeJobState.FAILED
    assert "max runtime" in job.state_reason
    assert row.state is ComputeInstanceState.DESTROYED
    assert fake.instances == {}
    assert job.hold_cents == 0


def test_a_machine_that_never_starts_is_given_up(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    _pass(db_session, client)

    _pass(db_session, client, NOW + timedelta(minutes=31))

    assert job.state is ComputeJobState.FAILED
    assert fake.instances == {}


def test_a_vanished_machine_requeues_the_job(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    fake.instances.pop(row.vast_instance_id)
    fake.offers[1001]["rented"] = True  # the host is gone

    _pass(db_session, client, NOW + timedelta(minutes=6))

    assert row.state is ComputeInstanceState.DESTROYED
    assert "lost" in _events(db_session, job)
    assert _instance(db_session, job).attempt == 2


def test_a_withdrawn_project_stops_its_running_machines(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    project.status = ProjectStatus.WITHDRAWN
    db_session.flush()

    _pass(db_session, client, NOW + timedelta(minutes=6))

    assert job.state is ComputeJobState.CANCELLED
    assert row.state is ComputeInstanceState.DESTROYED
    assert fake.instances == {}
    assert job.hold_cents == 0


def test_cancelling_destroys_the_machine_on_the_next_pass(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)

    compute_jobs.cancel_job(db_session, member, job=job, now=NOW + timedelta(minutes=6))
    _pass(db_session, client, NOW + timedelta(minutes=7))

    assert job.state is ComputeJobState.CANCELLED
    assert row.state is ComputeInstanceState.DESTROYED
    assert fake.instances == {}


def test_a_destroy_vast_does_not_confirm_is_retried(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    compute_jobs.cancel_job(db_session, member, job=job, now=NOW + timedelta(minutes=6))
    real_destroy = fake._destroy
    fake._destroy = lambda instance_id: httpx.Response(200, json={"success": True})  # says yes, does nothing

    _pass(db_session, client, NOW + timedelta(minutes=7))
    assert row.state is ComputeInstanceState.RUNNING and row.destroy_requested_at is not None

    fake._destroy = real_destroy
    _pass(db_session, client, NOW + timedelta(minutes=8))
    assert row.state is ComputeInstanceState.DESTROYED


# -- Submitting ---------------------------------------------------------------------------------------------


def test_only_the_project_team_submits(db_session, member, project, make_actor) -> None:  # noqa: F811
    outsider = make_actor(groups=frozenset({"ganymede:member"}))
    with pytest.raises(NotAllowed):
        _submit(db_session, outsider, project)


def test_prices_are_checked_against_the_cap(db_session, member, project) -> None:  # noqa: F811
    with pytest.raises(ValidationFailed) as excinfo:
        _submit(db_session, member, project, max_microusd=6_000_000)
    assert "max" in excinfo.value.errors
    with pytest.raises(ValidationFailed) as excinfo:
        _submit(db_session, member, project, target_microusd=200_000)
    assert "target" in excinfo.value.errors


def test_a_draft_project_cannot_run_jobs(db_session, member) -> None:  # noqa: F811
    draft = projects.create_project(db_session, member, title="Draft", write_up="w", budget_requested_cents=100)
    with pytest.raises(InvalidState):
        _submit(db_session, member, draft)


def test_bid_rounding_and_clamping() -> None:
    from krater.compute.offers import bid_for
    from krater.vast import Offer

    offer = Offer.from_json(
        {"id": 1, "gpu_name": "x", "num_gpus": 1, "dph_base": 0.1, "dph_total": 0.1, "min_bid": 0.0533333}
    )
    assert bid_for(offer, margin_percent=10, max_gpu=Decimal("1")) == Decimal("0.0587")
    assert bid_for(offer, margin_percent=10, max_gpu=Decimal("0.055")) == Decimal("0.055")
    assert bid_for(offer, margin_percent=10, max_gpu=Decimal("0.05")) is None


# -- The runner ---------------------------------------------------------------------------------------------


def test_a_runner_that_never_starts_fails_the_job(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)
    fake.set_status(row.vast_instance_id, "running")
    _pass(db_session, client, NOW + timedelta(minutes=5))

    _pass(db_session, client, NOW + timedelta(minutes=14))
    assert job.state is ComputeJobState.RUNNING
    _pass(db_session, client, NOW + timedelta(minutes=15))

    assert job.state is ComputeJobState.FAILED
    assert "curl, wget or Python" in job.state_reason
    assert fake.instances == {}


def test_a_silent_runner_fails_the_job(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    _beat(db_session, row, NOW + timedelta(minutes=20))

    _pass(db_session, client, NOW + timedelta(minutes=49))
    assert job.state is ComputeJobState.RUNNING
    _pass(db_session, client, NOW + timedelta(minutes=50))

    assert job.state is ComputeJobState.FAILED
    assert "stopped reporting" in job.state_reason


def test_after_a_pause_the_runner_must_start_again(db_session, member, project, client, fake) -> None:  # noqa: F811
    job, row = _running_job(db_session, member, project, client, fake)
    fake.set_status(row.vast_instance_id, "stopped")
    _pass(db_session, client, NOW + timedelta(minutes=6))
    fake.set_status(row.vast_instance_id, "running")
    _pass(db_session, client, NOW + timedelta(minutes=7))  # resumed: Vast restarted the container

    _runner_starts(db_session, fake, row, NOW + timedelta(minutes=8))
    _pass(db_session, client, NOW + timedelta(minutes=20))

    assert job.state is ComputeJobState.RUNNING
    assert row.runner_starts == 2


def test_no_runner_binary_means_nothing_is_rented(db_session, member, project, client, fake) -> None:  # noqa: F811
    job = _submit(db_session, member, project)

    scheduler.run_pass(db_session, client, SETTINGS, now=NOW, runner_sha256=None)

    assert job.state is ComputeJobState.QUEUED
    assert fake.instances == {}
