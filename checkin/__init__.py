"""ISN Event Check-in application factory."""

from __future__ import annotations

import os
from typing import Any

from flask import Flask, render_template
from werkzeug.exceptions import HTTPException

from . import db
from .routes import bp
from .security import init_csrf

DEV_SECRET = "dev-secret-change-me"
DEV_TOKEN = "dev-admin-token"


def create_app(config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", DEV_SECRET),
        ADMIN_TOKEN=os.environ.get("ADMIN_TOKEN", DEV_TOKEN),
        DATABASE=os.environ.get("DATABASE", os.path.join(app.instance_path, "checkin.db")),
        BASE_URL=os.environ.get("BASE_URL", ""),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    if config:
        app.config.update(config)
    if not app.config.get("TESTING"):
        for key, default in (("SECRET_KEY", DEV_SECRET), ("ADMIN_TOKEN", DEV_TOKEN)):
            if app.config[key] == default:
                app.logger.warning("%s is using the development default; set it via env var.", key)
    os.makedirs(app.instance_path, exist_ok=True)
    db.init_app(app)
    init_csrf(app)
    app.register_blueprint(bp)
    register_error_pages(app)
    return app


def register_error_pages(app: Flask) -> None:
    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def _error(exc: HTTPException) -> tuple[str, int]:
        return render_template("error.html", error=exc), exc.code or 500
