"""
The coordinator-facing result as DATA: summary cards, a date -> shift -> room roster, faculty duty distribution,
eligibility report, unfilled positions and the validation report.

Every value is read from stored records (session, duties, history, eligibility, validation).  Rendering
(Streamlit, Excel, PDF, chat text) only formats this structure, so all of them always agree.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from . import validation as vl
from .duty_rules import DutyRule
from .nlu import ExamRequest, SRC_DB, SRC_DEFAULT, SRC_INFERRED, SRC_LLM, SRC_USER
from .reports import Reporter

SOURCE_LABEL = {SRC_USER: "from your message", SRC_LLM: "read by the language model", SRC_DB: "from the database",
                SRC_DEFAULT: "configured default", SRC_INFERRED: "worked out from other facts"}


def clock12(hhmm: str) -> str:
    t = datetime.strptime(hhmm[:5], "%H:%M")
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def long_date(iso: Any) -> str:
    d = iso if isinstance(iso, date) else date.fromisoformat(str(iso))
    return f"{d.day} {d:%B %Y}"


def date_summary(dates: List[str]) -> str:
    ds = sorted(date.fromisoformat(d) for d in dates)
    if not ds:
        return "-"
    if len(ds) == 1:
        return long_date(ds[0])
    run = all((b - a).days == 1 for a, b in zip(ds, ds[1:]))
    if run:
        return f"{ds[0].day} {ds[0]:%b} - {ds[-1].day} {ds[-1]:%b %Y}" if ds[0].month != ds[-1].month else f"{ds[0].day}-{ds[-1].day} {ds[-1]:%B %Y}"
    if len({(d.month, d.year) for d in ds}) == 1:
        return ", ".join(str(d.day) for d in ds[:-1]) + f" & {ds[-1].day} {ds[-1]:%B %Y}"
    return ", ".join(f"{d.day} {d:%b}" for d in ds)


def understood(req: Optional[ExamRequest], session: Dict[str, Any], notes: List[str]) -> List[Dict[str, str]]:
    """What the system understood, with where each fact came from - so nothing is silently assumed or dropped."""

    rows: List[Dict[str, str]] = []
    opts = session.get("options") or {}
    r = req or ExamRequest.from_json(opts.get("request") or {})

    def add(label: str, value: Any, key: str, default_source: str = SRC_USER) -> None:
        if value in (None, "", [], {}):
            return
        rows.append({"item": label, "value": str(value), "source": SOURCE_LABEL.get(r.source(key) or default_source, "")})

    add("Examination type", r.exam_type, "exam_type")
    add("Semester(s)", ", ".join(map(str, r.semesters)), "semesters")
    add("Year(s)", ", ".join(str(y) for y in r.years), "years")
    add("Students' department", ", ".join(r.student_departments), "student_departments")
    inc = (opts.get("departments") or {}).get("include") or []
    exc = (opts.get("departments") or {}).get("exclude") or []
    rows.append({"item": "Faculty department", "value": ", ".join(inc) if inc else "All departments",
                 "source": SOURCE_LABEL[SRC_USER if inc or r.all_faculty_departments else SRC_DEFAULT]
                 + ("" if inc or r.all_faculty_departments else " (you did not restrict it)")})
    add("Departments excluded", ", ".join(exc), "dept_exclude")
    if opts.get("duty_rule"):
        add("Duty-count condition", DutyRule.from_dict(opts["duty_rule"]).describe(), "duty_rule")
    if opts.get("duty_priority") == "lowest_first":
        add("Fairness preference", "faculty with the lowest duty counts first", "duty_priority")
    if opts.get("max_per_day"):
        add("Duties per day", f"at most {opts['max_per_day']} per person", "max_per_day")
    if opts.get("prohibit_consecutive"):
        add("Consecutive days", "not allowed", "prohibit_consecutive")
    susp = {1: "All regular classes suspended", 0: "Regular classes continue"}.get(session.get("classes_suspended"))
    if susp and opts.get("suspended_classes"):
        susp = f"Classes taking the examination ({len(opts['suspended_classes'])}) suspended; all other classes keep teaching"
    add("Teaching", susp, "classes_suspended", SRC_INFERRED)
    for n in notes:
        rows.append({"item": "Note", "value": n, "source": ""})
    return rows


def build(service, aid: int, req: Optional[ExamRequest] = None, notes: Optional[List[str]] = None) -> Dict[str, Any]:
    repo = service.repo
    rb = Reporter(service)
    a = repo.allocation(aid)
    sid = a["session_id"]
    session = repo.session(sid)
    opts = session.get("options") or {}
    summ = rb.summary(aid)
    duties = repo.duties(aid)
    unf = repo.unfilled(aid)
    shifts = repo.session_shifts(sid)
    rooms = repo.session_rooms(sid)
    per = session["invigilators_per_room"]
    dates = [d.isoformat() for d in repo.session_dates(sid)]
    shift_by_label = {s["label"]: s for s in shifts}

    # ---- roster: date -> shift -> room --------------------------------------------------------------
    grid: Dict[tuple, Dict[str, Dict[int, Dict[str, Any]]]] = {}
    for d in duties:
        grid.setdefault((d["exam_date"], d["shift"]), {}).setdefault(d["room"], {})[d["seat"]] = d
    miss: Dict[tuple, Dict[str, int]] = {}
    for u in unf:
        miss.setdefault((u["exam_date"], u["shift"]), {})
        miss[(u["exam_date"], u["shift"])][u["room"]] = miss[(u["exam_date"], u["shift"])].get(u["room"], 0) + 1
    headers = ["Room"] + [h for i in range(1, per + 1) for h in (f"Invigilator {i}", "Department")]
    roster = []
    for dt in dates:
        day = {"date": dt, "label": long_date(dt), "weekday": date.fromisoformat(dt).strftime("%A"), "shifts": []}
        for sh in shifts:
            slot_rooms = rooms
            seats = grid.get((dt, sh["label"]), {})
            lost = miss.get((dt, sh["label"]), {})
            rows = []
            for room in slot_rooms:
                cells: List[str] = []
                for i in range(1, per + 1):
                    d = seats.get(room, {}).get(i)
                    cells += [d["faculty"], d["department"] or "Unknown"] if d else ["— UNFILLED —", ""]
                rows.append([room] + cells)
            day["shifts"].append({"label": sh["label"], "time": f"{clock12(sh['start_time'])} to {clock12(sh['end_time'])}",
                                  "headers": headers, "rows": rows,
                                  "filled": sum(1 for r in rows for c in r[1::2] if c and c != "— UNFILLED —"),
                                  "required": len(rooms) * per, "unfilled": sum(lost.values())})
        roster.append(day)

    # ---- faculty statistics ------------------------------------------------------------------------------
    counts = rb.counts(aid)
    assigned = sorted((c for c in counts if c["new"] > 0), key=lambda c: (-c["new"], c["name"]))
    dist = {"headers": ["Faculty", "Department", "Previous duties", "New duties", "Total duties"],
            "rows": [[c["name"], c["department"] if c["department"] != "—" else "Unknown", c["historical"] + c["committed"],
                      c["new"], c["combined"]] for c in assigned]}
    others = [c for c in counts if c["new"] == 0]
    rank = {"Excluded": 0, "Unavailable": 1, "Ineligible": 2, "Unverified": 3, "Eligible": 4}
    others.sort(key=lambda c: (rank.get(c["status"], 5), c["name"]))
    elig = {"headers": ["Faculty", "Department", "Status", "Reason"],
            "rows": [[c["name"], c["department"] if c["department"] != "—" else "Unknown",
                      "Not selected" if c["status"] == "Eligible" else
                      ("Unverified" if c["status"] == "Unverified" else "Not eligible"),
                      "Eligible, but not needed - the seats were filled by faculty with fewer duties" if c["status"] == "Eligible"
                      else c["why"]] for c in others]}

    # ---- unfilled ------------------------------------------------------------------------------------------
    by_room: Dict[tuple, List[Dict[str, Any]]] = OrderedDict()
    for u in unf:
        by_room.setdefault((u["exam_date"], u["shift"], u["room"]), []).append(u)
    unfilled = {"headers": ["Date", "Shift", "Room", "Required", "Assigned", "Missing", "Reason"],
                "rows": [[long_date(d), sh, room, per, per - len(us), len(us), us[0]["reason"]] for (d, sh, room), us in by_room.items()],
                "needed_extra": max((u["required"] - u["available"] for u in unf), default=0)}

    # ---- cards ----------------------------------------------------------------------------------------------
    n_dates, n_shifts = len(dates), len(shifts)
    required, filled = summ["total_slots"], summ["filled"]
    r = req or ExamRequest.from_json(opts.get("request") or {})
    inc = (opts.get("departments") or {}).get("include") or []
    sems = ", ".join(map(str, r.semesters)) or (", ".join(str(y) for y in r.years) and "Year " + ", ".join(str(y) for y in r.years)) or "Not specified"
    cards = [
        {"label": "Examination type", "value": r.exam_type.title() if r.exam_type else "Not specified"},
        {"label": "Semester", "value": sems},
        {"label": "Examination dates", "value": date_summary(dates)},
        {"label": "Faculty departments", "value": ", ".join(inc) if inc else "All departments"},
        {"label": "Total shifts", "value": f"{n_dates * n_shifts} ({n_shifts} per day)"},
        {"label": "Total rooms", "value": len(rooms)},
        {"label": "Invigilators per room", "value": per},
        {"label": "Positions required", "value": required},
        {"label": "Positions assigned", "value": filled, "tone": "good" if filled == required else "warn"},
        {"label": "Positions unfilled", "value": required - filled, "tone": "good" if required == filled else "bad"},
    ]

    checks = vl.validate_allocation(service, aid)
    state = {"draft": "Draft - awaiting your approval", "approved": "Approved and saved", "superseded": "Superseded",
             "cancelled": "Cancelled", "completed": "Completed"}[a["status"]]
    feas = {"complete": "Every position is filled", "partial": f"{required - filled} position(s) could not be filled",
            "infeasible": "No valid allocation under the current constraints"}[a["feasibility"]]
    return {"kind": "exam_allocation", "allocation_id": aid, "session_id": sid, "version": a["version"],
            "title": session["name"], "status": a["status"], "status_text": state, "feasibility": a["feasibility"],
            "feasibility_text": feas, "complete": required == filled and required > 0,
            "understood": understood(req, session, notes or []), "cards": cards, "roster": roster,
            "duty_distribution": dist, "eligibility": elig, "unfilled": unfilled,
            "validation": [c.as_dict() for c in checks], "validation_overall": vl.overall(checks),
            "counting_note": "Previous duties = imported duty sheet + completed examinations + approved duties of other "
                             "examinations. Draft duties are not counted until approved.",
            "notes": list(notes or [])}