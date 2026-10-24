"""The staging marker: a hazard-stripe frame and a STAGING tab on every page when `KRATER_STAGING` is on."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from krater.config import Settings, get_settings
from tests.conftest import MEMBER_SUB


def test_no_marker_by_default(client: TestClient, monkeypatch) -> None:
    # A developer's own .env may set KRATER_STAGING; the default is what this checks.
    monkeypatch.setattr(get_settings(), "staging", Settings.model_fields["staging"].default)

    html = client.get("/").text

    assert "staging-frame" not in html
    assert "staging-tab" not in html


@pytest.mark.parametrize("path", ["/", "/gallery", "/pricing", "/does-not-exist"])
def test_marker_on_public_and_error_pages_when_staging(client: TestClient, monkeypatch, path: str) -> None:
    monkeypatch.setattr(get_settings(), "staging", True)

    html = client.get(path, headers={"accept": "text/html"}).text

    assert '<div class="staging-frame" aria-hidden="true"></div>' in html
    assert '<div class="staging-tab" role="status">Staging</div>' in html


def test_marker_on_signed_in_pages_when_staging(client: TestClient, monkeypatch, login_as) -> None:
    monkeypatch.setattr(get_settings(), "staging", True)
    login_as(MEMBER_SUB)

    assert "staging-tab" in client.get("/projects/new").text


def test_marker_setting_reads_the_env(monkeypatch) -> None:
    monkeypatch.setenv("KRATER_STAGING", "true")

    assert Settings().staging is True
