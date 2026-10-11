"""The `krater` command against a fake Krater and Weave (`httpx.MockTransport`)."""

from __future__ import annotations

import json
import os
import stat
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from krater_cli import store
from krater_cli.main import run

KRATER = "https://krater.test"
WEAVE = "https://weave.test"
PROJECT = "11111111-1111-1111-1111-111111111111"
JOB = "22222222-2222-2222-2222-222222222222"


class Fake:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.authorize_query: dict | None = None
        self.valid_access = {"access-1"}
        self.refreshes = 0
        self.jobs: list[dict] = []
        self.secrets: dict[str, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == f"{KRATER}/api/v1/cli-config":
            return httpx.Response(
                200,
                json={
                    "issuer": WEAVE,
                    "client_id": "cli-app",
                    "scopes": "openid profile",
                    "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
                    "loopback": False,
                },
            )
        if url == f"{WEAVE}/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={"authorization_endpoint": f"{WEAVE}/oauth/authorize", "token_endpoint": f"{WEAVE}/oauth/token"},
            )
        if url == f"{WEAVE}/oauth/token":
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                assert form["code"] == ["the-code"] and form["client_id"] == ["cli-app"]
                assert form["code_verifier"][0] and form["redirect_uri"] == ["urn:ietf:wg:oauth:2.0:oob"]
                return httpx.Response(
                    200, json={"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 7200}
                )
            assert form["refresh_token"] == ["refresh-1"]
            self.refreshes += 1
            self.valid_access = {"access-2"}
            return httpx.Response(
                200, json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 7200}
            )
        if url.startswith(f"{KRATER}/api/v1/"):
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if token not in self.valid_access:
                return httpx.Response(401, json={"error": "expired"})
            return self.api(request, url.removeprefix(f"{KRATER}/api/v1"))
        if url.startswith("https://store.test/"):
            # A presigned URL: the CLI must not send its Krater sign-in along.
            assert "authorization" not in request.headers
            return httpx.Response(200, content={"score": b"0.93\n", "log": b"hi\n"}[url.rsplit("/", 1)[1]])
        return httpx.Response(404)

    def api(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "/me":
            return httpx.Response(
                200, json={"name": "Ada", "email": "ada@example.org", "member": True, "reviewer": False, "admin": False}
            )
        if path == f"/projects/{PROJECT}/jobs" and request.method == "POST":
            body = json.loads(request.content)
            job = {"id": JOB, "kind": body["kind"], "state": "queued", "state_reason": "Queued.", "body": body}
            self.jobs.append(job)
            return httpx.Response(201, json=job)
        if path == f"/jobs/{JOB}":
            return httpx.Response(
                200,
                json={
                    "id": JOB,
                    "name": "nb",
                    "kind": "box",
                    "state": "running",
                    "state_reason": None,
                    "machine": {"ssh_host": "1.2.3.4", "ssh_port": 10483},
                },
            )
        if path.startswith(f"/jobs/{JOB}/logs"):
            return httpx.Response(
                200, json={"attempt": 2, "text": "epoch 9\ndone\n", "size_bytes": 99, "truncated": True}
            )
        if path == f"/jobs/{JOB}/files":
            return httpx.Response(
                200,
                json={
                    "files": [
                        {
                            "area": "outputs",
                            "name": "eval/score.txt",
                            "size_bytes": 5,
                            "url": "https://store.test/score",
                        },
                        {"area": "logs", "name": "attempt-1.log", "size_bytes": 3, "url": "https://store.test/log"},
                        {"area": "outputs", "name": "../../escape.txt", "size_bytes": 1, "url": "https://store.test/x"},
                    ]
                },
            )
        if path.startswith(f"/projects/{PROJECT}/secrets/") and request.method == "PUT":
            name = path.rsplit("/", 1)[1]
            self.secrets[name] = json.loads(request.content)["value"]
            return httpx.Response(200, json={"name": name, "updated_at": "2026-10-11T12:00:00+00:00"})
        if path == f"/projects/{PROJECT}/jobs" and request.method == "GET":
            return httpx.Response(422, json={"error": "Check the job's fields.", "fields": {"target": "too high"}})
        return httpx.Response(404, json={"error": "No such thing."})


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KRATER_CONFIG_DIR", str(tmp_path / "krater"))
    return tmp_path / "krater"


@pytest.fixture
def fake() -> Fake:
    return Fake()


@pytest.fixture
def http(fake: Fake) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(fake.handler))


def _login(http: httpx.Client, said: list[str]) -> None:
    code = run(["login", "--url", KRATER, "--no-browser"], http=http, say=said.append, prompt=lambda _: " the-code \n")
    assert code == 0


def test_login_uses_pkce_and_stores_the_tokens_for_the_owner_only(http, fake, config_dir) -> None:
    said: list[str] = []
    _login(http, said)

    url = next(line for line in said if "oauth/authorize" in line).strip().split()[-1]
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["cli-app"] and query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["urn:ietf:wg:oauth:2.0:oob"]
    assert said[-1] == f"Signed in to {KRATER} as Ada."
    creds = store.load()
    assert creds["access_token"] == "access-1" and creds["refresh_token"] == "refresh-1"
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(store.credentials_path()).st_mode) == 0o600


def test_an_expired_access_token_is_refreshed_once(http, fake) -> None:
    _login(http, [])
    fake.valid_access = {"access-2"}  # Weave expired access-1 early

    said: list[str] = []
    assert run(["whoami"], http=http, say=said.append) == 0

    assert fake.refreshes == 1 and "Ada" in said[0]
    assert store.load()["refresh_token"] == "refresh-2"


def test_submit_sends_the_job_as_krater_expects(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    run(
        [
            "submit",
            PROJECT,
            "--name",
            "train",
            "--image",
            "ghcr.io/me/train:v3",
            "--command",
            "python train.py",
            "--gpu",
            "RTX 4090",
            "--target",
            "0.30",
            "--max",
            "0.45",
            "--max-runtime",
            "240",
        ],
        http=http,
        say=said.append,
    )

    body = fake.jobs[0]["body"]
    assert body["gpu_names"] == ["RTX 4090"] and body["target_usd_per_hour"] == "0.30"
    assert body["kind"] == "job" and body["interruptible"] is True and body["max_runtime_minutes"] == 240
    assert said == [f"Queued job {JOB}: Queued."]


def test_ssh_runs_ssh_with_the_boxs_address(http, fake) -> None:
    _login(http, [])
    commands: list[list[str]] = []

    run(["ssh", JOB, "--", "-L", "8888:localhost:8888"], http=http, exec_ssh=commands.append)

    assert commands == [["ssh", "-p", "10483", "-L", "8888:localhost:8888", "root@1.2.3.4"]]


def test_a_secret_value_never_goes_on_the_command_line(http, fake) -> None:
    _login(http, [])

    run(["secrets", "set", PROJECT, "HF_TOKEN"], http=http, say=lambda _: None, read_secret=lambda name: "hf_abc")

    assert fake.secrets == {"HF_TOKEN": "hf_abc"}


def test_krater_errors_carry_the_field_problems(http, fake) -> None:
    from krater_cli.api import KraterError

    _login(http, [])
    with pytest.raises(KraterError) as error:
        run(["jobs", PROJECT], http=http)

    assert error.value.status == 422 and error.value.fields == {"target": "too high"}


def test_without_a_sign_in_the_cli_says_how_to(http) -> None:
    from krater_cli.auth import AuthError

    with pytest.raises(AuthError, match="krater login"):
        run(["projects"], http=http)


def test_logs_prints_the_end_of_the_output(http, fake) -> None:
    _login(http, [])
    said: list[str] = []

    assert run(["logs", JOB, "--attempt", "2"], http=http, say=said.append) == 0

    assert said[0].startswith("[the last part of attempt 2's output")
    assert said[1] == "epoch 9\ndone"
    assert any("attempt=2" in str(r.url) for r in fake.requests)


def test_download_saves_each_file_under_its_kind_and_skips_odd_paths(http, fake, tmp_path) -> None:
    _login(http, [])
    said: list[str] = []

    assert run(["download", JOB, "--to", str(tmp_path / "results")], http=http, say=said.append) == 0

    assert (tmp_path / "results" / "outputs" / "eval" / "score.txt").read_bytes() == b"0.93\n"
    assert (tmp_path / "results" / "logs" / "attempt-1.log").read_bytes() == b"hi\n"
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "results" / "escape.txt").exists()
    assert any(line.startswith("Skipped ../../escape.txt") for line in said)
