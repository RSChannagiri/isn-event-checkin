# ISN Event Check-in

A QR code check-in app for the International Student Network (ISN) at Santa Clara University. Officers create an event, project its QR code at the door, and members check in from their phones with their @scu.edu email. Officers get a live attendee list, late and walk-in flags, one-click CSV export, and a post-event feedback survey link for every attendee, all rolled up into per-event summary stats.

## The problem

At ISN events we tracked attendance on a paper sign-in sheet or a shared Google Form. That caused three recurring issues:

1. **Dirty data.** Personal emails, typos, and the same person signing in twice made the attendance numbers unreliable for officer reports and funding requests.
2. **No timing signal.** We could not tell who arrived on time and who came late, so we could not decide whether start times were working.
3. **Feedback was an afterthought.** Survey links went out days later to a generic list, response rates were low, and responses could not be tied back to a specific event.

This app moves all of that to a single flow: scan, check in, get flagged correctly, and receive a personal feedback link.

## Features

| Area | What it does |
| --- | --- |
| Events | Officers create events with a location, start time, check-in window, and a late threshold in minutes. |
| Signed QR codes | Each event gets a QR code (generated with `qrcode`) pointing to a signed check-in URL. Raw or tampered IDs return 404, so nobody can guess another event's link. |
| Member check-in | Requires a valid `@scu.edu` email (normalized to lowercase), dedupes per event, enforces the open/close window, and flags late arrivals. |
| Walk-ins | Officers can register a walk-in from the admin page, even after the member window closes. Walk-ins are flagged separately. |
| Live admin view | The attendee table and stat tiles refresh every 10 seconds from a JSON endpoint without reloading the page. |
| CSV export | Download attendees with late, walk-in, rating, and comment columns. Cells are protected against spreadsheet formula injection. |
| Feedback survey | Every attendee gets a unique signed link to a 1 to 5 rating plus optional comment. One response per attendee. |
| Summary stats | Total, on time, late, walk-ins, average rating, feedback response rate, rating distribution, and an arrival curve in 15 minute buckets. |
| Security | Admin login uses a token from an env var (constant-time compare), all forms are CSRF protected, cookies are HttpOnly and SameSite=Lax, open redirects are blocked, and user input is HTML escaped. |

## Architecture

```
checkin/
  __init__.py     app factory, config from env vars, error pages
  services.py     business rules: validation, check-in window, late flag, stats, CSV (no Flask imports)
  db.py           SQLite schema and per-request connection
  security.py     admin token auth, signed tokens (itsdangerous), CSRF
  qr.py           QR PNG rendering
  routes.py       thin HTTP layer that calls services
  templates/      Jinja templates
  static/         one small stylesheet, no framework
scripts/seed.py   Faker-generated demo events, members, and feedback
tests/            pytest suite (services, routes, seed)
wsgi.py           entry point
```

Key design choices:

- **Logic lives in `services.py`** and takes a plain `sqlite3.Connection` and a `now` timestamp. That makes every rule (window boundaries, late threshold, dedupe) testable without a web server or a real clock.
- **Dedupe is enforced twice.** The service returns "already checked in" for a repeat scan, and a `UNIQUE(event_id, email)` constraint in SQLite backs it up.
- **Signed links instead of login for members.** Members never create accounts. Check-in and feedback links carry an HMAC signed ID with different salts, so a feedback token cannot be replayed as a check-in token.

Data model:

```
events (id, name, location, starts_at, opens_at, closes_at, late_after_minutes)
attendees (id, event_id -> events, email, name, checked_in_at, is_late, is_walk_in)   UNIQUE(event_id, email)
feedback (id, attendee_id -> attendees UNIQUE, rating 1..5, comment, submitted_at)
```

## Run it locally

Requires Python 3.12.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export ADMIN_TOKEN="choose-a-long-random-token"
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"

python scripts/seed.py --reset      # optional: 5 demo events with synthetic members
flask --app wsgi run
```

Open http://127.0.0.1:5000, log in with your `ADMIN_TOKEN`, and pick an event.

Configuration (env vars):

| Variable | Purpose | Default |
| --- | --- | --- |
| `ADMIN_TOKEN` | Officer login token | `dev-admin-token` (logs a warning) |
| `SECRET_KEY` | Signs sessions and QR / feedback links | `dev-secret-change-me` (logs a warning) |
| `DATABASE` | SQLite file path | `instance/checkin.db` |
| `BASE_URL` | Public URL used inside QR codes, for example `https://checkin.isn-scu.org` | request host |
| `SESSION_COOKIE_SECURE` | Set to `1` when served over HTTPS | `0` |

Changing `SECRET_KEY` invalidates every printed QR code and feedback link, so set it once per deployment.

## Test

```bash
pytest -q          # 113 tests
ruff check .
ruff format --check .
```

The suite covers email and name validation, check-in window boundaries to the minute, late threshold edges, dedupe (including case differences), walk-ins after close, feedback rules, stats math, CSV output and formula injection, database constraints and cascades, admin auth, CSRF rejection, open redirect protection, token tampering, HTML escaping, and the seed script. Time is injected through a patched clock so tests are deterministic. The same lint and test commands are intended to run in GitHub Actions on Python 3.12.

## Tech stack

Python 3.12, Flask, SQLite, Jinja2, itsdangerous, qrcode + Pillow, Faker (synthetic data), pytest, ruff.

All demo data is synthetic. No real ISN members appear in this repository.

## What I learned

- **Define the metric before the tool.** "Attendance" was not enough. Officers really wanted on time versus late, walk-ins versus registered, and feedback response rate. Writing those down first shaped the data model, the late threshold setting, and the stat tiles.
- **Data quality is a product decision.** Requiring a school email and deduping at the database level costs members almost nothing, yet it makes every downstream report trustworthy. That mirrors what I saw in healthcare operations, where clean intake data mattered more than fancy dashboards.
- **Edge cases are where trust is won.** What happens one minute after check-in closes, or when someone scans twice, or when a latecomer has no phone? Each of those became an explicit rule (closed message, friendly "already checked in", officer walk-in) and a test.
- **Separate logic from UI.** Keeping rules in a plain Python module meant I could test them like a spreadsheet model, change the UI freely, and explain the logic to non-technical officers.
- **Security basics are part of shipping.** Signed links, CSRF tokens, and formula-safe CSV exports are small, but skipping them would put member data at risk the moment the app is shared outside the board.
