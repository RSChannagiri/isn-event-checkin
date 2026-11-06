"""Business logic: validation, check-in rules, stats. No Flask imports."""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

EMAIL_RE = re.compile(r"^[a-z0-9._%+-]+@scu\.edu$")
TIME_FMT = "%Y-%m-%dT%H:%M"


class ValidationError(ValueError):
    """Raised when user input breaks a business rule."""


@dataclass(frozen=True)
class CheckinResult:
    attendee_id: int
    is_late: bool
    already_checked_in: bool


def normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise ValidationError("Please use a valid @scu.edu email address.")
    return email


def clean_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if not name:
        raise ValidationError("Name is required.")
    if len(name) > 100:
        raise ValidationError("Name must be 100 characters or fewer.")
    return name


def parse_minutes(raw: str | int | None, default: int = 10) -> int:
    """Parse the late-threshold form field."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Late threshold must be a whole number of minutes.") from exc


def parse_time(raw: str) -> datetime:
    try:
        return datetime.strptime((raw or "").strip(), TIME_FMT)
    except ValueError as exc:
        raise ValidationError(f"Invalid date/time: {raw!r}") from exc


def create_event(
    conn: sqlite3.Connection,
    name: str,
    location: str,
    starts_at: str,
    opens_at: str,
    closes_at: str,
    late_after_minutes: int = 10,
) -> int:
    name = (name or "").strip()
    if not name:
        raise ValidationError("Event name is required.")
    start, opens, closes = parse_time(starts_at), parse_time(opens_at), parse_time(closes_at)
    if closes <= opens:
        raise ValidationError("Check-in must close after it opens.")
    if not opens <= start <= closes:
        raise ValidationError("Event start must fall inside the check-in window.")
    if len(name) > 120:
        raise ValidationError("Event name must be 120 characters or fewer.")
    if late_after_minutes < 0:
        raise ValidationError("Late threshold cannot be negative.")
    cur = conn.execute(
        "INSERT INTO events (name, location, starts_at, opens_at, closes_at, late_after_minutes)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            name,
            (location or "").strip(),
            start.strftime(TIME_FMT),
            opens.strftime(TIME_FMT),
            closes.strftime(TIME_FMT),
            late_after_minutes,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def list_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT e.*, COUNT(a.id) AS attendee_count FROM events e"
        " LEFT JOIN attendees a ON a.event_id = e.id GROUP BY e.id ORDER BY e.starts_at DESC"
    ).fetchall()


def window_status(event: sqlite3.Row, now: datetime) -> str:
    """Return 'not_open', 'open', or 'closed'."""
    if now < parse_time(event["opens_at"]):
        return "not_open"
    if now > parse_time(event["closes_at"]):
        return "closed"
    return "open"


def is_late(event: sqlite3.Row, now: datetime) -> bool:
    cutoff = parse_time(event["starts_at"]) + timedelta(minutes=event["late_after_minutes"])
    return now > cutoff


def check_in(
    conn: sqlite3.Connection,
    event_id: int,
    email: str,
    name: str,
    now: datetime,
    walk_in: bool = False,
) -> CheckinResult:
    event = get_event(conn, event_id)
    if event is None:
        raise ValidationError("Event not found.")
    email = normalize_email(email)
    name = clean_name(name)
    existing = conn.execute(
        "SELECT id, is_late FROM attendees WHERE event_id = ? AND email = ?", (event_id, email)
    ).fetchone()
    if existing:
        return CheckinResult(existing["id"], bool(existing["is_late"]), True)
    # Officers can register walk-ins after the window closes; members cannot.
    status = window_status(event, now)
    if not walk_in and status != "open":
        msg = "Check-in has not opened yet." if status == "not_open" else "Check-in is closed."
        raise ValidationError(msg)
    late = is_late(event, now)
    cur = conn.execute(
        "INSERT INTO attendees (event_id, email, name, checked_in_at, is_late, is_walk_in)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, email, name, now.isoformat(timespec="seconds"), int(late), int(walk_in)),
    )
    conn.commit()
    return CheckinResult(int(cur.lastrowid), late, False)


def list_attendees(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.*, f.rating, f.comment FROM attendees a"
        " LEFT JOIN feedback f ON f.attendee_id = a.id"
        " WHERE a.event_id = ? ORDER BY a.checked_in_at",
        (event_id,),
    ).fetchall()


def get_attendee(conn: sqlite3.Connection, attendee_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM attendees WHERE id = ?", (attendee_id,)).fetchone()


def submit_feedback(
    conn: sqlite3.Connection, attendee_id: int, rating: int | str, comment: str, now: datetime
) -> None:
    if get_attendee(conn, attendee_id) is None:
        raise ValidationError("Attendee not found.")
    try:
        value = int(rating)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Rating must be a number from 1 to 5.") from exc
    if not 1 <= value <= 5:
        raise ValidationError("Rating must be a number from 1 to 5.")
    comment = (comment or "").strip()
    if len(comment) > 1000:
        raise ValidationError("Comment must be 1000 characters or fewer.")
    try:
        conn.execute(
            "INSERT INTO feedback (attendee_id, rating, comment, submitted_at) VALUES (?, ?, ?, ?)",
            (attendee_id, value, comment, now.isoformat(timespec="seconds")),
        )
    except sqlite3.IntegrityError as exc:
        raise ValidationError("Feedback was already submitted. Thank you!") from exc
    conn.commit()


@dataclass(frozen=True)
class EventStats:
    total: int
    on_time: int
    late: int
    walk_ins: int
    feedback_count: int
    avg_rating: float | None
    response_rate: float
    rating_counts: dict[int, int]
    arrivals: dict[str, int]

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "on_time": self.on_time,
            "late": self.late,
            "walk_ins": self.walk_ins,
            "feedback_count": self.feedback_count,
            "avg_rating": self.avg_rating,
            "response_rate": self.response_rate,
            "rating_counts": {str(k): v for k, v in self.rating_counts.items()},
            "arrivals": self.arrivals,
        }


def arrival_buckets(
    checkin_times: list[str], starts_at: datetime, bucket_minutes: int = 15
) -> dict[str, int]:
    """Count check-ins per time bucket relative to event start.

    Keys are labels like "-15 to 0" or "0 to 15" (minutes from start), ordered by time.
    """
    if bucket_minutes <= 0:
        raise ValueError("bucket_minutes must be positive")
    counts: dict[int, int] = {}
    for raw in checkin_times:
        delta = (datetime.fromisoformat(raw) - starts_at).total_seconds() / 60
        bucket = int(delta // bucket_minutes)
        counts[bucket] = counts.get(bucket, 0) + 1
    return {
        f"{b * bucket_minutes} to {(b + 1) * bucket_minutes}": counts[b] for b in sorted(counts)
    }


def event_stats(conn: sqlite3.Connection, event_id: int) -> EventStats:
    event = get_event(conn, event_id)
    if event is None:
        raise ValidationError("Event not found.")
    rows = list_attendees(conn, event_id)
    total = len(rows)
    late = sum(1 for r in rows if r["is_late"])
    walk = sum(1 for r in rows if r["is_walk_in"])
    ratings = [r["rating"] for r in rows if r["rating"] is not None]
    counts = {i: ratings.count(i) for i in range(1, 6)}
    return EventStats(
        total=total,
        on_time=total - late,
        late=late,
        walk_ins=walk,
        feedback_count=len(ratings),
        avg_rating=round(sum(ratings) / len(ratings), 2) if ratings else None,
        response_rate=round(len(ratings) / total, 3) if total else 0.0,
        rating_counts=counts,
        arrivals=arrival_buckets(
            [r["checked_in_at"] for r in rows], parse_time(event["starts_at"])
        ),
    )


CSV_HEADER = ["name", "email", "checked_in_at", "late", "walk_in", "rating", "comment"]


def attendees_csv(rows: list[sqlite3.Row]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for r in rows:
        writer.writerow(
            [
                _safe_cell(r["name"]),
                r["email"],
                r["checked_in_at"],
                "yes" if r["is_late"] else "no",
                "yes" if r["is_walk_in"] else "no",
                "" if r["rating"] is None else r["rating"],
                _safe_cell(r["comment"] or ""),
            ]
        )
    return buf.getvalue()


def _safe_cell(value: str) -> str:
    """Neutralize spreadsheet formula injection."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value
