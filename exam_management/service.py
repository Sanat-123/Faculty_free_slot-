"""
ExamService - orchestration.  Independent of the chatbot.

    roster / history   sync_roster(), sync_history()
    session            create_session(), update_session()
    restrictions       .restrictions (RestrictionService)
    generate           generate()  -> a DRAFT allocation version (never auto-approved)
    approve            approve()   -> re-validates every hard constraint first
    reallocate         replace_duty()  local replacement first, wider only if allowed
    validate           validate_allocation()  independent hard-constraint checker

Concurrency: a generation reads a state fingerprint, solves outside any lock, then
writes inside BEGIN IMMEDIATE only if the fingerprint is unchanged (else it
recomputes).  Approve / replace run entirely inside one write transaction.
"""

from __future__ import annotations

import secrets
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import db as dbm
from .config import ExamSettings, load_settings
from .db import transaction
from .eligibility import EligibilityEngine, EligibilityMatrix, clock_overlap, overlapping_pairs, session_slots
from .fairness import compute_targets
from .models import FacultyInfo, Problem, SlotInfo, SolveResult
from .groups import group_map, normalise_spec, seat_quota
from .optimizer import assign_rooms, evaluate, greedy_construct, solve, targets_for
from .providers import FacultyDirectory, HistoryProvider, NoTimetable, TimetableConflicts
from .repository import Repository, now
from .restrictions import RestrictionService, RestrictionSpec


class ExamError(RuntimeError):
    pass


class MissingInformation(ExamError):
    def __init__(self, fields: List[str], message: str = ""):
        super().__init__(message or "missing: " + ", ".join(fields))
        self.fields = fields


class ConcurrentModification(ExamError):
    pass


@dataclass
class SessionSpec:
    name: str
    dates: List[date]
    shifts: List[Dict[str, str]]                  # {"label","start","end"}
    per_room: int
    rooms: Optional[List[str]] = None
    room_count: Optional[int] = None
    min_duties: Optional[int] = None
    max_duties: Optional[int] = None
    classes_suspended: Optional[bool] = None
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Outcome:
    allocation_id: Optional[int]
    status: str                                   # complete | partial | infeasible | unchanged
    result: Optional[SolveResult]
    notes: List[str] = field(default_factory=list)
    changes: List[Dict[str, Any]] = field(default_factory=list)   # reallocation diff
    problem: Optional[Problem] = None


def _iso(d: date) -> str:
    return d.isoformat()


class ExamService:

    def __init__(self, db_path: Any, directory: Optional[FacultyDirectory] = None,
                 timetable: Optional[TimetableConflicts] = None,
                 history: Optional[HistoryProvider] = None,
                 settings: Optional[ExamSettings] = None, actor: str = "coordinator"):
        self.db_path = db_path
        self.conn = dbm.connect(db_path)
        dbm.migrate(self.conn)
        self.repo = Repository(self.conn)
        self.settings = settings or load_settings(self.conn)
        self.directory = directory
        self.timetable = timetable or NoTimetable()
        self.history = history
        self.actor = actor
        self.restrictions = RestrictionService(self.repo, actor)
        self.eligibility = EligibilityEngine(self.repo, self.timetable, self.settings)

    def reload_settings(self) -> None:
        self.settings = load_settings(self.conn)
        self.eligibility.settings = self.settings

    # ------------------------------------------------------ roster / history
    def sync_roster(self) -> Dict[str, int]:
        if self.directory is None:
            return {"added": 0, "updated": 0}
        return self.repo.sync_faculty(self.directory.faculty(), source="roster", deactivate_missing=True)

    def sync_history(self, source: str = "duty_sheet") -> Dict[str, Any]:
        if self.history is None:
            return {"imported": 0, "unknown": []}
        return self.repo.import_history(self.history.counts(), source)

    def import_department_file(self, path: str) -> Dict[str, Any]:
        """CSV/TXT with `name,department` per line (header optional). Names must match the roster."""

        import csv
        by_norm = {f["norm"]: f["id"] for f in self.repo.faculty_all(active_only=False)}
        from .repository import norm_name
        updated, unknown = 0, []
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.reader(fh):
                if len(row) < 2 or not row[0].strip():
                    continue
                if row[0].strip().lower() in ("name", "faculty", "faculty name"):
                    continue
                fid = by_norm.get(norm_name(row[0]))
                if fid is None:
                    unknown.append(row[0].strip())
                    continue
                self.repo.set_department([fid], row[1].strip(), source="file")
                updated += 1
        return {"updated": updated, "unknown": unknown}

    # ------------------------------------------------------------- sessions
    def validate_spec(self, spec: SessionSpec) -> List[str]:
        problems = []
        if not spec.dates:
            problems.append("exam dates")
        if not spec.shifts:
            problems.append("exam shifts (timings)")
        if not (spec.rooms or spec.room_count):
            problems.append("number of rooms")
        if not spec.per_room:
            problems.append("invigilators per room")
        return problems

    def create_session(self, spec: SessionSpec) -> int:
        missing = self.validate_spec(spec)
        if missing:
            raise MissingInformation(missing)
        if spec.min_duties is not None and spec.max_duties is not None and spec.min_duties > spec.max_duties:
            raise ExamError(f"minimum duties ({spec.min_duties}) is greater than maximum ({spec.max_duties})")
        dates = sorted(set(spec.dates))
        labels = [s["label"].lower() for s in spec.shifts]
        if len(set(labels)) != len(labels):
            raise ExamError("two shifts have the same name")
        rooms = list(spec.rooms) if spec.rooms else [f"Room {i + 1}" for i in range(spec.room_count or 0)]
        options = dict(spec.options)
        if not spec.rooms:
            options["rooms_generated"] = True
        return self.repo.create_session(
            name=spec.name, start=dates[0], end=dates[-1], dates=dates, shifts=spec.shifts,
            rooms=rooms, per_room=spec.per_room, min_duties=spec.min_duties,
            max_duties=spec.max_duties, classes_suspended=spec.classes_suspended, options=options)

    def update_session(self, sid: int, **fields: Any) -> None:
        sess = self.repo.session(sid)
        if sess is None:
            raise ExamError(f"unknown session {sid}")
        opts = dict(sess["options"])
        for k in ("departments", "prohibit_consecutive", "max_per_day", "group_split"):
            if k in fields:
                v = fields.pop(k)
                if v is None:
                    opts.pop(k, None)
                else:
                    opts[k] = v
        fields["options"] = opts
        self.repo.update_session(sid, **fields)
        self.repo.audit("update_session", "session", sid,
                        {k: v for k, v in fields.items() if k != "options"}, self.actor)

    # ---------------------------------------------------------- problem build
    def _fingerprint(self, sid: int) -> Tuple[Any, ...]:
        r = self.conn.execute(
            "SELECT (SELECT revision FROM exm_session WHERE id=?) rev,"
            "(SELECT COALESCE(MAX(id),0) FROM exm_allocation WHERE status IN ('approved','completed')) alloc,"
            "(SELECT COALESCE(MAX(id),0) FROM exm_restriction) rmax,"
            "(SELECT COUNT(*) FROM exm_restriction WHERE status='active') ract,"
            "(SELECT COALESCE(MAX(id),0) FROM exm_commitment) cmax,"
            "(SELECT COUNT(*) FROM exm_faculty WHERE active=1) fac", (sid,)).fetchone()
        return tuple(r)

    def build_problem(self, sid: int, *, seed: int = 0, previous: Optional[Set[Tuple[int, int]]] = None,
                      fixed: Optional[Set[Tuple[int, int]]] = None,
                      keep_preferred: Optional[Set[Tuple[int, int]]] = None,
                      forbidden: Optional[Set[Tuple[int, int]]] = None
                      ) -> Tuple[Problem, EligibilityMatrix]:
        session = self.repo.session(sid)
        if session is None:
            raise ExamError(f"unknown examination session {sid}")
        if session["classes_suspended"] is None and self.settings.require_class_suspension_answer:
            raise MissingInformation(["classes_suspended"],
                                     "Tell me whether regular classes are suspended during this examination.")
        matrix = self.eligibility.evaluate(sid)
        comps = self.repo.history_components(exclude_session_id=sid)
        faculty: List[FacultyInfo] = []
        for f in matrix.faculty:
            if not f["active"]:
                continue
            c = comps.get(f["id"], {})
            faculty.append(FacultyInfo(f["id"], f["name"], f["department"] or "",
                                       c.get("baseline", 0.0) + c.get("completed", 0.0),
                                       c.get("committed", 0.0)))
        eligible = {f.id: matrix.eligible_slots(f.id) for f in faculty}
        opts = session["options"]
        p = Problem(faculty=faculty, slots=matrix.slots, eligible=eligible,
                    min_duties=session["min_duties"], max_duties=session["max_duties"],
                    prohibit_consecutive=bool(opts.get("prohibit_consecutive")),
                    max_per_day=opts.get("max_per_day"), seed=seed,
                    fixed=fixed or set(), forbidden=forbidden or set(),
                    keep_preferred=keep_preferred or set(), previous=previous or set())
        p.overlaps = overlapping_pairs(p.slots)
        spec = opts.get("group_split")
        if spec:
            spec = normalise_spec(spec)
            p.groups = group_map(((f.id, f.department) for f in faculty), spec)
            for sl in p.slots:
                p.group_quota[sl.index] = seat_quota(sl.required, spec["shares"])
        return p, matrix

    def _slot_key(self, matrix: EligibilityMatrix) -> Dict[Tuple[str, int], int]:
        return {(s.exam_date.isoformat(), s.shift_id): s.index for s in matrix.slots}

    def _pairs_of(self, aid: int, matrix: EligibilityMatrix) -> Set[Tuple[int, int]]:
        key = self._slot_key(matrix)
        return {(d["faculty_id"], key[(d["exam_date"], d["shift_id"])])
                for d in self.repo.duties(aid) if (d["exam_date"], d["shift_id"]) in key}

    # ------------------------------------------------------------- generate
    def generate(self, sid: int, *, seed: Optional[int] = None, avoid_previous: bool = False,
                 attempts: int = 3) -> Outcome:
        """
        Build a new DRAFT version.  With avoid_previous the engine looks for a
        genuinely different allocation of comparable quality; if none exists it says
        so instead of returning an inferior one.
        """

        session = self.repo.session(sid)
        if session is None:
            raise ExamError(f"unknown examination session {sid}")
        prev_alloc = self.repo.latest_allocation(sid)
        base_seed = seed if seed is not None else secrets.randbelow(2 ** 31 - 10000)
        notes: List[str] = []

        for retry in range(4):
            fp = self._fingerprint(sid)
            prev_pairs: Set[Tuple[int, int]] = set()
            problem, matrix = self.build_problem(sid, seed=base_seed)
            if avoid_previous and prev_alloc:
                prev_pairs = self._pairs_of(prev_alloc["id"], matrix)
                problem.previous = prev_pairs
            best: Optional[SolveResult] = None
            prev_quality = None
            if prev_alloc and prev_pairs:
                targets = targets_for(problem, self.settings)
                pa: Dict[int, List[int]] = defaultdict(list)
                for f, s in prev_pairs:
                    pa[s].append(f)
                prev_quality = evaluate(problem, pa, targets, self.settings)["score"]
            different = None
            for k in range(attempts if avoid_previous and prev_pairs else 1):
                problem.seed = base_seed + k * 104729
                res = solve(problem, self.settings)
                if res.status == "infeasible" and not res.assignments:
                    best = res
                    break
                new_pairs = {(f, s) for s, fs in res.assignments.items() for f in fs}
                if not avoid_previous or not prev_pairs:
                    best = res
                    break
                tol = prev_quality * (1 + self.settings.variation_quality_tolerance) \
                    if prev_quality is not None else None
                same = new_pairs == prev_pairs
                worse = tol is not None and res.quality["score"] > max(tol, prev_quality + 1)
                if not same and not worse:
                    best, different = res, True
                    break
                if best is None or (not same and res.quality["score"] < best.quality["score"]):
                    best = res
                different = False
                reason = ("identical to the previous allocation" if same else
                          "less fair than the previous allocation")
                notes.append(f"attempt {k + 1}: result was {reason}")
            if avoid_previous and prev_pairs and different is False:
                # no meaningfully different, equally fair allocation
                return Outcome(None, "unchanged", best,
                               ["No different allocation of comparable fairness exists under the current "
                                "constraints (different attempts were identical to, or less fair than, the "
                                "current one). Relax a constraint (e.g. maximum duties, exclusions, "
                                "consecutive-duty rule) if you want more variety."] + notes, problem=problem)
            assert best is not None
            try:
                aid = self._persist(sid, best, problem, matrix, fp, parent=prev_alloc["id"] if prev_alloc else None,
                                    params={"seed": best.seed, "avoid_previous": avoid_previous})
            except ConcurrentModification:
                continue
            return Outcome(aid, best.status, best, notes, problem=problem)
        raise ConcurrentModification("the data kept changing while the allocation was being prepared; please retry")

    # ------------------------------------------------------------- persistence
    def _duty_rows(self, result: SolveResult, problem: Problem) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        by_index = {s.index: s for s in problem.slots}
        duties = []
        for sidx, seats in result.seats.items():
            sl = by_index[sidx]
            for room, seat, fid in seats:
                duties.append({"faculty_id": fid, "exam_date": sl.exam_date.isoformat(),
                               "shift_id": sl.shift_id, "room": room, "seat": seat})
        unfilled = [{k: v for k, v in u.items() if k != "slot"} for u in result.unfilled]
        return duties, unfilled

    def _persist(self, sid: int, result: SolveResult, problem: Problem, matrix: EligibilityMatrix,
                 fingerprint: Tuple[Any, ...], parent: Optional[int], params: Dict[str, Any],
                 notes: str = "") -> int:
        duties, unfilled = self._duty_rows(result, problem)
        errs = self.validate_rows(sid, duties, matrix)
        if errs:                      # never store something that breaks a hard rule
            raise ExamError("internal validation failed: " + "; ".join(errs[:5]))
        with transaction(self.conn):
            if self._fingerprint(sid) != fingerprint:
                raise ConcurrentModification("state changed")
            # only the newest draft stays open; older drafts are closed, approved ones untouched
            for r in self.conn.execute("SELECT id FROM exm_allocation WHERE session_id=? AND status='draft'",
                                       (sid,)).fetchall():
                self.repo.set_allocation_status(r["id"], "superseded")
            aid = self.repo.insert_allocation(
                sid, feasibility=result.status if result.status != "unchanged" else "complete",
                parent_id=parent, seed=result.seed, params=params,
                quality={k: v for k, v in result.quality.items()}, notes=notes,
                duties=duties, unfilled=unfilled, actor=self.actor)
        return aid

    # ------------------------------------------------------------ validation
    def validate_rows(self, sid: int, duties: Sequence[Dict[str, Any]],
                      matrix: Optional[EligibilityMatrix] = None) -> List[str]:
        """Independent hard-constraint check of a set of duty rows (no solver code)."""

        session = self.repo.session(sid)
        matrix = matrix or self.eligibility.evaluate(sid)
        slot_of = {(s.exam_date.isoformat(), s.shift_id): s for s in matrix.slots}
        by_name = {f["id"]: f["name"] for f in matrix.faculty}
        errs: List[str] = []
        seen: Set[Tuple[int, str, int]] = set()
        seats: Set[Tuple[str, int, str, int]] = set()
        per_fac: Dict[int, int] = defaultdict(int)
        per_day: Dict[Tuple[int, str], int] = defaultdict(int)
        by_fac_slots: Dict[int, List[SlotInfo]] = defaultdict(list)
        per_slot: Dict[int, int] = defaultdict(int)
        for d in duties:
            sl = slot_of.get((d["exam_date"], d["shift_id"]))
            fid = d["faculty_id"]
            if sl is None:
                errs.append(f"duty outside the examination slots: {d['exam_date']}")
                continue
            if (fid, d["exam_date"], d["shift_id"]) in seen:
                errs.append(f"{by_name.get(fid)} is assigned twice in {d['exam_date']} {sl.shift_label}")
            seen.add((fid, d["exam_date"], d["shift_id"]))
            key = (d["exam_date"], d["shift_id"], d["room"], d["seat"])
            if key in seats:
                errs.append(f"seat {d['room']}#{d['seat']} on {d['exam_date']} filled twice")
            seats.add(key)
            if d["room"] not in sl.rooms or not (1 <= d["seat"] <= sl.per_room):
                errs.append(f"invalid room/seat {d['room']}#{d['seat']} on {d['exam_date']}")
            if not matrix.ok(fid, sl.index):
                why = matrix.reasons(fid, sl.index)[0].text
                errs.append(f"{by_name.get(fid)} is not eligible on {d['exam_date']} {sl.shift_label}: {why}")
            per_fac[fid] += 1
            per_day[(fid, d["exam_date"])] += 1
            by_fac_slots[fid].append(sl)
            per_slot[sl.index] += 1
        for fid, n in per_fac.items():
            if session["max_duties"] is not None and n > session["max_duties"]:
                errs.append(f"{by_name.get(fid)} has {n} duties, above the maximum {session['max_duties']}")
            slots = by_fac_slots[fid]
            for i, a in enumerate(slots):
                for b in slots[i + 1:]:
                    if a.exam_date == b.exam_date and clock_overlap(a.start, a.end, b.start, b.end):
                        errs.append(f"{by_name.get(fid)} is on duty in two overlapping slots on {a.exam_date}")
            if session["options"].get("prohibit_consecutive"):
                pos = sorted({s.day_pos for s in slots})
                if any(b - a == 1 for a, b in zip(pos, pos[1:])):
                    errs.append(f"{by_name.get(fid)} has consecutive-day duties (prohibited)")
        cap = session["options"].get("max_per_day") or self.settings.max_shifts_per_day
        if cap:
            for (fid, dt), n in per_day.items():
                if n > cap:
                    errs.append(f"{by_name.get(fid)} has {n} duties on {dt} (limit {cap})")
        for sl in matrix.slots:
            if per_slot.get(sl.index, 0) > sl.required:
                errs.append(f"slot {sl.exam_date} {sl.shift_label} has more invigilators than seats")
        return errs

    def validate_allocation(self, aid: int) -> List[str]:
        a = self.repo.allocation(aid)
        if a is None:
            raise ExamError(f"unknown allocation {aid}")
        return self.validate_rows(a["session_id"], self.repo.duties(aid))

    # --------------------------------------------------------------- approve
    def approve(self, aid: int, allow_partial: bool = False) -> Dict[str, Any]:
        with transaction(self.conn):
            a = self.repo.allocation(aid)
            if a is None:
                raise ExamError(f"unknown allocation {aid}")
            if a["status"] == "approved":
                return {"approved": True, "already": True, "allocation": a, "problems": []}
            if a["status"] != "draft":
                raise ExamError(f"allocation version {a['version']} is {a['status']} and cannot be approved")
            problems = []
            if a["feasibility"] != "complete" and not allow_partial:
                problems.append(f"The allocation is {a['feasibility']}: {len(self.repo.unfilled(aid))} seat(s) "
                                f"are unfilled. Approve it anyway only with explicit confirmation.")
            errs = self.validate_allocation(aid)
            problems += errs
            if problems:
                return {"approved": False, "allocation": a, "problems": problems}
            for r in self.conn.execute("SELECT id FROM exm_allocation WHERE session_id=? AND status='approved' "
                                       "AND id<>?", (a["session_id"], aid)).fetchall():
                self.repo.set_allocation_status(r["id"], "superseded")
            self.repo.set_allocation_status(aid, "approved")
            self.conn.execute("UPDATE exm_session SET status='approved', updated_at=?, revision=revision+1 "
                              "WHERE id=?", (now(), a["session_id"]))
            self.repo.audit("approve", "allocation", aid, {"version": a["version"],
                                                           "partial": a["feasibility"] != "complete"}, self.actor)
        return {"approved": True, "allocation": self.repo.allocation(aid), "problems": []}

    def cancel_draft(self, aid: int) -> None:
        with transaction(self.conn):
            a = self.repo.allocation(aid)
            if a is None or a["status"] != "draft":
                raise ExamError("only a draft allocation can be discarded")
            self.repo.set_allocation_status(aid, "cancelled")
            self.repo.audit("discard", "allocation", aid, {"version": a["version"]}, self.actor)

    def complete_session(self, sid: int) -> int:
        """After the exams: approved duties become HISTORY (counted in future fairness)."""

        with transaction(self.conn):
            a = self.conn.execute("SELECT id FROM exm_allocation WHERE session_id=? AND status='approved'",
                                  (sid,)).fetchone()
            if a is None:
                raise ExamError("there is no approved allocation to complete")
            n = self.conn.execute("SELECT COUNT(*) c FROM exm_duty WHERE allocation_id=?", (a["id"],)).fetchone()["c"]
            self.repo.set_allocation_status(a["id"], "completed")
            self.conn.execute("UPDATE exm_session SET status='completed', updated_at=? WHERE id=?", (now(), sid))
            self.repo.audit("complete_session", "session", sid, {"duties": n}, self.actor)
        return n

    # ----------------------------------------------------------- reallocation
    def replace_duty(self, aid: int, faculty_id: int, on: date, *, shift_label: Optional[str] = None,
                     reason: str = "", allow_wider: bool = False, seed: Optional[int] = None) -> Outcome:
        """
        Remove one person from a date (optionally one shift) and fill ONLY the vacated seats.

          1. record the restriction (so every later run respects it)
          2. keep every other duty fixed; find the best eligible person for each vacated seat
             with the same fairness terms as the main engine
          3. only if that fails AND allow_wider: re-optimise that single date (other dates stay)
          4. result is a NEW draft version; the approved one stays in force until approved
        """

        base = self.repo.allocation(aid)
        if base is None:
            raise ExamError(f"unknown allocation {aid}")
        sid = base["session_id"]
        base_duties = self.repo.duties(aid)
        mine = [d for d in base_duties if d["faculty_id"] == faculty_id and d["exam_date"] == on.isoformat()
                and (shift_label is None or d["shift"].lower() == shift_label.lower())]
        if not mine:
            raise ExamError("that person has no duty on that date" +
                            (f" in the {shift_label} shift" if shift_label else ""))
        seed = seed if seed is not None else secrets.randbelow(2 ** 31 - 10000)
        notes: List[str] = []
        # 1. restriction (audited, reusable)
        kind = "shift" if shift_label else "date"
        spec = RestrictionSpec(faculty_id, kind, reason or "Removed from duty", "general", sid, on, None,
                               shift_label)
        res = self.restrictions.add_bulk([spec])
        if res.errors:
            raise ExamError(res.errors[0][1])

        for retry in range(3):
            fp = self._fingerprint(sid)
            problem, matrix = self.build_problem(sid, seed=seed)
            key = self._slot_key(matrix)
            removed_ids = {d["id"] for d in mine}
            keep = [d for d in base_duties if d["id"] not in removed_ids]
            keep_pairs = {(d["faculty_id"], key[(d["exam_date"], d["shift_id"])]) for d in keep}
            affected_slots = {key[(d["exam_date"], d["shift_id"])] for d in mine}
            problem.fixed = set(keep_pairs)
            targets = targets_for(problem, self.settings)
            assign = greedy_construct(problem, self.settings, targets, seed)
            dropped = keep_pairs - {(f, s) for s, fs in assign.items() for f in fs}
            local_ok = all(len(assign[s]) >= self._base_count(base_duties, key, s) for s in affected_slots)
            mode = "local replacement"
            if not local_ok and allow_wider:
                mode = "re-optimised that date"
                dates_free = {matrix.slots[s].exam_date for s in affected_slots}
                free_slots = {s.index for s in matrix.slots if s.exam_date in dates_free}
                problem.fixed = {(f, s) for f, s in keep_pairs if s not in free_slots}
                problem.keep_preferred = {(f, s) for f, s in keep_pairs if s in free_slots}
                problem.seed = seed
                wide = solve(problem, self.settings)
                assign = wide.assignments
                notes.append("No local replacement was possible, so the other duties of that date were "
                             "re-optimised (approved duties on other dates are untouched).")
            elif not local_ok:
                notes.append("No single eligible person could take the vacated seat without changing other "
                             "duties. Say 'allow wider changes' to let me re-optimise that date.")
            # keep original seats for unchanged people; new people take the vacated seats
            seats, unfilled_slots = self._merge_seats(problem, matrix, base_duties, assign, key)
            result = self._result_from(problem, assign, seats, targets, seed)
            result.notes.append(mode)
            try:
                new_id = self._persist(sid, result, problem, matrix, fp, parent=aid,
                                       params={"replaced": {"faculty": faculty_id, "date": on.isoformat(),
                                                            "shift": shift_label, "mode": mode,
                                                            "reason": reason}},
                                       notes=mode)
            except ConcurrentModification:
                continue
            changes = self.diff(aid, new_id)
            self.repo.audit("replace_duty", "allocation", new_id,
                            {"from": aid, "faculty": faculty_id, "date": on.isoformat(),
                             "changes": len(changes), "mode": mode}, self.actor)
            return Outcome(new_id, result.status, result, notes, changes, problem)
        raise ConcurrentModification("data kept changing during the reallocation; please retry")

    @staticmethod
    def _base_count(base_duties, key, slot_index) -> int:
        return sum(1 for d in base_duties if key[(d["exam_date"], d["shift_id"])] == slot_index)

    def _merge_seats(self, problem: Problem, matrix: EligibilityMatrix, base_duties: List[Dict[str, Any]],
                     assign: Dict[int, List[int]], key: Dict[Tuple[str, int], int]):
        base_seat: Dict[int, Dict[int, Tuple[str, int]]] = defaultdict(dict)
        for d in base_duties:
            base_seat[key[(d["exam_date"], d["shift_id"])]][d["faculty_id"]] = (d["room"], d["seat"])
        dept = {f.id: f.department for f in problem.faculty}
        rng = __import__("random").Random(problem.seed)
        seats: Dict[int, List[Tuple[str, int, int]]] = {}
        unfilled_slots = []
        for sl in problem.slots:
            now_set = list(assign.get(sl.index, []))
            old = base_seat.get(sl.index, {})
            if not old:
                seats[sl.index] = assign_rooms(sl, now_set, dept, rng)
                continue
            out: List[Tuple[str, int, int]] = []
            used = set()
            for fid in now_set:
                if fid in old:
                    out.append((old[fid][0], old[fid][1], fid))
                    used.add(old[fid])
            freed = [p for fid, p in sorted(old.items(), key=lambda kv: kv[1]) if p not in used]
            newcomers = [f for f in now_set if f not in old]
            for fid, (room, seat) in zip(newcomers, freed):
                out.append((room, seat, fid))
            leftovers = newcomers[len(freed):]
            if leftovers:       # more people than freed seats (wide mode): any empty seat
                taken = {(r, s) for r, s, _ in out}
                empty = [(r, s) for r in sl.rooms for s in range(1, sl.per_room + 1) if (r, s) not in taken]
                for fid, (room, seat) in zip(leftovers, empty):
                    out.append((room, seat, fid))
            seats[sl.index] = out
        return seats, unfilled_slots

    def _result_from(self, problem: Problem, assign: Dict[int, List[int]],
                     seats: Dict[int, List[Tuple[str, int, int]]], targets: Dict[int, int],
                     seed: int) -> SolveResult:
        from .optimizer import _unfilled_records
        from .fairness import fairness_metrics
        assign = {s: [f for _, _, f in seats.get(s, [])] for s in assign}
        unfilled = _unfilled_records(problem, assign, seats)
        counts: Dict[int, int] = defaultdict(int)
        for fs in assign.values():
            for f in fs:
                counts[f] += 1
        q = evaluate(problem, assign, targets, self.settings)
        q["fairness"] = fairness_metrics(problem.faculty, problem.eligible, counts, targets,
                                         {s.index: s.day_pos for s in problem.slots}, assign,
                                         self.settings.target_scale)
        q["targets"] = {str(k): v / self.settings.target_scale for k, v in targets.items()}
        status = "complete" if not unfilled else ("partial" if any(assign.values()) else "infeasible")
        return SolveResult(status, assign, seats, unfilled, q, seed, "REPLACEMENT", [])

    def diff(self, old_id: int, new_id: int) -> List[Dict[str, Any]]:
        def seatmap(aid):
            return {(d["exam_date"], d["shift"], d["room"], d["seat"]): d for d in self.repo.duties(aid)}
        a, b = seatmap(old_id), seatmap(new_id)
        out = []
        for k in sorted(set(a) | set(b)):
            x, y = a.get(k), b.get(k)
            if (x or {}).get("faculty_id") != (y or {}).get("faculty_id"):
                out.append({"date": k[0], "shift": k[1], "room": k[2], "seat": k[3],
                            "old": x["faculty"] if x else None, "new": y["faculty"] if y else None,
                            "new_department": y["department"] if y else None})
        return out

    # ---------------------------------------------------------------- misc
    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass