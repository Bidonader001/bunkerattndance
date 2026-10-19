"""SQLite storage for employees, static shifts, and work sessions."""

from __future__ import annotations

import os
import pickle
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from core.time_utils import format_hours, hours_between, hours_status, identify_shift

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "bunkeratt.db"


def db_path() -> Path:
    override = os.environ.get("BUNKERATT_DB")
    return Path(override) if override else _DEFAULT_DB


DB_PATH = _DEFAULT_DB

DEFAULT_SHIFTS = [
    ("Day", "08:00", "16:00"),
    ("Evening", "16:00", "00:00"),
    ("Night", "20:00", "04:00"),
]


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                embedding BLOB NOT NULL,
                created_at TEXT NOT NULL,
                shift_id INTEGER,
                FOREIGN KEY (shift_id) REFERENCES shifts(id)
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'admin'
            );

            CREATE TABLE IF NOT EXISTS work_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id TEXT NOT NULL,
                employee_name TEXT NOT NULL,
                work_date TEXT NOT NULL,
                clock_in TEXT NOT NULL,
                clock_out TEXT,
                hours REAL,
                confidence REAL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_employee
                ON work_sessions(employee_id, work_date);
            CREATE INDEX IF NOT EXISTS idx_sessions_open
                ON work_sessions(employee_id, clock_out);
            CREATE INDEX IF NOT EXISTS idx_sessions_date
                ON work_sessions(work_date);
            CREATE TABLE IF NOT EXISTS export_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                report_type TEXT NOT NULL,
                format TEXT NOT NULL,
                date_from TEXT,
                date_to TEXT,
                scope TEXT,
                outcome TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS gym_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        cols = _columns(conn, "employees")
        if "shift_id" not in cols:
            conn.execute("ALTER TABLE employees ADD COLUMN shift_id INTEGER")
        emp_extra = {
            "email": "TEXT",
            "job_title": "TEXT",
            "department": "TEXT",
            "branch": "TEXT",
            "employment_status": "TEXT",
            "joining_date": "TEXT",
            "manager": "TEXT",
            "phone": "TEXT",
            "emergency_name": "TEXT",
            "emergency_phone": "TEXT",
            "has_face": "INTEGER NOT NULL DEFAULT 1",
            "work_type": "TEXT NOT NULL DEFAULT 'full'",
        }
        for col, decl in emp_extra.items():
            if col not in cols:
                conn.execute(f"ALTER TABLE employees ADD COLUMN {col} {decl}")

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                mac_address TEXT UNIQUE NOT NULL,
                branch TEXT,
                location TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                last_seen TEXT,
                last_camera TEXT,
                last_ip TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS attendance_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                employee_id TEXT NOT NULL,
                employee_name TEXT,
                field TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                reason TEXT NOT NULL,
                actor TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            """
        )
        dev_cols = _columns(conn, "devices")
        if "last_ip" not in dev_cols:
            conn.execute("ALTER TABLE devices ADD COLUMN last_ip TEXT")
        if "labeled" not in _columns(conn, "devices"):
            conn.execute("ALTER TABLE devices ADD COLUMN labeled INTEGER NOT NULL DEFAULT 1")

        sess_cols = _columns(conn, "work_sessions")
        alters = {
            "shift_id": "INTEGER",
            "shift_name": "TEXT",
            "late": "INTEGER NOT NULL DEFAULT 0",
            "minutes_late": "INTEGER NOT NULL DEFAULT 0",
            "device_mac": "TEXT",
        }
        for col, decl in alters.items():
            if col not in sess_cols:
                conn.execute(f"ALTER TABLE work_sessions ADD COLUMN {col} {decl}")

        user_cols = _columns(conn, "users")
        if "role" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")

        from core.auth import hash_password

        super_name = os.getenv("SUPER_USERNAME", "bido001")
        super_pass = os.getenv("SUPER_PASSWORD", "b6102001")
        existing_super = conn.execute(
            "SELECT id, username FROM users WHERE username = ?", (super_name,)
        ).fetchone()
        if existing_super is None:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at, role) VALUES (?, ?, ?, 'super')",
                (super_name, hash_password(super_pass), datetime.now().isoformat()),
            )
        else:
            conn.execute("UPDATE users SET role = 'super' WHERE id = ?", (existing_super["id"],))

        existing = conn.execute("SELECT COUNT(*) FROM shifts").fetchone()[0]
        if existing == 0:
            conn.executemany(
                "INSERT INTO shifts (name, start_time, end_time) VALUES (?, ?, ?)",
                DEFAULT_SHIFTS,
            )

        face = conn.execute("SELECT value FROM gym_settings WHERE key = 'face_premium'").fetchone()
        if face is None:
            conn.execute("INSERT INTO gym_settings (key, value) VALUES ('face_premium', '1')")
        company = conn.execute("SELECT value FROM gym_settings WHERE key = 'company_name'").fetchone()
        if company is None or str(company[0]).strip() in {
            "",
            "CODAI",
            "absence-less",
            "Access Pass",
            "Absence Less",
        }:
            conn.execute(
                "INSERT INTO gym_settings (key, value) VALUES ('company_name', 'Bunker Attendance') ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
        conn.execute("DROP TABLE IF EXISTS memberships")
        conn.execute("DROP TABLE IF EXISTS payments")
        conn.execute("DROP TABLE IF EXISTS membership_packages")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS payroll_policy (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                effective_date TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payroll_policy_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                effective_date TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS employee_payroll (
                employee_id TEXT PRIMARY KEY,
                monthly_salary TEXT NOT NULL DEFAULT '0',
                divisor_override TEXT,
                leave_allowance_override TEXT,
                shift_id_override INTEGER,
                unused_leave_eligible INTEGER,
                effective_date TEXT,
                allowances_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT,
                updated_by TEXT
            );
            CREATE TABLE IF NOT EXISTS employee_salary_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id TEXT NOT NULL,
                monthly_salary TEXT NOT NULL,
                effective_date TEXT NOT NULL,
                actor TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leave_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id TEXT NOT NULL,
                work_date TEXT NOT NULL,
                kind TEXT NOT NULL,
                days TEXT NOT NULL DEFAULT '1',
                minutes INTEGER NOT NULL DEFAULT 0,
                note TEXT,
                actor TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leave_balances (
                employee_id TEXT NOT NULL,
                year TEXT NOT NULL,
                carried TEXT NOT NULL DEFAULT '0',
                paid_out TEXT NOT NULL DEFAULT '0',
                PRIMARY KEY (employee_id, year)
            );
            CREATE TABLE IF NOT EXISTS payroll_periods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                status TEXT NOT NULL,
                policy_snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                approved_at TEXT,
                approved_by TEXT,
                finalized_at TEXT,
                finalized_by TEXT,
                UNIQUE(start_date, end_date)
            );
            CREATE TABLE IF NOT EXISTS payroll_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period_id INTEGER NOT NULL,
                employee_id TEXT NOT NULL,
                snapshot TEXT NOT NULL,
                net_pay TEXT NOT NULL,
                UNIQUE(period_id, employee_id)
            );
            CREATE TABLE IF NOT EXISTS payroll_adjustments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period_id INTEGER,
                employee_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                amount TEXT NOT NULL,
                note TEXT,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'approved'
            );
            CREATE TABLE IF NOT EXISTS payroll_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                entity TEXT NOT NULL,
                entity_id TEXT,
                old_value TEXT,
                new_value TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leave_payouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id TEXT NOT NULL,
                period_id INTEGER NOT NULL,
                year TEXT NOT NULL,
                days TEXT NOT NULL,
                amount TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(employee_id, period_id)
            );
            CREATE INDEX IF NOT EXISTS idx_leave_ledger_emp ON leave_ledger(employee_id, work_date);
            CREATE INDEX IF NOT EXISTS idx_salary_hist_emp ON employee_salary_history(employee_id, effective_date);
            """
        )


def face_premium_enabled() -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM gym_settings WHERE key = 'face_premium'").fetchone()
    return bool(row and row[0] != "0")


def set_face_premium(on: bool) -> bool:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO gym_settings (key, value) VALUES ('face_premium', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if on else "0",),
        )
    return on


def normalize_work_type(value: str | None) -> str:
    text = (value or "").strip().lower().replace("-", " ").replace("_", " ")
    if text in {"part", "part time", "parttime"}:
        return "part"
    if text in {"leader", "team leader", "teamleader"}:
        return "leader"
    return "full"


def add_employee(
    employee_id: str,
    name: str,
    embedding: np.ndarray | None = None,
    shift_id: int | None = None,
    phone: str = "",
    emergency_name: str = "",
    emergency_phone: str = "",
) -> None:
    blob = pickle.dumps(embedding.astype(np.float32)) if embedding is not None else pickle.dumps(
        np.zeros((512,), dtype=np.float32)
    )
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO employees
                (employee_id, name, embedding, created_at, shift_id, phone, emergency_name, emergency_phone, has_face)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                employee_id.strip(),
                name.strip(),
                blob,
                now,
                shift_id,
                phone.strip(),
                emergency_name.strip(),
                emergency_phone.strip(),
                1 if embedding is not None else 0,
            ),
        )


def list_employees() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT e.id, e.employee_id, e.name, e.created_at, e.shift_id,
                   e.phone, e.emergency_name, e.emergency_phone,
                   e.email, e.job_title, e.department, e.branch,
                   e.employment_status, e.joining_date, e.manager, e.work_type,
                   s.name AS shift_name, s.start_time, s.end_time,
                   CASE WHEN e.has_face = 0 THEN 0 ELSE 1 END AS has_face,
                   ep.monthly_salary AS monthly_salary
            FROM employees e
            LEFT JOIN shifts s ON s.id = e.shift_id
            LEFT JOIN employee_payroll ep ON ep.employee_id = e.employee_id
            ORDER BY e.name
            """
        ).fetchall()
    return [dict(row) for row in rows]


def list_passes() -> list[dict]:
    return list_employees()


def get_all_employees() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT e.id, e.employee_id, e.name, e.embedding, e.created_at, e.shift_id,
                   e.phone, e.emergency_name, e.emergency_phone, e.has_face,
                   s.name AS shift_name, s.start_time, s.end_time
            FROM employees e
            LEFT JOIN shifts s ON s.id = e.shift_id
            ORDER BY e.name
            """
        ).fetchall()
    result = []
    for row in rows:
        blob = row["embedding"]
        if blob is None or int(row["has_face"] or 0) == 0:
            continue
        result.append(
            {
                "id": row["id"],
                "employee_id": row["employee_id"],
                "name": row["name"],
                "embedding": pickle.loads(blob),
                "created_at": row["created_at"],
                "shift_id": row["shift_id"],
                "shift_name": row["shift_name"],
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "phone": row["phone"],
                "emergency_name": row["emergency_name"],
                "emergency_phone": row["emergency_phone"],
            }
        )
    return result


def employee_id_exists(employee_id: str, exclude_id: str | None = None) -> bool:
    sql = "SELECT 1 FROM employees WHERE employee_id = ?"
    params: list = [employee_id.strip()]
    if exclude_id:
        sql += " AND employee_id != ?"
        params.append(exclude_id.strip())
    with get_connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return row is not None


def update_employee(
    employee_id: str,
    *,
    name: str,
    new_employee_id: str,
    shift_id: int | None,
    embedding: np.ndarray | None = None,
    phone: str | None = None,
    emergency_name: str | None = None,
    emergency_phone: str | None = None,
    email: str | None = None,
    job_title: str | None = None,
    department: str | None = None,
    branch: str | None = None,
    employment_status: str | None = None,
    joining_date: str | None = None,
    manager: str | None = None,
    work_type: str | None = None,
) -> dict | None:
    current = next((e for e in list_employees() if e["employee_id"] == employee_id.strip()), None)
    if current is None:
        return None
    next_id = new_employee_id.strip()
    next_name = name.strip()
    with get_connection() as conn:
        if embedding is not None:
            blob = pickle.dumps(embedding.astype(np.float32))
            conn.execute(
                """
                UPDATE employees
                SET employee_id = ?, name = ?, shift_id = ?, embedding = ?, has_face = 1
                WHERE employee_id = ?
                """,
                (next_id, next_name, shift_id, blob, employee_id.strip()),
            )
        else:
            conn.execute(
                """
                UPDATE employees SET employee_id = ?, name = ?, shift_id = ?
                WHERE employee_id = ?
                """,
                (next_id, next_name, shift_id, employee_id.strip()),
            )
        if phone is not None or emergency_name is not None or emergency_phone is not None:
            conn.execute(
                """
                UPDATE employees
                SET phone = COALESCE(?, phone),
                    emergency_name = COALESCE(?, emergency_name),
                    emergency_phone = COALESCE(?, emergency_phone)
                WHERE employee_id = ?
                """,
                (phone, emergency_name, emergency_phone, next_id),
            )
        conn.execute(
            """
            UPDATE employees
            SET email = COALESCE(?, email),
                job_title = COALESCE(?, job_title),
                department = COALESCE(?, department),
                branch = COALESCE(?, branch),
                employment_status = COALESCE(?, employment_status),
                joining_date = COALESCE(?, joining_date),
                manager = COALESCE(?, manager)
            WHERE employee_id = ?
            """,
            (email, job_title, department, branch, employment_status, joining_date, manager, next_id),
        )
        if work_type is not None:
            conn.execute(
                "UPDATE employees SET work_type = ? WHERE employee_id = ?",
                (normalize_work_type(work_type), next_id),
            )
        conn.execute(
            """
            UPDATE work_sessions
            SET employee_id = ?, employee_name = ?
            WHERE employee_id = ?
            """,
            (next_id, next_name, employee_id.strip()),
        )
        if next_id != employee_id.strip():
            conn.execute(
                "UPDATE employee_payroll SET employee_id = ? WHERE employee_id = ?",
                (next_id, employee_id.strip()),
            )
            conn.execute(
                "UPDATE employee_salary_history SET employee_id = ? WHERE employee_id = ?",
                (next_id, employee_id.strip()),
            )
    return next((e for e in list_employees() if e["employee_id"] == next_id), None)


def delete_employee(employee_id: str) -> bool:
    with get_connection() as conn:
        eid = employee_id.strip()
        conn.execute("DELETE FROM work_sessions WHERE employee_id = ?", (eid,))
        cur = conn.execute("DELETE FROM employees WHERE employee_id = ?", (eid,))
        return cur.rowcount > 0


def reset_people_data() -> dict[str, int]:
    """Remove employees, attendance, and per-person payroll. Keeps admins, shifts, and settings."""
    tables = (
        "payroll_rows",
        "payroll_adjustments",
        "payroll_audit",
        "payroll_periods",
        "leave_payouts",
        "leave_ledger",
        "leave_balances",
        "employee_salary_history",
        "employee_payroll",
        "attendance_audit",
        "work_sessions",
        "employees",
    )
    counts: dict[str, int] = {}
    with get_connection() as conn:
        for table in tables:
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                conn.execute(f"DELETE FROM {table}")
                counts[table] = int(n or 0)
            except sqlite3.OperationalError:
                counts[table] = 0
        conn.execute(
            "INSERT INTO gym_settings (key, value) VALUES ('company_name', 'Bunker Attendance') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
    return counts


def find_shift_session(employee_id: str, work_date: str, shift_id: int | None) -> dict | None:
    with get_connection() as conn:
        if shift_id is not None:
            row = conn.execute(
                """
                SELECT * FROM work_sessions
                WHERE employee_id = ? AND work_date = ? AND shift_id = ?
                ORDER BY clock_in DESC LIMIT 1
                """,
                (employee_id.strip(), work_date, shift_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM work_sessions
                WHERE employee_id = ? AND work_date = ?
                ORDER BY clock_in DESC LIMIT 1
                """,
                (employee_id.strip(), work_date),
            ).fetchone()
    return dict(row) if row else None


def list_shifts() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, name, start_time, end_time FROM shifts ORDER BY start_time, name"
        ).fetchall()
    return [dict(row) for row in rows]


def add_shift(name: str, start_time: str, end_time: str) -> dict:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO shifts (name, start_time, end_time) VALUES (?, ?, ?)",
            (name.strip(), start_time.strip(), end_time.strip()),
        )
        row = conn.execute(
            "SELECT id, name, start_time, end_time FROM shifts WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
    return dict(row)


COOLDOWN_HOURS = 6


def last_closed_session(employee_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM work_sessions
            WHERE employee_id = ? AND clock_out IS NOT NULL
            ORDER BY clock_out DESC LIMIT 1
            """,
            (employee_id.strip(),),
        ).fetchone()
    return dict(row) if row else None


def clock_in_blocked_until(employee_id: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now()
    last = last_closed_session(employee_id)
    if not last or not last.get("clock_out"):
        return None
    until = datetime.fromisoformat(last["clock_out"]) + timedelta(hours=COOLDOWN_HOURS)
    return until if now < until else None


def get_open_session(employee_id: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT * FROM work_sessions
            WHERE employee_id = ? AND clock_out IS NULL
            ORDER BY clock_in DESC LIMIT 1
            """,
            (employee_id.strip(),),
        ).fetchone()
    return dict(row) if row else None


def get_user(username: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role FROM users WHERE username = ?",
            (username.strip(),),
        ).fetchone()
    return dict(row) if row else None


MAX_ADMINS = 3


def public_user(row: dict) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row.get("role") or "admin",
        "created_at": row.get("created_at"),
    }


def list_users() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, username, role, created_at FROM users ORDER BY role DESC, username"
        ).fetchall()
    return [public_user(dict(r)) for r in rows]


def count_admins() -> int:
    with get_connection() as conn:
        n = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
    return int(n)


def add_user(username: str, password_hash: str, role: str = "admin") -> dict:
    role = "leader" if role == "leader" else "admin"
    if role == "admin" and count_admins() >= MAX_ADMINS:
        raise ValueError(f"Maximum of {MAX_ADMINS} admins")
    now = datetime.now().isoformat()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, created_at, role) VALUES (?, ?, ?, ?)",
            (username.strip(), password_hash, now, role),
        )
        row = conn.execute(
            "SELECT id, username, role, created_at FROM users WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
    return public_user(dict(row))


def get_user_by_id(user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def update_admin(user_id: int, *, username: str | None = None, password_hash: str | None = None) -> dict | None:
    current = get_user_by_id(user_id)
    if current is None or current.get("role") not in {"admin", "leader"}:
        return None
    next_name = (username or current["username"]).strip()
    next_hash = password_hash or current["password_hash"]
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET username = ?, password_hash = ? WHERE id = ? AND role IN ('admin', 'leader')",
            (next_name, next_hash, user_id),
        )
    updated = get_user_by_id(user_id)
    return public_user(updated) if updated else None


def set_super_password(password_hash: str) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE role = 'super'",
            (password_hash,),
        )
        return cur.rowcount > 0


def delete_admin(user_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM users WHERE id = ? AND role IN ('admin', 'leader')", (user_id,))
        return cur.rowcount > 0


def list_day_leave(work_date: str, kind: str = "unpermitted") -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT l.id, l.employee_id, l.work_date, l.kind, l.actor, e.name AS employee_name
            FROM leave_ledger l
            LEFT JOIN employees e ON e.employee_id = l.employee_id
            WHERE l.work_date = ? AND l.kind = ?
            ORDER BY e.name
            """,
            (work_date, kind),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_day_leave(employee_id: str, work_date: str, kind: str = "unpermitted") -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM leave_ledger WHERE employee_id = ? AND work_date = ? AND kind = ?",
            (employee_id.strip(), work_date, kind),
        )
        return cur.rowcount > 0


def delete_shift(shift_id: int) -> bool:
    with get_connection() as conn:
        conn.execute("UPDATE employees SET shift_id = NULL WHERE shift_id = ?", (shift_id,))
        cur = conn.execute("DELETE FROM shifts WHERE id = ?", (shift_id,))
        return cur.rowcount > 0


def current_shift_state(employee: dict, now: datetime | None = None) -> tuple[dict | None, dict | None]:
    now = now or datetime.now()
    matched = identify_shift(now, list_shifts(), preferred_id=employee.get("shift_id"))
    work_date = matched["work_date"].isoformat() if matched else now.date().isoformat()
    shift_id = matched["shift"].get("id") if matched else None
    existing = find_shift_session(employee["employee_id"], work_date, shift_id)
    return matched, existing


def clock_in(employee: dict, confidence: float, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    until = clock_in_blocked_until(employee["employee_id"], now)
    if until:
        return {
            "_blocked": True,
            "_reason": "cooldown",
            "_until": until.isoformat(),
            "employee_id": employee["employee_id"],
            "employee_name": employee["name"],
        }
    matched, existing = current_shift_state(employee, now)
    if existing and not existing.get("clock_out"):
        return {
            **existing,
            "_blocked": True,
            "_reason": "already_in",
        }
    work_date = matched["work_date"] if matched else now.date()
    shift = matched["shift"] if matched else {}
    late = bool(matched and matched["late"])
    minutes_late = int(matched["minutes_late"]) if matched else 0
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO work_sessions
                (employee_id, employee_name, work_date, clock_in, clock_out, hours, confidence,
                 created_at, shift_id, shift_name, late, minutes_late)
            VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                employee["employee_id"],
                employee["name"],
                work_date.isoformat(),
                now.isoformat(),
                float(confidence),
                now.isoformat(),
                shift.get("id"),
                shift.get("name"),
                1 if late else 0,
                minutes_late,
            ),
        )
        row = conn.execute(
            "SELECT * FROM work_sessions WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


def clock_out(session_id: int, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM work_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Session not found")
        if row["clock_out"]:
            return dict(row)
        started = datetime.fromisoformat(row["clock_in"])
        hours = round(hours_between(started, now), 4)
        conn.execute(
            "UPDATE work_sessions SET clock_out = ?, hours = ? WHERE id = ?",
            (now.isoformat(), hours, session_id),
        )
        updated = conn.execute(
            "SELECT * FROM work_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    return dict(updated)


def list_sessions(
    work_date: str = "",
    name: str = "",
    date_from: str = "",
    date_to: str = "",
    employee_id: str = "",
) -> list[dict]:
    query = "SELECT * FROM work_sessions WHERE 1=1"
    params: list = []
    if date_from.strip() and date_to.strip():
        query += " AND work_date >= ? AND work_date <= ?"
        params.extend([date_from.strip(), date_to.strip()])
    elif work_date.strip():
        query += " AND work_date = ?"
        params.append(work_date.strip())
    if name.strip():
        query += " AND employee_name LIKE ?"
        params.append(f"%{name.strip()}%")
    if employee_id.strip():
        query += " AND employee_id = ?"
        params.append(employee_id.strip())
    query += " ORDER BY work_date ASC, employee_name ASC, clock_in ASC"
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_session_out(dict(row)) for row in rows]


def _session_out(row: dict) -> dict:
    in_progress = not row.get("clock_out")
    hours = row.get("hours")
    if in_progress and row.get("clock_in"):
        started = datetime.fromisoformat(row["clock_in"])
        live = round(hours_between(started, datetime.now()), 4)
        return {
            **row,
            "hours": live,
            "in_progress": True,
            "hours_formatted": "IN PROGRESS",
            "hours_status": "open",
            "late": bool(row.get("late")),
            "minutes_late": int(row.get("minutes_late") or 0),
            "late_label": _late_label(row),
        }
    hrs = float(hours) if hours is not None else None
    return {
        **row,
        "in_progress": False,
        "hours_formatted": format_hours(hrs),
        "hours_status": hours_status(hrs),
        "late": bool(row.get("late")),
        "minutes_late": int(row.get("minutes_late") or 0),
        "late_label": _late_label(row),
    }


def _late_label(row: dict) -> str:
    if not row.get("late"):
        return "On time"
    mins = int(row.get("minutes_late") or 0)
    return f"Late {mins} min"


def list_open_sessions() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM work_sessions
            WHERE clock_out IS NULL
            ORDER BY clock_in DESC
            """
        ).fetchall()
    return [_session_out(dict(row)) for row in rows]


def set_employee_profile(
    employee_id: str,
    *,
    email: str = "",
    job_title: str = "",
    department: str = "",
    branch: str = "",
    employment_status: str = "",
    joining_date: str = "",
    manager: str = "",
    work_type: str | None = None,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE employees
            SET email = ?, job_title = ?, department = ?, branch = ?,
                employment_status = ?, joining_date = ?, manager = ?
            WHERE employee_id = ?
            """,
            (
                email.strip(),
                job_title.strip(),
                department.strip(),
                branch.strip() or "HQ",
                employment_status.strip() or "active",
                joining_date.strip(),
                manager.strip(),
                employee_id.strip(),
            ),
        )
        if work_type is not None:
            conn.execute(
                "UPDATE employees SET work_type = ? WHERE employee_id = ?",
                (normalize_work_type(work_type), employee_id.strip()),
            )


def set_employment_status(employee_id: str, status: str) -> None:
    value = (status or "active").strip().lower()
    if value not in {"active", "inactive"}:
        raise ValueError("Employment status must be active or inactive")
    with get_connection() as conn:
        conn.execute(
            "UPDATE employees SET employment_status = ? WHERE employee_id = ?",
            (value, employee_id.strip()),
        )


def get_session(session_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM work_sessions WHERE id = ?", (session_id,)).fetchone()
    return _session_out(dict(row)) if row else None


def add_attendance_audit(
    *,
    session_id: int,
    employee_id: str,
    employee_name: str,
    field: str,
    old_value: str,
    new_value: str,
    reason: str,
    actor: str,
) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO attendance_audit
                (session_id, employee_id, employee_name, field, old_value, new_value, reason, actor, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                session_id,
                employee_id,
                employee_name,
                field,
                old_value,
                new_value,
                reason.strip(),
                actor,
                now,
            ),
        )
        row_id = cur.lastrowid
    return {
        "id": row_id,
        "session_id": session_id,
        "employee_id": employee_id,
        "employee_name": employee_name,
        "field": field,
        "old_value": old_value,
        "new_value": new_value,
        "reason": reason.strip(),
        "actor": actor,
        "status": "pending",
        "created_at": now,
        "source": "live",
    }


def list_attendance_audit(limit: int = 80) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM attendance_audit ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def set_attendance_audit_status(audit_id: int, status: str, actor: str) -> dict | None:
    status = status.strip().lower()
    if status not in {"approved", "rejected", "pending"}:
        raise ValueError("Status must be approved, rejected, or pending")
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM attendance_audit WHERE id = ?", (audit_id,)).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE attendance_audit SET status = ? WHERE id = ?",
            (status, audit_id),
        )
    out = dict(row)
    out["status"] = status
    out["reviewed_by"] = actor
    out["source"] = "live"
    return out


DEVICE_ONLINE_SECONDS = 120


def normalize_mac(value: str) -> str:
    raw = "".join(ch for ch in (value or "") if ch in "0123456789abcdefABCDEF")
    if len(raw) != 12:
        raise ValueError("MAC address must be 12 hex digits, for example AA:BB:CC:DD:EE:FF")
    return ":".join(raw[i : i + 2].upper() for i in range(0, 12, 2))


def _device_out(row: dict) -> dict:
    last = str(row.get("last_seen") or "")
    online = False
    if last:
        try:
            seen = datetime.fromisoformat(last)
            online = (datetime.now() - seen).total_seconds() <= DEVICE_ONLINE_SECONDS
        except ValueError:
            online = False
    status = row.get("status") or "active"
    if status == "revoked":
        live = "revoked"
    elif online:
        live = "online"
    else:
        live = "offline"
    mac = str(row.get("mac_address") or "")
    labeled = int(row.get("labeled") or 0) == 1
    is_new = (not labeled) and status != "revoked"
    return {
        **row,
        "online": online,
        "live_status": live,
        "mac_resolved": not mac.startswith("02:FF:"),
        "labeled": labeled,
        "is_new": is_new,
        "source": "live",
    }


def list_devices() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM devices ORDER BY name").fetchall()
    return [_device_out(dict(row)) for row in rows]


def get_device(device_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return _device_out(dict(row)) if row else None


def get_device_by_mac(mac_address: str) -> dict | None:
    mac = normalize_mac(mac_address)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM devices WHERE mac_address = ?", (mac,)).fetchone()
    return _device_out(dict(row)) if row else None


def add_device(
    *,
    name: str,
    mac_address: str,
    branch: str = "HQ",
    location: str = "",
) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("Device name is required")
    mac = normalize_mac(mac_address)
    now = datetime.now().isoformat(timespec="seconds")
    with get_connection() as conn:
        existing = conn.execute("SELECT id FROM devices WHERE mac_address = ?", (mac,)).fetchone()
        if existing:
            raise ValueError("That MAC address is already registered")
        cur = conn.execute(
            """
            INSERT INTO devices (name, mac_address, branch, location, status, last_seen, last_camera, last_ip, created_at)
            VALUES (?, ?, ?, ?, 'active', ?, NULL, NULL, ?)
            """,
            (name, mac, (branch or "HQ").strip(), (location or "").strip(), now, now),
        )
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _device_out(dict(row))


def update_device(
    device_id: int,
    *,
    name: str | None = None,
    branch: str | None = None,
    location: str | None = None,
    status: str | None = None,
) -> dict | None:
    current = get_device(device_id)
    if not current:
        return None
    next_name = current["name"]
    if name is not None and name.strip():
        next_name = name.strip()
    next_branch = branch.strip() if branch is not None else (current.get("branch") or "HQ")
    next_location = location.strip() if location is not None else (current.get("location") or "")
    next_status = (status or current.get("status") or "active").strip().lower()
    if next_status not in {"active", "revoked"}:
        raise ValueError("Device status must be active or revoked")
    if not next_name:
        raise ValueError("Device name is required")
    labeled = 1 if next_status == "active" else int(bool(current.get("labeled")))
    with get_connection() as conn:
        conn.execute(
            "UPDATE devices SET name = ?, branch = ?, location = ?, status = ?, labeled = ? WHERE id = ?",
            (next_name, next_branch, next_location, next_status, labeled, device_id),
        )
    return get_device(device_id)


def get_device_by_ip(ip: str, include_revoked: bool = False) -> dict | None:
    ip = (ip or "").strip()
    if not ip:
        return None
    with get_connection() as conn:
        if include_revoked:
            row = conn.execute(
                "SELECT * FROM devices WHERE last_ip = ? ORDER BY id DESC LIMIT 1",
                (ip,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM devices
                WHERE last_ip = ? AND status != 'revoked'
                ORDER BY id DESC LIMIT 1
                """,
                (ip,),
            ).fetchone()
    return _device_out(dict(row)) if row else None


def delete_device(device_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))


def announce_device(ip: str, mac: str | None, camera: bool = False) -> dict:
    ip = (ip or "").strip()
    now = datetime.now().isoformat(timespec="seconds")
    cam = "live" if camera else "idle"
    real_mac = None
    if mac:
        try:
            real_mac = normalize_mac(mac)
        except ValueError:
            real_mac = None
    by_mac = get_device_by_mac(real_mac) if real_mac else None
    by_ip = get_device_by_ip(ip, include_revoked=True) if ip else None
    if by_mac and by_ip and by_mac["id"] != by_ip["id"]:
        delete_device(int(by_ip["id"]))
        by_ip = None
    target = by_mac or by_ip
    if target:
        device_id = int(target["id"])
        next_mac = real_mac or target["mac_address"]
        keep_status = target.get("status") or "active"
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE devices
                SET mac_address = ?, last_ip = ?, last_seen = ?, last_camera = ?, status = ?
                WHERE id = ?
                """,
                (
                    next_mac,
                    ip or target.get("last_ip"),
                    now,
                    cam if camera else (target.get("last_camera") or cam),
                    keep_status,
                    device_id,
                ),
            )
        return get_device(device_id)
    use_mac = real_mac or placeholder_mac_safe(ip)
    name = f"Kiosk {ip}" if ip else "Kiosk"
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO devices (name, mac_address, branch, location, status, last_seen, last_camera, last_ip, created_at, labeled)
            VALUES (?, ?, 'HQ', '', 'active', ?, ?, ?, ?, 0)
            """,
            (name, use_mac, now, cam, ip, now),
        )
        row_id = cur.lastrowid
    return get_device(int(row_id))


def placeholder_mac_safe(ip: str) -> str:
    from core.netinfo import placeholder_mac

    return placeholder_mac(ip)


def touch_device(mac_address: str, camera: bool = False) -> dict | None:
    try:
        mac = normalize_mac(mac_address)
    except ValueError:
        return None
    now = datetime.now().isoformat(timespec="seconds")
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM devices WHERE mac_address = ?", (mac,)).fetchone()
        if not row:
            return None
        if row["status"] == "revoked":
            return _device_out(dict(row))
        cam = "live" if camera else (row["last_camera"] or "")
        conn.execute(
            "UPDATE devices SET last_seen = ?, last_camera = ? WHERE mac_address = ?",
            (now, cam, mac),
        )
    return get_device_by_mac(mac)


def set_session_device(session_id: int, mac_address: str) -> None:
    try:
        mac = normalize_mac(mac_address)
    except ValueError:
        return
    with get_connection() as conn:
        conn.execute(
            "UPDATE work_sessions SET device_mac = ? WHERE id = ?",
            (mac, session_id),
        )


def log_export(
    *,
    actor: str,
    report_type: str,
    fmt: str,
    date_from: str = "",
    date_to: str = "",
    scope: str = "",
    outcome: str = "ok",
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO export_audit
                (actor, report_type, format, date_from, date_to, scope, outcome, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor,
                report_type,
                fmt,
                date_from,
                date_to,
                scope[:500],
                outcome,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
