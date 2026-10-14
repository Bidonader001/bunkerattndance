"""Absence Less web server."""

from __future__ import annotations

import json
import os
import re
import threading
from html import escape as html_escape
from urllib.parse import quote
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from fastapi import Cookie, Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core import console, db, exports, netinfo, reports
from core.auth import create_token, hash_password, read_token, verify_password
from core.face_engine import MATCH_THRESHOLD, FaceEngine
from core.face_quality import validate_enrollment_face
from core.images import decode_image_b64

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
ASSETS_DIR = ROOT / "assets"
CAPTURES_REQUIRED = 5

_engine = FaceEngine()
_enroll_engine = FaceEngine(det_size=(640, 640), min_det_score=0.18)
_engine_lock = threading.Lock()


def get_engine() -> FaceEngine:
    with _engine_lock:
        if not _engine.is_ready:
            if not _engine.initialize():
                raise HTTPException(
                    status_code=503,
                    detail=_engine.load_error or "Face engine failed to load",
                )
        return _engine


def get_enroll_engine() -> FaceEngine:
    with _engine_lock:
        if not _enroll_engine.is_ready:
            if not _enroll_engine.initialize():
                raise HTTPException(
                    status_code=503,
                    detail=_enroll_engine.load_error or "Face engine failed to load",
                )
        return _enroll_engine


def require_admin(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
    access_pass_token: str | None = Cookie(default=None),
) -> str:
    raw = None
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization.split(" ", 1)[1].strip()
    elif token:
        raw = token.strip()
    elif access_pass_token:
        raw = access_pass_token.strip()
    if not raw:
        raise HTTPException(status_code=401, detail="Admin login required")
    user = read_token(raw)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired. Sign in again.")
    return user


def require_super(user: dict = Depends(require_admin)) -> dict:
    if user.get("role") != "super":
        raise HTTPException(status_code=403, detail="Super admin only")
    return user


def identify_frame(image_b64: str) -> dict:
    try:
        frame = decode_image_b64(image_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid camera frame")
    engine = get_engine()
    faces = engine.detect_faces(frame)
    if len(faces) == 0:
        return {"status": "no_face"}
    if len(faces) > 1:
        return {"status": "multiple_faces"}
    employees = db.get_all_employees()
    if not employees:
        return {"status": "unrecognized"}
    match = engine.find_best_match(faces[0].embedding, employees, MATCH_THRESHOLD)
    if match is None:
        return {"status": "unrecognized"}
    employee, confidence = match
    matched_shift, existing = db.current_shift_state(employee)
    open_row = db.get_open_session(employee["employee_id"])
    open_session = db._session_out(open_row) if open_row else None
    shift_name = None
    if matched_shift and matched_shift.get("shift"):
        shift_name = matched_shift["shift"].get("name")
    if open_session and open_session.get("shift_name"):
        shift_name = open_session.get("shift_name")
    until = None if open_session else db.clock_in_blocked_until(employee["employee_id"])
    cooldown = None
    if until:
        cooldown = {
            "until": until.isoformat(),
            "hours": db.COOLDOWN_HOURS,
            "message": f"Hi {employee['name']}, next clock-in after {until.strftime('%H:%M')}.",
        }
    return {
        "status": "matched",
        "name": employee["name"],
        "employee_id": employee["employee_id"],
        "confidence": round(float(confidence), 3),
        "shift_name": shift_name or employee.get("shift_name"),
        "clocked_in": open_session is not None,
        "already_this_shift": bool(existing and existing.get("clock_out") and not open_session),
        "session": open_session,
        "cooldown": cooldown,
    }


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()

    def _warm():
        try:
            get_engine()
            get_enroll_engine()
        except Exception:
            pass

    threading.Thread(target=_warm, daemon=True, name="ModelWarmup").start()
    yield


app = FastAPI(title="Absence Less", version="2.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def disable_hot_cache(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/") or path.startswith("/assets/") or path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


class CaptureBody(BaseModel):
    image_b64: str = Field(min_length=32)
    previous_b64: str | None = None
    device_mac: str | None = None


class EnrollBody(BaseModel):
    employee_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    shift_id: int | None = None
    phone: str = ""
    emergency_name: str = ""
    emergency_phone: str = ""
    images_b64: list[str] = Field(default_factory=list, max_length=12)


class EmployeeUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    new_employee_id: str = Field(min_length=1, max_length=64)
    shift_id: int | None = None
    phone: str | None = None
    emergency_name: str | None = None
    emergency_phone: str | None = None
    images_b64: list[str] | None = None


class ShiftBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    start_time: str
    end_time: str


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class AdminCreateBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=6, max_length=128)


class AdminUpdateBody(BaseModel):
    username: str | None = Field(default=None, min_length=1, max_length=64)
    password: str | None = Field(default=None, min_length=6, max_length=128)


class FaceSettingBody(BaseModel):
    enabled: bool


class CorrectionBody(BaseModel):
    field: str
    new_value: str
    reason: str


class AuditStatusBody(BaseModel):
    status: str


class ConsoleSettingsBody(BaseModel):
    company_name: str | None = None
    timezone: str | None = None
    working_days: str | None = None
    contact: str | None = None
    grace_minutes: str | None = None
    duplicate_scan_seconds: str | None = None
    overtime_policy: str | None = None
    face_premium: bool | None = None


class ConsoleEmployeeBody(BaseModel):
    employee_id: str
    name: str
    phone: str = ""
    email: str = ""
    job_title: str = ""
    department: str = ""
    branch: str = "HQ"
    employment_status: str = "active"
    joining_date: str = ""
    manager: str = ""
    shift_id: int | None = None


class EmploymentStatusBody(BaseModel):
    status: str


class DeviceBody(BaseModel):
    name: str
    mac_address: str
    branch: str = "HQ"
    location: str = ""


class DevicePatchBody(BaseModel):
    name: str | None = None
    branch: str | None = None
    location: str | None = None
    status: str | None = None


class DeviceHeartbeatBody(BaseModel):
    mac_address: str = ""
    camera: bool = False


class DevicePreviewBody(BaseModel):
    image_b64: str
    camera: bool = True


def _hhmm(value: str) -> str:
    parts = value.strip().split(":")
    if len(parts) < 2:
        raise HTTPException(status_code=400, detail="Time must be HH:MM")
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}"


def _single_enroll_face(frame, engine: FaceEngine):
    faces = engine.detect_faces(frame)
    if len(faces) == 0:
        raise HTTPException(status_code=400, detail="No face detected — look at the camera")
    if len(faces) > 1:
        raise HTTPException(status_code=400, detail="Multiple faces — only one person")
    face = faces[0]
    try:
        validate_enrollment_face(frame, face.bbox, face.confidence)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return face


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "app": "Absence Less",
        "models_ready": _engine.is_ready,
        "models_message": None if _engine.is_ready else (_engine.load_error or "loading"),
    }


def _auth_cookie(response: Response, token: str | None) -> Response:
    if token:
        response.set_cookie(
            key="access_pass_token",
            value=token,
            max_age=12 * 3600,
            httponly=False,
            secure=True,
            samesite="lax",
            path="/",
        )
    else:
        response.delete_cookie("access_pass_token", path="/")
    return response


@app.post("/api/v1/auth/login")
def login(body: LoginBody):
    db.init_db()
    user = db.get_user(body.username)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Wrong username or password")
    role = user.get("role") or "admin"
    token = create_token(user["username"], role)
    return _auth_cookie(
        JSONResponse({"token": token, "username": user["username"], "role": role}),
        token,
    )


@app.post("/api/v1/auth/logout")
def logout():
    return _auth_cookie(JSONResponse({"ok": True}), None)


def _admin_href(page: str, token: str | None) -> str:
    if token:
        return f"/admin?page={page}&t={token}"
    return f"/admin?page={page}"


def _stat_cards(pairs: list[tuple]) -> str:
    return "".join(
        f'<article class="stat-card"><p class="eyebrow">{html_escape(str(k))}</p><strong>{html_escape(str(v))}</strong></article>'
        for k, v in pairs
    )


_ADMIN_PAGES = (
    "dashboard",
    "register",
    "people",
    "attendance",
    "shifts",
    "reports",
    "branches",
    "devices",
    "admins",
    "integrations",
    "notifications",
    "settings",
)


def _mark_admin_pages(html: str, page: str) -> str:
    keys = "|".join(_ADMIN_PAGES)

    def repl(match: re.Match) -> str:
        key = match.group(1)
        if key == page:
            return f'id="page-{key}" class="page active" style="display:block;visibility:visible"'
        return f'id="page-{key}" class="page" hidden style="display:none"'

    html = re.sub(rf'id="page-({keys})"[^>]*', repl, html, count=len(_ADMIN_PAGES))
    html = html.replace(
        "</head>",
        (
            f'<style id="ap-page">#page-{page},#page-{page} *{{visibility:visible}}'
            f"#page-{page}{{display:block!important;height:auto!important;overflow:visible!important}}"
            "body.is-authed #app-shell,.app-shell{display:block!important;height:auto!important;overflow:visible!important}"
            "body.is-authed .content,.content{display:block!important;overflow:visible!important;"
            "height:auto!important;min-height:240px}"
            ".page[hidden]{display:none!important}"
            ".form-grid-3,.form-grid{display:block!important}"
            "label{display:block;margin:0 0 12px}"
            "input,select{display:block;width:100%;max-width:100%;box-sizing:border-box}"
            "</style>\n</head>"
        ),
        1,
    )
    return html


def _fill_admin_data(
    html: str,
    page: str,
    token: str | None,
    role: str,
    *,
    msg: str = "",
    period: str = "daily",
    work_date: str = "",
    edit: str = "",
) -> str:
    db.init_db()
    html = html.replace('name="t" value=""', f'name="t" value="{html_escape(token or "")}"')
    if msg:
        html = html.replace(
            '<p id="admin-flash" class="banner hidden"></p>',
            f'<p id="admin-flash" class="banner">{html_escape(msg)}</p>',
            1,
        )
    html = _mark_admin_pages(html, page)
    html = html.replace('class="nav-btn active"', 'class="nav-btn"')
    html = html.replace(
        f'class="nav-btn" data-page="{page}"',
        f'class="nav-btn active" data-page="{page}"',
        1,
    )
    for key in _ADMIN_PAGES:
        html = html.replace(f'href="/admin?page={key}"', f'href="{_admin_href(key, token)}"')
    html = html.replace('href="/admin/logout"', f'href="/admin/logout?t={token or ""}"')

    if page == "dashboard":
        overview = console.overview()
        onsite = overview.get("onsite") or []
        cards = "".join(
            (
                f'<a class="stat-card" href="{html_escape(str(s.get("href") or "#"))}">'
                f'<p class="eyebrow">{html_escape(str(s.get("label") or ""))}'
                f'{" · preview" if s.get("source") == "preview" else ""}</p>'
                f'<strong>{html_escape(str(s.get("value") if s.get("value") is not None else "—"))}</strong></a>'
            )
            for s in (overview.get("stats") or [])
        )
        html = html.replace('<div id="dash-cards" class="stats-grid"></div>', f'<div id="dash-cards" class="stats-grid">{cards}</div>', 1)
        if onsite:
            rows = "".join(
                f'<article class="log-row"><header><span>{html_escape(s.get("employee_name"))}</span>'
                f'<span class="granted-tag">{html_escape(s.get("shift_name") or "—")}</span></header>'
                f'<p class="muted">In since {html_escape(str(s.get("clock_in") or "").replace("T", " ")[:16])}</p></article>'
                for s in onsite
            )
        else:
            rows = '<p class="muted">Nobody is clocked in right now.</p>'
        html = html.replace('<div id="dash-onsite" class="log-list"></div>', f'<div id="dash-onsite" class="log-list">{rows}</div>', 1)
        week = overview.get("week") or []
        max_c = max([1, *[int(d.get("checkins") or 0) for d in week]])
        cols = []
        for d in week:
            c = int(d.get("checkins") or 0)
            h = max(4, round(100 * c / max_c))
            cols.append(
                f'<div class="week-col"><div class="week-bar" style="height:{h}%"></div>'
                f'<span>{html_escape(str(d.get("date") or "")[5:])}<br>{c}</span></div>'
            )
        html = html.replace('<div id="dash-week" class="week-bars"></div>', f'<div id="dash-week" class="week-bars">{"".join(cols)}</div>', 1)

    shifts = db.list_shifts()
    shift_opts = ['<option value="">Auto at entrance</option>']
    for s in shifts:
        sel = ""
        shift_opts.append(
            f'<option value="{s["id"]}"{sel}>{html_escape(s["name"])}  {html_escape(s["start_time"])}–{html_escape(s["end_time"])}</option>'
        )
    html = html.replace(
        '<select id="enroll-shift" name="shift_id"></select>',
        f'<select id="enroll-shift" name="shift_id">{"".join(shift_opts)}</select>',
        1,
    )

    if page == "register" and edit:
        person = next((p for p in db.list_employees() if p.get("employee_id") == edit), None)
        if person:
            html = html.replace(
                'id="enroll-editing" value=""',
                f'id="enroll-editing" value="{html_escape(person["employee_id"])}"',
                1,
            )
            html = html.replace(
                'id="enroll-name" name="name" type="text" value=""',
                f'id="enroll-name" name="name" type="text" value="{html_escape(person.get("name"))}"',
                1,
            )
            html = html.replace(
                'id="enroll-id" name="employee_id" type="text" value=""',
                f'id="enroll-id" name="employee_id" type="text" value="{html_escape(person.get("employee_id"))}"',
                1,
            )
            html = html.replace(
                'id="enroll-phone" name="phone" type="tel" value=""',
                f'id="enroll-phone" name="phone" type="tel" value="{html_escape(person.get("phone") or "")}"',
                1,
            )
            html = html.replace(
                'id="enroll-em-name" name="emergency_name" type="text" value=""',
                f'id="enroll-em-name" name="emergency_name" type="text" value="{html_escape(person.get("emergency_name") or "")}"',
                1,
            )
            html = html.replace(
                'id="enroll-em-phone" name="emergency_phone" type="tel" value=""',
                f'id="enroll-em-phone" name="emergency_phone" type="tel" value="{html_escape(person.get("emergency_phone") or "")}"',
                1,
            )
            html = html.replace(
                'id="enroll-email" name="email" type="email" value=""',
                f'id="enroll-email" name="email" type="email" value="{html_escape(person.get("email") or "")}"',
                1,
            )
            html = html.replace(
                'id="enroll-title" name="job_title" type="text" value=""',
                f'id="enroll-title" name="job_title" type="text" value="{html_escape(person.get("job_title") or "")}"',
                1,
            )
            html = html.replace(
                'id="enroll-dept" name="department" type="text" value=""',
                f'id="enroll-dept" name="department" type="text" value="{html_escape(person.get("department") or "")}"',
                1,
            )
            html = html.replace(
                'id="enroll-branch" name="branch" type="text" value="HQ"',
                f'id="enroll-branch" name="branch" type="text" value="{html_escape(person.get("branch") or "HQ")}"',
                1,
            )
            html = html.replace(
                'id="enroll-manager" name="manager" type="text" value=""',
                f'id="enroll-manager" name="manager" type="text" value="{html_escape(person.get("manager") or "")}"',
                1,
            )
            html = html.replace(
                '<p id="edit-banner" class="banner hidden">',
                '<p id="edit-banner" class="banner">',
                1,
            )
            html = html.replace(
                'id="btn-save-person" class="ghost hidden"',
                'id="btn-save-person" class="ghost"',
                1,
            )
            html = html.replace(
                'id="btn-cancel-edit" class="ghost hidden"',
                'id="btn-cancel-edit" class="ghost"',
                1,
            )
            if person.get("shift_id") is not None:
                html = html.replace(
                    f'<option value="{person["shift_id"]}">',
                    f'<option value="{person["shift_id"]}" selected>',
                    1,
                )

    if page == "people":
        people = db.list_employees()
        if people:
            body = "".join(
                "<tr>"
                f"<td>{html_escape(p.get('employee_id'))}</td>"
                f"<td>{html_escape(p.get('name'))}</td>"
                f"<td>{html_escape(p.get('department') or '—')}</td>"
                f"<td>{html_escape(p.get('branch') or 'HQ')}</td>"
                f"<td>{html_escape(p.get('shift_name') or 'Auto')}</td>"
                f"<td>{'Enrolled' if p.get('has_face') else 'Not enrolled'}</td>"
                f"<td>{html_escape(p.get('employment_status') or 'active')}</td>"
                "<td class='people-actions'>"
                f"<a class='ghost small-del' href='{_admin_href('register', token)}&edit={quote(str(p.get('employee_id') or ''))}'>Edit</a>"
                f"<form method='post' action='/admin/do' class='inline-del' onsubmit=\"return confirm('Delete this employee?');\">"
                f"<input type='hidden' name='action' value='delete_person' />"
                f"<input type='hidden' name='t' value='{html_escape(token or '')}' />"
                f"<input type='hidden' name='employee_id' value='{html_escape(p.get('employee_id'))}' />"
                "<button class='ghost small-del danger' type='submit'>Delete</button></form>"
                "</td></tr>"
                for p in people
            )
        else:
            body = '<tr><td colspan="8" class="muted">No employees yet.</td></tr>'
        html = html.replace('<tbody id="people-body"></tbody>', f'<tbody id="people-body">{body}</tbody>', 1)

    if page == "shifts":
        if shifts:
            body = "".join(
                f'<article class="log-row"><header><span>{html_escape(s.get("name"))}</span>'
                f'<span class="granted-tag">{html_escape(s.get("start_time"))} → {html_escape(s.get("end_time"))}</span></header>'
                f'<form method="post" action="/admin/do" onsubmit="return confirm(\'Delete this shift?\');">'
                f'<input type="hidden" name="action" value="delete_shift" />'
                f'<input type="hidden" name="t" value="{html_escape(token or "")}" />'
                f'<input type="hidden" name="shift_id" value="{s["id"]}" />'
                f'<button class="ghost small-del danger" type="submit">Delete</button></form></article>'
                for s in shifts
            )
        else:
            body = '<p class="muted">No shifts yet.</p>'
        html = html.replace('<div id="shift-list" class="log-list"></div>', f'<div id="shift-list" class="log-list">{body}</div>', 1)

    if page == "reports":
        kind_in = period or "daily"
        rows, start, end, kind = reports.report_rows(kind_in, work_date or "")
        summary = reports.summarize(rows)
        html = html.replace(
            f'<option value="{kind}">',
            f'<option value="{kind}" selected>',
            1,
        )
        if start:
            html = html.replace(
                'id="report-date" name="work_date" type="date"',
                f'id="report-date" name="work_date" type="date" value="{html_escape(work_date or start)}"',
                1,
            )
        html = html.replace(
            '<p id="report-range" class="muted"></p>',
            f'<p id="report-range" class="muted">{html_escape(kind)} · {html_escape(start)} to {html_escape(end)} · '
            f'{summary.get("closed") or 0} closed sessions · {html_escape(summary.get("total_formatted") or "0:00")} total</p>',
            1,
        )
        extra = ["People", len(summary.get("employees") or [])] if kind == "monthly" else None
        cards = _stat_cards(
            [
                ("Sessions", summary.get("sessions") or 0),
                ("Total hours", summary.get("total_formatted") or "0:00"),
                ("Avg / employee", summary.get("avg_formatted") or "—"),
                *([extra] if extra else []),
            ]
        )
        html = html.replace('<div id="report-summary" class="stats-grid"></div>', f'<div id="report-summary" class="stats-grid">{cards}</div>', 1)
        if kind == "monthly":
            html = html.replace('id="report-avg-wrap" class="hidden"', 'id="report-avg-wrap"', 1)
            people = summary.get("employees") or []
            avg_body = (
                "".join(
                    "<tr>"
                    f"<td>{html_escape(p.get('employee_name'))}</td>"
                    f"<td>{html_escape(p.get('employee_id'))}</td>"
                    f"<td>{html_escape(p.get('days_worked'))}</td>"
                    f"<td>{html_escape(p.get('sessions'))}</td>"
                    f"<td>{html_escape(p.get('total_formatted'))}</td>"
                    f"<td>{html_escape(p.get('avg_formatted'))}</td>"
                    "</tr>"
                    for p in people
                )
                or '<tr><td colspan="6" class="muted">No closed sessions this month.</td></tr>'
            )
            html = html.replace('<tbody id="report-avg-body"></tbody>', f'<tbody id="report-avg-body">{avg_body}</tbody>', 1)
        if rows:
            body = "".join(
                "<tr>"
                f"<td>{html_escape(r.get('employee_name'))}</td>"
                f"<td>{html_escape(r.get('employee_id'))}</td>"
                f"<td>{html_escape(r.get('shift_name') or '—')}</td>"
                f"<td>{html_escape(str(r.get('clock_in') or '').replace('T', ' ')[:16])}</td>"
                f"<td>{html_escape(str(r.get('clock_out') or '—').replace('T', ' ')[:16])}</td>"
                f"<td>{html_escape(r.get('hours_formatted') or '—')}</td>"
                f"<td>{html_escape(r.get('late_label') or 'On time')}</td>"
                "</tr>"
                for r in rows
            )
        else:
            body = '<tr><td colspan="7" class="muted">No sessions in this period.</td></tr>'
        html = html.replace('<tbody id="report-body"></tbody>', f'<tbody id="report-body">{body}</tbody>', 1)
        qs = f"work_date={start}&period={kind}"
        if token:
            qs += f"&token={token}"
        html = html.replace('id="btn-pdf" class="primary link-btn" href="#"', f'id="btn-pdf" class="primary link-btn" href="/api/v1/reports/pdf?{qs}"', 1)
        html = html.replace('id="btn-xlsx" class="ghost link-btn" href="#"', f'id="btn-xlsx" class="ghost link-btn" href="/api/v1/reports/excel?{qs}"', 1)

    if page == "admins" and role == "super":
        users = db.list_users()
        quota = f"{db.count_admins()} of {db.MAX_ADMINS} admin seats used"
        html = html.replace('<p id="admin-quota" class="muted"></p>', f'<p id="admin-quota" class="muted">{html_escape(quota)}</p>', 1)
        body_rows = []
        for u in users:
            if u.get("role") == "super":
                actions = '<span class="muted">You</span>'
            else:
                actions = (
                    f"<form method='post' action='/admin/do' class='inline-del'>"
                    f"<input type='hidden' name='action' value='reset_admin' />"
                    f"<input type='hidden' name='t' value='{html_escape(token or '')}' />"
                    f"<input type='hidden' name='user_id' value='{u['id']}' />"
                    f"<input name='staff_password' type='password' placeholder='New password' required />"
                    f"<button class='ghost small-del' type='submit'>Reset</button></form>"
                    f"<form method='post' action='/admin/do' class='inline-del' onsubmit=\"return confirm('Remove this admin?');\">"
                    f"<input type='hidden' name='action' value='delete_admin' />"
                    f"<input type='hidden' name='t' value='{html_escape(token or '')}' />"
                    f"<input type='hidden' name='user_id' value='{u['id']}' />"
                    f"<button class='ghost small-del danger' type='submit'>Remove</button></form>"
                )
            body_rows.append(
                "<tr>"
                f"<td>{html_escape(u.get('username'))}</td>"
                f"<td>{'Super admin' if u.get('role') == 'super' else 'Admin'}</td>"
                f"<td class='people-actions'>{actions}</td></tr>"
            )
        html = html.replace('<tbody id="staff-body"></tbody>', f'<tbody id="staff-body">{"".join(body_rows)}</tbody>', 1)
    return html


def _admin_page(
    *,
    authed: bool = False,
    token: str | None = None,
    error: bool = False,
    page: str = "dashboard",
    role: str = "admin",
    msg: str = "",
    period: str = "daily",
    work_date: str = "",
    edit: str = "",
) -> HTMLResponse:
    html = (WEB_DIR / "admin.html").read_text(encoding="utf-8")
    if page not in _ADMIN_PAGES:
        page = "dashboard"
    if authed:
        body_cls = "is-authed is-super" if role == "super" else "is-authed"
        html = html.replace("<body>", f'<body class="{body_cls}">', 1)
        html = html.replace('id="login-screen" class="login-screen"', 'id="login-screen" class="login-screen hidden"', 1)
        html = html.replace('id="app-shell" class="app-shell hidden"', 'id="app-shell" class="app-shell"', 1)
        html = _fill_admin_data(
            html,
            page,
            token,
            role,
            msg=msg,
            period=period,
            work_date=work_date,
            edit=edit,
        )
    if token:
        boot = (
            f"<script>window.__BOOT_TOKEN__={json.dumps(token)};"
            "try{localStorage.setItem('access_pass_token',window.__BOOT_TOKEN__);"
            "sessionStorage.setItem('access_pass_token',window.__BOOT_TOKEN__);}catch(e){}</script>"
        )
        html = html.replace("</head>", boot + "\n</head>", 1)
    if error:
        html = html.replace(
            '<p id="login-status" class="status"></p>',
            '<p id="login-status" class="status">Wrong username or password.</p>',
            1,
        )
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})


@app.post("/admin/session")
def admin_session(username: str = Form(...), password: str = Form(...)):
    db.init_db()
    user = db.get_user(username.strip())
    if not user or not verify_password(password, user["password_hash"]):
        return _admin_page(error=True)
    role = user.get("role") or "admin"
    token = create_token(user["username"], role)
    return _auth_cookie(_admin_page(authed=True, token=token, page="dashboard", role=role), token)


@app.get("/admin/logout")
def admin_logout():
    return _auth_cookie(RedirectResponse(url="/admin", status_code=303), None)


@app.get("/admin/session")
def admin_session_get():
    return RedirectResponse(url="/admin", status_code=303)


def _admin_redirect(page: str, token: str, msg: str = "", extra: str = "") -> Response:
    url = _admin_href(page, token)
    if extra:
        url += ("&" if "?" in url else "?") + extra
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return _auth_cookie(RedirectResponse(url=url, status_code=303), token)


@app.post("/admin/do")
def admin_do(
    action: str = Form(...),
    t: str = Form(default=""),
    access_pass_token: str | None = Cookie(default=None),
    name: str = Form(default=""),
    employee_id: str = Form(default=""),
    phone: str = Form(default=""),
    emergency_name: str = Form(default=""),
    emergency_phone: str = Form(default=""),
    shift_id: str = Form(default=""),
    editing_id: str = Form(default=""),
    shift_name: str = Form(default=""),
    start_time: str = Form(default=""),
    end_time: str = Form(default=""),
    staff_username: str = Form(default=""),
    staff_password: str = Form(default=""),
    new_password: str = Form(default=""),
    user_id: str = Form(default=""),
    email: str = Form(default=""),
    job_title: str = Form(default=""),
    department: str = Form(default=""),
    branch: str = Form(default=""),
    employment_status: str = Form(default=""),
    joining_date: str = Form(default=""),
    manager: str = Form(default=""),
):
    raw = (t or access_pass_token or "").strip()
    user = read_token(raw) if raw else None
    if not user:
        return RedirectResponse(url="/admin", status_code=303)
    role = user.get("role") or "admin"
    db.init_db()
    sid = int(shift_id) if str(shift_id).isdigit() else None

    try:
        if action == "save_person":
            if not name.strip() or not employee_id.strip():
                return _admin_redirect("register", raw, "Name and employee ID are required.")
            if editing_id.strip():
                if db.employee_id_exists(employee_id, exclude_id=editing_id):
                    return _admin_redirect("register", raw, "That employee ID is already used.", extra=f"edit={quote(editing_id)}")
                db.update_employee(
                    editing_id.strip(),
                    name=name,
                    new_employee_id=employee_id,
                    shift_id=sid,
                    phone=phone,
                    emergency_name=emergency_name,
                    emergency_phone=emergency_phone,
                    email=email,
                    job_title=job_title,
                    department=department,
                    branch=branch,
                    employment_status=employment_status,
                    joining_date=joining_date,
                    manager=manager,
                )
                return _admin_redirect("people", raw, f"{name.strip()} updated.")
            if db.employee_id_exists(employee_id):
                return _admin_redirect("register", raw, "That employee ID is already used.")
            db.add_employee(
                employee_id,
                name,
                None,
                sid,
                phone,
                emergency_name,
                emergency_phone,
            )
            db.set_employee_profile(
                employee_id,
                email=email,
                job_title=job_title,
                department=department,
                branch=branch,
                employment_status=employment_status,
                joining_date=joining_date,
                manager=manager,
            )
            return _admin_redirect("people", raw, f"{name.strip()} saved.")

        if action == "delete_person":
            db.delete_employee(employee_id)
            return _admin_redirect("people", raw, "Employee deleted.")

        if action == "add_shift":
            if not shift_name.strip() or not start_time or not end_time:
                return _admin_redirect("shifts", raw, "Shift name, start, and end are required.")
            db.add_shift(shift_name, start_time, end_time)
            return _admin_redirect("shifts", raw, "Shift added.")

        if action == "delete_shift":
            if sid is not None:
                db.delete_shift(sid)
            return _admin_redirect("shifts", raw, "Shift deleted.")

        if action == "add_admin":
            if role != "super":
                return _admin_redirect("dashboard", raw, "Super admin only.")
            db.add_admin(staff_username, hash_password(staff_password))
            return _admin_redirect("admins", raw, "Admin added.")

        if action == "delete_admin":
            if role != "super":
                return _admin_redirect("dashboard", raw, "Super admin only.")
            if user_id.isdigit():
                db.delete_admin(int(user_id))
            return _admin_redirect("admins", raw, "Admin removed.")

        if action == "reset_admin":
            if role != "super":
                return _admin_redirect("dashboard", raw, "Super admin only.")
            if user_id.isdigit() and staff_password:
                db.update_admin(int(user_id), password_hash=hash_password(staff_password))
            return _admin_redirect("admins", raw, "Password updated.")

        if action == "super_password":
            if role != "super":
                return _admin_redirect("dashboard", raw, "Super admin only.")
            if new_password.strip():
                db.set_super_password(hash_password(new_password))
            return _admin_redirect("admins", raw, "Your password was updated.")
    except ValueError as exc:
        dest = "admins" if action in {"add_admin", "delete_admin", "reset_admin", "super_password"} else "dashboard"
        if action in {"save_person", "delete_person"}:
            dest = "register" if action == "save_person" else "people"
        if action in {"add_shift", "delete_shift"}:
            dest = "shifts"
        return _admin_redirect(dest, raw, str(exc))

    return _admin_redirect("dashboard", raw)


@app.get("/api/v1/auth/me")
def auth_me(user: dict = Depends(require_admin)):
    return {"ok": True, "username": user["username"], "role": user["role"]}


@app.get("/api/v1/staff")
def list_staff(_: dict = Depends(require_super)):
    db.init_db()
    return {"admins_used": db.count_admins(), "admins_max": db.MAX_ADMINS, "users": db.list_users()}


@app.post("/api/v1/staff")
def create_staff(body: AdminCreateBody, _: dict = Depends(require_super)):
    db.init_db()
    if db.get_user(body.username):
        raise HTTPException(status_code=409, detail="Username already exists")
    try:
        return db.add_admin(body.username, hash_password(body.password))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.patch("/api/v1/staff/me")
def update_super_password(body: SuperPasswordBody, user: dict = Depends(require_super)):
    db.init_db()
    db.set_super_password(hash_password(body.password))
    return {"ok": True, "username": user["username"]}


@app.patch("/api/v1/staff/{user_id}")
def update_staff(user_id: int, body: AdminUpdateBody, _: dict = Depends(require_super)):
    db.init_db()
    target = db.get_user_by_id(user_id)
    if not target or target.get("role") != "admin":
        raise HTTPException(status_code=404, detail="Admin not found")
    if body.username:
        clash = db.get_user(body.username)
        if clash and clash["id"] != user_id:
            raise HTTPException(status_code=409, detail="Username already exists")
    pwd = hash_password(body.password) if body.password else None
    updated = db.update_admin(user_id, username=body.username, password_hash=pwd)
    if not updated:
        raise HTTPException(status_code=404, detail="Admin not found")
    return updated


@app.delete("/api/v1/staff/{user_id}")
def remove_staff(user_id: int, _: dict = Depends(require_super)):
    db.init_db()
    if not db.delete_admin(user_id):
        raise HTTPException(status_code=404, detail="Admin not found")
    return {"ok": True}


@app.post("/api/v1/captures/validate")
def validate_capture(body: CaptureBody, _: str = Depends(require_admin)):
    engine = get_enroll_engine()
    try:
        frame = decode_image_b64(body.image_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid camera frame")
    face = _single_enroll_face(frame, engine)
    return {"ok": True, "message": "Good capture.", "confidence": round(face.confidence, 3)}


@app.get("/api/v1/shifts")
def get_shifts(_: str = Depends(require_admin)):
    db.init_db()
    return db.list_shifts()


@app.post("/api/v1/shifts")
def create_shift(body: ShiftBody, _: str = Depends(require_admin)):
    db.init_db()
    try:
        return db.add_shift(body.name, _hhmm(body.start_time), _hhmm(body.end_time))
    except Exception as exc:
        raise HTTPException(status_code=409, detail="Shift name already exists") from exc


@app.delete("/api/v1/shifts/{shift_id}")
def remove_shift(shift_id: int, _: str = Depends(require_admin)):
    db.init_db()
    if not db.delete_shift(shift_id):
        raise HTTPException(status_code=404, detail="Shift not found")
    return {"ok": True}


@app.get("/api/v1/passes")
def list_passes(_: str = Depends(require_admin)):
    db.init_db()
    return db.list_employees()


@app.post("/api/v1/passes/enroll")
def enroll_pass(body: EnrollBody, _: str = Depends(require_admin)):
    db.init_db()
    eid = body.employee_id.strip()
    if db.employee_id_exists(eid):
        raise HTTPException(status_code=409, detail="Employee ID already exists")

    avg = None
    if body.images_b64:
        if len(body.images_b64) < CAPTURES_REQUIRED:
            raise HTTPException(status_code=400, detail=f"Need {CAPTURES_REQUIRED} face captures, or save without Face ID")
        avg = FaceEngine.robust_average_embeddings(_embeddings_from_images(body.images_b64))
    db.add_employee(
        eid,
        body.name.strip(),
        avg,
        shift_id=body.shift_id,
        phone=body.phone,
        emergency_name=body.emergency_name,
        emergency_phone=body.emergency_phone,
    )
    created = next((p for p in db.list_employees() if p["employee_id"] == eid), None)
    if not created:
        raise HTTPException(status_code=500, detail="Enrollment failed")
    return created


def _embeddings_from_images(images_b64: list[str]) -> list:
    engine = get_enroll_engine()
    embeddings = []
    for i, img in enumerate(images_b64):
        try:
            frame = decode_image_b64(img)
        except Exception:
            raise HTTPException(status_code=400, detail=f"Capture {i + 1}: Invalid camera frame")
        try:
            face = _single_enroll_face(frame, engine)
        except HTTPException as exc:
            raise HTTPException(status_code=400, detail=f"Capture {i + 1}: {exc.detail}") from exc
        embeddings.append(face.embedding)
    return embeddings


@app.patch("/api/v1/passes/{employee_id}")
def update_pass(employee_id: str, body: EmployeeUpdate, _: str = Depends(require_admin)):
    db.init_db()
    new_id = body.new_employee_id.strip()
    if db.employee_id_exists(new_id, exclude_id=employee_id):
        raise HTTPException(status_code=409, detail="Employee ID already exists")
    embedding = None
    if body.images_b64:
        if len(body.images_b64) < CAPTURES_REQUIRED:
            raise HTTPException(status_code=400, detail=f"Need {CAPTURES_REQUIRED} captures to replace the face")
        embedding = FaceEngine.robust_average_embeddings(_embeddings_from_images(body.images_b64))
    updated = db.update_employee(
        employee_id,
        name=body.name.strip(),
        new_employee_id=new_id,
        shift_id=body.shift_id,
        embedding=embedding,
        phone=body.phone,
        emergency_name=body.emergency_name,
        emergency_phone=body.emergency_phone,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Employee not found")
    return updated


@app.delete("/api/v1/passes/{employee_id}")
def remove_pass(employee_id: str, _: str = Depends(require_admin)):
    db.init_db()
    if not db.delete_employee(employee_id):
        raise HTTPException(status_code=404, detail="Employee not found")
    return {"ok": True}


@app.post("/api/v1/identify")
def identify(body: CaptureBody):
    db.init_db()
    return identify_frame(body.image_b64)


@app.post("/api/v1/clock-in")
def clock_in(body: CaptureBody):
    db.init_db()
    result = identify_frame(body.image_b64)
    if result["status"] != "matched":
        return result
    if result.get("clocked_in"):
        return {**result, "action": "already_in", "message": f"Hi {result['name']}, you are already clocked in."}
    if result.get("cooldown"):
        return {
            **result,
            "action": "cooldown",
            "available_at": result["cooldown"]["until"],
            "message": result["cooldown"]["message"],
        }
    employee = next(
        e for e in db.get_all_employees() if e["employee_id"] == result["employee_id"]
    )
    session = db.clock_in(employee, result["confidence"])
    if body.device_mac:
        db.touch_device(body.device_mac, camera=True)
        if session.get("id") and not session.get("_blocked"):
            db.set_session_device(int(session["id"]), body.device_mac)
            session = db.get_session(int(session["id"])) or session
    if session.get("_blocked"):
        if session.get("_reason") == "cooldown":
            until = session.get("_until")
            msg = f"Hi {result['name']}, next clock-in after {datetime.fromisoformat(until).strftime('%H:%M')}." if until else f"Hi {result['name']}, wait 6 hours after clock-out."
            return {**result, "action": "cooldown", "available_at": until, "message": msg}
        viewed = db._session_out(session)
        return {**result, "action": "already_in", "session": viewed, "message": f"Hi {result['name']}, you are already clocked in."}
    viewed = db._session_out(session)
    late_bit = ""
    if viewed.get("late"):
        late_bit = f" You are {viewed['minutes_late']} minutes late."
    shift_bit = f" {viewed.get('shift_name')} shift." if viewed.get("shift_name") else ""
    return {
        **result,
        "clocked_in": True,
        "action": "clocked_in",
        "session": viewed,
        "shift_name": viewed.get("shift_name"),
        "late": viewed.get("late"),
        "message": f"Hi {result['name']}, you are clocked in.{shift_bit}{late_bit}",
    }


@app.post("/api/v1/clock-out")
def clock_out(body: CaptureBody):
    db.init_db()
    result = identify_frame(body.image_b64)
    if result["status"] != "matched":
        return result
    session = db.get_open_session(result["employee_id"])
    if session is None:
        return {
            **result,
            "action": "not_in",
            "message": f"Hi {result['name']}, you are not clocked in.",
        }
    closed = db.clock_out(session["id"])
    if body.device_mac:
        db.touch_device(body.device_mac, camera=True)
    return {
        **result,
        "clocked_in": False,
        "action": "clocked_out",
        "session": db._session_out(closed),
        "message": f"Hi {result['name']}, you are clocked out. Next clock-in in 6 hours.",
        "prompt": f"Hi {result['name']}, are you ready to clock out?",
    }


@app.get("/api/v1/reports")
def get_reports(
    work_date: str = Query(default=""),
    period: str = Query(default="daily"),
    _: str = Depends(require_admin),
):
    rows, start, end, kind = reports.report_rows(period, work_date)
    return {
        "period": kind,
        "date_from": start,
        "date_to": end,
        "rows": rows,
        "summary": reports.summarize(rows),
    }


@app.get("/api/v1/reports/excel")
def export_excel(
    work_date: str = Query(default=""),
    period: str = Query(default="daily"),
    _: str = Depends(require_admin),
):
    start, end, kind = reports.resolve_period(period, work_date)
    data = reports.build_excel(period, work_date)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="AbsenceLess-{kind}-{start}-to-{end}.xlsx"'},
    )


@app.get("/api/v1/reports/pdf")
def export_pdf(
    work_date: str = Query(default=""),
    period: str = Query(default="daily"),
    _: str = Depends(require_admin),
):
    start, end, kind = reports.resolve_period(period, work_date)
    data = reports.build_pdf(period, work_date)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="AbsenceLess-{kind}-{start}-to-{end}.pdf"'},
    )


def _export_actor(user) -> str:
    if isinstance(user, dict):
        return str(user.get("username") or "admin")
    return "admin"


def _export_params(
    report: str,
    fmt: str,
    start_date: str,
    end_date: str,
    branch: str,
    department: str,
    employee_id: str,
    status: str,
    columns: str,
    include_inactive: str,
    branch_id: str = "",
    department_id: str = "",
) -> dict:
    return {
        "report": report,
        "format": fmt,
        "start_date": start_date,
        "end_date": end_date,
        "branch": (branch or branch_id or "").strip(),
        "department": (department or department_id or "").strip(),
        "employee_id": employee_id,
        "status": status,
        "columns": columns,
        "include_inactive": include_inactive,
    }


def _export_response(params: dict, user) -> Response:
    db.init_db()
    try:
        payload, name, media = exports.build(params, _export_actor(user))
    except exports.ExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=payload,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/v1/exports/meta")
def export_meta(_: dict = Depends(require_admin)):
    db.init_db()
    return exports.meta()


@app.get("/api/v1/exports/count")
def export_count(
    report: str = Query(default="monthly"),
    start_date: str = Query(default=""),
    end_date: str = Query(default=""),
    branch: str = Query(default=""),
    department: str = Query(default=""),
    branch_id: str = Query(default=""),
    department_id: str = Query(default=""),
    employee_id: str = Query(default=""),
    status: str = Query(default=""),
    include_inactive: str = Query(default=""),
    _: dict = Depends(require_admin),
):
    db.init_db()
    try:
        data = exports.collect(
            _export_params(
                report,
                "xlsx",
                start_date,
                end_date,
                branch,
                department,
                employee_id,
                status,
                "",
                include_inactive,
                branch_id,
                department_id,
            )
        )
    except exports.ExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"count": data["count"], "hours_note": data["hours_note"], "source": "live"}


@app.get("/api/v1/exports")
def export_download(
    report: str = Query(default="monthly"),
    format: str = Query(default="xlsx"),
    start_date: str = Query(default=""),
    end_date: str = Query(default=""),
    branch: str = Query(default=""),
    department: str = Query(default=""),
    branch_id: str = Query(default=""),
    department_id: str = Query(default=""),
    employee_id: str = Query(default=""),
    status: str = Query(default=""),
    columns: str = Query(default=""),
    include_inactive: str = Query(default=""),
    user: dict = Depends(require_admin),
):
    return _export_response(
        _export_params(
            report,
            format,
            start_date,
            end_date,
            branch,
            department,
            employee_id,
            status,
            columns,
            include_inactive,
            branch_id,
            department_id,
        ),
        user,
    )


@app.get("/api/v1/exports/attendance")
def export_attendance(
    format: str = Query(default="xlsx"),
    start_date: str = Query(default=""),
    end_date: str = Query(default=""),
    branch: str = Query(default=""),
    department: str = Query(default=""),
    branch_id: str = Query(default=""),
    department_id: str = Query(default=""),
    employee_id: str = Query(default=""),
    status: str = Query(default=""),
    columns: str = Query(default=""),
    user: dict = Depends(require_admin),
):
    report = "monthly"
    if start_date and end_date and start_date == end_date:
        report = "daily"
    return _export_response(
        _export_params(
            report,
            format,
            start_date,
            end_date,
            branch,
            department,
            employee_id,
            status,
            columns,
            "",
            branch_id,
            department_id,
        ),
        user,
    )


@app.get("/api/v1/exports/employees")
def export_employees(
    format: str = Query(default="csv"),
    branch: str = Query(default=""),
    department: str = Query(default=""),
    branch_id: str = Query(default=""),
    department_id: str = Query(default=""),
    employee_id: str = Query(default=""),
    columns: str = Query(default=""),
    include_inactive: str = Query(default=""),
    user: dict = Depends(require_admin),
):
    today = date.today().isoformat()
    return _export_response(
        _export_params(
            "employees",
            format,
            today,
            today,
            branch,
            department,
            employee_id,
            "",
            columns,
            include_inactive,
            branch_id,
            department_id,
        ),
        user,
    )


@app.get("/api/v1/exports/attendance-summary")
def export_attendance_summary(
    format: str = Query(default="pdf"),
    start_date: str = Query(default=""),
    end_date: str = Query(default=""),
    branch: str = Query(default=""),
    department: str = Query(default=""),
    branch_id: str = Query(default=""),
    department_id: str = Query(default=""),
    employee_id: str = Query(default=""),
    user: dict = Depends(require_admin),
):
    return _export_response(
        _export_params(
            "monthly",
            format,
            start_date,
            end_date,
            branch,
            department,
            employee_id,
            "",
            "",
            "",
            branch_id,
            department_id,
        ),
        user,
    )


@app.get("/api/v1/dashboard")
def get_dashboard(_: str = Depends(require_admin)):
    db.init_db()
    return console.overview()


@app.get("/api/v1/console/overview")
def console_overview(_: dict = Depends(require_admin)):
    db.init_db()
    return console.overview()


@app.get("/api/v1/console/attendance")
def console_attendance(
    work_date: str = Query(default=""),
    name: str = Query(default=""),
    _: dict = Depends(require_admin),
):
    db.init_db()
    return console.attendance_rows(work_date, name)


@app.post("/api/v1/console/attendance/{session_id}/correct")
def console_correct(
    session_id: int,
    body: CorrectionBody,
    user: dict = Depends(require_admin),
):
    db.init_db()
    try:
        return console.request_correction(
            session_id=session_id,
            field=body.field,
            new_value=body.new_value,
            reason=body.reason,
            actor=user.get("username") or "admin",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/v1/console/attendance/audit/{audit_id}")
def console_audit_status(
    audit_id: int,
    body: AuditStatusBody,
    user: dict = Depends(require_admin),
):
    db.init_db()
    try:
        row = db.set_attendance_audit_status(audit_id, body.status, user.get("username") or "admin")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not row:
        raise HTTPException(status_code=404, detail="Correction request not found")
    return row


@app.get("/api/v1/console/notifications")
def console_notifications(_: dict = Depends(require_admin)):
    db.init_db()
    return console.notifications()


@app.get("/api/v1/console/settings")
def console_get_settings(_: dict = Depends(require_admin)):
    db.init_db()
    return console.company_settings()


@app.put("/api/v1/console/settings")
def console_put_settings(body: ConsoleSettingsBody, _: dict = Depends(require_admin)):
    db.init_db()
    payload = body.model_dump(exclude_none=True)
    return console.save_company_settings(payload)


@app.post("/api/v1/console/employees")
def console_add_employee(body: ConsoleEmployeeBody, _: dict = Depends(require_admin)):
    db.init_db()
    if db.employee_id_exists(body.employee_id):
        raise HTTPException(status_code=409, detail="That employee ID is already used")
    db.add_employee(body.employee_id, body.name, None, body.shift_id, body.phone, "", "")
    db.set_employee_profile(
        body.employee_id,
        email=body.email,
        job_title=body.job_title,
        department=body.department,
        branch=body.branch,
        employment_status=body.employment_status,
        joining_date=body.joining_date,
        manager=body.manager,
    )
    return {"ok": True, "source": "live", "employee_id": body.employee_id}


@app.post("/api/v1/console/employees/{employee_id}/status")
def console_employee_status(
    employee_id: str,
    body: EmploymentStatusBody,
    _: dict = Depends(require_admin),
):
    db.init_db()
    if not db.employee_id_exists(employee_id):
        raise HTTPException(status_code=404, detail="Employee not found")
    try:
        db.set_employment_status(employee_id, body.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "source": "live", "employee_id": employee_id, "status": body.status}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return netinfo.normalize_ip(forwarded.split(",")[0])
    host = request.client.host if request.client else ""
    return netinfo.normalize_ip(host)


def _announce_from_request(request: Request, camera: bool = False, mac_hint: str = "") -> dict:
    ip = _client_ip(request)
    found = netinfo.lookup_mac(ip)
    hint = None
    if mac_hint:
        try:
            hint = db.normalize_mac(mac_hint)
        except ValueError:
            hint = None
    return db.announce_device(ip, found or hint, camera=camera)


@app.get("/api/v1/console/devices")
def console_list_devices(_: dict = Depends(require_admin)):
    db.init_db()
    devices = db.list_devices()
    for row in devices:
        row["has_preview"] = netinfo.has_preview(int(row["id"]))
    return {"source": "live", "devices": devices}


@app.post("/api/v1/console/devices")
def console_add_device(body: DeviceBody, _: dict = Depends(require_admin)):
    db.init_db()
    try:
        return db.add_device(
            name=body.name,
            mac_address=body.mac_address,
            branch=body.branch,
            location=body.location,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/v1/console/devices/{device_id}")
def console_patch_device(device_id: int, body: DevicePatchBody, _: dict = Depends(require_admin)):
    db.init_db()
    try:
        updated = db.update_device(
            device_id,
            name=body.name,
            branch=body.branch,
            location=body.location,
            status=body.status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="Device not found")
    return updated


@app.delete("/api/v1/console/devices/{device_id}")
def console_delete_device(device_id: int, _: dict = Depends(require_admin)):
    db.init_db()
    if not db.get_device(device_id):
        raise HTTPException(status_code=404, detail="Device not found")
    db.delete_device(device_id)
    return {"ok": True, "deleted": device_id}


@app.post("/api/v1/devices/heartbeat")
def device_heartbeat(request: Request, body: DeviceHeartbeatBody):
    db.init_db()
    row = _announce_from_request(request, camera=body.camera, mac_hint=body.mac_address or "")
    return {"ok": True, "registered": True, "device": row, "source": "live"}


@app.post("/api/v1/devices/announce")
def device_announce(request: Request, body: DeviceHeartbeatBody):
    db.init_db()
    row = _announce_from_request(request, camera=body.camera, mac_hint=body.mac_address or "")
    return {"ok": True, "source": "live", "device": row}


@app.post("/api/v1/devices/preview")
def device_preview(request: Request, body: DevicePreviewBody):
    db.init_db()
    row = _announce_from_request(request, camera=True)
    if not netinfo.put_preview(int(row["id"]), body.image_b64):
        raise HTTPException(status_code=400, detail="Invalid camera frame")
    return {"ok": True, "source": "live", "device": row, "has_preview": True}


@app.get("/api/v1/console/devices/{device_id}/preview")
def console_device_preview(device_id: int, _: dict = Depends(require_admin)):
    data = netinfo.get_preview(device_id)
    if not data:
        raise HTTPException(status_code=404, detail="No live frame yet")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/v1/console/preview/{module}")
def console_preview(module: str, _: dict = Depends(require_admin)):
    db.init_db()
    return console.preview(module)


@app.post("/api/v1/console/preview/{module}/action")
def console_preview_action(module: str, _: dict = Depends(require_admin)):
    return JSONResponse(
        status_code=501,
        content={
            "ok": False,
            "source": "preview",
            "persisted": False,
            "module": module,
            "detail": console.PREVIEW_NOTE,
        },
    )


@app.post("/api/v1/settings/face")
def set_face(body: FaceSettingBody, _: str = Depends(require_admin)):
    db.init_db()
    return {"face_premium": db.set_face_premium(body.enabled)}


if ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/")
    def landing():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/employee")
    @app.get("/entrance")
    @app.get("/kiosk")
    def employee_page():
        return FileResponse(WEB_DIR / "employee.html")

    @app.get("/admin")
    def admin_page(
        page: str = Query(default="dashboard"),
        t: str | None = Query(default=None),
        period: str = Query(default="daily"),
        work_date: str = Query(default=""),
        edit: str = Query(default=""),
        msg: str = Query(default=""),
        access_pass_token: str | None = Cookie(default=None),
    ):
        raw = t or access_pass_token
        user = read_token(raw) if raw else None
        if not user:
            return _admin_page(authed=False)
        resp = _admin_page(
            authed=True,
            token=raw,
            page=page,
            role=user.get("role") or "admin",
            msg=msg,
            period=period,
            work_date=work_date,
            edit=edit,
        )
        if t:
            _auth_cookie(resp, raw)
        return resp
