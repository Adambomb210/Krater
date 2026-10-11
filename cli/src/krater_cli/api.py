"""Krater's API (`/api/v1/`), with the stored sign-in. Refreshes once and retries when Krater answers 401."""

from __future__ import annotations

from typing import Any

import httpx

from krater_cli import auth


class KraterError(Exception):
    """Krater refused or failed a request. `fields` holds per-field problems for a job or a secret."""

    def __init__(self, message: str, *, status: int, fields: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.fields = fields or {}


class Krater:
    def __init__(self, http: httpx.Client) -> None:
        self.http = http
        self.creds = auth.current(http=http)

    def request(self, method: str, path: str, *, json: Any = None) -> dict:
        response = self._send(method, path, json)
        if response.status_code == 401:
            self.creds = auth.refresh(self.creds, http=self.http)
            response = self._send(method, path, json)
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            message = data.get("error") if isinstance(data, dict) else None
            raise KraterError(
                message or f"Krater answered {response.status_code}.",
                status=response.status_code,
                fields=data.get("fields") if isinstance(data, dict) else None,
            )
        return data

    def _send(self, method: str, path: str, json: Any) -> httpx.Response:
        return self.http.request(
            method,
            f"{self.creds['url']}/api/v1{path}",
            json=json,
            headers={"Authorization": f"Bearer {self.creds['access_token']}"},
        )

    def get(self, path: str) -> dict:
        return self.request("GET", path)

    def post(self, path: str, json: Any = None) -> dict:
        return self.request("POST", path, json=json)

    def put(self, path: str, json: Any) -> dict:
        return self.request("PUT", path, json=json)

    def delete(self, path: str) -> dict:
        return self.request("DELETE", path)
