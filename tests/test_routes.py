from __future__ import annotations

import csv
import io
import re
from datetime import timedelta

import pytest
from flask import Flask
from flask.testing import FlaskClient

from checkin import services
from checkin.db import get_db
from checkin.security import CHECKIN_SALT, FEEDBACK_SALT, sign

from .conftest import CSRF, START, Clock


def token(app: Flask, value: int, salt: str) -> str:
    with app.test_request_context():
        return sign(value, salt)


def checkin_member(client: FlaskClient, app: Flask, event_id: int, email: str, name: str = "Ana"):
    return client.post(
        f"/c/{token(app, event_id, CHECKIN_SALT)}",
        data={"email": email, "name": name, "csrf_token": CSRF},
    )


def attendee_ids(app: Flask, event_id: int) -> list[int]:
    with app.app_context():
        return [r["id"] for r in services.list_attendees(get_db(), event_id)]


# ---- auth ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/admin/events",
        "/admin/events/1",
        "/admin/events/1/qr.png",
        "/admin/events/1/export.csv",
        "/admin/events/1/attendees.json",
    ],
)
def test_admin_pages_require_login(client: FlaskClient, path: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_login_with_valid_token(client: FlaskClient) -> None:
    resp = client.post(
        "/login?next=/admin/events", data={"token": "s3cret-token", "csrf_token": CSRF}
    )
    assert resp.status_code == 302 and resp.headers["Location"] == "/admin/events"
    with client.session_transaction() as s:
        assert s["is_admin"] is True


def test_login_with_wrong_token(client: FlaskClient) -> None:
    resp = client.post("/login", data={"token": "nope", "csrf_token": CSRF})
    assert resp.status_code == 200 and b"Invalid admin token" in resp.data
    with client.session_transaction() as s:
        assert "is_admin" not in s


@pytest.mark.parametrize("target", ["https://evil.example", "//evil.example", "javascript:x"])
def test_login_blocks_open_redirect(client: FlaskClient, target: str) -> None:
    resp = client.post(f"/login?next={target}", data={"token": "s3cret-token", "csrf_token": CSRF})
    assert resp.headers["Location"] == "/admin/events"


def test_logout_clears_admin(admin: FlaskClient) -> None:
    admin.post("/logout", data={"csrf_token": CSRF})
    assert admin.get("/admin/events").status_code == 302


# ---- csrf --------------------------------------------------------------------


def test_post_without_csrf_is_rejected(client: FlaskClient) -> None:
    resp = client.post("/login", data={"token": "s3cret-token"})
    assert resp.status_code == 400 and b"form session expired" in resp.data


def test_post_with_wrong_csrf_is_rejected(admin: FlaskClient) -> None:
    resp = admin.post("/admin/events", data={"name": "X", "csrf_token": "forged"})
    assert resp.status_code == 400


def test_forms_render_csrf_token(client: FlaskClient) -> None:
    html = client.get("/login").get_data(as_text=True)
    assert f'name="csrf_token" value="{CSRF}"' in html


# ---- admin events -------------------------------------------------------------


def test_create_event_via_form(admin: FlaskClient, app: Flask) -> None:
    resp = admin.post(
        "/admin/events",
        data={
            "name": "Tableau Workshop",
            "location": "Lucas 207",
            "opens_at": "2026-10-05T17:30",
            "starts_at": "2026-10-05T18:00",
            "closes_at": "2026-10-05T19:30",
            "late_after_minutes": "15",
            "csrf_token": CSRF,
        },
    )
    assert resp.status_code == 302
    page = admin.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Tableau Workshop" in page and "late after 15 min" in page


def test_create_event_invalid_shows_error(admin: FlaskClient) -> None:
    resp = admin.post(
        "/admin/events",
        data={
            "name": "Bad",
            "opens_at": "2026-10-05T19:00",
            "starts_at": "2026-10-05T18:00",
            "closes_at": "2026-10-05T18:30",
            "late_after_minutes": "abc",
            "csrf_token": CSRF,
        },
    )
    assert resp.status_code == 200 and b"whole number" in resp.data


def test_events_list_shows_count(admin: FlaskClient, app: Flask, app_event: int) -> None:
    checkin_member(admin, app, app_event, "a@scu.edu")
    html = admin.get("/admin/events").get_data(as_text=True)
    assert "PM Panel" in html and "<td>1</td>" in html


def test_admin_event_404(admin: FlaskClient) -> None:
    assert admin.get("/admin/events/999").status_code == 404


def test_admin_event_page_has_qr_and_checkin_link(
    admin: FlaskClient, app: Flask, app_event: int
) -> None:
    html = admin.get(f"/admin/events/{app_event}").get_data(as_text=True)
    assert f"https://checkin.example.org/c/{token(app, app_event, CHECKIN_SALT)}" in html
    assert f"/admin/events/{app_event}/qr.png" in html


def test_qr_png(admin: FlaskClient, app_event: int) -> None:
    resp = admin.get(f"/admin/events/{app_event}/qr.png")
    assert resp.mimetype == "image/png" and resp.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_walk_in_registration(admin: FlaskClient, app: Flask, app_event: int, clock: Clock) -> None:
    clock.value = START + timedelta(hours=5)  # after window closed
    resp = admin.post(
        f"/admin/events/{app_event}",
        data={"name": "Walk In", "email": "walk@scu.edu", "csrf_token": CSRF},
        follow_redirects=True,
    )
    assert b"Walk-in registered" in resp.data and b"walk@scu.edu" in resp.data


def test_walk_in_invalid_email(admin: FlaskClient, app_event: int) -> None:
    resp = admin.post(
        f"/admin/events/{app_event}",
        data={"name": "X", "email": "x@stanford.edu", "csrf_token": CSRF},
        follow_redirects=True,
    )
    assert b"valid @scu.edu" in resp.data


def test_attendees_json_live_feed(admin: FlaskClient, app: Flask, app_event: int) -> None:
    checkin_member(admin, app, app_event, "a@scu.edu", "Ana")
    data = admin.get(f"/admin/events/{app_event}/attendees.json").get_json()
    assert data["attendees"][0]["email"] == "a@scu.edu"
    assert data["attendees"][0]["feedback_url"].startswith("https://checkin.example.org/f/")
    assert data["stats"]["total"] == 1


def test_csv_export(admin: FlaskClient, app: Flask, app_event: int) -> None:
    checkin_member(admin, app, app_event, "a@scu.edu", "Ana")
    resp = admin.get(f"/admin/events/{app_event}/export.csv")
    assert resp.mimetype == "text/csv"
    assert f"event-{app_event}-attendees.csv" in resp.headers["Content-Disposition"]
    rows = list(csv.reader(io.StringIO(resp.get_data(as_text=True))))
    assert rows[1][:2] == ["Ana", "a@scu.edu"]


# ---- member check-in ------------------------------------------------------------


def test_checkin_page_open(client: FlaskClient, app: Flask, app_event: int) -> None:
    html = client.get(f"/c/{token(app, app_event, CHECKIN_SALT)}").get_data(as_text=True)
    assert "PM Panel" in html and 'name="email"' in html


def test_checkin_success(client: FlaskClient, app: Flask, app_event: int) -> None:
    resp = checkin_member(client, app, app_event, "Ana@SCU.edu")
    assert b"checked in!" in resp.data
    assert len(attendee_ids(app, app_event)) == 1


def test_checkin_duplicate_is_friendly(client: FlaskClient, app: Flask, app_event: int) -> None:
    checkin_member(client, app, app_event, "ana@scu.edu")
    resp = checkin_member(client, app, app_event, "ana@scu.edu")
    assert b"already checked in" in resp.data
    assert len(attendee_ids(app, app_event)) == 1


def test_checkin_late_message(
    client: FlaskClient, app: Flask, app_event: int, clock: Clock
) -> None:
    clock.value = START + timedelta(minutes=30)
    assert b"arriving late" in checkin_member(client, app, app_event, "ana@scu.edu").data


def test_checkin_rejects_non_scu(client: FlaskClient, app: Flask, app_event: int) -> None:
    resp = checkin_member(client, app, app_event, "ana@gmail.com")
    assert b"valid @scu.edu" in resp.data and attendee_ids(app, app_event) == []


def test_checkin_closed_window(
    client: FlaskClient, app: Flask, app_event: int, clock: Clock
) -> None:
    clock.value = START + timedelta(hours=4)
    tok = token(app, app_event, CHECKIN_SALT)
    assert b"Check-in is closed" in client.get(f"/c/{tok}").data
    assert b"Check-in is closed" in checkin_member(client, app, app_event, "a@scu.edu").data


def test_checkin_not_open_yet(
    client: FlaskClient, app: Flask, app_event: int, clock: Clock
) -> None:
    clock.value = START - timedelta(hours=2)
    html = client.get(f"/c/{token(app, app_event, CHECKIN_SALT)}").get_data(as_text=True)
    assert "Check-in opens at 2026-10-01 17:30" in html


def test_checkin_rejects_tampered_or_raw_id(
    client: FlaskClient, app: Flask, app_event: int
) -> None:
    tok = token(app, app_event, CHECKIN_SALT)
    assert client.get(f"/c/{tok[:-2]}xx").status_code == 404
    assert client.get(f"/c/{app_event}").status_code == 404


def test_feedback_token_cannot_be_used_for_checkin(
    client: FlaskClient, app: Flask, app_event: int
) -> None:
    assert client.get(f"/c/{token(app, app_event, FEEDBACK_SALT)}").status_code == 404


def test_checkin_signed_token_for_missing_event(client: FlaskClient, app: Flask) -> None:
    assert client.get(f"/c/{token(app, 999, CHECKIN_SALT)}").status_code == 404


# ---- feedback -----------------------------------------------------------------


def test_feedback_flow(client: FlaskClient, app: Flask, app_event: int) -> None:
    checkin_member(client, app, app_event, "ana@scu.edu", "Ana Lopez")
    (aid,) = attendee_ids(app, app_event)
    url = f"/f/{token(app, aid, FEEDBACK_SALT)}"
    assert b"Feedback for Ana Lopez" in client.get(url).data
    resp = client.post(url, data={"rating": "5", "comment": "Loved it", "csrf_token": CSRF})
    assert b"Thanks for your feedback" in resp.data
    again = client.post(url, data={"rating": "1", "comment": "", "csrf_token": CSRF})
    assert b"already submitted" in again.data
    with app.app_context():
        assert services.event_stats(get_db(), app_event).avg_rating == 5


def test_feedback_invalid_rating(client: FlaskClient, app: Flask, app_event: int) -> None:
    checkin_member(client, app, app_event, "ana@scu.edu")
    (aid,) = attendee_ids(app, app_event)
    resp = client.post(
        f"/f/{token(app, aid, FEEDBACK_SALT)}", data={"rating": "9", "csrf_token": CSRF}
    )
    assert b"1 to 5" in resp.data


def test_feedback_bad_token(client: FlaskClient, app: Flask) -> None:
    assert client.get("/f/garbage").status_code == 404
    assert client.get(f"/f/{token(app, 12345, FEEDBACK_SALT)}").status_code == 404


def test_feedback_links_listed_for_admin(admin: FlaskClient, app: Flask, app_event: int) -> None:
    checkin_member(admin, app, app_event, "ana@scu.edu")
    html = admin.get(f"/admin/events/{app_event}").get_data(as_text=True)
    assert re.search(r'value="https://checkin\.example\.org/f/[^"]+"', html)


# ---- misc ------------------------------------------------------------------------


def test_healthz(client: FlaskClient) -> None:
    assert client.get("/healthz").get_json() == {"status": "ok"}


def test_index_redirects(client: FlaskClient) -> None:
    assert client.get("/").status_code == 302


def test_user_input_is_escaped(admin: FlaskClient, app: Flask, app_event: int) -> None:
    checkin_member(admin, app, app_event, "x@scu.edu", "<script>alert(1)</script>")
    html = admin.get(f"/admin/events/{app_event}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_session_cookie_hardening(app: Flask) -> None:
    assert app.config["SESSION_COOKIE_HTTPONLY"] and app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
