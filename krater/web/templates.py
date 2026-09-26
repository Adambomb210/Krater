"""The single Jinja2Templates instance the web app renders from."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from krater.web.csrf import register_csrf_template_global

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_csrf_template_global(templates)
