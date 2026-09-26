from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz_runs_a_query_and_reports_ok(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_home_page_renders(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Krater" in response.text
