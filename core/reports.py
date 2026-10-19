"""PDF and Excel attendance reports."""

from __future__ import annotations

from datetime import date
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from core import db
from core.time_utils import format_hours, period_range

ORANGE = PatternFill("solid", fgColor="3B82F6")
INK = PatternFill("solid", fgColor="1C1C1C")
RED = PatternFill("solid", fgColor="FFC7C7")
GREEN = PatternFill("solid", fgColor="C6F4D6")
PURPLE = PatternFill("solid", fgColor="E3D0F2")
PDF_ORANGE = colors.HexColor("#3B82F6")
PDF_INK = colors.HexColor("#1C1C1C")
PDF_RED = colors.HexColor("#FFC7C7")
PDF_GREEN = colors.HexColor("#C6F4D6")
PDF_PURPLE = colors.HexColor("#E3D0F2")
HEADERS = ["Name", "ID", "Shift", "Clock in", "Clock out", "Total hours", "Late"]
AVG_HEADERS = [
    "Name",
    "ID",
    "Days worked",
    "Sessions",
    "Total hours",
    "Avg hours / day",
    "Base salary",
    "Net pay",
]
THIN = Border(
    left=Side(style="thin", color="DDDDDD"),
    right=Side(style="thin", color="DDDDDD"),
    top=Side(style="thin", color="DDDDDD"),
    bottom=Side(style="thin", color="DDDDDD"),
)


def _fill_for(status: str):
    if status == "under":
        return RED, PDF_RED
    if status == "exact":
        return GREEN, PDF_GREEN
    if status == "over":
        return PURPLE, PDF_PURPLE
    return None, None


def resolve_period(period: str, work_date: str) -> tuple[str, str, str]:
    day = date.fromisoformat(work_date) if work_date.strip() else date.today()
    start, end = period_range(period, day)
    return start.isoformat(), end.isoformat(), (period or "daily").lower()


def report_rows(period: str, work_date: str, employee_id: str = "") -> tuple[list[dict], str, str, str]:
    db.init_db()
    start, end, kind = resolve_period(period, work_date)
    return db.list_sessions(date_from=start, date_to=end, employee_id=employee_id or ""), start, end, kind


def attach_salary(rows: list[dict], start: str, end: str, *, id_key: str = "employee_id", employee_id: str = "") -> None:
    """Fill base_salary / net_pay from payroll preview for the same date range."""
    empty = {"currency": "EGP", "base_salary": "0.00", "net_pay": "0.00", "deductions_total": "0.00", "additions_total": "0.00"}
    if not rows:
        return
    pay_map: dict[str, dict] = {}
    currency = "EGP"
    if start and end:
        try:
            from core import payroll

            data = payroll.preview(start, end, employee_id=employee_id or "")
            currency = data.get("currency") or "EGP"
            for emp in data.get("employees") or []:
                pay_map[emp.get("employee_id") or ""] = emp
        except Exception:
            pay_map = {}
    for row in rows:
        hit = pay_map.get(row.get(id_key) or "") or {}
        row["currency"] = hit.get("currency") or currency
        row["base_salary"] = hit.get("base_salary") or empty["base_salary"]
        row["net_pay"] = hit.get("net_pay") or empty["net_pay"]
        row["additions_total"] = hit.get("additions_total") or empty["additions_total"]
        row["deductions_total"] = hit.get("deductions_total") or empty["deductions_total"]


def summarize(rows: list[dict], start: str = "", end: str = "", employee_id: str = "") -> dict:
    closed = [r for r in rows if r.get("clock_out") and r.get("hours") is not None]
    total = sum(float(r["hours"]) for r in closed)
    by_emp: dict[str, dict] = {}
    for r in closed:
        eid = r["employee_id"]
        item = by_emp.setdefault(
            eid,
            {"name": r["employee_name"], "hours": 0.0, "days": set(), "sessions": 0},
        )
        item["hours"] += float(r["hours"])
        item["days"].add(r.get("work_date"))
        item["sessions"] += 1
    people = []
    for eid, v in by_emp.items():
        days = max(1, len(v["days"]))
        avg = v["hours"] / days
        people.append(
            {
                "employee_id": eid,
                "employee_name": v["name"],
                "days_worked": len(v["days"]),
                "sessions": v["sessions"],
                "total_hours": round(v["hours"], 2),
                "avg_hours": round(avg, 2),
                "total_formatted": format_hours(v["hours"]),
                "avg_formatted": format_hours(avg),
            }
        )
    n = len(people)
    avg_emp = (total / n) if n else 0.0
    pay_map: dict[str, dict] = {}
    currency = "EGP"
    if start and end:
        try:
            from core import payroll

            data = payroll.preview(start, end, employee_id=employee_id or "")
            currency = data.get("currency") or "EGP"
            for emp in data.get("employees") or []:
                pay_map[emp.get("employee_id") or ""] = emp
        except Exception:
            pay_map = {}
    for p in people:
        hit = pay_map.get(p["employee_id"]) or {}
        p["currency"] = hit.get("currency") or currency
        p["base_salary"] = hit.get("base_salary") or "0.00"
        p["net_pay"] = hit.get("net_pay") or "0.00"
        p["additions_total"] = hit.get("additions_total") or "0.00"
        p["deductions_total"] = hit.get("deductions_total") or "0.00"
    people.sort(key=lambda x: (x.get("employee_name") or "").lower())
    net_total = 0.0
    for p in people:
        try:
            net_total += float(p.get("net_pay") or 0)
        except (TypeError, ValueError):
            pass
    currency = (people[0].get("currency") if people else "EGP") or "EGP"
    return {
        "sessions": len(rows),
        "closed": len(closed),
        "total_hours": round(total, 2),
        "total_formatted": format_hours(total) if closed else "0:00",
        "avg_hours_per_employee": round(avg_emp, 2),
        "avg_formatted": format_hours(avg_emp) if n else "—",
        "currency": currency,
        "net_total": round(net_total, 2),
        "net_formatted": f"{currency} {net_total:.2f}",
        "employees": people,
    }


def _row_values(r: dict) -> list:
    return [
        r["employee_name"],
        r["employee_id"],
        r.get("shift_name") or "—",
        _fmt_time(r.get("clock_in")),
        _fmt_time(r.get("clock_out")) if r.get("clock_out") else "—",
        r.get("hours_formatted") or "—",
        r.get("late_label") or "On time",
    ]


def build_excel(period: str, work_date: str, employee_id: str = "") -> bytes:
    rows, start, end, kind = report_rows(period, work_date, employee_id)
    summary = summarize(rows, start, end, employee_id)
    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance"
    title_font = Font(bold=True, color="FFFFFF", size=14)
    head_font = Font(bold=True, color="FFFFFF")
    ws.append(["Bunker Attendance", f"{kind.title()} attendance report"])
    ws.merge_cells("A1:B1")
    ws["A1"].font = title_font
    ws["A1"].fill = ORANGE
    ws["B1"].fill = ORANGE
    ws["B1"].font = Font(color="FFFFFF")
    ws.append(["Period", f"{start} to {end}"])
    ws.append(["Closed sessions", summary["closed"], "Total hours", summary["total_formatted"]])
    ws.append(["Average hours / employee", summary["avg_formatted"]])
    ws.append([])
    ws.append(HEADERS)
    for cell in ws[ws.max_row]:
        cell.font = head_font
        cell.fill = INK
        cell.alignment = Alignment(horizontal="center")

    for r in rows:
        ws.append(_row_values(r))
        excel_fill, _ = _fill_for(r.get("hours_status") or "")
        if excel_fill and r.get("clock_out"):
            ws.cell(ws.max_row, 6).fill = excel_fill
        for cell in ws[ws.max_row]:
            cell.border = THIN

    if summary["employees"]:
        avg = wb.create_sheet("Employee summary")
        avg.append(["Bunker Attendance", "Hours and calculated salary"])
        avg.merge_cells("A1:B1")
        avg["A1"].font = title_font
        avg["A1"].fill = ORANGE
        avg["B1"].fill = ORANGE
        avg.append(["Period", f"{start} to {end}"])
        avg.append(["Salary", f"{summary.get('currency') or 'EGP'} {summary.get('net_total') or 0:.2f} net"])
        avg.append([])
        avg.append(AVG_HEADERS)
        for cell in avg[avg.max_row]:
            cell.font = head_font
            cell.fill = INK
        for p in summary["employees"]:
            avg.append(
                [
                    p["employee_name"],
                    p["employee_id"],
                    p["days_worked"],
                    p["sessions"],
                    p["total_formatted"],
                    p["avg_formatted"],
                    f"{p.get('currency') or 'EGP'} {p.get('base_salary') or '0.00'}",
                    f"{p.get('currency') or 'EGP'} {p.get('net_pay') or '0.00'}",
                ]
            )
        for idx in range(1, avg.max_column + 1):
            avg.column_dimensions[get_column_letter(idx)].width = 20

    for idx in range(1, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(idx)].width = 18
        for row in ws.iter_rows(min_col=idx, max_col=idx):
            for cell in row:
                cell.alignment = Alignment(vertical="center")

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_pdf(period: str, work_date: str, employee_id: str = "") -> bytes:
    rows, start, end, kind = report_rows(period, work_date, employee_id)
    summary = summarize(rows, start, end, employee_id)
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        title=f"Bunker Attendance {kind} attendance",
        leftMargin=28,
        rightMargin=28,
        topMargin=28,
        bottomMargin=28,
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph("Bunker Attendance", styles["Title"]),
        Paragraph(
            f"{kind.title()} attendance · {start} to {end} · Bunker Attendance",
            styles["Normal"],
        ),
        Paragraph(
            f"Closed sessions {summary['closed']} · Total {summary['total_formatted']} · "
            f"Avg per employee {summary['avg_formatted']} · "
            f"Net salary {summary.get('net_formatted') or '—'}",
            styles["Normal"],
        ),
        Spacer(1, 12),
    ]
    if summary["employees"]:
        story.append(Paragraph("Employee summary (hours and calculated salary)", styles["Heading2"]))
        avg_data = [AVG_HEADERS]
        for p in summary["employees"]:
            avg_data.append(
                [
                    p["employee_name"],
                    p["employee_id"],
                    p["days_worked"],
                    p["sessions"],
                    p["total_formatted"],
                    p["avg_formatted"],
                    f"{p.get('currency') or 'EGP'} {p.get('base_salary') or '0.00'}",
                    f"{p.get('currency') or 'EGP'} {p.get('net_pay') or '0.00'}",
                ]
            )
        avg_table = Table(avg_data, repeatRows=1)
        avg_table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("BACKGROUND", (0, 0), (-1, 0), PDF_ORANGE),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#DDDDDD")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.append(avg_table)
        story.append(Spacer(1, 14))
    story.append(Paragraph("Session log", styles["Heading2"]))

    data = [HEADERS]
    status_by_row = [None]
    for r in rows:
        data.append(_row_values(r))
        status_by_row.append(r.get("hours_status") if r.get("clock_out") else None)

    table = Table(data, repeatRows=1)
    style_cmds = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), PDF_INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]
    for i, status in enumerate(status_by_row):
        _, pdf_fill = _fill_for(status or "")
        if pdf_fill:
            style_cmds.append(("BACKGROUND", (5, i), (5, i), pdf_fill))
    table.setStyle(TableStyle(style_cmds))
    story.append(table)
    doc.build(story)
    return buf.getvalue()


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime_from(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def datetime_from(iso: str):
    from datetime import datetime as dt

    return dt.fromisoformat(iso)
