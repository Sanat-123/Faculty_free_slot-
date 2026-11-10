"""
Reports A-E and the pre-allocation overview, built ONLY from database records, exported to Excel and PDF.

    ex = ReportExporter(service)
    ex.overview(session_id)             # who is in / out before allocating
    ex.report_a(aid) ... ex.report_e(sid)   -> (headers, rows)
    ex.to_excel(aid, path) / ex.to_pdf(aid, path)

A value that is not available is shown as "n/a" - never invented.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from .policy import PolicyService
from .reports import Reporter
from .restrictions import describe_scope

Table = Tuple[List[str], List[List[Any]]]
NA = "n/a"


def _r(x: float, nd: int = 2) -> float:
    return round(float(x), nd)


class ReportExporter:

    def __init__(self, service):
        self.svc = service
        self.repo = service.repo
        self.rb = Reporter(service)

    # ------------------------------------------------------------------ shared
    def _people(self, aid: int) -> List[Dict[str, Any]]:
        """One row per active faculty member with everything the reports need."""

        a, session, matrix, comps, duties = self.rb._ctx(aid)
        sid = a["session_id"]
        pol = PolicyService(self.repo)
        rules = pol.priority_rules(sid)
        new = Counter(d["faculty_id"] for d in duties)
        date_of = {i: s.exam_date for i, s in enumerate(matrix.slots)}
        hist_ok = {r[0]: r[1] for r in self.repo.conn.execute(
            "SELECT faculty_id, MIN(CASE verification WHEN 'verified' THEN 1 ELSE 0 END) FROM exm_history "
            "WHERE status='active' GROUP BY faculty_id")}
        out = []
        for f in matrix.faculty:
            if not f["active"]:
                continue
            c = comps.get(f["id"], {})
            hist = c.get("baseline", 0.0) + c.get("completed", 0.0)
            slots = matrix.eligible_slots(f["id"])
            days = {date_of[i] for i in slots}
            tier, why = PolicyService.tier_of(f, rules)
            n = new.get(f["id"], 0)
            status = matrix.summary(f["id"])[0]
            alloc = "Assigned" if n else ("Fully excluded" if not slots else "Eligible - no duty")
            out.append({"id": f["id"], "name": f["name"], "code": f.get("employee_code") or NA,
                        "department": f["department"] or NA, "designation": f.get("designation") or NA,
                        "hist": hist, "hist_known": f["id"] in hist_ok, "hist_verified": bool(hist_ok.get(f["id"], 0)),
                        "new": n, "combined": hist + c.get("committed", 0.0) + n, "days": len(days),
                        "slots": len(slots), "tier": tier, "tier_note": why, "status": status, "alloc": alloc,
                        "record": f.get("record_status", "verified")})
        return out

    @staticmethod
    def _category(p: Dict[str, Any]) -> str:
        return f"Priority {p['tier']}" + (f" ({p['tier_note']})" if p["tier_note"] not in ("default", "") else "")

    # --------------------------------------------------------------- overview
    def overview(self, sid: int) -> Dict[str, Any]:
        """Pre-allocation facts (spec 7), from the current eligibility matrix - not from allocation output."""

        matrix = self.svc.eligibility.evaluate(sid)
        rules = PolicyService(self.repo).priority_rules(sid)
        active = [f for f in matrix.faculty if f["active"]]
        by_dept = Counter(f["department"] or NA for f in active)
        elig = [f for f in active if matrix.eligible_slots(f["id"])]
        excluded = [f for f in active if not matrix.eligible_slots(f["id"])]
        partly = [f for f in elig if len(matrix.eligible_slots(f["id"])) < len(matrix.slots)]
        incomplete = [f for f in active if not (f.get("employee_code") and f["department"] and f.get("designation"))]
        nohist = [f for f in active if self.repo.conn.execute(
            "SELECT 1 FROM exm_history WHERE faculty_id=? AND status='active'", (f["id"],)).fetchone() is None]
        unver = self.repo.conn.execute("SELECT COUNT(*) FROM exm_faculty WHERE record_status='needs_verification'").fetchone()[0]
        tiers = Counter(PolicyService.tier_of(f, rules)[0] for f in elig)
        roster_ids = {f["id"] for f in active}
        orphan = [r[0] for r in self.repo.conn.execute(
            "SELECT DISTINCT h.faculty_id FROM exm_history h JOIN exm_faculty f ON f.id=h.faculty_id "
            "WHERE h.status='active' AND f.active=0")]
        return {"total_active": len(active), "by_department": dict(sorted(by_dept.items())),
                "eligible": len(elig), "fully_excluded": len(excluded), "partly_restricted": len(partly),
                "incomplete_records": [f["name"] for f in incomplete],
                "history_unavailable": [f["name"] for f in nohist],
                "awaiting_verification": unver, "eligible_by_priority_tier": dict(sorted(tiers.items())),
                "inactive_people_with_history": len(orphan), "slots": len(matrix.slots)}

    def render_overview(self, sid: int) -> str:
        from .reports import md_table
        o = self.overview(sid)
        head = [["Active faculty", o["total_active"]], ["Eligible for at least one slot", o["eligible"]],
                ["Fully excluded / ineligible", o["fully_excluded"]], ["Partly restricted", o["partly_restricted"]],
                ["Incomplete records (no code / department / designation)", len(o["incomplete_records"])],
                ["No historical duty count on file (treated as unknown, not zero)", len(o["history_unavailable"])],
                ["Timetable names awaiting verification (parked, not eligible)", o["awaiting_verification"]],
                ["Inactive people who still have history (kept, flagged)", o["inactive_people_with_history"]]]
        tiers = ", ".join(f"priority {k}: {v}" for k, v in o["eligible_by_priority_tier"].items())
        return ("**Faculty overview**\n" + md_table(["Item", "Count"], head) + f"\nEligible by priority tier - {tiers}\n\n**By department**\n" +
                md_table(["Department", "Faculty"], [[k, v] for k, v in o["by_department"].items()], right=[1]))

    # ---------------------------------------------------------------- reports
    def report_a(self, aid: int) -> Table:
        """Examination duty allocation: one row per room."""

        a, session, matrix, comps, duties = self.rb._ctx(aid)
        people = {p["id"]: p for p in self._people(aid)}
        rooms: Dict[Tuple[str, str, str], Dict[int, Dict[str, Any]]] = defaultdict(dict)
        for d in duties:
            rooms[(d["exam_date"], d["shift"], d["room"])][d["seat"]] = d
        rows = []
        for (day, shift, room), seats in sorted(rooms.items(), key=lambda kv: (kv[0][0], kv[0][1], _natkey(kv[0][2]))):
            first = next(iter(seats.values()))
            row = [day, f"{shift} {first['start_time']}-{first['end_time']}", room]
            for k in sorted(seats):
                d = seats[k]
                p = people.get(d["faculty_id"], {})
                row += [d["faculty"], d["department"] or NA, p.get("code", NA), self._category(p) if p else NA]
            rows.append(row)
        width = max((len(r) for r in rows), default=3)
        per = (width - 3) // 4
        headers = ["Date", "Time slot", "Room"]
        for i in range(1, per + 1):
            headers += [f"Invigilator {i}", f"Dept {i}", f"Emp code {i}", f"Priority {i}"]
        rows = [r + [""] * (len(headers) - len(r)) for r in rows]
        for u in self.repo.unfilled(aid):                 # unfilled seats are shown, never hidden
            rows.append([u.get("exam_date", ""), u.get("shift", ""), u.get("room", ""), "UNFILLED"] + [""] * (len(headers) - 4))
        return headers, rows

    def report_b(self, aid: int) -> Table:
        rows = []
        for p in sorted(self._people(aid), key=lambda x: (x["department"], x["name"])):
            rows.append([p["name"], p["code"], p["department"], _r(p["hist"], 1) if p["hist_known"] else NA, p["new"],
                         _r(p["combined"], 1) if p["hist_known"] else f"{p['new']} (history n/a)", p["days"],
                         self._category(p), p["alloc"]])
        return (["Faculty name", "Employee code", "Department", "Historical duties", "New duties", "Combined duties",
                 "Eligible days", "Priority category", "Allocation status"], rows)

    def report_c(self, aid: int) -> Table:
        people = self._people(aid)
        unf = len(self.repo.unfilled(aid))
        by = defaultdict(list)
        for p in people:
            by[p["department"]].append(p)
        rows = []
        for d, ps in sorted(by.items()):
            rows.append([d, len(ps), sum(1 for p in ps if p["slots"]), sum(1 for p in ps if not p["slots"]),
                         _r(sum(p["hist"] for p in ps), 1), sum(p["new"] for p in ps),
                         _r(sum(p["combined"] for p in ps), 1), ""])
        rows.append(["ALL DEPARTMENTS", len(people), sum(1 for p in people if p["slots"]), sum(1 for p in people if not p["slots"]),
                     _r(sum(p["hist"] for p in people), 1), sum(p["new"] for p in people),
                     _r(sum(p["combined"] for p in people), 1), unf])
        return (["Department", "Total faculty", "Eligible faculty", "Excluded faculty", "Historical duties",
                 "New duties", "Combined duties", "Unfilled seats"], rows)

    def report_d(self, aid: int) -> Table:
        people = self._people(aid)
        elig = [p for p in people if p["slots"]]
        new = [p["new"] for p in elig] or [0]
        comb = [p["combined"] for p in elig] or [0]
        sd = statistics.pstdev(new) if len(new) > 1 else 0.0
        viol = self.svc.validate_allocation(aid)
        consec = self.rb.consecutive_faculty(aid)
        rows: List[List[Any]] = [
            ["Summary", "Eligible faculty", len(elig), ""],
            ["Summary", "Average new duties", _r(statistics.mean(new)), ""],
            ["Summary", "Std deviation of new duties", _r(sd), ""],
            ["Summary", "Minimum / maximum new duties", f"{min(new)} / {max(new)}", ""],
            ["Summary", "Average combined duties", _r(statistics.mean(comb), 1), ""],
            ["Summary", "Std deviation of combined duties", _r(statistics.pstdev(comb), 1) if len(comb) > 1 else 0, "history spread, not removed by design"],
            ["Summary", "Eligible faculty with no duty", sum(1 for p in elig if p["new"] == 0), ""],
            ["Summary", "People on consecutive days", len(consec), ""],
            ["Summary", "Hard-constraint violations", len(viol), "; ".join(viol[:3]) or "none"]]
        by = defaultdict(list)
        for p in elig:
            by[p["department"]].append(p)
        for d, ps in sorted(by.items()):
            rows.append(["By department", d, f"avg new {_r(statistics.mean(x['new'] for x in ps))}",
                         f"min {min(x['new'] for x in ps)} / max {max(x['new'] for x in ps)}; avg history "
                         f"{_r(statistics.mean(x['hist'] for x in ps), 1)}"])
        bt = defaultdict(list)
        for p in elig:
            bt[p["tier"]].append(p)
        for t, ps in sorted(bt.items()):
            rows.append(["By priority tier", f"Priority {t}", f"{len(ps)} people, {sum(x['new'] for x in ps)} duties",
                         f"avg new {_r(statistics.mean(x['new'] for x in ps))}"])
        hi, lo = statistics.mean(new) + 2, 1
        for p in elig:
            if p["new"] >= hi:
                rows.append(["Unusually high", p["name"], f"{p['new']} new duties", f"history {_r(p['hist'], 1)}"])
        for p in elig:
            if p["new"] < lo and p["hist"] <= statistics.mean([q["hist"] for q in elig]):
                rows.append(["Unusually low", p["name"], "no duty", f"history {_r(p['hist'], 1)} (below average)"])
        buckets = [(0, 5), (6, 10), (11, 15), (16, 20), (21, 10 ** 6)]
        for lo_, hi_ in buckets:
            ps = [p for p in elig if lo_ <= p["hist"] <= hi_]
            if ps:
                rows.append(["By historical load", f"{lo_}-{hi_ if hi_ < 10 ** 6 else '+'}", f"{len(ps)} people",
                             f"avg new {_r(statistics.mean(x['new'] for x in ps))}"])
        return ["Section", "Item", "Value", "Detail"], rows

    def report_e(self, sid: int) -> Table:
        session = self.repo.session(sid)
        rows = []
        for r in self.rb.exclusion_rows(sid, include_inactive=True):
            rows.append(["Faculty", r["faculty"], r["department"], r["type"], r["scope"], r["shift"], r["reason"], r["status"]])
        for r in PolicyService(self.repo).department_restrictions(sid, active_only=False):
            rows.append(["Department", "(entire department)", r["department"], r["kind"], describe_scope(r, session),
                         r["shift_label"] or "all", r["reason"] or "—", r["status"]])
        return (["Level", "Faculty", "Department", "Restriction type", "Applicable dates", "Shift", "Reason", "Active status"], rows)

    # ------------------------------------------------------------------ files
    def _all(self, aid: int) -> List[Tuple[str, Table]]:
        sid = self.repo.allocation(aid)["session_id"]
        return [("A Duty allocation", self.report_a(aid)), ("B Faculty workload", self.report_b(aid)),
                ("C Department summary", self.report_c(aid)), ("D Fairness analysis", self.report_d(aid)),
                ("E Exclusions", self.report_e(sid))]

    def _meta(self, aid: int) -> List[str]:
        a = self.repo.allocation(aid)
        s = self.repo.session(a["session_id"])
        note = "  -  not approved or published" if a["status"] == "draft" else ""
        return [f"Examination: {s.get('name', '')}   |   Allocation version {a['version']}",
                f"Generated {datetime.now():%d %b %Y %H:%M} from database records.   Status: {a['status'].upper()}{note}"]

    def to_excel(self, aid: int, path: Any) -> Path:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        wb = Workbook()
        wb.remove(wb.active)
        meta = self._meta(aid)
        for title, (headers, rows) in self._all(aid):
            ws = wb.create_sheet(title[:31])
            ws.append([meta[0]])
            ws.append([meta[1]])
            ws.append([])
            ws.append(headers)
            for c in ws[4]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor="305496")
                c.alignment = Alignment(wrap_text=True, vertical="top")
            for r in rows:
                ws.append(r)
            ws.freeze_panes = "A5"
            for i, h in enumerate(headers, 1):
                width = max([len(str(h))] + [len(str(r[i - 1])) for r in rows if i - 1 < len(r)][:200])
                ws.column_dimensions[ws.cell(4, i).column_letter].width = min(max(width + 2, 8), 42)
        path = Path(path)
        wb.save(path)
        return path

    def to_pdf(self, aid: int, path: Any) -> Path:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, TableStyle
        st = getSampleStyleSheet()
        small = st["BodyText"].clone("cell", fontSize=6.3, leading=7.4)
        doc = SimpleDocTemplate(str(path), pagesize=landscape(A4), leftMargin=18, rightMargin=18, topMargin=20, bottomMargin=20)
        meta = self._meta(aid)
        story: List[Any] = []
        for k, (title, (headers, rows)) in enumerate(self._all(aid)):
            if k:
                story.append(PageBreak())
            story += [Paragraph(f"<b>Report {title}</b>", st["Heading2"]), Paragraph(meta[0], st["BodyText"]),
                      Paragraph(meta[1], st["BodyText"]), Spacer(1, 6)]
            data = [[Paragraph(f"<b>{h}</b>", small) for h in headers]] + \
                   [[Paragraph(str(c).replace("&", "&amp;").replace("<", "&lt;"), small) for c in r] for r in rows]
            if len(data) == 1:
                data.append([Paragraph("(no records)", small)] + [""] * (len(headers) - 1))
            t = LongTable(data, repeatRows=1)
            t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d9e1f2")),
                                   ("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                   ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")])]))
            story.append(t)
        doc.build(story)
        return Path(path)


def _natkey(text: str):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(text))]