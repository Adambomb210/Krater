"""HTTP-level tests for sharing proposals with OSPool: `/admin/funders/ospool` and its export. The rules are covered
in tests/services/test_funder_packet.py."""

from __future__ import annotations

import csv
import io

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from krater.models import AuditEvent
from krater.services import funder_packet
from krater.weave import StubWeaveClient, WeaveUnavailableError
from tests.conftest import ADMIN_SUB, MEMBER_SUB, OTHER_REVIEWER_SUB, REVIEWER_SUB, get_csrf_token

PAGE = "/admin/funders/ospool"


def _exports(db_session: Session) -> int:
    return db_session.scalar(
        sa.select(sa.func.count()).select_from(AuditEvent).where(AuditEvent.action == funder_packet.AUDIT_EXPORT)
    )


def _resident_project(login_as, approved_project, weave_stub: StubWeaveClient, title: str = "Funded rover"):
    weave_stub.set_us_resident(MEMBER_SUB, True)
    return approved_project(login_as(MEMBER_SUB), login_as(OTHER_REVIEWER_SUB), title=title)


def test_an_admin_sees_the_ospool_ready_projects(
    client: TestClient, login_as, approved_project, weave_stub: StubWeaveClient
) -> None:
    project = _resident_project(login_as, approved_project, weave_stub)

    login_as(ADMIN_SUB)
    response = client.get(PAGE)

    assert response.status_code == 200
    assert "Funded rover" in response.text
    assert f'name="project_id" value="{project.id}"' in response.text
    assert "Share with OSPool" in client.get("/admin").text


def test_a_reviewer_or_member_may_not_open_the_page(client: TestClient, login_as) -> None:
    login_as(REVIEWER_SUB)
    assert client.get(PAGE).status_code == 403

    login_as(MEMBER_SUB)
    assert client.get(PAGE).status_code == 403


def test_the_page_says_so_when_weave_is_down(
    client: TestClient, login_as, approved_project, weave_stub: StubWeaveClient, monkeypatch
) -> None:
    project = _resident_project(login_as, approved_project, weave_stub)
    login_as(ADMIN_SUB)
    real_get_user = weave_stub.get_user

    def get_user(sub: str, *, fresh: bool = False):
        if sub == MEMBER_SUB:
            raise WeaveUnavailableError("down")
        return real_get_user(sub, fresh=fresh)

    monkeypatch.setattr(weave_stub, "get_user", get_user)
    response = client.get(PAGE)

    assert response.status_code == 200
    assert "couldn&#39;t reach Weave" in response.text or "couldn't reach Weave" in response.text
    assert str(project.id) not in response.text


def test_an_admin_downloads_the_csv_and_the_export_is_audited(
    client: TestClient, login_as, approved_project, weave_stub: StubWeaveClient, db_session: Session
) -> None:
    project = _resident_project(login_as, approved_project, weave_stub)
    login_as(ADMIN_SUB)
    token = get_csrf_token(client.get(PAGE).text)

    response = client.post(
        f"{PAGE}/export", data={"csrf_token": token, "format": "csv", "project_id": [str(project.id)]}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'filename="ganymede-ospool-' in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"
    rows = list(csv.reader(io.StringIO(response.text)))
    assert [row[0] for row in rows[1:]] == ["Funded rover"]
    assert MEMBER_SUB not in response.text
    assert _exports(db_session) == 1


def test_an_admin_downloads_the_pdf(
    client: TestClient, login_as, approved_project, weave_stub: StubWeaveClient
) -> None:
    project = _resident_project(login_as, approved_project, weave_stub)
    login_as(ADMIN_SUB)
    token = get_csrf_token(client.get(PAGE).text)

    response = client.post(
        f"{PAGE}/export", data={"csrf_token": token, "format": "pdf", "project_id": [str(project.id)]}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")


def test_only_the_ticked_projects_go_in_the_file(
    client: TestClient, login_as, approved_project, weave_stub: StubWeaveClient
) -> None:
    chosen = _resident_project(login_as, approved_project, weave_stub, title="Chosen rover")
    _resident_project(login_as, approved_project, weave_stub, title="Other rover")
    login_as(ADMIN_SUB)
    token = get_csrf_token(client.get(PAGE).text)

    response = client.post(
        f"{PAGE}/export", data={"csrf_token": token, "format": "csv", "project_id": [str(chosen.id)]}
    )

    assert "Chosen rover" in response.text
    assert "Other rover" not in response.text


def test_an_export_with_nothing_ticked_goes_back_with_a_message(
    client: TestClient, login_as, approved_project, weave_stub: StubWeaveClient, db_session: Session
) -> None:
    _resident_project(login_as, approved_project, weave_stub)
    login_as(ADMIN_SUB)
    token = get_csrf_token(client.get(PAGE).text)

    response = client.post(f"{PAGE}/export", data={"csrf_token": token, "format": "csv"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == PAGE
    assert "Choose at least one OSPool-ready project." in client.get(PAGE).text
    assert _exports(db_session) == 0


def test_an_export_fails_closed_when_weave_is_down(
    client: TestClient, login_as, approved_project, weave_stub: StubWeaveClient, db_session: Session, monkeypatch
) -> None:
    project = _resident_project(login_as, approved_project, weave_stub)
    login_as(ADMIN_SUB)
    token = get_csrf_token(client.get(PAGE).text)
    real_get_user = weave_stub.get_user

    def get_user(sub: str, *, fresh: bool = False):
        if sub == MEMBER_SUB and fresh:
            raise WeaveUnavailableError("down")
        return real_get_user(sub, fresh=fresh)

    monkeypatch.setattr(weave_stub, "get_user", get_user)
    response = client.post(
        f"{PAGE}/export",
        data={"csrf_token": token, "format": "csv", "project_id": [str(project.id)]},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _exports(db_session) == 0


def test_a_reviewer_may_not_export(
    client: TestClient, login_as, approved_project, weave_stub: StubWeaveClient, db_session: Session
) -> None:
    project = _resident_project(login_as, approved_project, weave_stub)
    login_as(REVIEWER_SUB)
    token = get_csrf_token(client.get("/reviews").text)

    response = client.post(
        f"{PAGE}/export", data={"csrf_token": token, "format": "csv", "project_id": [str(project.id)]}
    )

    assert response.status_code == 403
    assert _exports(db_session) == 0


def test_an_export_without_the_csrf_token_is_refused(
    client: TestClient, login_as, approved_project, weave_stub: StubWeaveClient, db_session: Session
) -> None:
    project = _resident_project(login_as, approved_project, weave_stub)
    login_as(ADMIN_SUB)

    response = client.post(f"{PAGE}/export", data={"format": "csv", "project_id": [str(project.id)]})

    assert response.status_code == 403
    assert _exports(db_session) == 0
