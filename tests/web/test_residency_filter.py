"""HTTP-level tests for the OSPool residency filter: the review queue can show only projects whose whole team is
verified US residents, and the project page tells reviewers. The rule itself is covered in
tests/services/test_residency.py."""

from __future__ import annotations

from fastapi.testclient import TestClient

from krater.weave import StubWeaveClient
from tests.conftest import MEMBER_SUB, OTHER_MEMBER_SUB, REVIEWER_SUB


def test_the_queue_filters_to_teams_of_us_residents(
    client: TestClient, login_as, submitted_project, weave_stub: StubWeaveClient
) -> None:
    weave_stub.set_us_resident(MEMBER_SUB, True)
    resident_project = submitted_project(login_as(MEMBER_SUB), title="Resident rover")
    other_project = submitted_project(login_as(OTHER_MEMBER_SUB), title="Other rover")

    login_as(REVIEWER_SUB)
    everything = client.get("/reviews").text
    filtered = client.get("/reviews?us_residents=true").text

    assert "Resident rover" in everything and "Other rover" in everything
    assert "US residents</span>" in everything
    assert f"/projects/{resident_project.id}" in filtered
    assert f"/projects/{other_project.id}" not in filtered


def test_the_project_page_names_who_is_not_a_verified_us_resident(
    client: TestClient, login_as, submitted_project
) -> None:
    project = submitted_project(login_as(MEMBER_SUB), title="Rover")

    login_as(REVIEWER_SUB)
    page = client.get(f"/projects/{project.id}").text

    assert "Not OSPool-ready" in page
    assert MEMBER_SUB in page


def test_a_member_does_not_see_the_residency_line(client: TestClient, login_as, submitted_project) -> None:
    project = submitted_project(login_as(MEMBER_SUB), title="Rover")

    page = client.get(f"/projects/{project.id}").text

    assert "OSPool" not in page
