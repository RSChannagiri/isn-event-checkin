from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from checkin import create_app, routes, services
from checkin.db import connect, init_schema

START = datetime(2026, 10, 1, 18, 0)
CSRF = "test-csrf-token"


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = connect(":memory:")
    init_schema(c)
    yield c
    c.close()


@pytest.fixture
def event_id(conn: sqlite3.Connection) -> int:
    return services.create_event(
        conn,
        "Fall Mixer",
        "Benson Center",
        "2026-10-01T18:00",
        "2026-10-01T17:30",
        "2026-10-01T20:00",
        10,
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock(START)
    monkeypatch.setattr(routes, "now", c)
    return c


@pytest.fixture
def app(tmp_path: Path, clock: Clock) -> Flask:
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "test.db"),
            "SECRET_KEY": "test-secret",
            "ADMIN_TOKEN": "s3cret-token",
            "BASE_URL": "https://checkin.example.org",
        }
    )


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    c = app.test_client()
    with c.session_transaction() as s:
        s["csrf"] = CSRF
    return c


@pytest.fixture
def admin(client: FlaskClient) -> FlaskClient:
    with client.session_transaction() as s:
        s["is_admin"] = True
    return client


@pytest.fixture
def app_event(app: Flask) -> int:
    with app.app_context():
        from checkin.db import get_db

        return services.create_event(
            get_db(),
            "PM Panel",
            "Locatelli",
            "2026-10-01T18:00",
            "2026-10-01T17:30",
            "2026-10-01T20:00",
            10,
        )
