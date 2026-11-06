from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timedelta

import pytest

from checkin import services
from checkin.services import ValidationError

START = datetime(2026, 10, 1, 18, 0)


# ---- validation helpers -------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Jane.Doe@SCU.EDU", "jane.doe@scu.edu"),
        ("  jdoe@scu.edu  ", "jdoe@scu.edu"),
        ("a+isn@scu.edu", "a+isn@scu.edu"),
    ],
)
def test_normalize_email_accepts_scu(raw: str, expected: str) -> None:
    assert services.normalize_email(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "jdoe@gmail.com",
        "jdoe@scu.edu.evil.com",
        "jdoe@alumni.scu.edu",
        "@scu.edu",
        "j doe@scu.edu",
        "jdoe@scu,edu",
        None,
    ],
)
def test_normalize_email_rejects_non_scu(raw: str | None) -> None:
    with pytest.raises(ValidationError):
        services.normalize_email(raw)  # type: ignore[arg-type]


def test_clean_name_collapses_whitespace() -> None:
    assert services.clean_name("  Ana   Maria\tLopez ") == "Ana Maria Lopez"


@pytest.mark.parametrize("raw", ["", "   ", "x" * 101])
def test_clean_name_rejects_blank_or_long(raw: str) -> None:
    with pytest.raises(ValidationError):
        services.clean_name(raw)


def test_parse_time_rejects_garbage() -> None:
    with pytest.raises(ValidationError):
        services.parse_time("tomorrow at 6")


@pytest.mark.parametrize("raw, expected", [(None, 10), ("", 10), (" ", 10), ("5", 5), (0, 0)])
def test_parse_minutes(raw: str | int | None, expected: int) -> None:
    assert services.parse_minutes(raw) == expected


def test_parse_minutes_rejects_text() -> None:
    with pytest.raises(ValidationError, match="whole number"):
        services.parse_minutes("ten")


# ---- events -------------------------------------------------------------


def test_create_event_persists_normalized_times(conn: sqlite3.Connection, event_id: int) -> None:
    row = services.get_event(conn, event_id)
    assert row["name"] == "Fall Mixer"
    assert row["starts_at"] == "2026-10-01T18:00"
    assert row["late_after_minutes"] == 10


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"name": "  "}, "name is required"),
        ({"closes_at": "2026-10-01T17:00"}, "close after it opens"),
        ({"starts_at": "2026-10-01T21:00"}, "inside the check-in window"),
        ({"late_after_minutes": -1}, "cannot be negative"),
        ({"name": "x" * 121}, "120 characters"),
        ({"opens_at": "not-a-time"}, "Invalid date"),
    ],
)
def test_create_event_validation(conn: sqlite3.Connection, kwargs: dict, message: str) -> None:
    args = {
        "name": "Mixer",
        "location": "",
        "starts_at": "2026-10-01T18:00",
        "opens_at": "2026-10-01T17:30",
        "closes_at": "2026-10-01T20:00",
        "late_after_minutes": 10,
    }
    args.update(kwargs)
    with pytest.raises(ValidationError, match=message):
        services.create_event(conn, **args)


def test_list_events_counts_attendees_newest_first(conn: sqlite3.Connection, event_id: int) -> None:
    later = services.create_event(
        conn, "Later", "", "2026-11-01T18:00", "2026-11-01T17:00", "2026-11-01T19:00"
    )
    services.check_in(conn, event_id, "a@scu.edu", "A", START)
    services.check_in(conn, event_id, "b@scu.edu", "B", START)
    rows = services.list_events(conn)
    assert [r["id"] for r in rows] == [later, event_id]
    assert rows[1]["attendee_count"] == 2 and rows[0]["attendee_count"] == 0


def test_get_event_missing_returns_none(conn: sqlite3.Connection) -> None:
    assert services.get_event(conn, 999) is None


# ---- window + late --------------------------------------------------------


@pytest.mark.parametrize(
    "offset_min, expected",
    [(-31, "not_open"), (-30, "open"), (0, "open"), (120, "open"), (121, "closed")],
)
def test_window_status_boundaries(
    conn: sqlite3.Connection, event_id: int, offset_min: int, expected: str
) -> None:
    event = services.get_event(conn, event_id)
    assert services.window_status(event, START + timedelta(minutes=offset_min)) == expected


@pytest.mark.parametrize("offset_min, late", [(-5, False), (10, False), (11, True)])
def test_is_late_threshold(
    conn: sqlite3.Connection, event_id: int, offset_min: int, late: bool
) -> None:
    event = services.get_event(conn, event_id)
    assert services.is_late(event, START + timedelta(minutes=offset_min)) is late


# ---- check-in -------------------------------------------------------------


def test_check_in_records_attendee(conn: sqlite3.Connection, event_id: int) -> None:
    result = services.check_in(conn, event_id, "Maya@scu.edu", "Maya Patel", START)
    assert not result.already_checked_in and not result.is_late
    row = services.get_attendee(conn, result.attendee_id)
    assert row["email"] == "maya@scu.edu" and row["is_walk_in"] == 0


def test_check_in_dedupes_case_insensitive(conn: sqlite3.Connection, event_id: int) -> None:
    first = services.check_in(conn, event_id, "maya@scu.edu", "Maya", START)
    again = services.check_in(conn, event_id, "MAYA@scu.edu", "Maya P", START + timedelta(hours=1))
    assert again.already_checked_in and again.attendee_id == first.attendee_id
    assert len(services.list_attendees(conn, event_id)) == 1


def test_same_email_can_attend_different_events(conn: sqlite3.Connection, event_id: int) -> None:
    other = services.create_event(
        conn, "Other", "", "2026-10-02T18:00", "2026-10-02T17:00", "2026-10-02T19:00"
    )
    services.check_in(conn, event_id, "a@scu.edu", "A", START)
    r = services.check_in(conn, other, "a@scu.edu", "A", datetime(2026, 10, 2, 18, 0))
    assert not r.already_checked_in


def test_check_in_marks_late(conn: sqlite3.Connection, event_id: int) -> None:
    r = services.check_in(conn, event_id, "a@scu.edu", "A", START + timedelta(minutes=25))
    assert r.is_late


def test_check_in_rejects_before_window(conn: sqlite3.Connection, event_id: int) -> None:
    with pytest.raises(ValidationError, match="not opened"):
        services.check_in(conn, event_id, "a@scu.edu", "A", START - timedelta(hours=1))


def test_check_in_rejects_after_window(conn: sqlite3.Connection, event_id: int) -> None:
    with pytest.raises(ValidationError, match="closed"):
        services.check_in(conn, event_id, "a@scu.edu", "A", START + timedelta(hours=3))


def test_walk_in_allowed_after_close_and_flagged(conn: sqlite3.Connection, event_id: int) -> None:
    r = services.check_in(
        conn, event_id, "a@scu.edu", "A", START + timedelta(hours=3), walk_in=True
    )
    row = services.get_attendee(conn, r.attendee_id)
    assert row["is_walk_in"] == 1 and row["is_late"] == 1


def test_check_in_unknown_event(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError, match="not found"):
        services.check_in(conn, 42, "a@scu.edu", "A", START)


def test_check_in_invalid_email_writes_nothing(conn: sqlite3.Connection, event_id: int) -> None:
    with pytest.raises(ValidationError):
        services.check_in(conn, event_id, "a@gmail.com", "A", START)
    assert services.list_attendees(conn, event_id) == []


def test_db_unique_constraint_backstops_dedupe(conn: sqlite3.Connection, event_id: int) -> None:
    services.check_in(conn, event_id, "a@scu.edu", "A", START)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO attendees (event_id, email, name, checked_in_at) VALUES (?, ?, ?, ?)",
            (event_id, "a@scu.edu", "Dup", "2026-10-01T18:00:00"),
        )


# ---- feedback -------------------------------------------------------------


def test_submit_feedback_and_join(conn: sqlite3.Connection, event_id: int) -> None:
    r = services.check_in(conn, event_id, "a@scu.edu", "A", START)
    services.submit_feedback(conn, r.attendee_id, "4", "  Great panel ", START)
    row = services.list_attendees(conn, event_id)[0]
    assert row["rating"] == 4 and row["comment"] == "Great panel"


@pytest.mark.parametrize("rating", ["0", "6", "abc", "", None, "4.5"])
def test_submit_feedback_rejects_bad_rating(
    conn: sqlite3.Connection, event_id: int, rating: str | None
) -> None:
    r = services.check_in(conn, event_id, "a@scu.edu", "A", START)
    with pytest.raises(ValidationError, match="1 to 5"):
        services.submit_feedback(conn, r.attendee_id, rating, "", START)  # type: ignore[arg-type]


def test_submit_feedback_once_only(conn: sqlite3.Connection, event_id: int) -> None:
    r = services.check_in(conn, event_id, "a@scu.edu", "A", START)
    services.submit_feedback(conn, r.attendee_id, 5, "", START)
    with pytest.raises(ValidationError, match="already submitted"):
        services.submit_feedback(conn, r.attendee_id, 1, "changed my mind", START)


def test_submit_feedback_comment_too_long(conn: sqlite3.Connection, event_id: int) -> None:
    r = services.check_in(conn, event_id, "a@scu.edu", "A", START)
    with pytest.raises(ValidationError, match="1000"):
        services.submit_feedback(conn, r.attendee_id, 3, "x" * 1001, START)


def test_submit_feedback_unknown_attendee(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError, match="not found"):
        services.submit_feedback(conn, 7, 3, "", START)


# ---- stats + csv --------------------------------------------------------------


def _populate(conn: sqlite3.Connection, event_id: int) -> None:
    a = services.check_in(conn, event_id, "a@scu.edu", "A", START - timedelta(minutes=10))
    b = services.check_in(conn, event_id, "b@scu.edu", "B", START + timedelta(minutes=5))
    services.check_in(conn, event_id, "c@scu.edu", "C", START + timedelta(minutes=20))
    services.check_in(conn, event_id, "d@scu.edu", "D", START + timedelta(minutes=40), walk_in=True)
    services.submit_feedback(conn, a.attendee_id, 5, "", START)
    services.submit_feedback(conn, b.attendee_id, 4, "", START)


def test_event_stats(conn: sqlite3.Connection, event_id: int) -> None:
    _populate(conn, event_id)
    s = services.event_stats(conn, event_id)
    assert (s.total, s.on_time, s.late, s.walk_ins) == (4, 2, 2, 1)
    assert s.feedback_count == 2 and s.avg_rating == 4.5 and s.response_rate == 0.5
    assert s.rating_counts == {1: 0, 2: 0, 3: 0, 4: 1, 5: 1}
    assert s.arrivals == {"-15 to 0": 1, "0 to 15": 1, "15 to 30": 1, "30 to 45": 1}


def test_event_stats_empty_event(conn: sqlite3.Connection, event_id: int) -> None:
    s = services.event_stats(conn, event_id)
    assert s.total == 0 and s.avg_rating is None and s.response_rate == 0.0 and s.arrivals == {}


def test_event_stats_unknown_event(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValidationError):
        services.event_stats(conn, 404)


def test_stats_as_dict_is_json_friendly(conn: sqlite3.Connection, event_id: int) -> None:
    _populate(conn, event_id)
    d = services.event_stats(conn, event_id).as_dict()
    assert d["rating_counts"]["5"] == 1 and d["total"] == 4


def test_arrival_buckets_ordering_and_custom_width() -> None:
    times = ["2026-10-01T18:31:00", "2026-10-01T17:59:59", "2026-10-01T18:00:00"]
    assert services.arrival_buckets(times, START, 30) == {
        "-30 to 0": 1,
        "0 to 30": 1,
        "30 to 60": 1,
    }
    with pytest.raises(ValueError):
        services.arrival_buckets(times, START, 0)


def test_attendees_csv(conn: sqlite3.Connection, event_id: int) -> None:
    _populate(conn, event_id)
    rows = list(
        csv.reader(io.StringIO(services.attendees_csv(services.list_attendees(conn, event_id))))
    )
    assert rows[0] == services.CSV_HEADER
    assert len(rows) == 5
    assert rows[1][:2] == ["A", "a@scu.edu"] and rows[1][5] == "5"
    assert rows[4][3:5] == ["yes", "yes"]


def test_csv_neutralizes_formula_injection(conn: sqlite3.Connection, event_id: int) -> None:
    r = services.check_in(conn, event_id, "a@scu.edu", '=HYPERLINK("x")', START)
    services.submit_feedback(conn, r.attendee_id, 3, "@SUM(A1)", START)
    out = list(
        csv.reader(io.StringIO(services.attendees_csv(services.list_attendees(conn, event_id))))
    )
    assert out[1][0].startswith("'=") and out[1][6] == "'@SUM(A1)"


def test_deleting_event_cascades(conn: sqlite3.Connection, event_id: int) -> None:
    r = services.check_in(conn, event_id, "a@scu.edu", "A", START)
    services.submit_feedback(conn, r.attendee_id, 3, "", START)
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    assert conn.execute("SELECT COUNT(*) FROM attendees").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0


def test_rating_check_constraint(conn: sqlite3.Connection, event_id: int) -> None:
    r = services.check_in(conn, event_id, "a@scu.edu", "A", START)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO feedback (attendee_id, rating, submitted_at) VALUES (?, 9, 'x')",
            (r.attendee_id,),
        )
