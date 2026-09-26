"""The interim Slack membership gate (`krater.services.slack_membership`), enforced by
`/projects/{id}/submit` before handing off to `project_service.submit` -- see `docs/SPEC.md` "Roles &
authentication". Drafts are always allowed; only submitting is gated.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.models import ProjectStatus
from krater.slack import get_slack_client
from tests.conftest import MEMBER_SUB, get_csrf_token

# `MEMBER_SUB` (PWLMEMBERONE, stub_users.json) carries `slack_id: "U0001MEMBER"`.
MEMBER_SLACK_ID = "U0001MEMBER"


def test_a_restricted_guest_cannot_submit(client: TestClient, login_as, create_project, db_session: Session) -> None:
    slack_client = get_slack_client()
    slack_client.set_user_info(MEMBER_SLACK_ID, is_restricted=True)
    try:
        member = login_as(MEMBER_SUB)
        project = create_project(member, title="T", write_up="W", budget_requested_cents=100)
        # Mirrors production: the draft is created (and committed) in an earlier request.
        db_session.commit()

        page = client.get(f"/projects/{project.id}")
        csrf = get_csrf_token(page.text)
        response = client.post(f"/projects/{project.id}/submit", data={"csrf_token": csrf}, follow_redirects=False)

        assert response.status_code == 303
        db_session.refresh(project)
        assert project.status is ProjectStatus.DRAFT  # never made it to project_service.submit

        redirected = client.get(response.headers["location"])
        assert "code of conduct" in redirected.text.lower()
    finally:
        slack_client.unset_user_info(MEMBER_SLACK_ID)


def test_an_ultra_restricted_guest_cannot_submit(
    client: TestClient, login_as, create_project, db_session: Session
) -> None:
    slack_client = get_slack_client()
    slack_client.set_user_info(MEMBER_SLACK_ID, is_ultra_restricted=True)
    try:
        member = login_as(MEMBER_SUB)
        project = create_project(member, title="T", write_up="W", budget_requested_cents=100)
        # Mirrors production: the draft is created (and committed) in an earlier request.
        db_session.commit()

        response = client.post(
            f"/projects/{project.id}/submit",
            data={"csrf_token": get_csrf_token(client.get(f"/projects/{project.id}").text)},
            follow_redirects=False,
        )

        db_session.refresh(project)
        assert response.status_code == 303
        assert project.status is ProjectStatus.DRAFT
    finally:
        slack_client.unset_user_info(MEMBER_SLACK_ID)


def test_a_deleted_slack_account_cannot_submit(
    client: TestClient, login_as, create_project, db_session: Session
) -> None:
    slack_client = get_slack_client()
    slack_client.set_user_info(MEMBER_SLACK_ID, deleted=True)
    try:
        member = login_as(MEMBER_SUB)
        project = create_project(member, title="T", write_up="W", budget_requested_cents=100)
        # Mirrors production: the draft is created (and committed) in an earlier request.
        db_session.commit()

        response = client.post(
            f"/projects/{project.id}/submit",
            data={"csrf_token": get_csrf_token(client.get(f"/projects/{project.id}").text)},
            follow_redirects=False,
        )

        db_session.refresh(project)
        assert response.status_code == 303
        assert project.status is ProjectStatus.DRAFT
    finally:
        slack_client.unset_user_info(MEMBER_SLACK_ID)


def test_a_missing_slack_account_cannot_submit(
    client: TestClient, login_as, create_project, db_session: Session
) -> None:
    slack_client = get_slack_client()
    slack_client.remove_user_info(MEMBER_SLACK_ID)
    try:
        member = login_as(MEMBER_SUB)
        project = create_project(member, title="T", write_up="W", budget_requested_cents=100)
        # Mirrors production: the draft is created (and committed) in an earlier request.
        db_session.commit()

        response = client.post(
            f"/projects/{project.id}/submit",
            data={"csrf_token": get_csrf_token(client.get(f"/projects/{project.id}").text)},
            follow_redirects=False,
        )

        db_session.refresh(project)
        assert response.status_code == 303
        assert project.status is ProjectStatus.DRAFT
    finally:
        slack_client.unset_user_info(MEMBER_SLACK_ID)


def test_a_full_member_can_submit(client: TestClient, login_as, create_project, db_session: Session) -> None:
    member = login_as(MEMBER_SUB)
    project = create_project(member, title="T", write_up="W", budget_requested_cents=100)

    response = client.post(
        f"/projects/{project.id}/submit",
        data={"csrf_token": get_csrf_token(client.get(f"/projects/{project.id}").text)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    db_session.refresh(project)
    assert project.status is ProjectStatus.PENDING_REVIEW


def test_drafts_are_always_allowed_even_for_a_restricted_guest(
    client: TestClient, login_as, create_project, db_session: Session
) -> None:
    slack_client = get_slack_client()
    slack_client.set_user_info(MEMBER_SLACK_ID, is_restricted=True)
    try:
        member = login_as(MEMBER_SUB)
        project = create_project(member, title="", write_up="", budget_requested_cents=0)

        page = client.get(f"/projects/{project.id}/edit")
        csrf = get_csrf_token(page.text)
        response = client.post(
            f"/projects/{project.id}/edit",
            data={"csrf_token": csrf, "title": "T", "write_up": "W", "budget_requested": "10.00"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        db_session.refresh(project)
        assert project.status is ProjectStatus.DRAFT
        assert project.title == "T"
    finally:
        slack_client.unset_user_info(MEMBER_SLACK_ID)
