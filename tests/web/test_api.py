"""The CLI's API (`/api/v1/`): bearer tokens from the CLI's Weave app only, JSON in and out."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from krater.config import get_settings
from krater.models import ComputeJob, ComputeJobState
from krater.services import project_secrets
from krater.weave import StubWeaveClient
from tests.conftest import ADMIN_SUB, MEMBER_SUB, OTHER_MEMBER_SUB, REVIEWER_SUB

JOB = {
    "name": "train",
    "image": "ghcr.io/alice/train:v3",
    "command": "python train.py",
    "gpu_names": ["RTX 3060"],
    "disk_gb": 32,
    "target_usd_per_hour": "0.07",
    "max_usd_per_hour": "0.10",
    "max_runtime_minutes": 60,
}


@pytest.fixture(autouse=True)
def cli_on(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "compute_mode", "vast")
    monkeypatch.setattr(get_settings(), "weave_cli_client_id", "krater-cli-app")


@pytest.fixture
def project(login_as, approved_project):
    member = login_as(MEMBER_SUB)
    reviewer = login_as(REVIEWER_SUB)
    return approved_project(member, reviewer)


def _as(sub: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {StubWeaveClient.cli_token_for(sub)}"}


def test_cli_config_says_how_to_sign_in(client: TestClient) -> None:
    config = client.get("/api/v1/cli-config").json()

    assert config["client_id"] == "krater-cli-app"
    assert config["redirect_uri"] == "urn:ietf:wg:oauth:2.0:oob"


def test_the_api_is_off_without_the_cli_app_or_compute(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "weave_cli_client_id", "")
    assert client.get("/api/v1/cli-config").status_code == 404
    monkeypatch.setattr(get_settings(), "weave_cli_client_id", "krater-cli-app")
    monkeypatch.setattr(get_settings(), "compute_mode", "off")
    assert client.get("/api/v1/me", headers=_as(MEMBER_SUB)).status_code == 404


def test_no_token_or_a_bad_one_is_a_json_401(client: TestClient, login_as) -> None:
    login_as(MEMBER_SUB)  # a browser session cookie is not enough

    missing = client.get("/api/v1/me")
    wrong = client.get("/api/v1/me", headers={"Authorization": "Bearer someone-elses-token"})

    assert missing.status_code == 401 and "krater login" in missing.json()["error"]
    assert wrong.status_code == 401 and wrong.headers["www-authenticate"].startswith("Bearer")


def test_a_weave_user_who_never_signed_in_to_krater_is_asked_to(client: TestClient) -> None:
    response = client.get("/api/v1/me", headers=_as(MEMBER_SUB))

    assert response.status_code == 403 and "browser" in response.json()["error"]


def test_a_member_sees_their_projects_and_queues_a_job(client: TestClient, project, db_session) -> None:
    me = client.get("/api/v1/me", headers=_as(MEMBER_SUB)).json()
    assert me["member"] is True

    projects = client.get("/api/v1/projects", headers=_as(MEMBER_SUB)).json()["projects"]
    assert [p["id"] for p in projects] == [str(project.id)] and projects[0]["can_launch"]

    response = client.post(f"/api/v1/projects/{project.id}/jobs", json=JOB, headers=_as(MEMBER_SUB))

    assert response.status_code == 201, response.text
    job = db_session.scalars(sa.select(ComputeJob)).one()
    assert response.json()["id"] == str(job.id) and job.state is ComputeJobState.QUEUED
    assert (job.target_microusd, job.max_microusd) == (70_000, 100_000)
    listed = client.get(f"/api/v1/projects/{project.id}/jobs", headers=_as(MEMBER_SUB)).json()["jobs"]
    assert listed[0]["target_usd_per_hour"] == "0.0700"
    detail = client.get(f"/api/v1/jobs/{job.id}", headers=_as(MEMBER_SUB)).json()
    assert detail["events"][0]["kind"] == "queued" and detail["machine"] is None


def test_a_bad_job_gets_field_errors(client: TestClient, project) -> None:
    response = client.post(
        f"/api/v1/projects/{project.id}/jobs",
        json={**JOB, "target_usd_per_hour": "0.20", "max_usd_per_hour": "0.10"},
        headers=_as(MEMBER_SUB),
    )

    assert response.status_code == 422 and response.json()["fields"]


def test_a_member_cancels_their_job(client: TestClient, project) -> None:
    job = client.post(f"/api/v1/projects/{project.id}/jobs", json=JOB, headers=_as(MEMBER_SUB)).json()

    response = client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=_as(MEMBER_SUB))

    assert response.status_code == 200 and response.json()["state"] == "cancelled"


def test_outsiders_see_no_project_and_reviewers_cannot_launch(client: TestClient, project, login_as) -> None:
    login_as(OTHER_MEMBER_SUB)

    assert client.get(f"/api/v1/projects/{project.id}/jobs", headers=_as(OTHER_MEMBER_SUB)).status_code == 404
    assert client.get(f"/api/v1/projects/{project.id}/jobs", headers=_as(REVIEWER_SUB)).status_code == 200
    response = client.post(f"/api/v1/projects/{project.id}/jobs", json=JOB, headers=_as(REVIEWER_SUB))
    assert response.status_code == 403


def test_a_member_removed_in_weave_is_refused_at_once(client: TestClient, project, weave_stub) -> None:
    weave_stub.set_roles(MEMBER_SUB, [])

    response = client.post(f"/api/v1/projects/{project.id}/jobs", json=JOB, headers=_as(MEMBER_SUB))

    assert response.status_code == 403


def test_secrets_can_be_set_and_listed_but_never_read_back(client: TestClient, project, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "project_secrets_key", Fernet.generate_key().decode())
    url = f"/api/v1/projects/{project.id}/secrets"

    assert client.put(f"{url}/HF_TOKEN", json={"value": "hf_abc"}, headers=_as(MEMBER_SUB)).status_code == 200
    listing = client.get(url, headers=_as(MEMBER_SUB))
    assert listing.json()["secrets"][0]["name"] == "HF_TOKEN" and "hf_abc" not in listing.text
    assert client.put(f"{url}/HF_TOKEN", json={"value": "x"}, headers=_as(REVIEWER_SUB)).status_code == 403
    bad = client.put(f"{url}/KRATER_X", json={"value": "x"}, headers=_as(MEMBER_SUB))
    assert bad.status_code == 422
    assert client.delete(f"{url}/HF_TOKEN", headers=_as(MEMBER_SUB)).status_code == 200
    assert project_secrets.list_secrets(project._sa_instance_state.session, project) == []


def test_prices_list_the_newest_samples(client: TestClient, project, login_as) -> None:
    login_as(ADMIN_SUB)

    response = client.get("/api/v1/prices", headers=_as(ADMIN_SUB))

    assert response.status_code == 200 and response.json() == {"prices": []}


def test_unknown_routes_under_the_api_are_json(client: TestClient) -> None:
    response = client.get("/api/v1/nothing-here", headers={"accept": "text/html"})

    assert response.status_code == 404 and response.json()["error"]
