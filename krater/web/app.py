"""The FastAPI application factory: `create_app()`.

Run with: `uv run uvicorn krater.web.app:create_app --factory --reload`
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from krater.config import get_settings
from krater.services.errors import NotAllowed, NotFound
from krater.web.routers import admin, auth, gallery, pages, projects, reviews, skypilot_policy
from krater.web.templates import templates

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(title="Krater")

    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(pages.router)
    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(reviews.router)
    app.include_router(admin.router)
    app.include_router(gallery.router)
    app.include_router(skypilot_policy.router)

    @app.exception_handler(NotAllowed)
    def _handle_not_allowed(request: Request, exc: NotAllowed):
        return templates.TemplateResponse(request, "errors/403.html", status_code=403)

    @app.exception_handler(NotFound)
    def _handle_not_found(request: Request, exc: NotFound):
        return templates.TemplateResponse(request, "errors/404.html", status_code=404)

    return app
