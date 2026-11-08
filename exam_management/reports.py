"""
Reports and explanations.  Everything is computed from stored records
(duties, restrictions, history, eligibility) - no figure or sentence is invented.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .eligibility import EligibilityMatrix, STATUS_OF
from .fairness import compute_targets
from .groups import describe, group_map, normalise_spec, seat_quota
from .restrictions import describe_scope
from .service import ExamService


def md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]], right: Sequence[int] = ()) -> str:
    if not rows:
        return "_none_"
    head = "| " + " | ".join(headers) + " |"
    sep = "|" + "|".join("---:" if i in right else "---" for i in range(len(headers))) + "|"
    body = ["| " + " | ".join("" if c is None else str(c) for c in r) + " |" for r in rows]
    return "\n".join([head, sep, *body])


def fmt_date(d: Any) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return d.strftime("%d %b %Y")


def num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.1f}"


class Reporter:

    def __init__(self, service: ExamService):
        self.svc = service
        self.repo = service.repo

    # ------------------------------------------------------------ data
    def _ctx(self, aid: int):
        a = self.repo.allocation(aid)
        sid = a["session_id"]
        session = self.repo.session(sid)
        matrix = self.svc.eligibility.evaluate(sid)
        comps = self.repo.history_components(exclude_session_id=sid)
        duties = self.repo.duties(aid)
        return a, session, matrix, comps, duties

    def counts(self, aid: int) -> List[Dict[str, Any]]:
        """One row per ACTIVE faculty member: historical / new / combined / dept."""

        a, session, matrix, comps, duties = self._ctx(aid)
        new: Dict[int, int] = defaultdict(int)
        for d in duties:
            new[d["faculty_id"]] += 1
        rows = []
        for f in matrix.faculty:
            if not f["active"]:
                continue
            c = comps.get(f["id"], {})
            hist = c.get("baseline", 0.0) + c.get("completed", 0.0)
            com = c.get("committed", 0.0)
            elig = len(matrix.eligible_slots(f["id"]))
            status, why = matrix.summary(f["id"])
            rows.append({"id": f["id"], "name": f["name"], "department": f["department"] or "—",
                         "historical": hist, "committed": com, "new": new.get(f["id"], 0),
                         "combined": hist + com + new.get(f["id"], 0), "eligible_slots": elig,
                         "status": status, "why": why})
        return rows

    # --------------------------------------------------------- A. summary
    def summary(self, aid: int) -> Dict[str, Any]:
        a, session, matrix, comps, duties = self._ctx(aid)
        slots = matrix.slots
        dates = sorted({s.exam_date for s in slots})
        shifts = self.repo.session_shifts(session["id"])
        rooms = self.repo.session_rooms(session["id"])
        total_slots = sum(s.required for s in slots)
        unf = self.repo.unfilled(aid)
        rows = self.counts(aid)
        excluded = [r for r in rows if r["eligible_slots"] == 0]
        return {"session": session, "allocation": a, "days": len(dates), "shifts": len(shifts),
                "rooms": len(rooms), "per_room": session["invigilators_per_room"],
                "total_slots": total_slots, "filled": len(duties), "unfilled": len(unf),
                "eligible_faculty": sum(1 for r in rows if r["eligible_slots"] > 0),
                "excluded_faculty": len(excluded),
                "partially_restricted": sum(1 for r in rows if 0 < r["eligible_slots"] < len(slots)),
                "assigned_faculty": sum(1 for r in rows if r["new"] > 0)}

    def render_summary(self, aid: int) -> str:
        s = self.summary(aid)
        se, a = s["session"], s["allocation"]
        state = {"draft": "awaiting your approval", "approved": "APPROVED", "superseded": "superseded",
                 "cancelled": "cancelled", "completed": "completed"}[a["status"]]
        feas = {"complete": "complete - every seat filled",
                "partial": f"PARTIAL - {s['unfilled']} seat(s) could not be filled",
                "infeasible": "NOT FEASIBLE under the current constraints"}[a["feasibility"]]
        lines = [
            f"**{se['name']}** - allocation version {a['version']} ({state}); {feas}",
            md_table(["Item", "Value"], [
                ["Examination period", f"{fmt_date(se['start_date'])} to {fmt_date(se['end_date'])}"],
                ["Examination days", s["days"]],
                ["Shifts per day", s["shifts"]],
                ["Rooms", s["rooms"]],
                ["Invigilators per room", s["per_room"]],
                ["Total duty seats", s["total_slots"]],
                ["Seats filled / unfilled", f"{s['filled']} / {s['unfilled']}"],
                ["Eligible faculty", s["eligible_faculty"]],
                ["Fully excluded faculty", s["excluded_faculty"]],
                ["Partly restricted faculty", s["partially_restricted"]],
                ["Faculty assigned", s["assigned_faculty"]],
                *([["Duty split between groups", describe(normalise_spec(se["options"]["group_split"]))]]
                  if (se["options"] or {}).get("group_split") else []),
                ["Regular classes suspended", {1: "yes", 0: "no"}.get(se["classes_suspended"], "not stated")],
            ])]
        return "\n\n".join(lines)

    # ------------------------------------------------------ B. day-wise
    def day_wise(self, aid: int, on: Optional[date] = None) -> List[Dict[str, Any]]:
        a, session, matrix, comps, duties = self._ctx(aid)
        per = session["invigilators_per_room"]
        grid: Dict[Tuple[str, str, str], Dict[int, Dict[str, Any]]] = defaultdict(dict)
        order: List[Tuple[str, str, str]] = []
        for d in duties:
            if on and d["exam_date"] != on.isoformat():
                continue
            k = (d["exam_date"], d["shift"], d["room"])
            if k not in grid:
                order.append(k)
            grid[k][d["seat"]] = d
        out = []
        for k in order:
            seats = grid[k]
            out.append({"date": k[0], "shift": k[1], "room": k[2],
                        "invigilators": [(seats[i]["faculty"], seats[i]["department"] or "—") if i in seats
                                         else ("— unfilled —", "") for i in range(1, per + 1)]})
        return out

    def render_day_wise(self, aid: int, on: Optional[date] = None, limit_rows: Optional[int] = None) -> str:
        rows = self.day_wise(aid, on)
        if not rows:
            return "_No duties on that date._"
        per = len(rows[0]["invigilators"])
        shown = rows if limit_rows is None else rows[:limit_rows]
        head = ["Date", "Shift", "Room"]
        for i in range(per):
            head += [f"Invigilator {i + 1}", "Department"]
        body = []
        for r in shown:
            line = [fmt_date(r["date"]), r["shift"], r["room"]]
            for n, d in r["invigilators"]:
                line += [n, d]
            body.append(line)
        txt = md_table(head, body)
        if limit_rows is not None and len(rows) > limit_rows:
            txt += f"\n\n_{len(rows) - limit_rows} more rows - ask for the full report or one date._"
        return txt

    # --------------------------------------------------- C. workload
    def render_workload(self, aid: int, only_assigned: bool = False) -> str:
        rows = self.counts(aid)
        if only_assigned:
            rows = [r for r in rows if r["new"] > 0]
        rows.sort(key=lambda r: (r["department"], r["name"]))
        return md_table(["Faculty", "Department", "Historical duties", "New duties", "Combined total"],
                        [[r["name"], r["department"], num(r["historical"] + r["committed"]), r["new"],
                          num(r["combined"])] for r in rows], right=(2, 3, 4))

    # ------------------------------------------------ D. department
    def department_rows(self, aid: int) -> List[Dict[str, Any]]:
        rows = self.counts(aid)
        by: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by[r["department"]].append(r)
        out = []
        for dept, rs in sorted(by.items()):
            assigned = [r for r in rs if r["new"] > 0]
            total = sum(r["new"] for r in rs)
            out.append({"department": dept, "faculty": len(rs),
                        "eligible": sum(1 for r in rs if r["eligible_slots"] > 0),
                        "excluded": sum(1 for r in rs if r["eligible_slots"] == 0),
                        "assigned": len(assigned), "duties": total,
                        "avg": round(total / len(assigned), 2) if assigned else 0.0})
        return out

    def render_departments(self, aid: int) -> str:
        rows = self.department_rows(aid)
        return md_table(["Department", "Total faculty", "Eligible", "Excluded", "Assigned", "Total duties",
                         "Avg per assigned"],
                        [[r["department"], r["faculty"], r["eligible"], r["excluded"], r["assigned"],
                          r["duties"], r["avg"]] for r in rows], right=(1, 2, 3, 4, 5, 6))

    # --------------------------------------------------------- group split
    def group_rows(self, aid: int) -> List[Dict[str, Any]]:
        a, session, matrix, comps, duties = self._ctx(aid)
        spec = (session["options"] or {}).get("group_split")
        if not spec:
            return []
        spec = normalise_spec(spec)
        active = [f for f in matrix.faculty if f["active"]]
        gm = group_map(((f["id"], f["department"] or "") for f in active), spec)
        wanted: Dict[str, int] = defaultdict(int)
        for s in matrix.slots:
            for g, q in seat_quota(s.required, spec["shares"]).items():
                wanted[g] += q
        filled: Dict[str, int] = defaultdict(int)
        for d in duties:
            filled[gm.get(d["faculty_id"], "(no group)")] += 1
        rows = []
        for g in spec["groups"]:
            people = [f for f in active if gm.get(f["id"]) == g]
            assigned = {d["faculty_id"] for d in duties if gm.get(d["faculty_id"]) == g}
            rows.append({"group": g, "faculty": len(people),
                         "eligible": sum(1 for f in people if matrix.eligible_slots(f["id"])),
                         "wanted": wanted[g], "filled": filled[g], "assigned": len(assigned),
                         "avg": round(filled[g] / len(assigned), 2) if assigned else 0.0})
        return rows

    def render_groups(self, aid: int) -> str:
        a = self.repo.allocation(aid)
        session = self.repo.session(a["session_id"])
        spec = (session["options"] or {}).get("group_split")
        if not spec:
            return ("No group split is set. Say e.g. *divide the duties equally between CS and non-CS faculty*.")
        rows = self.group_rows(aid)
        txt = f"Split: {describe(normalise_spec(spec))}\n\n" + md_table(
            ["Group", "Faculty", "Eligible", "Seats planned", "Seats filled", "Faculty assigned", "Avg duties per assigned"],
            [[r["group"], r["faculty"], r["eligible"], r["wanted"], r["filled"], r["assigned"], r["avg"]]
             for r in rows], right=(1, 2, 3, 4, 5, 6))
        short = [r for r in rows if r["filled"] < r["wanted"]]
        if short:
            txt += "\n\n" + "; ".join(f"{r['group']} is {r['wanted'] - r['filled']} seat(s) short of its share "
                                       f"(too few eligible {r['group']} faculty under the current limits)" for r in short)
        return txt

    # ------------------------------------------------------ E. fairness
    def consecutive_faculty(self, aid: int) -> List[Dict[str, Any]]:
        a, session, matrix, comps, duties = self._ctx(aid)
        pos = {s.exam_date.isoformat(): s.day_pos for s in matrix.slots}
        mine: Dict[int, Dict[int, List[str]]] = defaultdict(lambda: defaultdict(list))
        names = {}
        for d in duties:
            mine[d["faculty_id"]][pos[d["exam_date"]]].append(d["exam_date"])
            names[d["faculty_id"]] = (d["faculty"], d["department"])
        out = []
        for fid, days in mine.items():
            ps = sorted(days)
            runs = [(p, q) for p, q in zip(ps, ps[1:]) if q - p == 1]
            if runs:
                out.append({"faculty": names[fid][0], "department": names[fid][1] or "—",
                            "pairs": [(days[p][0], days[q][0]) for p, q in runs]})
        return sorted(out, key=lambda r: r["faculty"])

    def render_fairness(self, aid: int) -> str:
        a = self.repo.allocation(aid)
        f = (a["quality"] or {}).get("fairness") or {}
        if not f:
            return "_No fairness data stored for this version._"
        rest = f"{f.get('min_gap')} / {f.get('avg_gap')}" if f.get("min_gap") is not None else "n/a"
        rows = [
            ["Eligible faculty", f["eligible_faculty"]],
            ["New duties per eligible faculty (avg / min / max)", f"{f['avg_new']} / {f['min_new']} / {f['max_new']}"],
            ["Historical workload (min / avg / max / std)",
             f"{num(f['historical']['min'])} / {f['historical']['avg']} / {num(f['historical']['max'])} / {f['historical']['std']}"],
            ["Current-exam workload (min / avg / max / std)",
             f"{f['current']['min']} / {f['current']['avg']} / {f['current']['max']} / {f['current']['std']}"],
            ["Combined workload (min / avg / max / std)",
             f"{num(f['combined']['min'])} / {f['combined']['avg']} / {num(f['combined']['max'])} / {f['combined']['std']}"],
            ["Workload deviation (std of combined)", f["workload_std"]],
            ["Mean distance from fair target (duties)", f["mean_abs_target_deviation"]],
            ["Faculty given duties", f["assigned_faculty"]],
            ["Eligible faculty with zero duties", f["zero_duty_eligible"]],
            ["Consecutive-day duty pairs", f["consecutive_pairs"]],
            ["Faculty with consecutive duties", len(f["faculty_with_consecutive"])],
            ["Gap between duties in exam days (min / avg)", rest],
            ["Same-day repeat duties", f["same_day_repeats"]],
        ]
        txt = md_table(["Measure", "Value"], rows)
        counts = sorted(self.counts(aid), key=lambda r: -r["combined"])[:5]
        txt += "\n\nHighest combined workload:\n" + md_table(
            ["Faculty", "Department", "Combined"], [[r["name"], r["department"], num(r["combined"])] for r in counts],
            right=(2,))
        below = (a["quality"] or {}).get("below_minimum")
        if below:
            txt += f"\n\n**Below the minimum duty requirement:** {', '.join(below)} - they could not be given " \
                   f"more without breaking another rule or creating artificial duties."
        return txt

    # ----------------------------------------------------- F. exclusions
    def exclusion_rows(self, sid: int, include_inactive: bool = True) -> List[Dict[str, Any]]:
        session = self.repo.session(sid)
        out = []
        for r in self.svc.restrictions.active(session_id=sid, include_inactive=include_inactive):
            out.append({"id": r["id"], "faculty": r["faculty"], "department": r["department"] or "—",
                        "type": r["kind"] + ("/" + r["category"] if r["category"] != "general" else ""),
                        "scope": describe_scope(r, session), "shift": r.get("shift_label") or "all",
                        "reason": r["reason"] or "—", "status": r["status"]})
        return out

    def render_exclusions(self, sid: int, include_inactive: bool = False) -> str:
        rows = self.exclusion_rows(sid, include_inactive)
        return md_table(["#", "Faculty", "Department", "Type", "Dates", "Shift", "Reason", "Status"],
                        [[r["id"], r["faculty"], r["department"], r["type"], r["scope"], r["shift"], r["reason"],
                          r["status"]] for r in rows])

    # -------------------------------------------------- G. unfilled rooms
    def render_unfilled(self, aid: int) -> str:
        un = self.repo.unfilled(aid)
        if not un:
            return "Every room is fully staffed - there are no unfilled seats."
        # one line per room/shift/date with the shortage reason
        grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
        for u in un:
            grouped[(u["exam_date"], u["shift"], u["room"])].append(u)
        rows = []
        for (d, sh, room), us in grouped.items():
            u = us[0]
            rows.append([fmt_date(d), sh, room, len(us), u["required"], u["available"], u["reason"]])
        return md_table(["Date", "Shift", "Room", "Seats missing", "Required in slot", "Eligible in slot", "Reason"],
                        rows, right=(3, 4, 5))

    # --------------------------------------------------- Part 8 eligibility
    def render_eligibility(self, sid: int, only_problem: bool = False) -> str:
        session = self.repo.session(sid)
        matrix = self.svc.eligibility.evaluate(sid)
        rows = []
        for f in matrix.faculty:
            if not f["active"]:
                continue
            status, why = matrix.summary(f["id"])
            if only_problem and status == "Eligible" and "Not available" not in why:
                continue
            rows.append([f["name"], f["department"] or "—", status, why])
        rows.sort(key=lambda r: ({"Excluded": 0, "Unavailable": 1, "Ineligible": 2, "Eligible": 3}[r[2]], r[0]))
        return md_table(["Faculty", "Department", "Status", "Explanation"], rows)

    # --------------------------------------------------------- full report
    def render_full(self, aid: int) -> str:
        a = self.repo.allocation(aid)
        sid = a["session_id"]
        parts = ["## A. Examination summary", self.render_summary(aid),
                 "## B. Day-wise allocation", self.render_day_wise(aid),
                 "## C. Faculty workload", self.render_workload(aid),
                 "## D. Department-wise report", self.render_departments(aid)]
        if (self.repo.session(a["session_id"])["options"] or {}).get("group_split"):
            parts += ["## D2. Group split", self.render_groups(aid)]
        parts += [
                 "## E. Fairness report", self.render_fairness(aid),
                 "## F. Exclusion report", self.render_exclusions(sid, include_inactive=True)]
        if self.repo.unfilled(aid):
            parts += ["## G. Unfilled rooms", self.render_unfilled(aid)]
        return "\n\n".join(parts)

    # ============================================================ explanations
    def _target_of(self, aid: int, fid: int) -> float:
        a = self.repo.allocation(aid)
        t = (a["quality"] or {}).get("targets") or {}
        return float(t.get(str(fid), 0.0))

    def _profile(self, aid: int, name: str) -> Dict[str, Any]:
        rows = {r["name"]: r for r in self.counts(aid)}
        r = rows[name]
        a, session, matrix, comps, duties = self._ctx(aid)
        pos = {s.exam_date.isoformat(): s.day_pos for s in matrix.slots}
        mine = sorted({pos[d["exam_date"]] for d in duties if d["faculty_id"] == r["id"]})
        gaps = [b - a_ for a_, b in zip(mine, mine[1:])]
        pool = [x for x in rows.values() if x["eligible_slots"] > 0]
        r = dict(r)
        r["target"] = self._target_of(aid, r["id"])
        r["total_slots"] = len(matrix.slots)
        r["pool_avg_hist"] = round(sum(x["historical"] + x["committed"] for x in pool) / len(pool), 2) if pool else 0
        r["gaps"] = gaps
        r["restrictions"] = [x for x in self.svc.restrictions.active(r["id"], session["id"])]
        r["max"] = session["max_duties"]
        r["session"] = session
        return r

    def explain_faculty(self, aid: int, name: str) -> str:
        p = self._profile(aid, name)
        h = p["historical"] + p["committed"]
        lines = [f"**{name}** ({p['department']}) was given **{p['new']}** duties "
                 f"(historical {num(h)}, combined {num(p['combined'])})."]
        if p["eligible_slots"] == 0:
            lines.append(f"They could not be assigned at all: {p['why']}.")
        else:
            cmp_ = "below" if h < p["pool_avg_hist"] else "above" if h > p["pool_avg_hist"] else "equal to"
            lines.append(f"- Eligible for {p['eligible_slots']} of {p['total_slots']} examination slots.")
            lines.append(f"- Historical workload {num(h)} is {cmp_} the average of eligible faculty "
                         f"({p['pool_avg_hist']}), which set a fair target of about {p['target']:.1f} new duties.")
            if p["max"] is not None and p["new"] >= p["max"]:
                lines.append(f"- They reached the maximum of {p['max']} duties, so no more could be given.")
            if p["gaps"]:
                lines.append(f"- Gaps between their duty days (in exam days): {', '.join(map(str, p['gaps']))}"
                             f"{' - includes consecutive days' if 1 in p['gaps'] else ''}.")
            if p["eligible_slots"] < p["total_slots"]:
                lines.append(f"- Not available for every slot: {p['why'].split('Not available: ')[-1]}")
        return "\n".join(lines)

    def compare(self, aid: int, a_name: str, b_name: str) -> str:
        pa, pb = self._profile(aid, a_name), self._profile(aid, b_name)
        rows = []
        for label, key in (("Department", "department"), ("Historical duties", None), ("Eligible slots", "eligible_slots"),
                           ("Fair target (new duties)", "target"), ("New duties given", "new"),
                           ("Combined workload", "combined")):
            def val(p):
                if key is None:
                    return num(p["historical"] + p["committed"])
                v = p[key]
                return f"{v:.1f}" if key == "target" else (num(v) if isinstance(v, float) else v)
            rows.append([label, val(pa), val(pb)])
        reasons = []
        ha, hb = pa["historical"] + pa["committed"], pb["historical"] + pb["committed"]
        hi, lo = (pa, pb) if pa["new"] > pb["new"] else (pb, pa)
        if pa["new"] == pb["new"]:
            reasons.append(f"They received the same number of duties ({pa['new']}).")
        else:
            hh, hl = hi["historical"] + hi["committed"], lo["historical"] + lo["committed"]
            reasons.append(f"{hi['name']} received more duties ({hi['new']} vs {lo['new']}).")
            if hh < hl:
                reasons.append(f"- Their historical workload is lower ({num(hh)} vs {num(hl)}), so the fairness "
                               f"target gave them more.")
            if hi["eligible_slots"] > lo["eligible_slots"]:
                reasons.append(f"- They were eligible for more slots ({hi['eligible_slots']} vs "
                               f"{lo['eligible_slots']}); {lo['name']} was limited by: {lo['why']}.")
            if lo["max"] is not None and lo["new"] >= lo["max"]:
                reasons.append(f"- {lo['name']} reached the maximum of {lo['max']} duties.")
            if hh >= hl and hi["eligible_slots"] <= lo["eligible_slots"]:
                reasons.append("- Their workloads and eligibility are comparable; the difference comes from the "
                               "controlled tie-breaking among equally suitable candidates and from rest-day rules.")
        return md_table(["", a_name, b_name], rows) + "\n\n" + "\n".join(reasons)

    def explain_assignment(self, aid: int, name: str, on: date) -> str:
        p = self._profile(aid, name)
        a, session, matrix, comps, duties = self._ctx(aid)
        mine = [d for d in duties if d["faculty"] == name and d["exam_date"] == on.isoformat()]
        if not mine:
            return self.why_not(aid, name, on)
        pos = {s.exam_date.isoformat(): s.day_pos for s in matrix.slots}
        others = sorted(pos[d["exam_date"]] for d in duties if d["faculty"] == name and d["exam_date"] != on.isoformat())
        here = pos[on.isoformat()]
        nearest = min((abs(o - here) for o in others), default=None)
        h = p["historical"] + p["committed"]
        parts = [f"{name} was assigned on {fmt_date(on)} ({', '.join(sorted({d['shift'] + ' ' + d['room'] for d in mine}))}) because:",
                 f"- they were eligible for that slot (no restriction, conflicting duty or class applied);",
                 f"- their historical workload is {num(h)} against an eligible-pool average of {p['pool_avg_hist']}, "
                 f"giving a fair target of about {p['target']:.1f} new duties (they received {p['new']});"]
        if nearest is None:
            parts.append("- it is their only duty, so there is no rest-day conflict.")
        elif nearest >= 2:
            parts.append(f"- the nearest other duty is {nearest} exam days away, so rest between duties is respected.")
        else:
            parts.append("- the nearest other duty is on an adjacent exam day; the engine accepted this because "
                         "avoiding it would have left seats unfilled or pushed someone further from their fair share.")
        return "\n".join(parts)

    def why_not(self, aid: int, name: str, on: date) -> str:
        a, session, matrix, comps, duties = self._ctx(aid)
        f = next((x for x in matrix.faculty if x["name"] == name), None)
        if f is None:
            return f"{name} is not in the faculty list."
        slots = [s for s in matrix.slots if s.exam_date == on]
        if not slots:
            return f"{fmt_date(on)} is not an examination date of this session."
        lines = []
        for s in slots:
            vs = matrix.reasons(f["id"], s.index)
            if vs:
                lines.append(f"- {s.shift_label} shift: not eligible - " + "; ".join(v.text for v in vs))
            else:
                n = sum(1 for d in duties if d["faculty_id"] == f["id"])
                if session["max_duties"] is not None and n >= session["max_duties"]:
                    lines.append(f"- {s.shift_label} shift: eligible, but they already reached the maximum of "
                                 f"{session['max_duties']} duties.")
                else:
                    lines.append(f"- {s.shift_label} shift: eligible, but other candidates fitted the fairness "
                                 f"targets and rest rules better for the seats available.")
        return f"{name} was not assigned on {fmt_date(on)}:\n" + "\n".join(lines)

    def render_changes(self, changes: List[Dict[str, Any]]) -> str:
        if not changes:
            return "_No assignment changed._"
        return md_table(["Date", "Shift", "Room", "Seat", "Was", "Now", "Department"],
                        [[fmt_date(c["date"]), c["shift"], c["room"], c["seat"], c["old"] or "—",
                          c["new"] or "— unfilled —", c["new_department"] or "—"] for c in changes], right=(3,))