"""
Allocation policy as DATA: department exclusion and priority tiers.

* Department names are never written here.  A typed phrase (a short name, initials or a
  common word from the label) is resolved against the departments that exist in the database: the full
  label, its significant words, its initials, plus any administrator aliases stored
  under the setting `department_aliases` ({"alias": "Department label"}).  When
  more than one department fits, the caller must ask - nothing is guessed.
* A department exclusion is its own record (exm_dept_restriction).  It is applied
  to people through their department at eligibility time, so restoring a
  department never touches a person's own restriction and vice versa.
* Priority is a TIER (1 = first choice).  Rules match a department, a designation
  or one person; session rules override global ones.  Tier 1 is the default.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .db import transaction
from .repository import Repository, now

_FILLER = {"of", "and", "the", "dept", "department", "departments", "engg", "engineering", "branch", "school", "faculty"}


def _toks(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").lower())


def _sig(text: str) -> List[str]:
    return [t for t in _toks(text) if t not in _FILLER]


# ------------------------------------------------------------------ resolution
@dataclass
class Resolution:
    label: Optional[str]                  # the one department, when unambiguous
    candidates: List[str] = field(default_factory=list)
    how: str = ""

    @property
    def ok(self) -> bool:
        return self.label is not None


class DepartmentResolver:
    def __init__(self, departments: Sequence[str], aliases: Optional[Dict[str, str]] = None):
        self.departments = sorted({d for d in departments if d})
        self.alias_map: Dict[str, set] = {}
        sigs = {d: _sig(d) for d in self.departments}
        token_owner: Dict[str, set] = {}
        for d, s in sigs.items():
            for t in s:
                token_owner.setdefault(t, set()).add(d)
        for d in self.departments:
            s = sigs[d]
            forms = {" ".join(_toks(d)), " ".join(s)}
            if len(s) > 1:
                ini = "".join(t[0] for t in s)
                forms.add(ini)
                if "engg" in _toks(d) or "engineering" in _toks(d):
                    forms.add(ini + "e")
            elif s:
                forms.add(s[0])
                if "engg" in _toks(d) or "engineering" in _toks(d):
                    forms.add(s[0][0] + "e")                # e.g. a one-word engineering branch: initial + E
            for t in s:                                     # a single word counts only when it is unique
                if len(token_owner[t]) == 1 and len(t) > 2:
                    forms.add(t)
            for f in forms:
                if f:
                    self.alias_map.setdefault(f, set()).add(d)
        by_norm = {" ".join(_toks(d)): d for d in self.departments}
        for a, target in (aliases or {}).items():
            tgt = by_norm.get(" ".join(_toks(target)))
            if tgt:
                self.alias_map.setdefault(" ".join(_toks(a)), set()).add(tgt)

    def resolve(self, phrase: str) -> Resolution:
        from rapidfuzz import fuzz, process
        q = " ".join(_toks(phrase))
        qs = " ".join(_sig(phrase))
        for key in (q, qs):
            hit = self.alias_map.get(key)
            if hit:
                return Resolution(next(iter(hit)) if len(hit) == 1 else None, sorted(hit),
                                  "alias" if len(hit) == 1 else "ambiguous alias")
        if not q:
            return Resolution(None)
        if len(qs) >= 3 and " " not in qs:                    # unique word-prefix ("mech" -> the one word it starts)
            pre = {d for d in self.departments for t in _sig(d) if t.startswith(qs)}
            if len(pre) == 1:
                return Resolution(next(iter(pre)), sorted(pre), "prefix")
        scored = process.extract(q, list(self.alias_map), scorer=fuzz.WRatio, limit=6, score_cutoff=85)
        found: Dict[str, float] = {}
        for alias, score, _ in scored:
            for d in self.alias_map[alias]:
                found[d] = max(found.get(d, 0), score)
        if not found:
            return Resolution(None)
        ranked = sorted(found.items(), key=lambda kv: -kv[1])
        if len(ranked) == 1 or ranked[0][1] - ranked[1][1] >= 8:
            return Resolution(ranked[0][0], [ranked[0][0]], "fuzzy")
        return Resolution(None, [d for d, _ in ranked], "ambiguous")


class PolicyError(ValueError):
    pass


# ---------------------------------------------------------------------- service
class PolicyService:

    def __init__(self, repo: Repository, actor: str = "coordinator"):
        self.repo = repo
        self.conn = repo.conn
        self.actor = actor

    # ---- departments -------------------------------------------------------
    def resolver(self) -> DepartmentResolver:
        depts = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT department FROM exm_faculty WHERE department<>''")]
        row = self.conn.execute("SELECT value FROM exm_settings WHERE key='department_aliases'").fetchone()
        aliases = json.loads(row["value"]) if row else {}
        return DepartmentResolver(depts, aliases)

    def resolve_department(self, phrase: str) -> str:
        r = self.resolver().resolve(phrase)
        if r.ok:
            return r.label  # type: ignore[return-value]
        if r.candidates:
            raise PolicyError(f"'{phrase}' could mean: " + "; ".join(r.candidates) + " - which one?")
        raise PolicyError(f"No department matches '{phrase}'. Known: " + "; ".join(self.resolver().departments))

    # ---- department exclusion ----------------------------------------------
    def exclude_department(self, phrase_or_label: str, *, kind: str = "session", session_id: Optional[int] = None,
                           start: Optional[date] = None, end: Optional[date] = None,
                           shift_label: Optional[str] = None, reason: str = "") -> Dict[str, Any]:
        if kind not in ("permanent", "session", "date", "date_range", "shift"):
            raise PolicyError(f"unknown exclusion kind '{kind}'")
        if kind == "session" and session_id is None:
            raise PolicyError("a whole-session exclusion needs the examination session")
        if kind in ("date", "date_range", "shift") and start is None:
            raise PolicyError("a dated exclusion needs a start date")
        if start and end and end < start:
            raise PolicyError("the end date is before the start date")
        dept = self.resolve_department(phrase_or_label)
        with transaction(self.conn):
            dup = self.conn.execute(
                "SELECT * FROM exm_dept_restriction WHERE status='active' AND department=? AND kind=? "
                "AND COALESCE(session_id,0)=COALESCE(?,0) AND COALESCE(start_date,'')=COALESCE(?,'') "
                "AND COALESCE(end_date,'')=COALESCE(?,'') AND COALESCE(shift_label,'')=COALESCE(?,'')",
                (dept, kind, session_id, start.isoformat() if start else None,
                 end.isoformat() if end else None, shift_label)).fetchone()
            if dup:
                return {**dict(dup), "already_active": True}
            cur = self.conn.execute(
                "INSERT INTO exm_dept_restriction(department,kind,session_id,start_date,end_date,shift_label,reason,"
                "created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (dept, kind, session_id, start.isoformat() if start else None, end.isoformat() if end else None,
                 shift_label, reason or "", self.actor, now()))
            rid = cur.lastrowid
            self.repo.audit("exclude_department", "dept_restriction", rid,
                            {"department": dept, "kind": kind, "start": str(start), "end": str(end),
                             "shift": shift_label, "reason": reason}, actor=self.actor)
        return dict(self.conn.execute("SELECT * FROM exm_dept_restriction WHERE id=?", (rid,)).fetchone())

    def restore_department(self, phrase_or_label: str, *, session_id: Optional[int] = None,
                           start: Optional[date] = None, end: Optional[date] = None) -> Dict[str, Any]:
        """Lift this department's exclusions (all, or only inside a date window).  Nothing else changes."""

        dept = self.resolve_department(phrase_or_label)
        rows = [dict(r) for r in self.conn.execute(
            "SELECT * FROM exm_dept_restriction WHERE status='active' AND department=? "
            "AND (session_id IS NULL OR session_id=?)", (dept, session_id))]
        restored, kept = [], []
        with transaction(self.conn):
            for r in rows:
                a = date.fromisoformat(r["start_date"]) if r["start_date"] else None
                b = date.fromisoformat(r["end_date"]) if r["end_date"] else (a if r["kind"] in ("date", "shift") else None)
                windowed = start is not None or end is not None
                if not windowed or r["kind"] in ("permanent", "session") and not (a or b):
                    self._mark(r["id"], "restored")
                    restored.append(r["id"])
                    continue
                if a is None:                                  # open-ended rule + window: keep, cannot split safely
                    kept.append(r["id"])
                    continue
                lo, hi = start or date.min, end or date.max
                if hi < a or (b is not None and lo > b):
                    continue                                   # window does not touch this rule
                self._mark(r["id"], "superseded")
                restored.append(r["id"])
                if a < lo:                                     # part before the window stays excluded
                    self._clone(r, a, lo - timedelta(days=1))
                if b is not None and hi < b:                   # part after the window stays excluded
                    self._clone(r, hi + timedelta(days=1), b)
            self.repo.audit("restore_department", "dept_restriction", None,
                            {"department": dept, "restored": restored, "kept": kept,
                             "window": [str(start), str(end)]}, actor=self.actor)
        return {"department": dept, "restored": restored, "unchanged_open_ended": kept}

    def _mark(self, rid: int, status: str) -> None:
        self.conn.execute("UPDATE exm_dept_restriction SET status=?, restored_at=? WHERE id=?", (status, now(), rid))

    def _clone(self, r: Dict[str, Any], a: date, b: date) -> None:
        kind = "date" if a == b else "date_range"
        self.conn.execute(
            "INSERT INTO exm_dept_restriction(department,kind,session_id,start_date,end_date,shift_label,reason,"
            "created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (r["department"], kind, r["session_id"], a.isoformat(), b.isoformat(), r["shift_label"],
             r["reason"], self.actor, now()))

    def department_restrictions(self, session_id: Optional[int] = None, active_only: bool = True) -> List[Dict[str, Any]]:
        q = "SELECT * FROM exm_dept_restriction WHERE (session_id IS NULL OR session_id=?)"
        if active_only:
            q += " AND status='active'"
        return [dict(r) for r in self.conn.execute(q + " ORDER BY department, id", (session_id,))]

    # ---- priority tiers ----------------------------------------------------
    def set_priority(self, scope: str, pattern: str, tier: int, *, session_id: Optional[int] = None,
                     label: str = "") -> Dict[str, Any]:
        if scope not in ("department", "designation", "faculty"):
            raise PolicyError("priority scope must be department, designation or faculty")
        if int(tier) < 1:
            raise PolicyError("priority tier must be 1 or more (1 = first choice)")
        if scope == "department":
            pattern = self.resolve_department(pattern)
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE exm_priority_rule SET status='removed' WHERE status='active' AND scope=? "
                "AND lower(pattern)=lower(?) AND COALESCE(session_id,0)=COALESCE(?,0)", (scope, pattern, session_id))
            cur = self.conn.execute(
                "INSERT INTO exm_priority_rule(scope,pattern,tier,session_id,label,created_by,created_at) "
                "VALUES (?,?,?,?,?,?,?)", (scope, pattern, int(tier), session_id, label, self.actor, now()))
            self.repo.audit("set_priority", "priority_rule", cur.lastrowid,
                            {"scope": scope, "pattern": pattern, "tier": int(tier), "session": session_id}, actor=self.actor)
        return dict(self.conn.execute("SELECT * FROM exm_priority_rule WHERE id=?", (cur.lastrowid,)).fetchone())

    def remove_priority(self, scope: str, pattern: str, session_id: Optional[int] = None) -> int:
        if scope == "department":
            pattern = self.resolve_department(pattern)
        with transaction(self.conn):
            n = self.conn.execute(
                "UPDATE exm_priority_rule SET status='removed' WHERE status='active' AND scope=? "
                "AND lower(pattern)=lower(?) AND COALESCE(session_id,0)=COALESCE(?,0)",
                (scope, pattern, session_id)).rowcount
            self.repo.audit("remove_priority", "priority_rule", None, {"scope": scope, "pattern": pattern}, actor=self.actor)
        return n

    def priority_rules(self, session_id: Optional[int] = None) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM exm_priority_rule WHERE status='active' AND (session_id IS NULL OR session_id=?) "
            "ORDER BY tier DESC, id", (session_id,))]

    @staticmethod
    def tier_of(faculty: Dict[str, Any], rules: Sequence[Dict[str, Any]]) -> Tuple[int, str]:
        """(tier, reason) for one person.  Faculty rule > (department / designation: the lower priority wins);
        a session-specific rule overrides global rules of the same scope."""

        def matches(r: Dict[str, Any]) -> bool:
            p = str(r["pattern"]).lower()
            if r["scope"] == "faculty":
                return p == str(faculty["id"])
            if r["scope"] == "department":
                return (faculty.get("department") or "").lower() == p
            return p in (faculty.get("designation") or "").lower()

        hits = [r for r in rules if matches(r)]
        if not hits:
            return 1, "default"
        for scope in ("faculty",):
            mine = [r for r in hits if r["scope"] == scope]
            if mine:
                r = sorted(mine, key=lambda x: (x["session_id"] is None, -x["id"]))[0]
                return r["tier"], r["label"] or f"{scope} rule"
        sess = [r for r in hits if r["session_id"] is not None]
        use = sess or hits
        r = max(use, key=lambda x: (x["tier"], x["id"]))
        return r["tier"], r["label"] or f"{r['scope']} '{r['pattern']}'"