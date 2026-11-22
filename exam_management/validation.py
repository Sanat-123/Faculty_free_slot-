"""
Independent validation of a finished allocation.

The checks recompute everything from the stored duty rows, the timetable provider and the database - they do NOT
reuse the solver or the eligibility verdicts that produced the allocation.  Each condition ends as exactly one of

    Passed         the system verified it and it holds
    Failed         the system verified it and it is violated
    Not verified   the data needed to verify it is missing (never reported as Passed)
    Not applicable the coordinator did not ask for it / it does not apply to this examination
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Set, Tuple

from .duty_rules import DutyRule
from .eligibility import clock_overlap, session_slots
from .restrictions import applies

PASSED, FAILED, NOT_VERIFIED, NA = "Passed", "Failed", "Not verified", "Not applicable"


@dataclass
class Check:
    check: str
    status: str
    detail: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {"check": self.check, "status": self.status, "detail": self.detail}


def _plain(name: str, titles: List[str]) -> str:
    from .importer import strip_titles
    return strip_titles(re.sub(r"\s*\((?:EC|unverified)[^)]*\)", "", name), titles)


def validate_allocation(service, aid: int) -> List[Check]:
    repo = service.repo
    alloc = repo.allocation(aid)
    sid = alloc["session_id"]
    session = repo.session(sid)
    opts = session.get("options") or {}
    slots = session_slots(repo, session)
    duties = repo.duties(aid)
    unfilled = repo.unfilled(aid)
    names = {f["id"]: f["name"] for f in repo.faculty_all(active_only=False)}
    dept = {f["id"]: (f["department"] or "") for f in repo.faculty_all(active_only=False)}
    slot_of = {(s.exam_date.isoformat(), s.shift_id): s for s in slots}
    out: List[Check] = []

    # 1. duplicates and simultaneous rooms ---------------------------------------------------------------
    by_fac: Dict[int, List[Any]] = defaultdict(list)
    for d in duties:
        s = slot_of.get((d["exam_date"], d["shift_id"]))
        if s is not None:
            by_fac[d["faculty_id"]].append((s, d))
    dup, overlap = [], []
    for fid, rows in by_fac.items():
        seen = Counter((s.exam_date, s.shift_id) for s, _ in rows)
        dup += [f"{names[fid]} twice on {d:%d %b}" for (d, _), n in seen.items() if n > 1]
        for i, (a, _) in enumerate(rows):
            for b, _ in rows[i + 1:]:
                if a.exam_date == b.exam_date and a.shift_id != b.shift_id and clock_overlap(a.start, a.end, b.start, b.end):
                    overlap.append(f"{names[fid]} on {a.exam_date:%d %b} ({a.shift_label} / {b.shift_label})")
    out.append(Check("No faculty in two rooms at the same time", FAILED if dup or overlap else PASSED,
                     "; ".join((dup + overlap)[:5]) or f"{len(duties)} duties compared by actual start and end time"))

    # 2. teaching timetable --------------------------------------------------------------------------------
    tt = service.timetable
    if session["classes_suspended"] == 1:
        out.append(Check("Teaching timetable conflicts", NA, "all regular classes are suspended for this examination"))
    elif not hasattr(tt, "busy") or type(tt).__name__ == "NoTimetable":
        out.append(Check("Teaching timetable conflicts", NOT_VERIFIED, "no teaching timetable is loaded"))
    else:
        from .importer import load_columns
        titles = load_columns().get("titles", [])
        stopped = set(opts.get("suspended_classes") or [])
        cache: Dict[Tuple[date, str, str], Set[str]] = {}
        clashes, unknown = [], set()
        known = {_plain(n, titles) for n in tt.known_names()} if hasattr(tt, "known_names") else None
        for fid, rows in by_fac.items():
            me = _plain(names[fid], titles)
            if known is not None and me not in known:
                unknown.add(names[fid])
            for s, d in rows:
                key = (s.exam_date, s.start, s.end)
                if key not in cache:
                    got = tt.busy_outside(*key, stopped) if stopped and hasattr(tt, "busy_outside") else tt.busy(*key)
                    cache[key] = {_plain(n, titles) for n in got}
                if me in cache[key]:
                    clashes.append(f"{names[fid]} on {s.exam_date:%d %b} {s.shift_label}")
        if clashes:
            out.append(Check("Teaching timetable conflicts", FAILED, "; ".join(clashes[:5])))
        elif unknown:
            out.append(Check("Teaching timetable conflicts", NOT_VERIFIED,
                             f"{len(unknown)} assigned faculty have no timetable on record, so a clash cannot be ruled out"))
        else:
            scope = f" (classes taking the examination are not counted: {len(stopped)})" if stopped else ""
            out.append(Check("Teaching timetable conflicts", PASSED, f"every assigned faculty member's timetable checked{scope}"))

    # 3. existing examination duties / registered commitments ------------------------------------------------
    dates = sorted({s.exam_date for s in slots})
    clash = []
    for row in repo.other_session_duties(sid, dates):
        for s, d in by_fac.get(row["faculty_id"], []):
            if row["exam_date"] == s.exam_date.isoformat() and clock_overlap(row["start_time"], row["end_time"], s.start, s.end):
                clash.append(f"{names[row['faculty_id']]} on {s.exam_date:%d %b}")
    for row in repo.commitments(dates):
        for s, d in by_fac.get(row["faculty_id"], []):
            if row["commit_date"] == s.exam_date.isoformat() and clock_overlap(row["start_time"], row["end_time"], s.start, s.end):
                clash.append(f"{names[row['faculty_id']]} on {s.exam_date:%d %b} ({row['label'] or 'commitment'})")
    out.append(Check("Existing examination duty conflicts", FAILED if clash else PASSED,
                     "; ".join(clash[:5]) or "compared with every other approved duty and registered commitment on these dates"))

    # 4. availability restrictions ---------------------------------------------------------------------------
    bad = []
    for r in service.restrictions.active(session_id=sid):
        for s, d in by_fac.get(r["faculty_id"], []):
            if applies(r, session, s.exam_date, s.shift_label):
                bad.append(f"{names[r['faculty_id']]} on {s.exam_date:%d %b} {s.shift_label}")
    out.append(Check("Faculty availability (leave, exclusions, unavailability)", FAILED if bad else PASSED,
                     "; ".join(bad[:5]) or "no assigned duty falls inside an active restriction"))

    # 5. department restriction --------------------------------------------------------------------------------
    inc = {x.lower() for x in (opts.get("departments") or {}).get("include", [])}
    exc = {x.lower() for x in (opts.get("departments") or {}).get("exclude", [])}
    if not inc and not exc:
        out.append(Check("Faculty department restriction", NA, "all departments may invigilate"))
    else:
        wrong, blank = [], []
        for fid in by_fac:
            dl = dept[fid].lower()
            if not dl:
                blank.append(names[fid])
            elif (inc and dl not in inc) or dl in exc:
                wrong.append(names[fid])
        out.append(Check("Faculty department restriction", FAILED if wrong else NOT_VERIFIED if blank else PASSED,
                         "; ".join(wrong[:5]) or (f"{len(blank)} assigned faculty have no department on record" if blank
                                                  else "every assigned faculty member is in an allowed department")))

    # 6. duty-count rule ----------------------------------------------------------------------------------------
    rule = DutyRule.from_dict(opts["duty_rule"]) if opts.get("duty_rule") else None
    if rule is None:
        out.append(Check("Duty-count condition", NA, "no duty-count condition was given"))
    else:
        comps = repo.history_components(exclude_session_id=sid)
        new = Counter(d["faculty_id"] for d in duties)
        broke = []
        for fid in by_fac:
            c = comps.get(fid, {})
            prior = c.get("baseline", 0.0) + c.get("completed", 0.0) + c.get("committed", 0.0)
            if not rule.holds(prior + (new[fid] if rule.basis == "projected" else 0.0)):
                broke.append(f"{names[fid]} ({prior:g}{'+' + str(new[fid]) if rule.basis == 'projected' else ''})")
        if broke and rule.strength == "required":
            out.append(Check("Duty-count condition", FAILED, rule.describe() + " - broken by " + "; ".join(broke[:5])))
        elif rule.period and rule.period != "all recorded duties":
            out.append(Check("Duty-count condition", NOT_VERIFIED,
                             f"{rule.describe()}. You asked for '{rule.period}', but recorded duties carry no dates, "
                             "so ALL recorded duties were counted"))
        elif rule.strength == "preferred":
            out.append(Check("Duty-count condition", PASSED if not broke else NOT_VERIFIED,
                             rule.describe() + ("" if not broke else f" - {len(broke)} assigned faculty do not meet this preference "
                                                "(needed to fill the seats)")))
        else:
            out.append(Check("Duty-count condition", PASSED, rule.describe() + " - checked against recorded duties"))

    # 7. rooms and invigilators ------------------------------------------------------------------------------------
    missing = len(unfilled)
    out.append(Check("Every room has its required invigilators", FAILED if missing else PASSED,
                     f"{missing} position(s) unfilled" if missing else
                     f"{sum(s.required for s in slots)} of {sum(s.required for s in slots)} positions filled"))
    seats = Counter((d["exam_date"], d["shift_id"], d["room"]) for d in duties)
    over = [k for k, n in seats.items() if n > session["invigilators_per_room"]]
    wrong_room = [d for d in duties if (slot_of.get((d["exam_date"], d["shift_id"])) is None
                                        or d["room"] not in slot_of[(d["exam_date"], d["shift_id"])].rooms)]
    out.append(Check("Invigilators per room within requirement", FAILED if over or wrong_room else PASSED,
                     f"required {session['invigilators_per_room']} per room; "
                     + (f"{len(over)} room(s) over-staffed" if over else "no room over-staffed")))

    # 8. consecutive / per-day / per-faculty limits ---------------------------------------------------------------------
    if opts.get("prohibit_consecutive"):
        days = sorted(dates)
        pos = {d: i for i, d in enumerate(days)}
        cons = []
        for fid, rows in by_fac.items():
            used = sorted({pos[s.exam_date] for s, _ in rows})
            if any(b - a == 1 for a, b in zip(used, used[1:])):
                cons.append(names[fid])
        out.append(Check("No consecutive-day duties", FAILED if cons else PASSED, "; ".join(cons[:5]) or "checked for every assigned faculty member"))
    else:
        out.append(Check("No consecutive-day duties", NA, "not requested"))
    cap = opts.get("max_per_day")
    if cap:
        per_day = Counter((d["faculty_id"], d["exam_date"]) for d in duties)
        over_d = [f"{names[f]} on {dt}" for (f, dt), n in per_day.items() if n > cap]
        out.append(Check(f"At most {cap} duty per person per day", FAILED if over_d else PASSED, "; ".join(over_d[:5]) or "checked"))
    else:
        out.append(Check("Duties per person per day", NA, "no daily limit requested"))
    total = Counter(d["faculty_id"] for d in duties)
    if session["max_duties"] is not None or session["min_duties"] is not None:
        hi = [names[f] for f, n in total.items() if session["max_duties"] is not None and n > session["max_duties"]]
        out.append(Check("Duties per person in this examination (min/max)", FAILED if hi else PASSED,
                         "; ".join(hi[:5]) or f"min {session['min_duties'] if session['min_duties'] is not None else '-'}, "
                         f"max {session['max_duties'] if session['max_duties'] is not None else '-'}"))
    # 9. things the coordinator said that no checker understands ------------------------------------------------------
    for text in (opts.get("request") or {}).get("unmapped", []):
        out.append(Check("Your condition: " + (text if len(text) < 70 else text[:67] + "..."), NOT_VERIFIED,
                         "this requirement was not understood well enough to apply or check automatically"))
    return out


def overall(checks: List[Check]) -> str:
    if any(c.status == FAILED for c in checks):
        return FAILED
    if any(c.status == NOT_VERIFIED for c in checks):
        return NOT_VERIFIED
    return PASSED