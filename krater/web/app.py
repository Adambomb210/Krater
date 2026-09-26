"""The FastAPI application factory: `create_app()`.

Run with: `uv run uvicorn krater.web.app:create_app --factory --reload`
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from krater.config import get_settings
from krater.web.routers import auth, pages

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(title="Krater")

    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(pages.router)
    app.include_router(auth.router)

    return app
