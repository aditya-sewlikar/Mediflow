"""MediFlow - Flask backend for a multi-tenant clinic appointment and queue system.

Tenant context always comes from the signed Flask session cookie (and is
revalidated against the database on every request). Client-supplied clinic
identifiers are never used to scope a query.
"""
import hmac
import os
import re
import secrets
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlsplit

import oracledb
from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from db import execute, query, query_one, transaction

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("FLASK_ENV") == "production",
)

APP_VERSION = "3.0"
CLINIC_NAME = os.getenv("CLINIC_NAME", "MediFlow Clinic")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "support@mediflow.clinic")

STATUSES = ["BOOKED", "WAITING", "IN_PROGRESS", "COMPLETED", "CANCELLED"]
ACTIVE = ("BOOKED", "WAITING", "IN_PROGRESS")
STATUS_LABEL = {
    "BOOKED": "Scheduled",
    "WAITING": "Waiting",
    "IN_PROGRESS": "In progress",
    "COMPLETED": "Completed",
    "CANCELLED": "Cancelled",
}
STATUS_TRANSITIONS = {
    "BOOKED": {"WAITING", "CANCELLED"},
    "WAITING": {"IN_PROGRESS", "CANCELLED"},
    "IN_PROGRESS": {"COMPLETED", "CANCELLED"},
    "COMPLETED": set(),
    "CANCELLED": set(),
}
DOCTOR_STATE_LABEL = {
    "consulting": "Consulting",
    "waiting": "Waiting",
    "available": "Available",
    "busy": "Busy",
    "offline": "Offline",
}
DOCTOR_STATE_ORDER = {
    "consulting": 0, "waiting": 1, "available": 2, "busy": 3, "offline": 4,
}
# Manual availability stored on the doctor record.  AVAILABLE keeps showing the
# live queue-derived state; BUSY/OFFLINE override it until the doctor changes it.
AVAILABILITY = ("AVAILABLE", "BUSY", "OFFLINE")
AVAILABILITY_LABEL = {
    "AVAILABLE": "Available for patients",
    "BUSY": "Busy — do not assign",
    "OFFLINE": "Offline / on break",
}
PHONE_RE = re.compile(r"^[0-9]{7,15}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,50}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Platform administrators (legacy ADMIN accounts included) see every clinic.
# Every other account belongs to exactly one clinic. Self-service sign-ups
# create a new clinic AND its clinic administrator - never a platform admin.
PLATFORM_ROLES = ("ADMIN", "PLATFORM_ADMIN")
CLINIC_ADMIN_ROLE = "CLINIC_ADMIN"
MANAGE_ROLES = PLATFORM_ROLES + (CLINIC_ADMIN_ROLE, "RECEPTIONIST")
REGISTRATION_ROLE = CLINIC_ADMIN_ROLE
DEFAULT_CLINIC_SLUG = "default-clinic"  # a stable slug, never a hardcoded id

LEDGER_PAGE_SIZE = 25
LEDGER_SORTS = {
    "appointment_id": "a.appointment_id",
    "appointment_datetime": "a.appointment_datetime",
    "patient_name": "p.full_name",
    "doctor_name": "d.full_name",
    "department": "d.specialization",
    "status": "a.status",
    "token_no": "a.token_no",
}

DEFAULT_PREFS = {
    "notifications": True,
    "queue_refresh": 30,
    "density": "comfortable",
}

# Set DISABLE_LOGIN=1 to skip login in a local demo.
LOGIN_ENABLED = os.getenv("DISABLE_LOGIN", "0") != "1"


# ---------- helpers ----------
def parse_date(text):
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return date.today()


def day_range(d):
    start = datetime.combine(d, datetime.min.time())
    return start, start + timedelta(days=1)


def safe_next(target, fallback):
    """Allow only same-origin paths as redirect targets."""
    if not target:
        return fallback
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return fallback
    if "\\" in target or not target.startswith("/") or target.startswith("//"):
        return fallback
    return target


def parse_iso_date(text):
    """Return a date for a strict YYYY-MM-DD string, otherwise None."""
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def schema_column_exists(table, column):
    """Check an existing Oracle schema without changing it."""
    row = query_one(
        """SELECT COUNT(*) AS c FROM user_tab_columns
           WHERE table_name = :t AND column_name = :c""",
        {"t": table.upper(), "c": column.upper()},
    )
    return bool(row and int(row["c"]))


# ---------- tenant context ----------
# Every data query in this file goes through one of these helpers. The clinic
# id is read from the signed session (or re-read from the database below),
# never from request.args / request.form / a URL path.
_demo_clinic_id = None


def is_platform_admin():
    """True only for a signed-in platform administrator, never for demo mode."""
    return bool(LOGIN_ENABLED and session.get("is_platform_admin")
                and session.get("role") in PLATFORM_ROLES)


def demo_clinic_id():
    """Local DISABLE_LOGIN demo: use the default tenant, resolved from the DB."""
    global _demo_clinic_id
    if _demo_clinic_id is None:
        row = query_one(
            """SELECT clinic_id FROM clinics WHERE is_default = 1
               ORDER BY clinic_id FETCH FIRST 1 ROWS ONLY"""
        ) or query_one("SELECT MIN(clinic_id) AS clinic_id FROM clinics")
        if not row or not row["clinic_id"]:
            abort(500, description="No clinic exists. Run sql/06_multi_tenant_migration.sql.")
        _demo_clinic_id = int(row["clinic_id"])
    return _demo_clinic_id


def tenant_id():
    """Clinic id for data isolation. None means a global platform view.

    A platform admin starts in the global view and may explicitly choose a
    clinic to work in from the Platform Clinics page (stored in the session).
    """
    if is_platform_admin():
        acting = session.get("acting_clinic_id")
        return int(acting) if acting else None
    if not LOGIN_ENABLED:
        return demo_clinic_id()
    if not session.get("clinic_id"):
        abort(403)
    return int(session["clinic_id"])


def scope_condition(alias=None):
    """Return (predicate, bind params) for the current tenant."""
    cid = tenant_id()
    if cid is None:
        return "", {}
    column = f"{alias}.clinic_id" if alias else "clinic_id"
    return f"{column} = :clinic_id", {"clinic_id": cid}


def where_clause(conditions=None, alias=None):
    """Build an optional WHERE clause that always includes the tenant filter."""
    clauses = [c for c in (conditions or []) if c]
    params = {}
    tenant_clause, tenant_params = scope_condition(alias)
    if tenant_clause:
        clauses.append(tenant_clause)
        params.update(tenant_params)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def tenant_key():
    """Pulse/notifications cache key - never share one tenant's cache."""
    return "global" if tenant_id() is None else f"clinic:{tenant_id()}"


def revalidate_session():
    """Re-read the signed-in identity from the DB on every protected request."""
    uid = session.get("user_id")
    if not uid:
        return False
    try:
        row = query_one(
            """SELECT u.user_id, u.username, u.full_name, u.email, u.role,
                      u.clinic_id, c.clinic_name, c.status AS clinic_status
                 FROM users u
                 LEFT JOIN clinics c ON c.clinic_id = u.clinic_id
                WHERE u.user_id = :i""",
            {"i": uid},
        )
    except oracledb.Error:
        app.logger.exception("Could not revalidate the signed-in account")
        abort(500, description=(
            "The database schema is out of date. Run "
            "@sql/06_multi_tenant_migration.sql and sign in again."
        ))
    if not row:
        return False
    admin = row["role"] in PLATFORM_ROLES
    if not admin and (
        not row["clinic_id"] or str(row["clinic_status"] or "").upper() != "ACTIVE"
    ):
        return False
    session["user"] = row["username"]
    session["name"] = row["full_name"] or row["username"]
    session["email"] = row["email"] or ""
    session["role"] = row["role"]
    session["is_platform_admin"] = admin
    session["clinic_id"] = int(row["clinic_id"]) if row["clinic_id"] else None
    acting_clinic = None
    if admin and session.get("acting_clinic_id"):
        # An acting clinic must still exist and be active, and its name is the
        # workspace label to keep on subsequent requests.
        acting_clinic = query_one(
            "SELECT clinic_name FROM clinics WHERE clinic_id = :i AND status = 'ACTIVE'",
            {"i": session["acting_clinic_id"]},
        )
        if not acting_clinic:
            session.pop("acting_clinic_id", None)
    if not admin:
        session.pop("acting_clinic_id", None)
        session["clinic_name"] = row["clinic_name"] or CLINIC_NAME
    elif acting_clinic:
        session["clinic_name"] = acting_clinic["clinic_name"]
    else:
        session["clinic_name"] = "MediFlow Platform"
    return True


def require_platform_admin():
    if not is_platform_admin():
        abort(403)


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:70]
    return slug or "clinic"


def ledger_filters():
    """Parse and sanitise every appointment-ledger query parameter."""
    args = request.args
    period = (args.get("period") or "all").lower()
    if period not in ("all", "past", "today", "upcoming"):
        period = "all"
    status = (args.get("status") or "").upper()
    if status not in STATUSES:
        status = ""
    sort = (args.get("sort") or "appointment_datetime").lower()
    if sort not in LEDGER_SORTS:
        sort = "appointment_datetime"
    direction = "desc" if (args.get("dir") or "asc").lower() == "desc" else "asc"
    try:
        page = max(1, int(args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    return {
        "q": (args.get("q") or "").strip()[:100],
        "doctor_id": args.get("doctor_id", type=int),
        "status": status,
        "period": period,
        "date": parse_iso_date(args.get("date")),
        "from_date": parse_iso_date(args.get("from")),
        "to_date": parse_iso_date(args.get("to")),
        "sort": sort,
        "dir": direction,
        "page": page,
    }


def ledger_where(filters):
    """Build the ledger's bind-variable WHERE clause and parameters."""
    clauses = []
    params = {}
    tenant_clause, tenant_params = scope_condition("a")
    if tenant_clause:
        clauses.append(tenant_clause)
        params.update(tenant_params)
    if filters["q"]:
        clauses.append("(LOWER(p.full_name) LIKE :q OR TO_CHAR(a.appointment_id) = :q_exact)")
        params["q"] = f"%{filters['q'].lower()}%"
        params["q_exact"] = filters["q"] if filters["q"].isdigit() else ""
    if filters["doctor_id"]:
        clauses.append("a.doctor_id = :doctor_id")
        params["doctor_id"] = filters["doctor_id"]
    if filters["status"]:
        clauses.append("a.status = :status")
        params["status"] = filters["status"]

    if filters["from_date"] or filters["to_date"]:
        if filters["from_date"]:
            clauses.append("a.appointment_datetime >= :from_dt")
            params["from_dt"] = datetime.combine(filters["from_date"], datetime.min.time())
        if filters["to_date"]:
            clauses.append("a.appointment_datetime < :to_dt")
            params["to_dt"] = datetime.combine(
                filters["to_date"] + timedelta(days=1), datetime.min.time()
            )
    elif filters["date"]:
        clauses.append("a.appointment_datetime >= :day_start")
        clauses.append("a.appointment_datetime < :day_end")
        params["day_start"] = datetime.combine(filters["date"], datetime.min.time())
        params["day_end"] = datetime.combine(
            filters["date"] + timedelta(days=1), datetime.min.time()
        )
    elif filters["period"] != "all":
        today = date.today()
        today_start = datetime.combine(today, datetime.min.time())
        tomorrow = today_start + timedelta(days=1)
        if filters["period"] == "past":
            clauses.append("a.appointment_datetime < :today_start")
            params["today_start"] = today_start
        elif filters["period"] == "today":
            clauses.append("a.appointment_datetime >= :today_start")
            clauses.append("a.appointment_datetime < :tomorrow")
            params.update({"today_start": today_start, "tomorrow": tomorrow})
        else:
            clauses.append("a.appointment_datetime >= :tomorrow")
            params["tomorrow"] = tomorrow

    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def ledger_url(**updates):
    """Build a ledger link that preserves the current filters."""
    args = request.args.to_dict(flat=True)
    for key, value in updates.items():
        if value in (None, ""):
            args.pop(key, None)
        else:
            args[key] = str(value)
    if "sort" in updates and "dir" not in updates:
        args.pop("dir", None)
    return url_for("appointment_ledger", **args)


def appointment_period_counts():
    """All-time appointment totals split into past, today and upcoming."""
    today = date.today()
    today_start = datetime.combine(today, datetime.min.time())
    tomorrow = today_start + timedelta(days=1)
    where_sql, params = where_clause()
    row = query_one(
        """SELECT COUNT(*) AS total,
                  COUNT(CASE WHEN appointment_datetime < :s THEN 1 END) AS past,
                  COUNT(CASE WHEN appointment_datetime >= :s
                              AND appointment_datetime < :e THEN 1 END) AS today,
                  COUNT(CASE WHEN appointment_datetime >= :e THEN 1 END) AS upcoming
           FROM appointments""" + where_sql,
        {**params, "s": today_start, "e": tomorrow},
    ) or {}
    return {key: int(row.get(key) or 0) for key in ("total", "past", "today", "upcoming")}


def can_manage():
    return (not LOGIN_ENABLED) or session.get("role") in MANAGE_ROLES


def require_write_access():
    if not can_manage():
        abort(403)
    # A platform admin in the global view has no clinic to own a new record.
    if is_platform_admin() and tenant_id() is None:
        abort(403, description=(
            "Choose a clinic from Platform Clinics before changing clinical records."
        ))


def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def get_prefs():
    stored = session.get("prefs") or {}
    merged = dict(DEFAULT_PREFS)
    for key in DEFAULT_PREFS:
        if key in stored:
            merged[key] = stored[key]
    return merged


def active_session():
    return (not LOGIN_ENABLED) or bool(session.get("user"))


def greeting_for(moment):
    hour = moment.hour
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


def trend_for(current, previous, invert=False):
    """Build a small, honest comparison object for a statistic card."""
    if previous == 0:
        if current == 0:
            return {"text": "0%", "direction": "flat", "tone": "neutral"}
        return {"text": "New", "direction": "up", "tone": "neutral" if invert else "positive"}
    change = round((current - previous) / previous * 100)
    direction = "up" if change > 0 else ("down" if change < 0 else "flat")
    tone = "neutral"
    if direction != "flat":
        good = (change > 0) != invert
        tone = "positive" if good else "negative"
    return {"text": f"{abs(change)}%", "direction": direction, "tone": tone}


def average_wait(rows, now=None):
    """Average minutes elapsed since the scheduled time for waiting patients."""
    if not rows:
        return None
    now = now or datetime.now()
    waits = []
    for row in rows:
        when = row.get("appointment_datetime")
        if when:
            waits.append(max(0, round((now - when).total_seconds() / 60)))
    return round(sum(waits) / len(waits)) if waits else None


def today_appointment_rows(day):
    """A day's appointments for the current tenant (or all for platform admin)."""
    start, end = day_range(day)
    where_sql, params = where_clause([
        "a.appointment_datetime >= :s",
        "a.appointment_datetime < :e",
    ], "a")
    return query(
        """SELECT a.appointment_id, a.patient_id, a.doctor_id, a.token_no, a.status,
                  a.appointment_datetime, a.reason,
                  p.full_name AS patient_name, p.phone,
                  d.full_name AS doctor_name, d.specialization
           FROM appointments a
           JOIN patients p ON a.patient_id = p.patient_id AND p.clinic_id = a.clinic_id
           JOIN doctors d ON a.doctor_id = d.doctor_id AND d.clinic_id = a.clinic_id"""
        + where_sql +
        """ ORDER BY a.appointment_datetime, a.token_no""",
        {**params, "s": start, "e": end},
    )


def all_doctors():
    """Doctor list including the manual availability column.

    Falls back to the original doctor list when the availability column has not
    been added to an existing database yet, so the app keeps working until the
    schema migration in sql/01_schema.sql is applied.
    """
    where_sql, params = where_clause()
    try:
        rows = query(
            """SELECT doctor_id, full_name, specialization, phone, availability
               FROM doctors""" + where_sql + " ORDER BY full_name", params
        )
    except oracledb.Error:
        app.logger.warning(
            "Doctor availability column is missing. Run "
            "@sql/05_auth_and_availability_migration.sql to update the schema."
        )
        rows = query(
            "SELECT doctor_id, full_name, specialization, phone FROM doctors"
            + where_sql + " ORDER BY full_name", params
        )
    for row in rows:
        value = str(row.get("availability") or "AVAILABLE").upper()
        row["availability"] = value if value in AVAILABILITY else "AVAILABLE"
    return rows


def all_appointment_rows():
    """Appointments for the current tenant, newest day first."""
    where_sql, params = where_clause(alias="a")
    return query(
        """SELECT a.appointment_id, a.patient_id, a.doctor_id, a.token_no, a.status,
                  a.appointment_datetime, a.reason,
                  p.full_name AS patient_name, p.phone,
                  d.full_name AS doctor_name, d.specialization
           FROM appointments a
           JOIN patients p ON a.patient_id = p.patient_id AND p.clinic_id = a.clinic_id
           JOIN doctors d ON a.doctor_id = d.doctor_id AND d.clinic_id = a.clinic_id"""
        + where_sql +
        """ ORDER BY a.appointment_datetime DESC, a.token_no""",
        params,
    )


def build_doctor_cards(docs, rows, now=None, patient_stats=None):
    """Group today's appointments per doctor and derive live queue state.

    ``patient_stats`` carries the canonical doctor-wise patient totals from
    :func:`doctor_stats_by_id`, so every card exposes the same numbers as the
    reports and insights pages while keeping its live queue information.
    """
    now = now or datetime.now()
    patient_stats = patient_stats or {}
    by_doc = defaultdict(list)
    for row in rows:
        by_doc[row["doctor_id"]].append(row)

    cards = []
    for doctor in docs:
        doc_rows = sorted(
            by_doc.get(doctor["doctor_id"], []),
            key=lambda r: (r["appointment_datetime"], r["token_no"] or 0),
        )
        active_rows = [r for r in doc_rows if r["status"] in ACTIVE]
        waiting_rows = [r for r in active_rows if r["status"] == "WAITING"]
        serving = next((r for r in active_rows if r["status"] == "IN_PROGRESS"), None)
        next_up = next((r for r in waiting_rows), None)
        state = "consulting" if serving else ("waiting" if waiting_rows else "available")
        manual = str(doctor.get("availability") or "AVAILABLE").upper()
        if manual in ("BUSY", "OFFLINE"):
            state = manual.lower()
        totals = patient_stats.get(doctor["doctor_id"], {})
        cards.append({
            **doctor,
            "patient_count": totals.get("patient_count", 0),
            "total_visits": totals.get("total", 0),
            "completed_visits": totals.get("completed", 0),
            "serving_token": serving["token_no"] if serving else None,
            "serving_patient": serving["patient_name"] if serving else None,
            "next_token": next_up["token_no"] if next_up else None,
            "next_patient": next_up["patient_name"] if next_up else None,
            "next_time": next_up["appointment_datetime"] if next_up else None,
            "waiting": len(waiting_rows),
            "booked": sum(1 for r in active_rows if r["status"] == "BOOKED"),
            "completed": sum(1 for r in doc_rows if r["status"] == "COMPLETED"),
            "avg_wait": average_wait(waiting_rows, now),
            "state": state,
            "state_label": DOCTOR_STATE_LABEL[state],
        })

    cards.sort(key=lambda c: (DOCTOR_STATE_ORDER[c["state"]], c["full_name"].lower()))
    return cards


def hourly_flow(rows):
    """Appointment volume by hour, with the completed share for the chart."""
    if rows:
        min_hour = min(r["appointment_datetime"].hour for r in rows)
        max_hour = max(r["appointment_datetime"].hour for r in rows)
    else:
        min_hour, max_hour = 8, 17
    start_hour = max(0, min(8, min_hour))
    end_hour = min(23, max(18, max_hour))

    buckets = {h: {"total": 0, "completed": 0} for h in range(start_hour, end_hour + 1)}
    for row in rows:
        bucket = buckets.get(row["appointment_datetime"].hour)
        if bucket is not None:
            bucket["total"] += 1
            if row["status"] == "COMPLETED":
                bucket["completed"] += 1

    peak = max((b["total"] for b in buckets.values()), default=1) or 1
    return [
        {
            "hour": h,
            "label": f"{h:02d}",
            "total": buckets[h]["total"],
            "completed": buckets[h]["completed"],
            "total_pct": round(buckets[h]["total"] / peak * 100),
            "done_pct": round(buckets[h]["completed"] / peak * 100),
        }
        for h in range(start_hour, end_hour + 1)
    ]


def completion_rate(completed, total):
    return round(completed / total * 100) if total else 0


# ---------- doctor-wise patient statistics ----------
# This is the single definition of the per-doctor numbers shown on the doctors,
# dashboard, reports and insights pages.  The LEFT JOIN keeps every clinician in
# the result (with honest zeroes instead of a missing row) and COUNT(DISTINCT)
# counts *patients*, not appointment rows.  Everything is derived from the live
# appointments table, so a status change is reflected the next time any page is
# rendered - there is no separate summary table that can drift out of date.
DOCTOR_STATS_BASE = """
SELECT d.doctor_id,
       d.full_name AS full_name,
       d.full_name AS doctor_name,
       d.specialization,
       COUNT(DISTINCT a.patient_id) AS patient_count,
       COUNT(a.appointment_id) AS total,
       COUNT(CASE WHEN a.status = 'BOOKED' THEN 1 END) AS booked,
       COUNT(CASE WHEN a.status = 'WAITING' THEN 1 END) AS waiting,
       COUNT(CASE WHEN a.status = 'IN_PROGRESS' THEN 1 END) AS in_progress,
       COUNT(CASE WHEN a.status = 'COMPLETED' THEN 1 END) AS completed,
       COUNT(CASE WHEN a.status = 'CANCELLED' THEN 1 END) AS cancelled,
       COUNT(CASE WHEN a.status IN ('BOOKED', 'WAITING', 'IN_PROGRESS')
                  THEN 1 END) AS open_count
FROM doctors d
LEFT JOIN appointments a
       ON a.doctor_id = d.doctor_id AND a.clinic_id = d.clinic_id{date_filter}
{where_clause}
GROUP BY d.doctor_id, d.full_name, d.specialization
ORDER BY d.full_name
"""

STAT_COUNT_FIELDS = (
    "patient_count", "total", "booked", "waiting", "in_progress",
    "completed", "cancelled", "open_count",
)


def doctor_patient_stats(start=None, end=None):
    """Per-doctor patient/appointment counts for an optional period.

    ``start`` and ``end`` are datetimes.  With no bounds every appointment is
    counted.  Doctors without appointments are still returned with zero counts.
    """
    date_filter = ""
    params = {}
    if start is not None:
        date_filter += " AND a.appointment_datetime >= :s"
        params["s"] = start
    if end is not None:
        date_filter += " AND a.appointment_datetime < :e"
        params["e"] = end
    tenant_clause, tenant_params = scope_condition("d")
    where_sql = " WHERE " + tenant_clause if tenant_clause else ""
    params.update(tenant_params)
    rows = query(
        DOCTOR_STATS_BASE.format(date_filter=date_filter, where_clause=where_sql),
        params,
    )
    for row in rows:
        for field in STAT_COUNT_FIELDS:
            row[field] = int(row.get(field) or 0)
        row["completion_rate"] = completion_rate(row["completed"], row["total"])
    return rows


def doctor_stats_by_id(start=None, end=None):
    """Index :func:`doctor_patient_stats` by doctor_id for card lookups."""
    return {int(row["doctor_id"]): row for row in doctor_patient_stats(start, end)}


def database_ok():
    try:
        return bool(query_one("SELECT 1 AS ok FROM dual"))
    except Exception:
        return False


# ---------- clinic pulse / notifications ----------
_pulse_cache = {}  # keyed by tenant so one clinic's alerts never leak to another


def invalidate_pulse():
    _pulse_cache.clear()


def invalidate_stats():
    """Refresh derived clinic statistics after any write to appointments.

    Doctor-wise figures are computed from the live appointments table at render
    time, so clearing the short-lived pulse snapshot is all that is needed to
    keep every consumer (dashboard, doctors, queue, reports and insights) in
    sync after a booking or a status change.
    """
    invalidate_pulse()


def load_pulse():
    """Real, cached clinic alerts used by the topbar and sidebar."""
    empty = {"waiting": 0, "soon": 0, "doctors": 0, "completed": 0}
    if not active_session():
        return empty, []

    moment = datetime.now()
    key = tenant_key()
    cached = _pulse_cache.get(key)
    if cached and (moment - cached["at"]).total_seconds() < 30:
        return cached["pulse"], cached["notifications"]

    try:
        start, end = day_range(date.today())
        waiting_where, waiting_params = where_clause([
            "status = 'WAITING'",
            "appointment_datetime >= :s",
            "appointment_datetime < :e",
        ])
        waiting_row = query_one(
            "SELECT COUNT(*) AS c FROM appointments" + waiting_where,
            {**waiting_params, "s": start, "e": end},
        )
        soon_where, soon_params = where_clause([
            "status = 'BOOKED'",
            "appointment_datetime >= :n",
            "appointment_datetime <= :e",
        ])
        soon_row = query_one(
            "SELECT COUNT(*) AS c FROM appointments" + soon_where,
            {**soon_params, "n": moment, "e": moment + timedelta(hours=1)},
        )
        scope_sql, scope_params = where_clause()
        doctor_row = query_one(
            "SELECT COUNT(*) AS c FROM doctors" + scope_sql, scope_params
        )
        completed_where, completed_params = where_clause([
            "status = 'COMPLETED'",
            "appointment_datetime >= :s",
            "appointment_datetime < :e",
        ])
        completed_row = query_one(
            "SELECT COUNT(*) AS c FROM appointments" + completed_where,
            {**completed_params, "s": start, "e": end},
        )
        pulse = {
            "waiting": int(waiting_row["c"]) if waiting_row else 0,
            "soon": int(soon_row["c"]) if soon_row else 0,
            "doctors": int(doctor_row["c"]) if doctor_row else 0,
            "completed": int(completed_row["c"]) if completed_row else 0,
        }

        notifications = []
        if pulse["waiting"]:
            notifications.append({
                "tone": "warning",
                "icon": "clock",
                "title": f"{pulse['waiting']} patient{'s' if pulse['waiting'] != 1 else ''} waiting",
                "meta": "Checked-in patients need a consultation.",
                "url": url_for("queue_select"),
            })
        if pulse["soon"]:
            notifications.append({
                "tone": "info",
                "icon": "calendar",
                "title": f"{pulse['soon']} appointment{'s' if pulse['soon'] != 1 else ''} within the hour",
                "meta": "Scheduled visits are coming up next.",
                "url": url_for("appointments"),
            })
        if pulse["doctors"] == 0:
            notifications.append({
                "tone": "warning",
                "icon": "doctor",
                "title": "Add your first doctor",
                "meta": "Queues and appointments need a clinician on the roster.",
                "url": url_for("doctors"),
            })
        elif pulse["completed"]:
            notifications.append({
                "tone": "success",
                "icon": "check-circle",
                "title": f"{pulse['completed']} visit{'s' if pulse['completed'] != 1 else ''} completed today",
                "meta": "See today's reconciled completion rate.",
                "url": url_for("reports"),
            })

        _pulse_cache[key] = {"at": moment, "pulse": pulse, "notifications": notifications}
        return pulse, notifications
    except Exception:
        app.logger.exception("Could not load clinic pulse")
        return empty, []


# ---------- template filters / context ----------
@app.template_filter("dt")
def fmt_dt(v):
    return v.strftime("%d %b %Y, %H:%M") if v else ""


@app.template_filter("hm")
def fmt_hm(v):
    return v.strftime("%H:%M") if v else ""


@app.template_filter("status_label")
def status_label(v):
    return STATUS_LABEL.get(v, v)


@app.template_filter("initials")
def initials(name):
    parts = [p for p in (name or "").replace("Dr.", "").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


@app.context_processor
def inject():
    pulse, notifications = load_pulse()
    name = session.get("user")
    admin = is_platform_admin()
    clinic_label = "All clinics · platform admin" if admin and tenant_id() is None \
        else (session.get("clinic_name") or CLINIC_NAME)
    return {
        "login_enabled": LOGIN_ENABLED,
        "is_platform_admin": admin,
        "STATUSES": STATUSES,
        "STATUS_LABEL": STATUS_LABEL,
        "DOCTOR_STATE_LABEL": DOCTOR_STATE_LABEL,
        "AVAILABILITY": AVAILABILITY,
        "AVAILABILITY_LABEL": AVAILABILITY_LABEL,
        "today": date.today(),
        "now": datetime.now(),
        "csrf_token": csrf_token(),
        "role": session.get("role"),
        "user_name": name,
        "account_name": session.get("name") or name,
        "account_email": session.get("email") or "",
        "can_manage": can_manage(),
        "can_set_availability": active_session(),
        "clinic_name": clinic_label,
        "account_clinic": session.get("clinic_name") or CLINIC_NAME,
        "support_email": SUPPORT_EMAIL,
        "app_version": APP_VERSION,
        "prefs": get_prefs(),
        "pulse": pulse,
        "notifications": notifications,
        "greeting": greeting_for(datetime.now()),
        "greeting_name": (name.split()[0].title() if name else "Clinic team"),
        "ledger_url": ledger_url,
    }


@app.before_request
def protect_request():
    if request.method == "POST":
        expected = session.get("_csrf")
        sent = request.form.get("_csrf", "")
        if not expected or not hmac.compare_digest(expected, sent):
            abort(400, description="Your form session expired. Refresh the page and try again.")

    if not LOGIN_ENABLED or request.endpoint in ("login", "signup", "static"):
        return
    if "user" not in session:
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    if not revalidate_session():
        session.clear()
        invalidate_pulse()
        return redirect(url_for("login", next=request.full_path.rstrip("?")))


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # Statistics must never be served from a browser cache after a status change.
    response.headers.setdefault("Cache-Control", "no-store, max-age=0, must-revalidate")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "script-src 'self'; "
        "connect-src 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'",
    )
    return response


# ---------- errors ----------
def render_error(code, message):
    return render_template("error.html", code=code, message=message), code


@app.errorhandler(400)
def bad_request(error):
    return render_error(400, getattr(error, "description", "The request could not be understood."))


@app.errorhandler(403)
def forbidden(error):
    return render_error(
        403,
        getattr(error, "description", None)
        or "You do not have permission to perform that action.",
    )


@app.errorhandler(404)
def not_found(error):
    return render_error(
        404,
        getattr(error, "description", None)
        or "We could not find the page or record you were looking for.",
    )


@app.errorhandler(500)
def server_error(error):
    app.logger.exception("Unhandled error: %s", error)
    return render_error(
        500,
        getattr(error, "description", None)
        or "Something went wrong on our side. Please try again.",
    )


# ---------- authentication ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = safe_next(request.args.get("next"), url_for("index"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        try:
            u = query_one(
                """SELECT u.user_id, u.username, u.full_name, u.email,
                          u.password_hash, u.role, u.clinic_id,
                          c.clinic_name, c.status AS clinic_status
                   FROM users u
                   LEFT JOIN clinics c ON c.clinic_id = u.clinic_id
                  WHERE UPPER(u.username) = UPPER(:u)""",
                {"u": username},
            )
        except oracledb.Error:
            app.logger.exception("Could not validate sign-in credentials")
            flash("Sign-in is temporarily unavailable. Please try again.", "error")
            return render_template("login.html", next_url=next_url)

        if u and check_password_hash(u["password_hash"], password):
            admin = u["role"] in PLATFORM_ROLES
            if not admin and (
                not u["clinic_id"] or str(u["clinic_status"] or "").upper() != "ACTIVE"
            ):
                flash("This clinic account is not active. Contact platform support.", "error")
                return render_template("login.html", next_url=next_url)
            # Keep the current CSRF token so forms rendered before sign-in
            # (and the first POST after the redirect) remain valid.
            current_csrf = session.get("_csrf")
            session.clear()
            session["_csrf"] = current_csrf or secrets.token_urlsafe(32)
            session["user_id"] = u["user_id"]
            session["user"] = u["username"]
            session["name"] = u["full_name"] or u["username"]
            session["email"] = u["email"] or ""
            session["role"] = u["role"]
            session["is_platform_admin"] = admin
            session["clinic_id"] = int(u["clinic_id"]) if u["clinic_id"] else None
            session["clinic_name"] = (
                "MediFlow Platform" if admin else (u["clinic_name"] or CLINIC_NAME)
            )
            session.pop("acting_clinic_id", None)
            invalidate_pulse()
            return redirect(next_url)
        flash("Username or password is incorrect.", "error")
    return render_template("login.html", next_url=next_url)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Create a new clinic plus its clinic administrator in one transaction."""
    next_url = safe_next(
        request.form.get("next") or request.args.get("next"), url_for("login")
    )
    form_data = {
        "clinic_name": (request.form.get("clinic_name") or "").strip(),
        "full_name": (request.form.get("full_name") or "").strip(),
        "username": (request.form.get("username") or "").strip(),
        "email": (request.form.get("email") or "").strip().lower(),
    }

    if request.method == "POST":
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        errors = []

        if not 2 <= len(form_data["clinic_name"]) <= 150:
            errors.append("Enter your clinic name (2 to 150 characters).")
        if not 2 <= len(form_data["full_name"]) <= 100:
            errors.append("Enter your full name (2 to 100 characters).")
        if not USERNAME_RE.fullmatch(form_data["username"]):
            errors.append(
                "Choose a username with 3 to 50 letters, numbers, dots, dashes or underscores."
            )
        if not EMAIL_RE.fullmatch(form_data["email"]) or len(form_data["email"]) > 100:
            errors.append("Enter a valid email address (100 characters or fewer).")
        if len(password) < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
            errors.append("Use a password of at least 8 characters with letters and numbers.")
        if password != confirm:
            errors.append("The passwords do not match.")

        if not errors:
            try:
                existing = query_one(
                    """SELECT username, email FROM users
                       WHERE UPPER(username) = UPPER(:u)
                          OR (email IS NOT NULL AND LOWER(email) = LOWER(:e))""",
                    {"u": form_data["username"], "e": form_data["email"]},
                )
            except oracledb.Error:
                app.logger.exception("Could not check sign-up duplicates")
                errors.append("Registration is temporarily unavailable. Please try again.")
            else:
                if existing:
                    if (existing["username"] or "").upper() == form_data["username"].upper():
                        errors.append("That username is already registered. Choose another one.")
                    else:
                        errors.append("That email address is already registered. Use a different one.")

        if not errors:
            try:
                # One transaction: the clinic and its administrator either
                # both exist or neither does. The clinic_id is generated by
                # Oracle and never accepted from the browser.
                with transaction() as cur:
                    base_slug = slugify(form_data["clinic_name"])
                    slug = base_slug
                    cur.execute("SELECT COUNT(*) FROM clinics WHERE slug = :s", {"s": slug})
                    if cur.fetchone()[0]:
                        slug = f"{base_slug}-{secrets.token_hex(3)}"
                    new_id = cur.var(oracledb.NUMBER)
                    cur.execute(
                        """INSERT INTO clinics (clinic_name, slug, email)
                           VALUES (:n, :s, :e) RETURNING clinic_id INTO :id""",
                        {"n": form_data["clinic_name"], "s": slug,
                         "e": form_data["email"], "id": new_id},
                    )
                    cur.execute(
                        """INSERT INTO users
                              (clinic_id, full_name, username, email, password_hash, role)
                           VALUES (:c, :n, :u, :e, :p, :r)""",
                        {
                            "c": new_id.getvalue()[0],
                            "n": form_data["full_name"],
                            "u": form_data["username"],
                            "e": form_data["email"],
                            "p": generate_password_hash(password),
                            "r": REGISTRATION_ROLE,
                        },
                    )
            except oracledb.IntegrityError:
                errors.append("Those details are already registered. Check the username and email.")
            except oracledb.Error:
                app.logger.exception("Could not create clinic and administrator")
                errors.append("Registration is temporarily unavailable. Please try again.")
            else:
                flash(
                    "Clinic created. Sign in with your new administrator credentials.", "ok"
                )
                return redirect(url_for("login", next=next_url))

        for message in errors:
            flash(message, "error")

    return render_template("signup.html", form_data=form_data, next_url=next_url)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    invalidate_pulse()
    return redirect(url_for("login"))


# ---------- dashboard ----------
@app.route("/")
def index():
    today_date = date.today()
    rows = today_appointment_rows(today_date)

    counts = {s: 0 for s in STATUSES}
    patient_ids = set()
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        patient_ids.add(row["patient_id"])
    total = len(rows)
    total_patients = len(patient_ids)

    yesterday = today_date - timedelta(days=1)
    y_start, y_end = day_range(yesterday)
    previous_where, previous_params = where_clause([
        "appointment_datetime >= :s", "appointment_datetime < :e"
    ])
    prev_rows = query(
        "SELECT status, COUNT(*) AS total FROM appointments" + previous_where
        + " GROUP BY status",
        {**previous_params, "s": y_start, "e": y_end},
    )
    prev_counts = {s: 0 for s in STATUSES}
    prev_counts.update({r["status"]: int(r["total"]) for r in prev_rows})
    prev_patients_row = query_one(
        "SELECT COUNT(DISTINCT patient_id) AS c FROM appointments" + previous_where,
        {**previous_params, "s": y_start, "e": y_end},
    )
    prev_patients = int(prev_patients_row["c"]) if prev_patients_row else 0

    rate = completion_rate(counts["COMPLETED"], total)
    waiting_rows = [r for r in rows if r["status"] == "WAITING"]
    wait_avg = average_wait(waiting_rows)

    stats = [
        {
            "label": "Total patients today",
            "value": total_patients,
            "icon": "users",
            "tone": "blue",
            "change": trend_for(total_patients, prev_patients),
            "compare": f"{prev_patients} patient{'s' if prev_patients != 1 else ''} yesterday",
        },
        {
            "label": "Waiting patients",
            "value": counts["WAITING"],
            "icon": "clock",
            "tone": "orange",
            "change": trend_for(counts["WAITING"], prev_counts["WAITING"], invert=True),
            "compare": f"{prev_counts['WAITING']} waiting yesterday",
        },
        {
            "label": "In consultation",
            "value": counts["IN_PROGRESS"],
            "icon": "activity",
            "tone": "teal",
            "change": trend_for(counts["IN_PROGRESS"], prev_counts["IN_PROGRESS"]),
            "compare": f"{prev_counts['IN_PROGRESS']} in consultation yesterday",
        },
        {
            "label": "Completed appointments",
            "value": counts["COMPLETED"],
            "icon": "check-circle",
            "tone": "green",
            "change": trend_for(counts["COMPLETED"], prev_counts["COMPLETED"]),
            "compare": f"{rate}% completion rate today",
        },
    ]

    doctors = all_doctors()
    queue_summary = build_doctor_cards(
        doctors, rows, patient_stats=doctor_stats_by_id()
    )

    performance = {
        "total": total,
        "completed": counts["COMPLETED"],
        "waiting": counts["WAITING"],
        "in_progress": counts["IN_PROGRESS"],
        "cancelled": counts["CANCELLED"],
        "completion_rate": rate,
        "avg_wait": wait_avg,
        "avg_wait_label": f"{wait_avg} min" if wait_avg is not None else "—",
    }

    flow = hourly_flow(rows)

    return render_template(
        "index.html",
        counts=counts,
        total=total,
        total_patients=total_patients,
        stats=stats,
        performance=performance,
        completion_rate=rate,
        queue_summary=queue_summary,
        appointments=rows[:12],
        appointment_count=total,
        flow=flow,
        avg_wait=wait_avg,
        yesterday=yesterday,
        ledger_summary=appointment_period_counts(),
    )


# ---------- global search ----------
@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    patients = []
    doctors = []
    appointments = []
    if q:
        pattern = f"%{q.lower()}%"
        phone_pattern = f"%{q}%"
        patient_where, patient_params = where_clause([
            "(LOWER(full_name) LIKE :pattern OR phone LIKE :phone"
            " OR LOWER(NVL(email, ' ')) LIKE :pattern OR TO_CHAR(patient_id) = :q)"
        ])
        patient_params.update({"pattern": pattern, "q": q, "phone": phone_pattern})
        doctor_where, doctor_params = where_clause([
            "(LOWER(full_name) LIKE :pattern OR LOWER(specialization) LIKE :pattern"
            " OR phone LIKE :phone)"
        ])
        doctor_params.update({"pattern": pattern, "phone": phone_pattern})
        appt_where, appointment_params = where_clause([
            "(LOWER(p.full_name) LIKE :pattern OR LOWER(d.full_name) LIKE :pattern"
            " OR LOWER(NVL(a.reason, ' ')) LIKE :pattern OR TO_CHAR(a.token_no) = :q)"
        ], "a")
        appointment_params.update({"pattern": pattern, "q": q})
        patients = query(
            "SELECT patient_id, full_name, phone, email, gender FROM patients"
            + patient_where + " ORDER BY full_name FETCH FIRST 8 ROWS ONLY",
            patient_params,
        )
        doctors = query(
            "SELECT doctor_id, full_name, specialization, phone FROM doctors"
            + doctor_where + " ORDER BY full_name FETCH FIRST 8 ROWS ONLY",
            doctor_params,
        )
        appointments = query(
            """SELECT a.appointment_id, a.appointment_datetime, a.token_no, a.status,
                      a.patient_id, p.full_name AS patient_name,
                      a.doctor_id, d.full_name AS doctor_name, d.specialization
               FROM appointments a
               JOIN patients p ON a.patient_id = p.patient_id AND p.clinic_id = a.clinic_id
               JOIN doctors d ON a.doctor_id = d.doctor_id AND d.clinic_id = a.clinic_id"""
            + appt_where + " ORDER BY a.appointment_datetime DESC FETCH FIRST 10 ROWS ONLY",
            appointment_params,
        )
    return render_template(
        "search.html",
        q=q,
        patients=patients,
        doctors=doctors,
        appointments=appointments,
        result_count=len(patients) + len(doctors) + len(appointments),
    )


# ---------- patients ----------
@app.route("/patients", methods=["GET", "POST"])
def patients():
    if request.method == "POST":
        require_write_access()
        name = (request.form.get("full_name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        email = (request.form.get("email") or "").strip() or None
        gender = request.form.get("gender") or None
        dob_raw = (request.form.get("date_of_birth") or "").strip()

        errors = []
        if not name or len(name) > 100:
            errors.append("Enter the patient's full name (up to 100 characters).")
        if not PHONE_RE.fullmatch(phone):
            errors.append("Enter a phone number with 7 to 15 digits, using numbers only.")
        if email and len(email) > 100:
            errors.append("The email address must be 100 characters or fewer.")

        dob = None
        if dob_raw:
            try:
                dob = datetime.strptime(dob_raw, "%Y-%m-%d").date()
            except ValueError:
                errors.append("Enter a valid date of birth.")

        if errors:
            for message in errors:
                flash(message, "error")
        else:
            try:
                execute(
                    """INSERT INTO patients
                          (clinic_id, full_name, phone, email, gender, date_of_birth)
                       VALUES (:c, :n, :p, :e, :g, :d)""",
                    {"c": tenant_id(), "n": name, "p": phone,
                     "e": email, "g": gender, "d": dob},
                )
                invalidate_stats()
                flash(f"Patient {name} registered.", "ok")
                return redirect(url_for("patients"))
            except oracledb.IntegrityError:
                flash("A patient with this phone number already exists.", "error")

    q = (request.args.get("q") or "").strip()
    if q:
        where_sql, params = where_clause([
            "(TO_CHAR(patient_id) = :q OR phone LIKE :p OR LOWER(full_name) LIKE :n)"
        ])
        params.update({"q": q, "p": f"%{q}%", "n": f"%{q.lower()}%"})
        rows = query(
            """SELECT patient_id, full_name, phone, email, gender, date_of_birth, created_at
               FROM patients""" + where_sql + " ORDER BY full_name", params,
        )
    else:
        where_sql, params = where_clause()
        rows = query(
            """SELECT patient_id, full_name, phone, email, gender, date_of_birth, created_at
               FROM patients""" + where_sql
            + " ORDER BY patient_id DESC FETCH FIRST 100 ROWS ONLY", params,
        )

    month_start = datetime.combine(date.today().replace(day=1), datetime.min.time())
    summary_where, summary_params = where_clause()
    summary = query_one(
        """SELECT COUNT(*) AS total,
                  COUNT(CASE WHEN created_at >= :s THEN 1 END) AS new_month
           FROM patients""" + summary_where,
        {**summary_params, "s": month_start},
    ) or {}

    return render_template(
        "patients.html",
        patients=rows,
        q=q,
        open_form=request.method == "POST",
        form_data=request.form if request.method == "POST" else {},
        patient_total=int(summary.get("total") or 0),
        new_this_month=int(summary.get("new_month") or 0),
    )


@app.route("/patients/<int:patient_id>/edit", methods=["GET", "POST"])
def patient_edit(patient_id):
    where_sql, params = where_clause(["patient_id = :i"])
    p = query_one("SELECT * FROM patients" + where_sql, {**params, "i": patient_id})
    if not p:
        abort(404, description="This patient is not in your clinic.")
    if request.method == "POST":
        require_write_access()
        name = (request.form.get("full_name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        email = (request.form.get("email") or "").strip() or None
        gender = request.form.get("gender") or None
        dob_raw = (request.form.get("date_of_birth") or "").strip()

        errors = []
        if not name or len(name) > 100:
            errors.append("Enter the patient's full name (up to 100 characters).")
        if not PHONE_RE.fullmatch(phone):
            errors.append("Enter a phone number with 7 to 15 digits, using numbers only.")
        if email and len(email) > 100:
            errors.append("The email address must be 100 characters or fewer.")

        dob = None
        if dob_raw:
            try:
                dob = datetime.strptime(dob_raw, "%Y-%m-%d").date()
            except ValueError:
                errors.append("Enter a valid date of birth.")

        if errors:
            for message in errors:
                flash(message, "error")
        else:
            try:
                execute(
                    """UPDATE patients
                       SET full_name = :n, phone = :p, email = :e,
                           gender = :g, date_of_birth = :d
                       WHERE patient_id = :i AND clinic_id = :c""",
                    {"n": name, "p": phone, "e": email, "g": gender, "d": dob,
                     "i": patient_id, "c": p["clinic_id"]},
                )
                flash("Patient details saved.", "ok")
                return redirect(url_for("patient_edit", patient_id=patient_id))
            except oracledb.IntegrityError:
                flash("Another patient already uses this phone number.", "error")

    history = query(
        """SELECT a.appointment_datetime, a.token_no, a.status, a.reason,
                  d.full_name AS doctor_name
           FROM appointments a JOIN doctors d
             ON a.doctor_id = d.doctor_id AND d.clinic_id = a.clinic_id
          WHERE a.patient_id = :i AND a.clinic_id = :c
          ORDER BY a.appointment_datetime DESC""",
        {"i": patient_id, "c": p["clinic_id"]},
    )
    completed_visits = sum(1 for row in history if row["status"] == "COMPLETED")
    return render_template(
        "patient_edit.html",
        p=p,
        history=history,
        completed_visits=completed_visits,
        last_visit=history[0]["appointment_datetime"] if history else None,
        form_data=request.form if request.method == "POST" else {},
    )


# ---------- doctors ----------
@app.route("/doctors", methods=["GET", "POST"])
def doctors():
    if request.method == "POST":
        require_write_access()
        name = (request.form.get("full_name") or "").strip()
        spec = (request.form.get("specialization") or "").strip()
        phone = (request.form.get("phone") or "").strip() or None

        errors = []
        if not name or len(name) > 100:
            errors.append("Enter the doctor's full name (up to 100 characters).")
        if not spec or len(spec) > 80:
            errors.append("Enter a specialization (up to 80 characters).")
        if phone and not PHONE_RE.fullmatch(phone):
            errors.append("Enter a phone number with 7 to 15 digits, using numbers only.")

        if errors:
            for message in errors:
                flash(message, "error")
        else:
            try:
                execute(
                    """INSERT INTO doctors (clinic_id, full_name, specialization, phone)
                       VALUES (:c, :n, :s, :p)""",
                    {"c": tenant_id(), "n": name, "s": spec, "p": phone},
                )
                invalidate_stats()
                flash(f"{name} added.", "ok")
                return redirect(url_for("doctors"))
            except oracledb.IntegrityError:
                flash("A doctor with this phone number already exists.", "error")

    rows = all_doctors()
    today_rows = today_appointment_rows(date.today())
    doctor_cards = build_doctor_cards(
        rows, today_rows, patient_stats=doctor_stats_by_id()
    )
    return render_template(
        "doctors.html",
        doctors=rows,
        doctor_cards=doctor_cards,
        open_form=request.method == "POST",
        form_data=request.form if request.method == "POST" else {},
    )


@app.route("/doctors/<int:doctor_id>/availability", methods=["POST"])
def doctor_availability(doctor_id):
    """Let a clinician publish whether they can take patients right now."""
    value = (request.form.get("availability") or "").strip().upper()
    if value not in AVAILABILITY:
        abort(400, description="Choose a valid availability status.")

    next_url = safe_next(request.form.get("next"), url_for("doctors"))
    where_sql, params = where_clause(["doctor_id = :i"])
    doctor = query_one(
        "SELECT doctor_id, clinic_id FROM doctors" + where_sql,
        {**params, "i": doctor_id},
    )
    if not doctor:
        abort(404, description="This doctor is not in your clinic.")

    if not schema_column_exists("doctors", "availability"):
        app.logger.error(
            "Doctors.availability is missing. Run "
            "@sql/05_auth_and_availability_migration.sql before saving availability."
        )
        flash(
            "The database schema is out of date. Ask the administrator to run "
            "@sql/05_auth_and_availability_migration.sql, then try again.",
            "error",
        )
        return redirect(next_url)

    try:
        changed = execute(
            "UPDATE doctors SET availability = :a "
            "WHERE doctor_id = :i AND clinic_id = :c",
            {"a": value, "i": doctor_id, "c": doctor["clinic_id"]},
        )
    except oracledb.IntegrityError:
        app.logger.exception("Invalid availability value rejected by the database")
        flash("Availability could not be saved because the value is not allowed.", "error")
        return redirect(next_url)
    except oracledb.Error as exc:
        app.logger.exception("Could not save doctor availability (ORA-%s)", getattr(exc, "code", "?"))
        flash("Availability could not be saved. Please try again or contact support.", "error")
        return redirect(next_url)

    if not changed:
        flash("That doctor no longer exists.", "error")
        return redirect(next_url)

    invalidate_stats()
    flash(f"Availability set to {AVAILABILITY_LABEL[value].lower()}.", "ok")
    return redirect(next_url)


# ---------- appointments ----------
def book(patient_id, doctor_id, when, reason, token):
    """Book inside one transaction, verifying that both records own the same clinic.

    The clinic id comes from the session; a forged form field cannot influence
    it. Locking the doctor row also serialises concurrent token allocation.
    """
    clinic = tenant_id()
    if clinic is None:
        raise ValueError("Choose a clinic from Platform Clinics before booking.")
    start, end = day_range(when.date())
    with transaction() as cur:
        cur.execute(
            "SELECT clinic_id FROM doctors WHERE doctor_id = :d FOR UPDATE",
            {"d": doctor_id},
        )
        row = cur.fetchone()
        if not row or int(row[0]) != clinic:
            raise ValueError("Choose a doctor from your clinic.")
        cur.execute(
            "SELECT clinic_id FROM patients WHERE patient_id = :p",
            {"p": patient_id},
        )
        row = cur.fetchone()
        if not row or int(row[0]) != clinic:
            raise ValueError("Choose a patient from your clinic.")

        cur.execute(
            """SELECT COUNT(*) FROM appointments
               WHERE clinic_id = :c AND doctor_id = :d AND appointment_datetime = :w
                 AND status <> 'CANCELLED'""",
            {"c": clinic, "d": doctor_id, "w": when},
        )
        if cur.fetchone()[0]:
            raise ValueError("That doctor already has an appointment at that time.")

        day = {"c": clinic, "d": doctor_id, "s": start, "e": end}
        if token is None:
            cur.execute(
                """SELECT NVL(MAX(token_no), 0) + 1 FROM appointments
                   WHERE clinic_id = :c AND doctor_id = :d
                     AND appointment_datetime >= :s AND appointment_datetime < :e""",
                day,
            )
            token = cur.fetchone()[0]
        else:
            cur.execute(
                """SELECT COUNT(*) FROM appointments
                   WHERE clinic_id = :c AND doctor_id = :d
                     AND appointment_datetime >= :s AND appointment_datetime < :e
                     AND token_no = :t""",
                {**day, "t": token},
            )
            if cur.fetchone()[0]:
                raise ValueError(f"Token {token} is already used for this doctor on that day.")

        cur.execute(
            """INSERT INTO appointments
               (clinic_id, patient_id, doctor_id, appointment_datetime,
                token_no, status, reason)
               VALUES (:c, :p, :d, :w, :t, 'BOOKED', :r)""",
            {"c": clinic, "p": patient_id, "d": doctor_id,
             "w": when, "t": token, "r": reason},
        )
    return token


@app.route("/appointments", methods=["GET", "POST"])
def appointments():
    if request.method == "POST":
        require_write_access()
        f = request.form
        try:
            when = datetime.strptime(f["appointment_datetime"], "%Y-%m-%dT%H:%M")
            patient_id, doctor_id = int(f["patient_id"]), int(f["doctor_id"])
            token = int(f["token_no"]) if f.get("token_no", "").strip() else None
        except (ValueError, KeyError):
            flash("Check the date, patient, doctor and token. The token must be a whole number.", "error")
        else:
            try:
                if token is not None and token < 1:
                    raise ValueError("Token must be 1 or higher.")
                token = book(patient_id, doctor_id, when,
                            (f.get("reason") or "").strip()[:250] or None, token)
                invalidate_stats()
                flash(f"Appointment booked. Token {token}.", "ok")
                return redirect(url_for("appointments", date=when.date().isoformat()))
            except ValueError as e:
                flash(str(e), "error")
            except oracledb.IntegrityError:
                flash("This doctor already has that token at that exact time.", "error")

    d = parse_date(request.args.get("date"))
    start, end = day_range(d)
    where_sql, params = where_clause([
        "a.appointment_datetime >= :s", "a.appointment_datetime < :e"
    ], "a")
    rows = query(
        """SELECT a.appointment_id, a.patient_id, a.token_no, a.status, a.reason,
                  a.appointment_datetime,
                  p.full_name AS patient_name, p.phone,
                  d.full_name AS doctor_name, d.specialization
           FROM appointments a
           JOIN patients p ON a.patient_id = p.patient_id AND p.clinic_id = a.clinic_id
           JOIN doctors d ON a.doctor_id = d.doctor_id AND d.clinic_id = a.clinic_id"""
        + where_sql + " ORDER BY a.appointment_datetime, a.token_no",
        {**params, "s": start, "e": end},
    )
    list_where, list_params = where_clause()
    return render_template(
        "appointments.html",
        rows=rows,
        day=d,
        prev_day=(d - timedelta(days=1)).isoformat(),
        next_day=(d + timedelta(days=1)).isoformat(),
        patients=query(
            "SELECT patient_id, full_name, phone FROM patients"
            + list_where + " ORDER BY full_name", list_params,
        ),
        doctors=query(
            "SELECT doctor_id, full_name, specialization FROM doctors"
            + list_where + " ORDER BY full_name", list_params,
        ),
        preselect_patient=request.args.get("patient_id", type=int),
        preselect_doctor=request.args.get("doctor_id", type=int),
        min_datetime=datetime.now().strftime("%Y-%m-%dT%H:%M"),
        form_data=request.form if request.method == "POST" else {},
    )


@app.route("/appointment-ledger")
def appointment_ledger():
    """Excel-style, server-filtered view of every appointment in the database."""
    filters = ledger_filters()
    where_sql, params = ledger_where(filters)

    base_from = (
        " FROM appointments a "
        "JOIN patients p ON a.patient_id = p.patient_id AND p.clinic_id = a.clinic_id "
        "JOIN doctors d ON a.doctor_id = d.doctor_id AND d.clinic_id = a.clinic_id"
    )
    select_sql = (
        "SELECT a.appointment_id, a.appointment_datetime, a.token_no, a.status, a.reason,"
        " a.patient_id, a.doctor_id, p.full_name AS patient_name, p.phone,"
        " d.full_name AS doctor_name, d.specialization AS department" + base_from
    )

    total_row = query_one("SELECT COUNT(*) AS c" + base_from + where_sql, params)
    total = int(total_row["c"]) if total_row else 0
    page_count = max(1, (total + LEDGER_PAGE_SIZE - 1) // LEDGER_PAGE_SIZE)
    page = min(filters["page"], page_count)
    filters["page"] = page

    order_sql = (
        f" ORDER BY {LEDGER_SORTS[filters['sort']]} {filters['dir'].upper()}, "
        "a.appointment_datetime ASC, a.appointment_id ASC"
    )
    rows = query(
        select_sql + where_sql + order_sql
        + " OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY",
        {**params, "offset": (page - 1) * LEDGER_PAGE_SIZE, "limit": LEDGER_PAGE_SIZE},
    )

    doctor_where, doctor_params = where_clause()
    doctors = query(
        "SELECT doctor_id, full_name, specialization FROM doctors"
        + doctor_where + " ORDER BY full_name", doctor_params,
    )
    return render_template(
        "appointment_ledger.html",
        rows=rows,
        doctors=doctors,
        filters=filters,
        total=total,
        page=page,
        page_count=page_count,
        page_size=LEDGER_PAGE_SIZE,
        start_row=(page - 1) * LEDGER_PAGE_SIZE + 1 if total else 0,
        end_row=min(page * LEDGER_PAGE_SIZE, total),
        summary=appointment_period_counts(),
        active_date=(filters["date"] or date.today()).isoformat(),
        nav_prev=(filters["date"] or date.today() - timedelta(days=1)).isoformat(),
        nav_next=(filters["date"] or date.today() + timedelta(days=1)).isoformat(),
    )


@app.route("/appointments/<int:appointment_id>/status", methods=["POST"])
def update_status(appointment_id):
    require_write_access()
    new = request.form.get("status", "")
    if new not in STATUSES:
        abort(400)
    next_url = safe_next(request.form.get("next"), url_for("appointments"))
    clinic = tenant_id()

    # SELECT ... FOR UPDATE plus a tenant-qualified UPDATE: another clinic's
    # appointment cannot be found, locked or modified here.
    with transaction() as cur:
        cur.execute(
            """SELECT appointment_id, doctor_id, clinic_id, appointment_datetime, status
                 FROM appointments
                WHERE appointment_id = :i AND clinic_id = :c FOR UPDATE""",
            {"i": appointment_id, "c": clinic},
        )
        row = cur.fetchone()
        if not row:
            abort(404, description="This appointment is not in your clinic.")
        appt = {
            "appointment_id": row[0], "doctor_id": row[1], "clinic_id": row[2],
            "appointment_datetime": row[3], "status": row[4],
        }
        current = appt["status"]
        changed, message, tone = 0, "", "error"
        if new != current and new not in STATUS_TRANSITIONS.get(current, set()):
            message = (f"Cannot move this appointment from {STATUS_LABEL[current]} "
                       f"to {STATUS_LABEL[new]}.")
        elif new == "IN_PROGRESS":
            start, end = day_range(appt["appointment_datetime"].date())
            cur.execute(
                """SELECT appointment_id FROM appointments
                    WHERE clinic_id = :c AND doctor_id = :d
                      AND appointment_datetime >= :s AND appointment_datetime < :e
                      AND status = 'IN_PROGRESS' AND appointment_id <> :i""",
                {"c": clinic, "d": appt["doctor_id"], "s": start,
                 "e": end, "i": appointment_id},
            )
            if cur.fetchone():
                message = ("Another patient is already in consultation with this doctor. "
                           "Complete that visit first.")
            else:
                cur.execute(
                    "UPDATE appointments SET status = :s "
                    "WHERE appointment_id = :i AND clinic_id = :c",
                    {"s": new, "i": appointment_id, "c": clinic},
                )
                changed = cur.rowcount
        elif new == current:
            message, tone = f"Appointment is already {STATUS_LABEL[new].lower()}.", "info"
        else:
            cur.execute(
                "UPDATE appointments SET status = :s "
                "WHERE appointment_id = :i AND clinic_id = :c",
                {"s": new, "i": appointment_id, "c": clinic},
            )
            changed = cur.rowcount

    if not message:
        message = (f"Status changed to {STATUS_LABEL[new].lower()}." if changed
                   else "Appointment not found.")
        tone = "ok" if changed else "error"
    # All stats are derived from the appointments table; clear the cached
    # snapshot so dashboard, queue, reports and insights show the new state.
    invalidate_stats()
    flash(message, tone)
    return redirect(next_url)


# ---------- queue ----------
@app.route("/queue")
def queue_select():
    docs = all_doctors()
    scope = request.args.get("scope", "all")
    all_appointments = scope != "day"
    rows = all_appointment_rows() if all_appointments else today_appointment_rows(date.today())
    return render_template(
        "queue_select.html",
        doctors=build_doctor_cards(docs, rows, patient_stats=doctor_stats_by_id()),
        scope="all" if all_appointments else "day",
    )


@app.route("/queue/<int:doctor_id>")
def queue_view(doctor_id):
    where_sql, params = where_clause(["doctor_id = :i"])
    doc = query_one("SELECT * FROM doctors" + where_sql, {**params, "i": doctor_id})
    if not doc:
        abort(404, description="This doctor is not in your clinic.")
    doc["availability"] = str(doc.get("availability") or "AVAILABLE").upper()
    if doc["availability"] not in AVAILABILITY:
        doc["availability"] = "AVAILABLE"
    d = parse_date(request.args.get("date"))
    scope = request.args.get("scope", "all")
    all_appointments = scope != "day"
    if all_appointments:
        # Show the doctor's complete appointment history, including visits
        # booked for other dates, newest day first.
        where_sql, row_params = where_clause(["a.doctor_id = :d"], "a")
        rows = query(
            """SELECT a.appointment_id, a.token_no, a.status, a.reason, a.appointment_datetime,
                      p.full_name AS patient_name, p.phone
               FROM appointments a JOIN patients p
                 ON a.patient_id = p.patient_id AND p.clinic_id = a.clinic_id"""
            + where_sql + " ORDER BY a.appointment_datetime DESC, a.token_no",
            {**row_params, "d": doctor_id},
        )
    else:
        start, end = day_range(d)
        where_sql, row_params = where_clause([
            "a.doctor_id = :d", "a.appointment_datetime >= :s",
            "a.appointment_datetime < :e",
        ], "a")
        rows = query(
            """SELECT a.appointment_id, a.token_no, a.status, a.reason, a.appointment_datetime,
                      p.full_name AS patient_name, p.phone
               FROM appointments a JOIN patients p
                 ON a.patient_id = p.patient_id AND p.clinic_id = a.clinic_id"""
            + where_sql + " ORDER BY a.token_no",
            {**row_params, "d": doctor_id, "s": start, "e": end},
        )
    active = [r for r in rows if r["status"] in ACTIVE]
    closed = [r for r in rows if r["status"] not in ACTIVE]
    serving = next((r for r in active if r["status"] == "IN_PROGRESS"), None)
    nxt = next((r for r in active if r["status"] == "WAITING"), None)
    waiting_rows = [r for r in rows if r["status"] == "WAITING"]
    state = "consulting" if serving else ("waiting" if waiting_rows else "available")
    if doc["availability"] in ("BUSY", "OFFLINE"):
        state = doc["availability"].lower()
    return render_template(
        "queue.html",
        doc=doc,
        day=d,
        scope="all" if all_appointments else "day",
        prev_day=(d - timedelta(days=1)).isoformat(),
        next_day=(d + timedelta(days=1)).isoformat(),
        active=active,
        closed=closed,
        serving=serving,
        next_up=nxt,
        waiting=len(waiting_rows),
        booked=sum(r["status"] == "BOOKED" for r in rows),
        completed=sum(r["status"] == "COMPLETED" for r in rows),
        avg_wait=average_wait(waiting_rows),
        state=state,
        state_label=DOCTOR_STATE_LABEL[state],
    )


# ---------- reports ----------
@app.route("/reports")
def reports():
    d = parse_date(request.args.get("date"))
    scope = request.args.get("range", "day")
    all_time = scope == "all"
    start, end = (None, None) if all_time else day_range(d)
    if all_time:
        status_where, p = where_clause()
    else:
        status_where, p = where_clause([
            "appointment_datetime >= :s", "appointment_datetime < :e"
        ])
        p.update({"s": start, "e": end})
    by_status = query(
        "SELECT status, COUNT(*) AS total FROM appointments"
        + status_where + " GROUP BY status ORDER BY status", p,
    )
    # Use the shared doctor-wise statistics query so reports, insights and the
    # doctor cards always agree.  LEFT JOIN keeps doctors with no visits on the
    # report instead of silently dropping them.
    by_doctor = doctor_patient_stats(start, end)
    detail_conditions = [] if all_time else [
        "a.appointment_datetime >= :s", "a.appointment_datetime < :e"
    ]
    detail_where, detail_params = where_clause(detail_conditions, "a")
    if not all_time:
        detail_params.update({"s": start, "e": end})
    detail = query(
        """SELECT a.token_no, a.status, a.appointment_datetime,
                  p.full_name AS patient_name, d.full_name AS doctor_name
           FROM appointments a
           JOIN patients p ON a.patient_id = p.patient_id AND p.clinic_id = a.clinic_id
           JOIN doctors d ON a.doctor_id = d.doctor_id AND d.clinic_id = a.clinic_id"""
        + detail_where + " ORDER BY d.full_name, a.token_no",
        detail_params,
    )

    counts = {s: 0 for s in STATUSES}
    counts.update({r["status"]: r["total"] for r in by_status})
    total = sum(counts.values())
    completed = counts.get("COMPLETED", 0)
    cancelled = counts.get("CANCELLED", 0)
    status_rows = [
        {"status": s, "label": STATUS_LABEL[s], "total": counts[s]}
        for s in STATUSES
    ]
    for row in by_doctor:
        row["completion_rate"] = completion_rate(row["completed"], row["total"])

    return render_template(
        "reports.html",
        day=d,
        range="all" if all_time else "day",
        prev_day=(d - timedelta(days=1)).isoformat(),
        next_day=(d + timedelta(days=1)).isoformat(),
        by_status=by_status,
        status_rows=status_rows,
        by_doctor=by_doctor,
        detail=detail,
        total=total,
        completed=completed,
        cancelled=cancelled,
        completion_rate=completion_rate(completed, total),
        cancelled_rate=round(cancelled / total * 100) if total else 0,
    )


# ---------- insights ----------
@app.route("/insights")
def insights():
    try:
        range_days = int(request.args.get("range", 7))
    except (TypeError, ValueError):
        range_days = 7
    if range_days not in (7, 30):
        range_days = 7

    end_date = date.today()
    start_date = end_date - timedelta(days=range_days - 1)
    start = datetime.combine(start_date, datetime.min.time())
    end = datetime.combine(end_date + timedelta(days=1), datetime.min.time())

    period_where, period_params = where_clause([
        "appointment_datetime >= :s", "appointment_datetime < :e"
    ])
    grouped = query(
        """SELECT TRUNC(CAST(appointment_datetime AS DATE)) AS day_start,
                  status, COUNT(*) AS total
           FROM appointments""" + period_where +
        """ GROUP BY TRUNC(CAST(appointment_datetime AS DATE)), status""",
        {**period_params, "s": start, "e": end},
    )
    by_day = defaultdict(lambda: {s: 0 for s in STATUSES})
    for row in grouped:
        raw_day = row["day_start"]
        if isinstance(raw_day, datetime):
            raw_day = raw_day.date()
        by_day[raw_day][row["status"]] += int(row["total"])

    day_series = []
    status_totals = {s: 0 for s in STATUSES}
    for offset in range(range_days):
        d = start_date + timedelta(days=offset)
        c = by_day.get(d, {s: 0 for s in STATUSES})
        day_total = sum(c.values())
        for status in STATUSES:
            status_totals[status] += c[status]
        day_series.append({
            "date": d,
            "label": d.strftime("%d"),
            "full_label": d.strftime("%a %d %b"),
            "total": day_total,
            "completed": c["COMPLETED"],
            "cancelled": c["CANCELLED"],
            "active": c["BOOKED"] + c["WAITING"] + c["IN_PROGRESS"],
        })

    peak_value = max((day["total"] for day in day_series), default=1) or 1
    for day in day_series:
        day["total_pct"] = round(day["total"] / peak_value * 100)
        day["done_pct"] = round(day["completed"] / peak_value * 100)

    hour_rows = query(
        """SELECT TO_CHAR(appointment_datetime, 'HH24') AS hour_no,
                  COUNT(*) AS total
           FROM appointments""" + period_where +
        " GROUP BY TO_CHAR(appointment_datetime, 'HH24')",
        {**period_params, "s": start, "e": end},
    )
    hour_map = {int(r["hour_no"]): int(r["total"]) for r in hour_rows}
    peak_hour_total = max(hour_map.values(), default=1) or 1
    busiest_hour = max(hour_map, key=hour_map.get) if hour_map else None
    peak_hours = []
    for hour in range(7, 20):
        value = hour_map.get(hour, 0)
        peak_hours.append({
            "hour": hour,
            "label": f"{hour:02d}",
            "total": value,
            "pct": round(value / peak_hour_total * 100),
            "is_peak": hour == busiest_hour,
        })

    # Shared doctor-wise statistics for the selected range.  Sorting stays in
    # Python so the SQL definition remains identical everywhere it is used.
    doctor_rows = sorted(
        doctor_patient_stats(start, end),
        key=lambda row: (-row["total"], row["full_name"].lower()),
    )

    patients_where, patients_params = where_clause([
        "created_at >= :s", "created_at < :e"
    ])
    new_patients_row = query_one(
        "SELECT COUNT(*) AS c FROM patients" + patients_where,
        {**patients_params, "s": start, "e": end},
    )
    new_patients = int(new_patients_row["c"]) if new_patients_row else 0

    total = sum(status_totals.values())
    completed = status_totals["COMPLETED"]
    busiest_day = max(day_series, key=lambda day: day["total"]) if day_series else None
    status_rows = [
        {"status": s, "label": STATUS_LABEL[s], "total": status_totals[s]}
        for s in STATUSES
    ]

    return render_template(
        "insights.html",
        range_days=range_days,
        range_start=start_date,
        range_end=end_date,
        day_series=day_series,
        peak_hours=peak_hours,
        busiest_hour=f"{busiest_hour:02d}:00" if busiest_hour is not None else "—",
        doctor_rows=doctor_rows,
        status_rows=status_rows,
        status_totals=status_totals,
        total=total,
        completed=completed,
        completion_rate=completion_rate(completed, total),
        new_patients=new_patients,
        avg_per_day=round(total / range_days, 1) if range_days else 0,
        busiest_day=busiest_day,
    )


# ---------- doctor statistics API ----------
@app.route("/api/doctor-stats")
def doctor_stats_api():
    """JSON view of the same doctor-wise statistics used by the pages.

    ``range`` may be ``all`` (default), ``today``, ``7`` or ``30``.  All figures
    come from the live appointments table, so the response is never stale.
    """
    scope = request.args.get("range", "all")
    start = end = None
    if scope == "today":
        start, end = day_range(date.today())
    elif scope in ("7", "30"):
        end = datetime.combine(date.today() + timedelta(days=1), datetime.min.time())
        start = end - timedelta(days=int(scope))
    else:
        scope = "all"
    return {"range": scope, "doctors": doctor_patient_stats(start, end)}


# ---------- platform administration (deliberately separate from clinic admin) ----------
@app.route("/admin/clinics")
def admin_clinics():
    """Global tenant directory. Available only to platform administrators."""
    require_platform_admin()
    clinics = query(
        """SELECT c.clinic_id, c.clinic_name, c.slug, c.email, c.status,
                  c.is_default, c.created_at,
                  (SELECT COUNT(*) FROM users u WHERE u.clinic_id = c.clinic_id) AS users,
                  (SELECT COUNT(*) FROM patients p WHERE p.clinic_id = c.clinic_id) AS patients,
                  (SELECT COUNT(*) FROM doctors d WHERE d.clinic_id = c.clinic_id) AS doctors,
                  (SELECT COUNT(*) FROM appointments a WHERE a.clinic_id = c.clinic_id) AS appointments
             FROM clinics c
            ORDER BY c.clinic_id DESC"""
    )
    accounts = query(
        """SELECT u.user_id, u.full_name, u.username, u.email, u.role,
                  u.clinic_id, c.clinic_name
             FROM users u LEFT JOIN clinics c ON c.clinic_id = u.clinic_id
            ORDER BY u.user_id DESC"""
    )
    totals = query_one(
        """SELECT (SELECT COUNT(*) FROM clinics) AS clinics,
                  (SELECT COUNT(*) FROM users) AS accounts,
                  (SELECT COUNT(*) FROM patients) AS patients,
                  (SELECT COUNT(*) FROM doctors) AS doctors,
                  (SELECT COUNT(*) FROM appointments) AS appointments
             FROM dual"""
    ) or {}
    return render_template(
        "admin_clinics.html",
        clinics=clinics,
        accounts=accounts,
        totals=totals,
        acting_clinic_id=session.get("acting_clinic_id"),
    )


@app.route("/admin/clinics/<int:clinic_id>/work", methods=["POST"])
def admin_work_as_clinic(clinic_id):
    """Platform admin explicitly scopes their session to one tenant."""
    require_platform_admin()
    clinic = query_one(
        "SELECT clinic_id, clinic_name FROM clinics "
        "WHERE clinic_id = :i AND status = 'ACTIVE'", {"i": clinic_id},
    )
    if not clinic:
        abort(404)
    session["acting_clinic_id"] = int(clinic_id)
    session["clinic_name"] = clinic["clinic_name"]
    invalidate_pulse()
    flash(f"Now working in {clinic['clinic_name']}.", "ok")
    return redirect(url_for("index"))


@app.route("/admin/clinics/global", methods=["POST"])
def admin_global_view():
    """Return a platform admin session from one clinic to the global view."""
    require_platform_admin()
    session.pop("acting_clinic_id", None)
    session["clinic_name"] = "MediFlow Platform"
    invalidate_pulse()
    flash("Global platform view restored.", "ok")
    return redirect(url_for("admin_clinics"))


# ---------- settings ----------
@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        if (request.form.get("action") or "") == "reset":
            session.pop("prefs", None)
            flash("Preferences reset to the clinic defaults.", "ok")
        else:
            try:
                refresh = int(request.form.get("queue_refresh", "30"))
            except (TypeError, ValueError):
                refresh = 30
            if refresh not in (15, 30, 60):
                refresh = 30
            density = request.form.get("density", "comfortable")
            if density not in ("comfortable", "compact"):
                density = "comfortable"
            session["prefs"] = {
                "notifications": request.form.get("notifications") == "on",
                "queue_refresh": refresh,
                "density": density,
            }
            flash("Preferences saved.", "ok")
        return redirect(url_for("settings"))

    counts = {}
    try:
        scope_sql, scope_params = where_clause()
        row = query_one(
            f"""SELECT (SELECT COUNT(*) FROM patients{scope_sql}) AS patients,
                      (SELECT COUNT(*) FROM doctors{scope_sql}) AS doctors,
                      (SELECT COUNT(*) FROM appointments{scope_sql}) AS appointments
               FROM dual""",
            scope_params,
        )
        counts = row or {}
    except oracledb.Error:
        app.logger.exception("Could not load settings counts")

    return render_template(
        "settings.html",
        db_ok=database_ok(),
        counts=counts,
    )


# ---------- help ----------
@app.route("/help")
def help():
    return render_template("help.html", db_ok=database_ok())


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1")
