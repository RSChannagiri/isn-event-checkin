from __future__ import annotations

import sqlite3

from checkin import services
from scripts.seed import reset, scu_email, seed


def test_seed_generates_valid_realistic_data(conn: sqlite3.Connection) -> None:
    totals = seed(conn, seed_value=7)
    assert totals["events"] == 5 and totals["attendees"] >= 5 * 22
    emails = [r[0] for r in conn.execute("SELECT email FROM attendees")]
    assert all(e.endswith("@scu.edu") for e in emails)
    for event in services.list_events(conn):
        stats = services.event_stats(conn, event["id"])
        assert stats.total == event["attendee_count"] and stats.feedback_count <= stats.total


def test_seed_is_deterministic_and_resettable(conn: sqlite3.Connection) -> None:
    first = seed(conn, seed_value=3)
    reset(conn)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert seed(conn, seed_value=3) == first


def test_scu_email_strips_accents_and_symbols() -> None:
    assert scu_email("José", "O'Brien-Trần", 5) == "jobrientrn5@scu.edu"
    assert services.normalize_email(scu_email("李", "王", 1)) == "member1@scu.edu"
