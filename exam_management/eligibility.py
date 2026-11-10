"""
Eligibility engine: for EVERY faculty member and EVERY (date, shift) decide
whether duty is allowed - and say why not.  It runs BEFORE any fairness maths, so
the optimiser only ever sees opportunities a person genuinely has.

Reasons are checked in a fixed order and all are recorded; the first one is the
headline.  Nothing is dropped silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Set, Tuple

from .config import ExamSettings
from .models import SlotInfo
from .providers import NoTimetable, TimetableConflicts
from .policy import PolicyService
from .repository import Repository
from .restrictions import RestrictionService, applies, describe_scope


def _min(hhmm: str) -> int:
    h, m = hhmm.split(":")[:2]
    return int(h) * 60 + int(m)


def clock_overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return _min(a_start) < _min(b_end) and _min(b_start) < _min(a_end)


# status headline by reason code
STATUS_OF = {
    "inactive": "Ineligible",
    "department": "Excluded",
    "permanent": "Excluded",
    "session": "Excluded",
    "administrative": "Excluded",
    "leave": "Unavailable",
    "date": "Unavailable",
    "shift": "Unavailable",
    "existing_duty": "Ineligible",
    "commitment": "Ineligible",
    "timetable": "Ineligible",
}
SEVERITY = {"Excluded": 3, "Unavailable": 2, "Ineligible": 1, "Eligible": 0}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    code: str = ""
    text: str = "Available"


@dataclass
class EligibilityMatrix:
    session: Dict[str, Any]
    faculty: List[Dict[str, Any]]
    slots: List[SlotInfo]
    verdicts: Dict[Tuple[int, int], List[Verdict]] = field(default_factory=dict)  # (fid, slot idx)

    def ok(self, fid: int, slot_index: int) -> bool:
        return not self.verdicts.get((fid, slot_index))

    def eligible_slots(self, fid: int) -> Set[int]:
        return {s.index for s in self.slots if self.ok(fid, s.index)}

    def reasons(self, fid: int, slot_index: int) -> List[Verdict]:
        return self.verdicts.get((fid, slot_index), [])

    def summary(self, fid: int) -> Tuple[str, str]:
        """(status, explanation) for the whole period."""

        total = len(self.slots)
        good = len(self.eligible_slots(fid))
        if good == total and total:
            return "Eligible", "Available for every examination slot"
        # tally blocking reasons by text
        tally: Dict[str, int] = {}
        worst = "Eligible"
        for s in self.slots:
            for v in self.verdicts.get((fid, s.index), [])[:1]:
                tally[v.text] = tally.get(v.text, 0) + 1
                st = STATUS_OF.get(v.code, "Ineligible")
                if SEVERITY[st] > SEVERITY[worst]:
                    worst = st
        parts = "; ".join(f"{t} ({n} slot{'s' if n != 1 else ''})" for t, n in
                          sorted(tally.items(), key=lambda kv: -kv[1]))
        if good == 0:
            return worst, parts or "No eligible slot"
        return "Eligible", f"Eligible for {good} of {total} slots. Not available: {parts}"


def session_slots(repo: Repository, session: Dict[str, Any]) -> List[SlotInfo]:
    dates = repo.session_dates(session["id"])
    shifts = repo.session_shifts(session["id"])
    rooms = repo.session_rooms(session["id"])
    overrides = (session.get("options") or {}).get("rooms_override") or {}
    slots: List[SlotInfo] = []
    for pos, d in enumerate(dates):
        for sh in shifts:
            n = overrides.get(f"{d.isoformat()}|{sh['label']}")
            labels = tuple(rooms[:n]) if isinstance(n, int) and 0 <= n <= len(rooms) else tuple(rooms)
            slots.append(SlotInfo(len(slots), d, sh["id"], sh["label"], sh["start_time"],
                                  sh["end_time"], pos, labels, session["invigilators_per_room"]))
    return slots


def overlapping_pairs(slots: List[SlotInfo]) -> List[Tuple[int, int]]:
    out = []
    for i, a in enumerate(slots):
        for b in slots[i + 1:]:
            if a.exam_date == b.exam_date and clock_overlap(a.start, a.end, b.start, b.end):
                out.append((a.index, b.index))
    return out


class EligibilityEngine:

    def __init__(self, repo: Repository, timetable: Optional[TimetableConflicts] = None,
                 settings: Optional[ExamSettings] = None):
        self.repo = repo
        self.timetable = timetable or NoTimetable()
        self.settings = settings or ExamSettings()
        self.restrictions = RestrictionService(repo)

    def evaluate(self, session_id: int) -> EligibilityMatrix:
        session = self.repo.session(session_id)
        if session is None:
            raise ValueError(f"unknown examination session {session_id}")
        slots = session_slots(self.repo, session)
        faculty = self.repo.faculty_all(active_only=False)
        by_name = {f["name"]: f["id"] for f in faculty}
        matrix = EligibilityMatrix(session, faculty, slots)

        opts = session.get("options") or {}
        dept = opts.get("departments") or {}
        include = {d.lower() for d in dept.get("include", [])}
        exclude = {d.lower() for d in dept.get("exclude", [])}

        restr: Dict[int, List[Dict[str, Any]]] = {}
        for r in self.restrictions.active(session_id=session_id):
            restr.setdefault(r["faculty_id"], []).append(r)

        dept_rules: Dict[str, List[Dict[str, Any]]] = {}
        for r in PolicyService(self.repo).department_restrictions(session_id):
            dept_rules.setdefault(r["department"].lower(), []).append(r)

        dates = sorted({s.exam_date for s in slots})
        other = {}
        for row in self.repo.other_session_duties(session_id, dates):
            other.setdefault(row["faculty_id"], []).append(row)
        commits: Dict[int, List[Dict[str, Any]]] = {}
        for row in self.repo.commitments(dates):
            commits.setdefault(row["faculty_id"], []).append(row)

        # teaching conflicts only when classes are NOT suspended
        busy_cache: Dict[Tuple[int, str, str], Set[int]] = {}
        check_timetable = session["classes_suspended"] == 0

        def busy_ids(slot: SlotInfo) -> Set[int]:
            key = (slot.exam_date.weekday(), slot.start, slot.end)
            if key not in busy_cache:
                names = self.timetable.busy(slot.exam_date, slot.start, slot.end)
                busy_cache[key] = {by_name[n] for n in names if n in by_name}
            return busy_cache[key]

        for f in faculty:
            fid = f["id"]
            for s in slots:
                why: List[Verdict] = []
                if not f["active"]:
                    why.append(Verdict(False, "inactive", "Not an active faculty member"))
                dl = (f["department"] or "").lower()
                if include and dl not in include:
                    why.append(Verdict(False, "department", "Department not included in this allocation"))
                if exclude and dl in exclude:
                    why.append(Verdict(False, "department", "Department excluded from this allocation"))
                for r in dept_rules.get(dl, []):
                    if applies(r, session, s.exam_date, s.shift_label):
                        why.append(Verdict(False, "department",
                                           f"Department excluded: {r['reason'] or 'no reason recorded'} "
                                           f"[{describe_scope(r, session)}]"))
                for r in restr.get(fid, []):
                    if applies(r, session, s.exam_date, s.shift_label):
                        code = ("permanent" if r["kind"] == "permanent" else
                                "session" if r["kind"] == "session" and r["category"] == "general" else
                                r["category"] if r["category"] in ("administrative", "leave") else
                                "shift" if r["kind"] == "shift" else "date")
                        base = r["reason"] or "No reason recorded"
                        why.append(Verdict(False, code, f"{base} [{describe_scope(r, session)}]"
                                           if code not in ("administrative",) else
                                           f"{base} [{describe_scope(r, session)}]"))
                for row in other.get(fid, []):
                    if row["exam_date"] == s.exam_date.isoformat() and \
                            clock_overlap(row["start_time"], row["end_time"], s.start, s.end):
                        why.append(Verdict(False, "existing_duty", "Existing examination duty (another session)"))
                        break
                for row in commits.get(fid, []):
                    if row["commit_date"] == s.exam_date.isoformat() and \
                            clock_overlap(row["start_time"], row["end_time"], s.start, s.end):
                        why.append(Verdict(False, "commitment",
                                           f"Registered commitment: {row['label'] or 'busy'}"))
                        break
                if check_timetable and fid in busy_ids(s):
                    why.append(Verdict(False, "timetable", "Teaching a regular class at that time"))
                if why:
                    matrix.verdicts[(fid, s.index)] = why
        return matrix