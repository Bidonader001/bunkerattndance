"""Absence Less admin console: live attendance aggregates plus isolated preview modules."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from core import db

PREVIEW_NOTE = (
    "Preview only — this module is not stored in the database yet. "
    "Actions here are not saved."
)

SETTING_KEYS = (
    "company_name",
    "timezone",
    "working_days",
    "contact",
    "grace_minutes",
    "duplicate_scan_seconds",
    "overtime_policy",
)

SETTING_DEFAULTS = {
    "company_name": "Bunker Attendance",
    "timezone": "Asia/Riyadh",
    "working_days": "Sun-Thu",
    "contact": "",
    "grace_minutes": "10",
    "duplicate_scan_seconds": "30",
    "overtime_policy": "Do not treat raw duration as payroll hours until policy is applied.",
}

PREVIEW_MODULES = {
    "branches",
    "integrations",
    "recognition",
}


def _iso_today() -> str:
    return date.today().isoformat()


def _setting(key: str) -> str:
    with db.get_connection() as conn:
        row = conn.execute("SELECT value FROM gym_settings WHERE key = ?", (key,)).fetchone()
    if row and row[0] not in (None, ""):
        return str(row[0])
    return SETTING_DEFAULTS.get(key, "")


def company_settings() -> dict:
    values = {key: _setting(key) for key in SETTING_KEYS}
    values["face_premium"] = db.face_premium_enabled()
    return {
        "source": "live",
        "note": "Company, attendance policy, and Face ID settings are saved. Recognition hardware rules remain preview.",
        "values": values,
    }


def save_company_settings(payload: dict) -> dict:
    with db.get_connection() as conn:
        for key in SETTING_KEYS:
            if key not in payload:
                continue
            value = str(payload.get(key) or "").strip()
            conn.execute(
                """
                INSERT INTO gym_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
        if "face_premium" in payload:
            db.set_face_premium(bool(payload.get("face_premium")))
    return company_settings()


def overview() -> dict:
    people = db.list_employees()
    today = _iso_today()
    sessions = db.list_sessions(work_date=today)
    present_ids = {s.get("employee_id") for s in sessions}
    missing = [s for s in sessions if not s.get("clock_out")]
    onsite = db.list_open_sessions()
    on = date.today()
    week = []
    for i in range(6, -1, -1):
        d = on - timedelta(days=i)
        rows = db.list_sessions(work_date=d.isoformat())
        week.append({"date": d.isoformat(), "checkins": len(rows)})
    present = len(present_ids)
    total = len(people)
    absent = max(0, total - present)
    late = sum(1 for s in sessions if s.get("late"))
    company = _setting("company_name") or "Bunker Attendance"
    branch = "Headquarters"
    distribution = [
        {"label": "Present", "value": max(0, present - late), "tone": "ok"},
        {"label": "Late", "value": late, "tone": "late"},
        {"label": "Absent", "value": absent, "tone": "warn"},
        {"label": "Open sessions", "value": len(onsite), "tone": "over"},
    ]
    activity = []
    for s in sessions[:16]:
        activity.append(
            {
                "name": s.get("employee_name") or "—",
                "employee_id": s.get("employee_id") or "—",
                "event": "Check-out" if s.get("clock_out") else "Check-in",
                "time": str(s.get("clock_out") or s.get("clock_in") or "")[:16].replace("T", " "),
                "status": s.get("late_label") or "On time",
                "source": "live",
            }
        )
    pending = []
    for s in missing:
        pending.append(
            {
                "kind": "missing_checkout",
                "title": "Missing check-out",
                "detail": f"{s.get('employee_name')} · in since {str(s.get('clock_in') or '')[:16].replace('T', ' ')}",
                "href": "/admin?page=attendance",
                "source": "live",
            }
        )
    result = {
        "source": "live",
        "company": company,
        "branch": branch,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stats": [
            {"key": "employees", "label": "Total active employees", "value": total, "href": "/admin?page=people", "source": "live"},
            {"key": "present", "label": "Present today", "value": present, "href": "/admin?page=attendance", "source": "live"},
            {"key": "absent", "label": "Absent today", "value": absent, "href": "/admin?page=attendance", "source": "live"},
            {"key": "late", "label": "Late arrivals", "value": late, "href": "/admin?page=reports", "source": "live"},
            {"key": "working", "label": "Currently working", "value": len(onsite), "href": "/admin?page=dashboard", "source": "live"},
            {"key": "missing", "label": "Missing check-outs", "value": len(missing), "href": "/admin?page=attendance", "source": "live"},
        ],
        "week": week,
        "distribution": distribution,
        "activity": activity,
        "onsite": onsite,
        "pending": pending,
        "branches": [
            {
                "name": branch,
                "employees": total,
                "present": present,
                "absent": absent,
                "pct": round(100 * present / total, 1) if total else 0,
                "source": "live",
            }
        ],
        "devices": [],
        "unread_alerts": len(pending),
    }
    return _stamp_today(_attach_month_board(result), sessions, today)


def _attach_month_board(payload: dict) -> dict:
    try:
        from core import payroll

        board = payroll.month_board()
    except Exception:
        board = {
            "employees": [],
            "currency": "EGP",
            "through": _iso_today(),
            "days_off_allowance": 4,
            "month": _iso_today()[:7],
        }
    payload["staff"] = board.get("employees") or []
    payload["staff_through"] = board.get("through") or _iso_today()
    payload["staff_month"] = board.get("month") or _iso_today()[:7]
    payload["currency"] = board.get("currency") or "EGP"
    payload["days_off_allowance"] = board.get("days_off_allowance") or 4
    return payload


def _stamp_today(payload: dict, sessions: list, today: str) -> dict:
    status_by_id: dict[str, str] = {}
    for session in sessions:
        eid = str(session.get("employee_id") or "")
        if not eid:
            continue
        if not session.get("clock_out"):
            status_by_id[eid] = "Missing Check-out"
        elif session.get("late"):
            status_by_id.setdefault(eid, "Late")
        else:
            status_by_id.setdefault(eid, "Present")
    absent_ids = {str(row.get("employee_id") or "") for row in db.list_day_leave(today, "unpermitted")}
    day_off_ids = {str(row.get("employee_id") or "") for row in db.list_day_leave(today, "day_off")}
    for row in payload.get("staff") or []:
        eid = str(row.get("employee_id") or "")
        if eid in status_by_id:
            row["today"] = status_by_id[eid]
        elif eid in absent_ids:
            row["today"] = "Absent"
        elif eid in day_off_ids:
            row["today"] = "Day off"
        elif row.get("work_type") == "part" and date.fromisoformat(today).weekday() not in {3, 4, 5}:
            row["today"] = "Off"
        else:
            row["today"] = "No clock-in"
        row["today_date"] = today
    return payload


def attendance_rows(work_date: str = "", name: str = "") -> dict:
    day = (work_date or _iso_today()).strip()
    rows = db.list_sessions(work_date=day, name=name)
    out = []
    for s in rows:
        if not s.get("clock_out"):
            status = "Missing Check-out"
        elif s.get("late"):
            status = "Late"
        else:
            status = "Present"
        out.append(
            {
                "id": s.get("id"),
                "employee_name": s.get("employee_name"),
                "employee_id": s.get("employee_id"),
                "date": s.get("work_date"),
                "scheduled_start": s.get("start_time") or "—",
                "clock_in": str(s.get("clock_in") or "")[:16].replace("T", " "),
                "scheduled_end": s.get("end_time") or "—",
                "clock_out": str(s.get("clock_out") or "")[:16].replace("T", " ") if s.get("clock_out") else "—",
                "hours": s.get("hours_formatted") or "—",
                "late_minutes": int(s.get("minutes_late") or 0),
                "late": s.get("late_label") or "On time",
                "overtime": "—",
                "status": status,
                "shift": s.get("shift_name") or "Auto",
                "device": s.get("device_mac") or "Kiosk",
                "source": "live",
            }
        )
    seen = {str(row["employee_id"]) for row in out}
    needle = name.strip().lower()

    def _marked(leave: dict, status: str, prefix: str, device_fallback: str) -> None:
        eid = str(leave.get("employee_id") or "")
        if not eid or eid in seen:
            return
        label = leave.get("employee_name") or eid
        if needle and needle not in label.lower() and needle not in eid.lower():
            return
        seen.add(eid)
        out.append(
            {
                "id": f"{prefix}-{leave.get('id')}",
                "employee_name": label,
                "employee_id": eid,
                "date": day,
                "scheduled_start": "—",
                "clock_in": "—",
                "scheduled_end": "—",
                "clock_out": "—",
                "hours": "—",
                "late_minutes": 0,
                "late": "—",
                "overtime": "—",
                "status": status,
                "shift": "—",
                "device": leave.get("actor") or device_fallback,
                "source": "live",
            }
        )

    for leave in db.list_day_leave(day, "unpermitted"):
        _marked(leave, "Absent", "absent", "Marked absent")
    for leave in db.list_day_leave(day, "day_off"):
        _marked(leave, "Day off", "dayoff", "Day off")
    for person in db.list_employees():
        if (person.get("employment_status") or "active").lower() == "inactive":
            continue
        eid = str(person.get("employee_id") or "")
        if not eid or eid in seen:
            continue
        label = person.get("name") or eid
        if needle and needle not in label.lower() and needle not in eid.lower():
            continue
        try:
            work_day = date.fromisoformat(day)
        except ValueError:
            work_day = date.today()
        if str(person.get("work_type") or "full") == "part" and work_day.weekday() not in {3, 4, 5}:
            continue
        seen.add(eid)
        out.append(
            {
                "id": f"noclock-{eid}",
                "employee_name": label,
                "employee_id": eid,
                "date": day,
                "scheduled_start": "—",
                "clock_in": "—",
                "scheduled_end": "—",
                "clock_out": "—",
                "hours": "—",
                "late_minutes": 0,
                "late": "—",
                "overtime": "—",
                "status": "No clock-in",
                "shift": person.get("shift_name") or "—",
                "device": "—",
                "source": "live",
            }
        )
    out.sort(key=lambda row: (str(row.get("employee_name") or ""), str(row.get("clock_in") or "")))
    return {"source": "live", "work_date": day, "rows": out, "audits": db.list_attendance_audit(40)}


def request_correction(
    *,
    session_id: int,
    field: str,
    new_value: str,
    reason: str,
    actor: str,
) -> dict:
    field = (field or "").strip()
    if field not in {"clock_in", "clock_out"}:
        raise ValueError("Only clock_in or clock_out can be corrected")
    if not reason.strip():
        raise ValueError("An explanation is required. Original events are never overwritten.")
    session = db.get_session(session_id)
    if not session:
        raise ValueError("Attendance record not found")
    old = str(session.get(field) or "")
    return db.add_attendance_audit(
        session_id=session_id,
        employee_id=str(session.get("employee_id") or ""),
        employee_name=str(session.get("employee_name") or ""),
        field=field,
        old_value=old,
        new_value=new_value.strip(),
        reason=reason,
        actor=actor,
    )


def notifications() -> dict:
    today = _iso_today()
    sessions = db.list_sessions(work_date=today)
    items = []
    for s in sessions:
        if s.get("late"):
            items.append(
                {
                    "id": f"late-{s.get('id')}",
                    "type": "late",
                    "title": "Late arrival",
                    "message": f"{s.get('employee_name')} · {s.get('late_label')}",
                    "href": "/admin?page=attendance",
                    "read": False,
                    "time": str(s.get("clock_in") or "")[:16].replace("T", " "),
                    "source": "live",
                }
            )
        if not s.get("clock_out"):
            items.append(
                {
                    "id": f"open-{s.get('id')}",
                    "type": "missing_checkout",
                    "title": "Missing check-out",
                    "message": f"{s.get('employee_name')} is still clocked in",
                    "href": "/admin?page=attendance",
                    "read": False,
                    "time": str(s.get("clock_in") or "")[:16].replace("T", " "),
                    "source": "live",
                }
            )
    return {"source": "live", "items": items, "unread": len(items)}


def preview(module: str) -> dict:
    module = (module or "").strip().lower()
    catalogs = {
        "branches": {
            "title": "Branches & departments",
            "columns": ["Name", "Code", "Manager", "Employees", "Status"],
            "rows": [["Headquarters", "HQ", "—", str(len(db.list_employees())), "Active (live employee count)"]],
        },
        "integrations": {
            "title": "API & integrations",
            "columns": ["Client", "Scopes", "Last used", "Status"],
            "rows": [["None issued", "employees:read attendance:read", "—", "Secrets are only shown once, after this module is persisted"]],
        },
        "recognition": {
            "title": "Recognition settings",
            "columns": ["Setting", "Value", "Source"],
            "rows": [
                ["Face premium", "on" if db.face_premium_enabled() else "off", "live"],
                ["Liveness", "not configured", "preview"],
                ["Enrollment policy", "5 captures · quality checks on", "live"],
            ],
        },
    }
    body = catalogs.get(module)
    if not body:
        return {"source": "preview", "note": PREVIEW_NOTE, "module": module, "error": "Unknown module"}
    return {"source": "preview", "note": PREVIEW_NOTE, "module": module, **body}
