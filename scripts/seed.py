"""Seed the database with realistic synthetic ISN events, attendees, and feedback.

All people are generated with Faker; no real members are used.

    python scripts/seed.py            # add demo data
    python scripts/seed.py --reset    # wipe tables first
"""

from __future__ import annotations

import argparse
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from faker import Faker  # noqa: E402

from checkin import create_app, services  # noqa: E402
from checkin.db import connect  # noqa: E402

EVENTS = [
    ("Fall Kickoff Mixer", "Benson Memorial Center", 0),
    ("Tableau Dashboard Workshop", "Learning Commons 301", 7),
    ("Product Manager Panel", "Locatelli Center", 14),
    ("SQL Interview Prep Night", "Lucas Hall 207", 21),
    ("Diwali Cultural Night", "Mission Gardens", 35),
]
COMMENTS = [
    "Loved the speakers!",
    "Great snacks and networking.",
    "Could be a bit shorter.",
    "Very practical, more workshops like this please.",
    "",
    "",
    "Room was crowded, maybe a bigger space next time.",
    "Super helpful for internship recruiting.",
    "Check-in with the QR code was really fast.",
    "Would love slides shared afterwards.",
]
LOCALES = ["en_US", "en_IN", "es_MX", "vi_VN", "pt_BR", "fr_FR"]


def scu_email(first: str, last: str, n: int) -> str:
    clean = "".join(c for c in (first[0] + last).lower() if c.isascii() and c.isalnum())
    return f"{clean or 'member'}{n}@scu.edu"


def reset(conn: sqlite3.Connection) -> None:
    conn.executescript(
        "DELETE FROM feedback; DELETE FROM attendees; DELETE FROM events;"
        " DELETE FROM sqlite_sequence;"
    )
    conn.commit()


def seed(conn: sqlite3.Connection, seed_value: int = 42) -> dict[str, int]:
    """Insert demo events. Returns counts of inserted rows."""
    rng = random.Random(seed_value)
    fake = Faker(LOCALES)
    Faker.seed(seed_value)
    members: list[tuple[str, str]] = []
    for i in range(120):
        parts = [fake.first_name(), fake.last_name()]
        name = " ".join(parts)
        first, last = parts
        members.append((name, scu_email(first, last, 100 + i)))

    base = datetime(2026, 9, 24, 18, 0)
    totals = {"events": 0, "attendees": 0, "feedback": 0}
    for title, location, offset in EVENTS:
        start = base + timedelta(days=offset)
        event_id = services.create_event(
            conn,
            title,
            location,
            start.strftime(services.TIME_FMT),
            (start - timedelta(minutes=30)).strftime(services.TIME_FMT),
            (start + timedelta(hours=2)).strftime(services.TIME_FMT),
            late_after_minutes=rng.choice([5, 10, 15]),
        )
        totals["events"] += 1
        for name, email in rng.sample(members, rng.randint(22, 55)):
            minutes = int(rng.triangular(-25, 50, -8))
            when = start + timedelta(minutes=minutes, seconds=rng.randint(0, 59))
            result = services.check_in(
                conn, event_id, email, name, when, walk_in=rng.random() < 0.08
            )
            totals["attendees"] += 1
            if rng.random() < 0.55:
                services.submit_feedback(
                    conn,
                    result.attendee_id,
                    rng.choices([1, 2, 3, 4, 5], weights=[1, 2, 6, 14, 12])[0],
                    rng.choice(COMMENTS),
                    when + timedelta(hours=3),
                )
                totals["feedback"] += 1
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="delete existing data first")
    args = parser.parse_args()
    app = create_app()
    conn = connect(app.config["DATABASE"])
    if args.reset:
        reset(conn)
    totals = seed(conn)
    conn.close()
    print(
        f"Seeded {totals['events']} events, {totals['attendees']} check-ins, "
        f"{totals['feedback']} feedback responses into {app.config['DATABASE']}"
    )


if __name__ == "__main__":
    main()
