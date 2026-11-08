"""
Faculty exclusion / availability management.

A *restriction* says "this person may not be given duty ..." for a scope:

    permanent    always (until restored)
    session      the whole examination session
    date         one calendar date                (all shifts, or one shift)
    date_range   first..last date (either end may be open)
    shift        one shift on a date / date range

Restrictions are DATA (table exm_restriction).  Several may coexist for one
person.  Restoring removes ONLY what was asked: a restriction that is only partly
covered by a restore window is split, so the uncovered part stays in force.
Every change is audited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .db import transaction
from .repository import Repository, now

MIN_DATE, MAX_DATE = date.min, date.max
KINDS = ("permanent", "session", "date", "date_range", "shift")
CATEGORIES = ("administrative", "leave", "general")


class RestrictionError(ValueError):
    pass


@dataclass
class RestrictionSpec:
    faculty_id: int
    kind: str
    reason: str = ""
    category: str = "general"
    session_id: Optional[int] = None
    start: Optional[date] = None
    end: Optional[date] = None
    shift_label: Optional[str] = None


@dataclass
class BulkResult:
    created: List[Dict[str, Any]] = field(default_factory=list)
    existing: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[Tuple[RestrictionSpec, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


def _parse(s: Optional[str]) -> Optional[date]:
    return date.fromisoformat(s) if s else None


def interval_of(r: Dict[str, Any], session: Optional[Dict[str, Any]] = None) -> Tuple[date, date]:
    """Closed date interval a restriction covers (open ends become min/max)."""

    s, e = _parse(r.get("start_date")), _parse(r.get("end_date"))
    if r["kind"] in ("date", "shift") and s is not None and e is None:
        e = s                      # a single date (a shift restriction may give a range)
    if r["kind"] == "session" and session is not None and s is None and e is None:
        return date.fromisoformat(session["start_date"]), date.fromisoformat(session["end_date"])
    return s or MIN_DATE, e or MAX_DATE


def applies(r: Dict[str, Any], session: Dict[str, Any], on: date, shift_label: Optional[str]) -> bool:
    """Does restriction `r` forbid duty in `session` on `on` during `shift_label`?"""

    if r["status"] != "active":
        return False
    if r.get("session_id") not in (None, session["id"]):
        return False
    if r["kind"] == "session" and r.get("session_id") != session["id"]:
        return False
    a, b = interval_of(r, session)
    if not (a <= on <= b):
        return False
    sl = r.get("shift_label")
    if sl and shift_label and sl.strip().lower() != shift_label.strip().lower():
        return False
    return True


def describe_scope(r: Dict[str, Any], session: Optional[Dict[str, Any]] = None) -> str:
    kind = r["kind"]
    a, b = _parse(r.get("start_date")), _parse(r.get("end_date"))
    sh = f" ({r['shift_label']} shift)" if r.get("shift_label") else ""
    if kind in ("date", "shift") and a and not b:
        b = a
    if kind == "permanent":
        return "permanent"
    if kind == "session":
        return "entire examination session"
    if a and b and a == b:
        return f"{a:%d %b %Y}{sh}"
    if a and b:
        return f"{a:%d %b %Y} to {b:%d %b %Y}{sh}"
    if a:
        return f"from {a:%d %b %Y}{sh}"
    if b:
        return f"until {b:%d %b %Y}{sh}"
    return "whole period" + sh


class RestrictionService:

    def __init__(self, repo: Repository, actor: str = "coordinator"):
        self.repo = repo
        self.actor = actor

    # ------------------------------------------------------------ queries
    def active(self, faculty_id: Optional[int] = None, session_id: Optional[int] = None,
               include_inactive: bool = False) -> List[Dict[str, Any]]:
        q = ("SELECT r.*, f.name AS faculty, f.department FROM exm_restriction r "
             "JOIN exm_faculty f ON f.id=r.faculty_id WHERE 1=1")
        args: List[Any] = []
        if not include_inactive:
            q += " AND r.status='active'"
        if faculty_id is not None:
            q += " AND r.faculty_id=?"
            args.append(faculty_id)
        if session_id is not None:
            q += " AND (r.session_id IS NULL OR r.session_id=?)"
            args.append(session_id)
        q += " ORDER BY f.name, r.id"
        return [dict(x) for x in self.repo.conn.execute(q, args)]

    # --------------------------------------------------------- validation
    def _validate(self, spec: RestrictionSpec) -> Optional[Dict[str, Any]]:
        fac = self.repo.faculty(spec.faculty_id)
        if fac is None:
            raise RestrictionError(f"faculty id {spec.faculty_id} does not exist")
        if spec.kind not in KINDS:
            raise RestrictionError(f"unknown restriction type '{spec.kind}'")
        if spec.category not in CATEGORIES:
            raise RestrictionError(f"unknown category '{spec.category}'")
        session = self.repo.session(spec.session_id) if spec.session_id else None
        if spec.session_id and session is None:
            raise RestrictionError(f"examination session {spec.session_id} does not exist")
        if spec.kind == "session" and session is None:
            raise RestrictionError("a session exclusion needs an examination session")
        if spec.kind == "permanent" and (spec.start or spec.end or spec.shift_label):
            raise RestrictionError("a permanent exclusion cannot carry dates or a shift")
        if spec.kind == "date" and (spec.start is None or (spec.end and spec.end != spec.start)):
            raise RestrictionError("a date restriction needs exactly one date")
        if spec.kind == "date_range":
            if spec.start is None and spec.end is None:
                raise RestrictionError("a date range needs a start and/or an end date")
            if spec.start and spec.end and spec.end < spec.start:
                raise RestrictionError(
                    f"the end date {spec.end:%d %b} is before the start date {spec.start:%d %b}")
        if spec.kind == "shift":
            if not spec.shift_label:
                raise RestrictionError("a shift restriction needs a shift")
            if spec.start is None:
                raise RestrictionError("a shift restriction needs a date")
            if spec.end and spec.end < spec.start:
                raise RestrictionError("the end date is before the start date")
        if session is not None and spec.kind in ("date", "date_range", "shift"):
            lo, hi = date.fromisoformat(session["start_date"]), date.fromisoformat(session["end_date"])
            for d in (spec.start, spec.end):
                if d is not None and not (lo <= d <= hi):
                    raise RestrictionError(
                        f"{d:%d %b %Y} is outside the examination period "
                        f"({lo:%d %b %Y} to {hi:%d %b %Y})")
            if spec.shift_label:
                labels = [s["label"].lower() for s in self.repo.session_shifts(session["id"])]
                if spec.shift_label.strip().lower() not in labels:
                    raise RestrictionError(
                        f"'{spec.shift_label}' is not a shift of this session "
                        f"(shifts: {', '.join(s['label'] for s in self.repo.session_shifts(session['id']))})")
        return session

    def _find_duplicate(self, spec: RestrictionSpec) -> Optional[Dict[str, Any]]:
        row = self.repo.conn.execute(
            "SELECT * FROM exm_restriction WHERE faculty_id=? AND kind=? AND status='active' "
            "AND COALESCE(session_id,0)=COALESCE(?,0) AND COALESCE(start_date,'')=COALESCE(?,'') "
            "AND COALESCE(end_date,'')=COALESCE(?,'') AND lower(COALESCE(shift_label,''))=lower(COALESCE(?,''))",
            (spec.faculty_id, spec.kind, spec.session_id, _iso(spec.start), _iso(spec.end),
             spec.shift_label)).fetchone()
        return dict(row) if row else None

    def _covered_by(self, spec: RestrictionSpec, session: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """A broader active restriction that already forbids everything `spec` does."""

        a = spec.start or (date.fromisoformat(session["start_date"]) if session and spec.kind == "session" else MIN_DATE)
        b = spec.end or (date.fromisoformat(session["end_date"]) if session and spec.kind == "session" else MAX_DATE)
        if spec.kind == "date":
            b = a
        if spec.kind == "shift" and spec.end is None:
            b = a
        for r in self.active(spec.faculty_id):
            if r["session_id"] not in (None, spec.session_id):
                continue
            if r["kind"] == "session" and r["session_id"] != spec.session_id:
                continue
            same_shift = (not r.get("shift_label")) or \
                (spec.shift_label and r["shift_label"].lower() == spec.shift_label.lower())
            if not same_shift:
                continue
            ra, rb = interval_of(r, session)
            if ra <= a and b <= rb:
                return r
        return None

    # ---------------------------------------------------------------- add
    def add(self, spec: RestrictionSpec, actor: Optional[str] = None) -> Tuple[Dict[str, Any], bool, str]:
        res = self.add_bulk([spec], actor=actor, all_or_nothing=True)
        if res.errors:
            raise RestrictionError(res.errors[0][1])
        if res.created:
            return res.created[0], True, "; ".join(res.notes)
        return res.existing[0], False, "an identical restriction was already active"

    def add_bulk(self, specs: Sequence[RestrictionSpec], actor: Optional[str] = None,
                 all_or_nothing: bool = False) -> BulkResult:
        """
        Validate EVERY spec first, then apply the valid ones in one transaction.
        Each person's restriction is handled on its own - nothing is shared.
        """

        actor = actor or self.actor
        result = BulkResult()
        valid: List[Tuple[RestrictionSpec, Optional[Dict[str, Any]]]] = []
        for spec in specs:
            try:
                session = self._validate(spec)
                valid.append((spec, session))
            except RestrictionError as e:
                result.errors.append((spec, str(e)))
        if all_or_nothing and result.errors:
            return result
        with transaction(self.repo.conn):
            for spec, session in valid:
                dup = self._find_duplicate(spec)
                if dup:
                    result.existing.append(dup)
                    continue
                cover = self._covered_by(spec, session)
                if cover:
                    result.notes.append(
                        f"{self.repo.faculty(spec.faculty_id)['name']}: already covered by restriction "
                        f"#{cover['id']} ({describe_scope(cover, session)})")
                cur = self.repo.conn.execute(
                    "INSERT INTO exm_restriction(faculty_id,kind,category,session_id,start_date,end_date,"
                    "shift_label,reason,status,created_by,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?, 'active',?,?,?)",
                    (spec.faculty_id, spec.kind, spec.category, spec.session_id, _iso(spec.start),
                     _iso(spec.end), spec.shift_label, spec.reason, actor, now(), now()))
                row = dict(self.repo.conn.execute("SELECT * FROM exm_restriction WHERE id=?",
                                                  (cur.lastrowid,)).fetchone())
                result.created.append(row)
                self.repo.audit("exclude", "restriction", cur.lastrowid,
                                {"faculty": spec.faculty_id, "kind": spec.kind, "reason": spec.reason,
                                 "category": spec.category, "start": _iso(spec.start),
                                 "end": _iso(spec.end), "shift": spec.shift_label}, actor)
            if valid:
                self._touch_sessions({s.session_id for s, _ in valid if s.session_id})
        return result

    def _touch_sessions(self, ids) -> None:
        for sid in ids:
            self.repo.conn.execute("UPDATE exm_session SET revision=revision+1, updated_at=? WHERE id=?",
                                   (now(), sid))

    # ------------------------------------------------------------ restore
    def restore(self, faculty_id: int, *, session_id: Optional[int] = None,
                from_date: Optional[date] = None, to_date: Optional[date] = None,
                shift_label: Optional[str] = None, categories: Optional[Sequence[str]] = None,
                kinds: Optional[Sequence[str]] = None, restriction_id: Optional[int] = None,
                actor: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Make a person eligible again - only for what is asked.

        No window  -> every matching active restriction ends (listed in the result).
        A window   -> only that window is lifted; the rest of each restriction stays
                      (split into new rows), including other shifts when a single
                      shift is restored.
        Returns one change record per restriction touched.
        """

        actor = actor or self.actor
        session = self.repo.session(session_id) if session_id else None
        changes: List[Dict[str, Any]] = []
        shifts = ([s["label"] for s in self.repo.session_shifts(session_id)] if session_id else [])
        with transaction(self.repo.conn):
            for r in self.active(faculty_id, session_id):
                if restriction_id is not None and r["id"] != restriction_id:
                    continue
                if categories and r["category"] not in categories:
                    continue
                if kinds and r["kind"] not in kinds:
                    continue
                if shift_label and r.get("shift_label") and \
                        r["shift_label"].lower() != shift_label.lower():
                    continue
                ra, rb = interval_of(r, session)
                wa, wb = from_date or MIN_DATE, to_date or MAX_DATE
                if from_date is None and to_date is None:
                    ch = self._end(r, "restored", actor)
                    changes.append({"restriction": r, "action": "restored", "remaining": []})
                    continue
                if rb < wa or ra > wb:
                    continue                      # window does not touch this restriction
                pieces: List[Dict[str, Any]] = []
                whole_shift = shift_label is None or (r.get("shift_label") and
                                                      r["shift_label"].lower() == shift_label.lower())
                # parts of the restriction outside the window keep their scope
                if ra < wa:
                    pieces.append(self._piece(r, ra, wa - timedelta(days=1), r.get("shift_label")))
                if rb > wb:
                    pieces.append(self._piece(r, wb + timedelta(days=1), rb, r.get("shift_label")))
                # inside the window, a single restored shift leaves the OTHER shifts restricted
                if not whole_shift and shift_label:
                    ia, ib = max(ra, wa), min(rb, wb)
                    for other in shifts:
                        if other.lower() != shift_label.lower():
                            pieces.append(self._piece(r, ia, ib, other))
                self._end(r, "superseded" if pieces else "restored", actor)
                made = []
                for p in pieces:
                    made.append(self._insert_piece(r, p, actor))
                changes.append({"restriction": r, "action": "narrowed" if pieces else "restored",
                                "remaining": made})
            for c in changes:
                self.repo.audit("restore", "restriction", c["restriction"]["id"],
                                {"faculty": faculty_id, "action": c["action"],
                                 "window": [_iso(from_date), _iso(to_date)], "shift": shift_label,
                                 "remaining": [m["id"] for m in c["remaining"]]}, actor)
            if changes and session_id:
                self._touch_sessions({session_id})
        return changes

    def _end(self, r: Dict[str, Any], status: str, actor: str) -> None:
        self.repo.conn.execute(
            "UPDATE exm_restriction SET status=?, restored_at=?, updated_at=? WHERE id=?",
            (status, now(), now(), r["id"]))

    @staticmethod
    def _piece(r: Dict[str, Any], a: date, b: date, shift: Optional[str]) -> Dict[str, Any]:
        return {"a": a, "b": b, "shift": shift}

    def _insert_piece(self, parent: Dict[str, Any], p: Dict[str, Any], actor: str) -> Dict[str, Any]:
        a = None if p["a"] == MIN_DATE else p["a"]
        b = None if p["b"] == MAX_DATE else p["b"]
        if p["shift"]:
            kind = "shift"
        elif a and b and a == b:
            kind = "date"
        else:
            kind = "date_range"
        cur = self.repo.conn.execute(
            "INSERT INTO exm_restriction(faculty_id,kind,category,session_id,start_date,end_date,shift_label,"
            "reason,status,parent_id,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?, 'active',?,?,?,?)",
            (parent["faculty_id"], kind, parent["category"], parent["session_id"], _iso(a), _iso(b),
             p["shift"], parent["reason"], parent["id"], actor, now(), now()))
        return dict(self.repo.conn.execute("SELECT * FROM exm_restriction WHERE id=?",
                                           (cur.lastrowid,)).fetchone())

    def restore_bulk(self, faculty_ids: Sequence[int], **kw: Any) -> Dict[int, List[Dict[str, Any]]]:
        with transaction(self.repo.conn):
            return {fid: self.restore(fid, **kw) for fid in faculty_ids}

    # --------------------------------------------------- administrative
    def apply_admin_policy(self, session_id: int, roles: Optional[Sequence[str]] = None,
                           actor: Optional[str] = None) -> Dict[str, Any]:
        """
        Exclude faculty who hold ACTIVE administrative responsibilities overlapping
        the session.  `roles` (optional) limits it to those roles - nothing assumes
        that HODs/Deans must be excluded.  Returns {created, existing, none_found}.
        """

        actor = actor or self.actor
        session = self.repo.session(session_id)
        lo, hi = date.fromisoformat(session["start_date"]), date.fromisoformat(session["end_date"])
        wanted = {r.strip().lower() for r in roles or []}
        specs: List[RestrictionSpec] = []
        for resp in self.repo.responsibilities():
            if wanted and resp["role"].strip().lower() not in wanted and \
                    not any(w in resp["role"].lower() for w in wanted):
                continue
            a, b = _parse(resp["start_date"]), _parse(resp["end_date"])
            if (b and b < lo) or (a and a > hi):
                continue
            reason = f"Administrative responsibility: {resp['role']}"
            if a or b:
                s, e = max(a or lo, lo), min(b or hi, hi)
                specs.append(RestrictionSpec(resp["faculty_id"], "date_range", reason,
                                             "administrative", session_id, s, e))
            else:
                specs.append(RestrictionSpec(resp["faculty_id"], "session", reason,
                                             "administrative", session_id))
        res = self.add_bulk(specs, actor)
        opts = session["options"]
        opts["admin_exclusion"] = {"mode": "roles" if wanted else "all", "roles": sorted(wanted)}
        self.repo.update_session(session_id, options=opts)
        return {"created": res.created, "existing": res.existing, "none_found": not specs,
                "errors": res.errors}