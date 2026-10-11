"""The runner's HTTP API (`/runner/v1/...`): token auth, start, heartbeat, exit, and checkpoint URLs."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from krater.compute import runner_api
from krater.config import get_settings
from krater.db import get_session
from krater.models import ComputeJobState
from krater.services import compute_jobs
from krater.storage import FakeObjectStore
from krater.web.app import create_app
from tests.compute.test_scheduler import NOW, _instance, _pass, _submit, _token


@pytest.fixture
def store(monkeypatch) -> FakeObjectStore:
    store = FakeObjectStore()
    monkeypatch.setattr("krater.web.routers.runner.get_compute_store", lambda: store)
    return store


@pytest.fixture
def http(db_session):
    app = create_app()
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def machine(db_session, member, project, client, fake):  # noqa: F811
    """A rented, running machine: (job, instance row, its runner token)."""
    job = _submit(db_session, member, project)
    _pass(db_session, client)
    row = _instance(db_session, job)
    fake.set_status(row.vast_instance_id, "running")
    _pass(db_session, client, NOW + timedelta(minutes=5))
    return job, row, _token(fake, row)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_start_tells_the_runner_its_command_and_whether_it_resumes(http, machine) -> None:
    job, row, token = machine

    first = http.post("/runner/v1/start", headers=_auth(token))
    second = http.post("/runner/v1/start", headers=_auth(token))

    assert first.status_code == 200
    assert first.json()["command"] == "python train.py"
    assert first.json()["resumed"] is False
    assert first.json()["job_id"] == str(job.id)
    assert second.json()["resumed"] is True  # the container restarted on the same machine
    assert row.runner_starts == 2


def test_a_wrong_or_missing_token_is_refused(http, machine) -> None:
    assert http.post("/runner/v1/start").status_code == 403
    assert http.post("/runner/v1/start", headers=_auth("not-a-token")).status_code == 403
    # Never from a query string.
    _, _, token = machine
    assert http.post(f"/runner/v1/start?token={token}").status_code == 403


def test_heartbeat_records_and_says_when_to_stop(http, machine, db_session, member) -> None:  # noqa: F811
    job, row, token = machine

    response = http.post("/runner/v1/heartbeat", headers=_auth(token), json={"gpu_util": 97.5, "sessions": 1, "x": 1})
    assert response.json() == {"stop": False}
    assert row.last_heartbeat == {"gpu_util": 97.5, "sessions": 1}

    compute_jobs.cancel_job(db_session, member, job=job, now=NOW + timedelta(minutes=6))
    assert http.post("/runner/v1/heartbeat", headers=_auth(token), json={}).json() == {"stop": True}


def test_exit_finishes_the_job_once(http, machine) -> None:
    job, row, token = machine

    assert http.post("/runner/v1/exit", headers=_auth(token), json={"exit_code": 0}).json() == {"state": "succeeded"}
    again = http.post("/runner/v1/exit", headers=_auth(token), json={"exit_code": 3})

    assert again.json() == {"state": "succeeded"}
    assert job.state is ComputeJobState.SUCCEEDED and job.exit_code == 0
    assert http.post("/runner/v1/exit", headers=_auth(token), json={"exit_code": "x"}).status_code == 422


def test_a_failing_command_fails_the_job_and_the_machine_is_destroyed(http, machine, db_session, client, fake) -> None:  # noqa: F811
    job, row, token = machine

    http.post("/runner/v1/exit", headers=_auth(token), json={"exit_code": 2})
    _pass(db_session, client, NOW + timedelta(minutes=6))

    assert job.state is ComputeJobState.FAILED and "code 2" in job.state_reason
    assert fake.instances == {}
    # The destroyed machine's token is dead.
    assert http.post("/runner/v1/heartbeat", headers=_auth(token), json={}).status_code == 403


def test_small_uploads_get_one_url_and_big_ones_get_parts(http, machine, store) -> None:
    job, row, token = machine

    small = http.post("/runner/v1/files/checkpoints/upload", headers=_auth(token), json={"name": "s.pt", "size": 10})
    big = http.post(
        "/runner/v1/files/checkpoints/upload",
        headers=_auth(token),
        json={"name": "dir/big.pt", "size": runner_api.PART_BYTES * 2 + 1},
    )

    prefix = f"projects/{job.project_id}/jobs/{job.id}/checkpoints/"
    assert small.json()["url"].startswith(f"fake://krater-storage/{prefix}s.pt?put")
    assert len(big.json()["parts"]) == 3
    assert big.json()["part_bytes"] == runner_api.PART_BYTES
    done = http.post(
        "/runner/v1/files/checkpoints/complete",
        headers=_auth(token),
        json={"name": "dir/big.pt", "upload_id": big.json()["upload_id"], "etags": ['"a"', '"b"', '"c"']},
    )
    assert done.json() == {"ok": True}
    listed = http.post("/runner/v1/files/checkpoints/list", headers=_auth(token)).json()["files"]
    assert [f["name"] for f in listed] == ["dir/big.pt"]


def test_a_job_only_sees_its_own_files(http, machine, store) -> None:
    job, row, token = machine
    store.put("projects/other/jobs/other/checkpoints/x.pt", content_type="application/octet-stream", size_bytes=1)
    store.put(
        f"projects/{job.project_id}/jobs/{job.id}/outputs/result.json", content_type="application/json", size_bytes=2
    )

    assert http.post("/runner/v1/files/checkpoints/list", headers=_auth(token)).json() == {"files": []}
    outputs = http.post("/runner/v1/files/outputs/list", headers=_auth(token)).json()["files"]
    assert [(f["name"], f["size"]) for f in outputs] == [("result.json", 2)]


@pytest.mark.parametrize("name", ["../x", "a/../../x", "/abs", "a//b", "dir/", "", "a b"])
def test_upload_names_must_stay_inside_the_jobs_prefix(http, machine, store, name) -> None:
    _, _, token = machine

    response = http.post("/runner/v1/files/checkpoints/upload", headers=_auth(token), json={"name": name, "size": 1})

    assert response.status_code == 422


def test_unknown_areas_are_refused(http, machine, store) -> None:
    _, _, token = machine

    assert http.post("/runner/v1/files/secrets/list", headers=_auth(token)).status_code == 422


def test_the_runner_binary_is_served_when_built(http, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_runner_binary_path", str(tmp_path / "missing"))
    assert http.get("/runner/v1/krater-runner").status_code == 404

    binary = tmp_path / "krater-runner-linux-amd64"
    binary.write_bytes(b"\x7fELF fake")
    monkeypatch.setattr(get_settings(), "compute_runner_binary_path", str(binary))
    response = http.get("/runner/v1/krater-runner")

    assert response.status_code == 200 and response.content == b"\x7fELF fake"


def test_runner_sha256_follows_the_file(tmp_path) -> None:
    from krater.compute import launch

    binary = tmp_path / "runner"
    assert launch.runner_sha256(str(binary)) is None
    binary.write_bytes(b"one")
    first = launch.runner_sha256(str(binary))
    binary.write_bytes(b"two!")
    assert launch.runner_sha256(str(binary)) != first
    assert len(first) == 64
