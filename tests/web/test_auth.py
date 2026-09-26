"""The stub sign-in flow end to end, and everything that should reject it."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from krater.models import User

# Fixture subs from krater/weave/stub_users.json.
MEMBER_SUB = "PWLMEMBERONE"
NON_MEMBER_SUB = "PWLNONMEMBER"


def _start_login(client: TestClient, next: str | None = None) -> str:
    """GET /login, and return the `state` value it put in the stub-redirect URL."""
    params = {"next": next} if next else {}
    response = client.get("/login", params=params, follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("/auth/stub?")
    return parse_qs(urlparse(location).query)["state"][0]


def test_full_stub_sign_in_flow(client: TestClient, db_session: Session) -> None:
    # 1. /login redirects to the stub picker.
    state = _start_login(client)

    # 2. The stub picker lists fixture users to click.
    picker = client.get("/auth/stub", params={"state": state})
    assert picker.status_code == 200
    assert "Mia Member" in picker.text

    # 3. Picking a user posts/links to the callback with code=<sub>.
    callback = client.get("/auth/callback", params={"code": MEMBER_SUB, "state": state}, follow_redirects=False)
    assert callback.status_code == 302
    assert callback.headers["location"] == "/"

    user = db_session.execute(select(User).where(User.weave_sub == MEMBER_SUB)).scalar_one()
    assert user.display_name == "Mia Member"
    assert user.groups_cached == ["ganymede:member"]
    assert user.last_login_at is not None

    # 4. The signed-in page shows the user's name and a sign-out control.
    home = client.get("/")
    assert home.status_code == 200
    assert "Mia Member" in home.text
    assert "Sign out" in home.text

    # 5. Logging out (with a valid CSRF token, picked up from the page) clears the session.
    csrf_token = home.text.split('name="csrf_token" value="')[1].split('"')[0]
    logout = client.post("/logout", data={"csrf_token": csrf_token}, follow_redirects=False)
    assert logout.status_code == 302

    after_logout = client.get("/")
    assert "Sign out" not in after_logout.text
    assert "Sign in" in after_logout.text


def test_login_next_round_trips_to_the_final_redirect(client: TestClient) -> None:
    state = _start_login(client, next="/somewhere/safe")

    callback = client.get("/auth/callback", params={"code": MEMBER_SUB, "state": state}, follow_redirects=False)

    assert callback.headers["location"] == "/somewhere/safe"


def test_callback_rejects_a_missing_state(client: TestClient) -> None:
    _start_login(client)

    response = client.get("/auth/callback", params={"code": MEMBER_SUB, "state": "not-the-real-state"})

    assert response.status_code == 400


def test_callback_rejects_when_no_login_was_started(client: TestClient) -> None:
    response = client.get("/auth/callback", params={"code": MEMBER_SUB, "state": "anything"})

    assert response.status_code == 400


def test_callback_rejects_a_non_member(client: TestClient, db_session: Session) -> None:
    state = _start_login(client)

    response = client.get("/auth/callback", params={"code": NON_MEMBER_SUB, "state": state})

    assert response.status_code == 403
    assert "Ganymede" in response.text
    assert db_session.execute(select(User).where(User.weave_sub == NON_MEMBER_SUB)).scalar_one_or_none() is None


def test_logout_without_csrf_token_is_rejected(client: TestClient) -> None:
    state = _start_login(client)
    client.get("/auth/callback", params={"code": MEMBER_SUB, "state": state}, follow_redirects=False)

    response = client.post("/logout")

    assert response.status_code == 403
    # The session survives: still signed in.
    assert "Mia Member" in client.get("/").text


def test_login_rejects_an_open_redirect_in_next(client: TestClient) -> None:
    state = _start_login(client, next="https://evil.example/steal")

    callback = client.get("/auth/callback", params={"code": MEMBER_SUB, "state": state}, follow_redirects=False)

    # Falls back to "/" instead of honoring the attacker-controlled absolute URL.
    assert callback.headers["location"] == "/"


def test_login_rejects_a_protocol_relative_next(client: TestClient) -> None:
    state = _start_login(client, next="//evil.example/steal")

    callback = client.get("/auth/callback", params={"code": MEMBER_SUB, "state": state}, follow_redirects=False)

    assert callback.headers["location"] == "/"


def test_stub_routes_404_in_live_mode(client: TestClient, monkeypatch) -> None:
    from krater.config import get_settings

    monkeypatch.setattr(get_settings(), "weave_mode", "live")

    response = client.get("/auth/stub")

    assert response.status_code == 404
