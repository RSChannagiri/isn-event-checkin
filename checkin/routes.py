"""HTTP routes. Thin layer over services."""

from __future__ import annotations

from datetime import datetime

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import services
from .db import get_db
from .qr import qr_png
from .security import CHECKIN_SALT, FEEDBACK_SALT, admin_required, sign, token_matches, unsign

bp = Blueprint("main", __name__)


def now() -> datetime:
    """Current local time; patched in tests."""
    return datetime.now().replace(microsecond=0)


def external(endpoint: str, **values: object) -> str:
    base = current_app.config.get("BASE_URL")
    if base:
        return base.rstrip("/") + url_for(endpoint, **values)
    return url_for(endpoint, _external=True, **values)


def feedback_url(attendee_id: int) -> str:
    return external("main.feedback", token=sign(attendee_id, FEEDBACK_SALT))


def load_event(event_id: int):
    event = services.get_event(get_db(), event_id)
    if event is None:
        abort(404)
    return event


@bp.get("/healthz")
def healthz():
    get_db().execute("SELECT 1")
    return {"status": "ok"}


@bp.get("/")
def index():
    return redirect(url_for("main.admin_events"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if token_matches(request.form.get("token", "")):
            session["is_admin"] = True
            target = request.args.get("next", "")
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("main.admin_events")
            return redirect(target)
        flash("Invalid admin token.", "error")
    return render_template("login.html")


@bp.post("/logout")
def logout():
    session.pop("is_admin", None)
    return redirect(url_for("main.login"))


@bp.route("/admin/events", methods=["GET", "POST"])
@admin_required
def admin_events():
    conn = get_db()
    if request.method == "POST":
        f = request.form
        try:
            event_id = services.create_event(
                conn,
                f.get("name", ""),
                f.get("location", ""),
                f.get("starts_at", ""),
                f.get("opens_at", ""),
                f.get("closes_at", ""),
                services.parse_minutes(f.get("late_after_minutes")),
            )
        except services.ValidationError as exc:
            flash(str(exc), "error")
        else:
            flash("Event created.", "success")
            return redirect(url_for("main.admin_event", event_id=event_id))
    return render_template("admin_events.html", events=services.list_events(conn))


@bp.route("/admin/events/<int:event_id>", methods=["GET", "POST"])
@admin_required
def admin_event(event_id: int):
    conn = get_db()
    event = load_event(event_id)
    if request.method == "POST":
        try:
            services.check_in(
                conn,
                event_id,
                request.form.get("email", ""),
                request.form.get("name", ""),
                now(),
                walk_in=True,
            )
            flash("Walk-in registered.", "success")
        except services.ValidationError as exc:
            flash(str(exc), "error")
        return redirect(url_for("main.admin_event", event_id=event_id))
    attendees = services.list_attendees(conn, event_id)
    links = {a["id"]: feedback_url(a["id"]) for a in attendees}
    return render_template(
        "admin_event.html",
        event=event,
        attendees=attendees,
        stats=services.event_stats(conn, event_id),
        status=services.window_status(event, now()),
        checkin_url=external("main.checkin", token=sign(event_id, CHECKIN_SALT)),
        feedback_links=links,
    )


@bp.get("/admin/events/<int:event_id>/attendees.json")
@admin_required
def attendees_json(event_id: int):
    load_event(event_id)
    conn = get_db()
    rows = services.list_attendees(conn, event_id)
    fields = ("name", "email", "checked_in_at", "is_late", "is_walk_in", "rating")
    return {
        "attendees": [
            {**{k: r[k] for k in fields}, "feedback_url": feedback_url(r["id"])} for r in rows
        ],
        "stats": services.event_stats(conn, event_id).as_dict(),
    }


@bp.get("/admin/events/<int:event_id>/qr.png")
@admin_required
def event_qr(event_id: int):
    load_event(event_id)
    url = external("main.checkin", token=sign(event_id, CHECKIN_SALT))
    return Response(qr_png(url), mimetype="image/png")


@bp.get("/admin/events/<int:event_id>/export.csv")
@admin_required
def export_csv(event_id: int):
    event = load_event(event_id)
    body = services.attendees_csv(services.list_attendees(get_db(), event_id))
    filename = f"event-{event['id']}-attendees.csv"
    return Response(
        body,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@bp.route("/c/<token>", methods=["GET", "POST"])
def checkin(token: str):
    event_id = unsign(token, CHECKIN_SALT)
    event = load_event(event_id)
    if request.method == "POST":
        try:
            result = services.check_in(
                get_db(),
                event_id,
                request.form.get("email", ""),
                request.form.get("name", ""),
                now(),
            )
        except services.ValidationError as exc:
            flash(str(exc), "error")
        else:
            return render_template("checked_in.html", event=event, result=result)
    return render_template("checkin.html", event=event, status=services.window_status(event, now()))


@bp.route("/f/<token>", methods=["GET", "POST"])
def feedback(token: str):
    attendee_id = unsign(token, FEEDBACK_SALT)
    conn = get_db()
    attendee = services.get_attendee(conn, attendee_id)
    if attendee is None:
        abort(404)
    event = load_event(attendee["event_id"])
    if request.method == "POST":
        try:
            services.submit_feedback(
                conn,
                attendee_id,
                request.form.get("rating", ""),
                request.form.get("comment", ""),
                now(),
            )
        except services.ValidationError as exc:
            flash(str(exc), "error")
        else:
            return render_template("thanks.html", event=event)
    return render_template("feedback.html", event=event, attendee=attendee)
