"""Admin token auth, signed links, and CSRF protection."""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import Flask, abort, current_app, redirect, request, session, url_for
from itsdangerous import BadSignature, URLSafeSerializer

CHECKIN_SALT = "event-checkin"
FEEDBACK_SALT = "attendee-feedback"


def _serializer(salt: str) -> URLSafeSerializer:
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt=salt)


def sign(value: int, salt: str) -> str:
    return _serializer(salt).dumps(value)


def unsign(token: str, salt: str) -> int:
    try:
        value = _serializer(salt).loads(token)
    except BadSignature:
        abort(404)
    if not isinstance(value, int):
        abort(404)
    return value


def token_matches(candidate: str) -> bool:
    expected = current_app.config["ADMIN_TOKEN"]
    return bool(candidate) and hmac.compare_digest(candidate.encode(), expected.encode())


def admin_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not session.get("is_admin"):
            return redirect(url_for("main.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def init_csrf(app: Flask) -> None:
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def _check_csrf() -> None:
        if request.method != "POST" or app.config.get("CSRF_DISABLED"):
            return
        sent = request.form.get("csrf_token", "")
        stored = session.get("csrf", "")
        if not stored or not hmac.compare_digest(sent, stored):
            abort(400, "Invalid CSRF token")
