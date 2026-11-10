"""All SQL lives here.  Methods take/return plain dicts and primitives."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .db import transaction


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_name(name: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(name or "").lower()))


def _d(row) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


class Repository:

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------ audit
    def audit(self, action: str, entity: str, entity_id: Optional[int],
              details: Optional[Dict[str, Any]] = None, actor: str = "coordinator") -> None:
        self.conn.execute(
            "INSERT INTO exm_audit(ts, actor, action, entity, entity_id, details) "
            "VALUES (?,?,?,?,?,?)",
            (now(), actor, action, entity, entity_id,
             json.dumps(details or {}, default=str, ensure_ascii=False)))

    def audit_log(self, entity: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if entity:
            rows = self.conn.execute(
                "SELECT * FROM exm_audit WHERE entity=? ORDER BY id DESC LIMIT ?", (entity, limit))
        else:
            rows = self.conn.execute("SELECT * FROM exm_audit ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ---------------------------------------------------------- faculty
    def sync_faculty(self, records: Iterable[Tuple[str, str]], source: str = "roster",
                     deactivate_missing: bool = False) -> Dict[str, int]:
        """Upsert the roster.  Never deletes; optionally deactivates absent people."""

        added = updated = 0
        seen = set()
        # Once an authoritative (imported) roster exists, a timetable spelling of someone already on it is the SAME
        # person (alias), and a name found nowhere on it is parked as "needs verification" - never double-counted.
        authoritative = self.conn.execute("SELECT 1 FROM exm_faculty WHERE employee_code IS NOT NULL LIMIT 1").fetchone() is not None
        aliases: Dict[str, int] = {}
        titles: List[str] = []
        if authoritative:
            from .importer import load_columns, strip_titles
            titles = load_columns().get("titles", [])
            aliases = {r["alias_norm"]: r["faculty_id"] for r in self.conn.execute("SELECT * FROM exm_faculty_alias")}
        with transaction(self.conn):
            for name, dept in records:
                name = " ".join(str(name).split())
                if not name:
                    continue
                seen.add(name)
                row = self.conn.execute("SELECT id, department, active, record_status FROM exm_faculty WHERE name=?",
                                        (name,)).fetchone()
                if row is not None and row["record_status"] in ("needs_verification", "merged"):
                    continue                                # parked / merged by the administrator: the sync never revives it
                if row is None and authoritative:
                    fid = aliases.get(strip_titles(name, titles))
                    if fid is not None:
                        continue                            # already on the roster under another spelling
                if row is not None:
                    ov = self.conn.execute("SELECT department FROM exm_dept_override WHERE faculty_id=?",
                                           (row["id"],)).fetchone()
                    if ov:
                        dept = ov["department"]          # the coordinator's label wins
                if row is None:
                    self.conn.execute(
                        "INSERT INTO exm_faculty(name,norm,department,active,source,updated_at,record_status) "
                        "VALUES (?,?,?,?,?,?,?)", (name, norm_name(name), dept or "", 0 if authoritative else 1, source,
                                                   now(), "needs_verification" if authoritative else "verified"))
                    added += 1
                elif (row["department"] or "") != (dept or "") or not row["active"]:
                    self.conn.execute(
                        "UPDATE exm_faculty SET department=?, active=1, updated_at=? WHERE id=?",
                        (dept or "", now(), row["id"]))
                    updated += 1
            if deactivate_missing:
                for r in self.conn.execute("SELECT id,name FROM exm_faculty WHERE active=1 AND source=?",
                                           (source,)).fetchall():
                    if r["name"] not in seen:
                        self.conn.execute("UPDATE exm_faculty SET active=0, updated_at=? WHERE id=?",
                                          (now(), r["id"]))
        return {"added": added, "updated": updated}

    def faculty_all(self, active_only: bool = True) -> List[Dict[str, Any]]:
        q = "SELECT * FROM exm_faculty" + (" WHERE active=1" if active_only else "") + " ORDER BY name"
        return [dict(r) for r in self.conn.execute(q)]

    def faculty(self, fid: int) -> Optional[Dict[str, Any]]:
        return _d(self.conn.execute("SELECT * FROM exm_faculty WHERE id=?", (fid,)).fetchone())

    def faculty_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        return _d(self.conn.execute("SELECT * FROM exm_faculty WHERE employee_code=?",
                                    (str(code).strip(),)).fetchone())

    def faculty_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        return _d(self.conn.execute("SELECT * FROM exm_faculty WHERE name=?", (name,)).fetchone())

    def set_department(self, faculty_ids: Sequence[int], department: str, source: str = "coordinator") -> int:
        """Assign a department/group label to people (kept across roster syncs; audited)."""

        with transaction(self.conn):
            for fid in faculty_ids:
                self.conn.execute(
                    "INSERT INTO exm_dept_override(faculty_id,department,source,updated_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(faculty_id) DO UPDATE SET department=excluded.department, "
                    "source=excluded.source, updated_at=excluded.updated_at",
                    (fid, department, source, now()))
                self.conn.execute("UPDATE exm_faculty SET department=?, updated_at=? WHERE id=?",
                                  (department, now(), fid))
            self.audit("set_department", "faculty", None,
                       {"faculty": list(faculty_ids), "department": department, "source": source})
        return len(faculty_ids)

    def clear_department(self, faculty_ids: Sequence[int]) -> int:
        """Drop the override; the label learned from the timetable returns at the next roster sync."""

        with transaction(self.conn):
            n = 0
            for fid in faculty_ids:
                n += self.conn.execute("DELETE FROM exm_dept_override WHERE faculty_id=?", (fid,)).rowcount
            self.audit("clear_department", "faculty", None, {"faculty": list(faculty_ids)})
        return n

    def departments(self) -> List[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT department FROM exm_faculty WHERE active=1 AND department<>'' "
            "ORDER BY department")]

    # ---------------------------------------------------------- history
    def import_history(self, counts: Dict[str, float], source: str) -> Dict[str, Any]:
        """
        Replace the historical totals that came from `source`.  Re-importing the
        same source can never double count; unknown names are returned, not guessed.
        """

        unknown: List[str] = []
        with transaction(self.conn):
            self.conn.execute("DELETE FROM exm_history WHERE source=? AND status='active'", (source,))
            deferred = 0
            for name, value in counts.items():
                row = self.faculty_by_name(name)
                if row is None:
                    unknown.append(name)
                    continue
                if not source.startswith("import:") and self.conn.execute(
                        "SELECT 1 FROM exm_history WHERE faculty_id=? AND status='active' AND source LIKE 'import:%'",
                        (row["id"],)).fetchone():
                    deferred += 1          # an imported (file-verified) total exists: never add this sheet on top of it
                    continue
                self.conn.execute(
                    "INSERT INTO exm_history(faculty_id,duties,source,recorded_at) VALUES (?,?,?,?)",
                    (row["id"], float(value), source, now()))
            self.audit("import_history", "history", None,
                       {"source": source, "rows": len(counts) - len(unknown) - deferred, "unknown": unknown,
                        "deferred_to_imported_total": deferred})
        return {"imported": len(counts) - len(unknown) - deferred, "unknown": unknown, "deferred": deferred}

    def history_components(self, exclude_session_id: Optional[int] = None
                           ) -> Dict[int, Dict[str, float]]:
        """
        Per faculty:  baseline (imported sheets)  +  completed duties  = historical
                      approved-but-not-completed duties of OTHER sessions = committed
        Draft / cancelled / superseded duties are never counted.
        """

        out: Dict[int, Dict[str, float]] = {}

        def slot(fid):
            return out.setdefault(fid, {"baseline": 0.0, "completed": 0.0, "committed": 0.0})

        for r in self.conn.execute("SELECT faculty_id, SUM(duties) t FROM exm_history "
                                   "WHERE status='active' GROUP BY faculty_id"):
            slot(r["faculty_id"])["baseline"] = r["t"] or 0.0
        q = ("SELECT d.faculty_id, d.status, COUNT(*) c FROM exm_duty d "
             "JOIN exm_allocation a ON a.id=d.allocation_id "
             "WHERE d.status IN ('completed','approved') AND a.status IN ('approved','completed') ")
        args: Tuple[Any, ...] = ()
        if exclude_session_id is not None:
            q += "AND a.session_id<>? "
            args = (exclude_session_id,)
        q += "GROUP BY d.faculty_id, d.status"
        for r in self.conn.execute(q, args):
            key = "completed" if r["status"] == "completed" else "committed"
            slot(r["faculty_id"])[key] += r["c"]
        return out

    # --------------------------------------------------------- sessions
    def create_session(self, *, name: str, start: date, end: date, dates: Sequence[date],
                       shifts: Sequence[Dict[str, str]], rooms: Sequence[str],
                       per_room: int, min_duties: Optional[int], max_duties: Optional[int],
                       classes_suspended: Optional[bool], options: Dict[str, Any]) -> int:
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO exm_session(name,start_date,end_date,classes_suspended,"
                "invigilators_per_room,min_duties,max_duties,options,status,revision,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?, 'planning',0,?,?)",
                (name, start.isoformat(), end.isoformat(),
                 None if classes_suspended is None else int(classes_suspended),
                 per_room, min_duties, max_duties, json.dumps(options), now(), now()))
            sid = cur.lastrowid
            for d in dates:
                self.conn.execute("INSERT INTO exm_session_date VALUES (?,?)", (sid, d.isoformat()))
            for i, sh in enumerate(shifts):
                self.conn.execute(
                    "INSERT INTO exm_shift(session_id,label,start_time,end_time,position) VALUES (?,?,?,?,?)",
                    (sid, sh["label"], sh["start"], sh["end"], i))
            for i, r in enumerate(rooms):
                self.conn.execute("INSERT INTO exm_room(session_id,label,position) VALUES (?,?,?)",
                                  (sid, r, i))
            self.audit("create_session", "session", sid,
                       {"name": name, "dates": len(dates), "shifts": len(shifts), "rooms": len(rooms)})
        return sid

    def session(self, sid: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM exm_session WHERE id=?", (sid,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["options"] = json.loads(d.get("options") or "{}")
        return d

    def latest_session(self, statuses: Sequence[str] = ("planning", "approved")) -> Optional[Dict[str, Any]]:
        qs = ",".join("?" * len(statuses))
        row = self.conn.execute(
            f"SELECT id FROM exm_session WHERE status IN ({qs}) ORDER BY id DESC LIMIT 1", tuple(statuses)).fetchone()
        return self.session(row["id"]) if row else None

    def update_session(self, sid: int, **fields: Any) -> None:
        if not fields:
            return
        if "options" in fields:
            fields["options"] = json.dumps(fields["options"])
        cols = ", ".join(f"{k}=?" for k in fields)
        with transaction(self.conn):
            self.conn.execute(f"UPDATE exm_session SET {cols}, updated_at=?, revision=revision+1 WHERE id=?",
                              (*fields.values(), now(), sid))

    def session_dates(self, sid: int) -> List[date]:
        return [date.fromisoformat(r[0]) for r in self.conn.execute(
            "SELECT exam_date FROM exm_session_date WHERE session_id=? ORDER BY exam_date", (sid,))]

    def session_shifts(self, sid: int) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM exm_shift WHERE session_id=? ORDER BY position", (sid,))]

    def session_rooms(self, sid: int) -> List[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT label FROM exm_room WHERE session_id=? ORDER BY position", (sid,))]

    # ----------------------------------------------------- allocations
    def next_version(self, sid: int) -> int:
        r = self.conn.execute("SELECT COALESCE(MAX(version),0)+1 v FROM exm_allocation WHERE session_id=?",
                              (sid,)).fetchone()
        return r["v"]

    def allocation(self, aid: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM exm_allocation WHERE id=?", (aid,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["params"] = json.loads(d.get("params") or "{}")
        d["quality"] = json.loads(d.get("quality") or "{}")
        return d

    def allocations(self, sid: int) -> List[Dict[str, Any]]:
        return [self.allocation(r["id"]) for r in self.conn.execute(
            "SELECT id FROM exm_allocation WHERE session_id=? ORDER BY version", (sid,))]

    def active_allocation(self, sid: int) -> Optional[Dict[str, Any]]:
        """Approved version if there is one, else the newest draft."""

        row = self.conn.execute(
            "SELECT id FROM exm_allocation WHERE session_id=? AND status='approved' "
            "ORDER BY version DESC LIMIT 1", (sid,)).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT id FROM exm_allocation WHERE session_id=? AND status='draft' "
                "ORDER BY version DESC LIMIT 1", (sid,)).fetchone()
        return self.allocation(row["id"]) if row else None

    def latest_allocation(self, sid: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT id FROM exm_allocation WHERE session_id=? AND status IN ('draft','approved','completed') "
            "ORDER BY version DESC LIMIT 1", (sid,)).fetchone()
        return self.allocation(row["id"]) if row else None

    def insert_allocation(self, sid: int, *, feasibility: str, parent_id: Optional[int], seed: int,
                          params: Dict[str, Any], quality: Dict[str, Any], notes: str,
                          duties: Sequence[Dict[str, Any]], unfilled: Sequence[Dict[str, Any]],
                          actor: str) -> int:
        """Caller wraps in a transaction.  Duplicate rows are rejected by UNIQUE keys."""

        version = self.next_version(sid)
        cur = self.conn.execute(
            "INSERT INTO exm_allocation(session_id,version,status,feasibility,parent_id,seed,params,"
            "quality,notes,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, version, "draft", feasibility, parent_id, seed, json.dumps(params, default=str),
             json.dumps(quality, default=str), notes, actor, now()))
        aid = cur.lastrowid
        for d in duties:
            self.conn.execute(
                "INSERT INTO exm_duty(allocation_id,faculty_id,exam_date,shift_id,room,seat,status) "
                "VALUES (?,?,?,?,?,?, 'draft')",
                (aid, d["faculty_id"], d["exam_date"], d["shift_id"], d["room"], d["seat"]))
        for u in unfilled:
            self.conn.execute(
                "INSERT INTO exm_unfilled(allocation_id,exam_date,shift_id,room,seat,required,available,reason)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (aid, u["exam_date"], u["shift_id"], u["room"], u["seat"], u["required"],
                 u["available"], u["reason"]))
        self.audit("create_allocation", "allocation", aid,
                   {"session": sid, "version": version, "feasibility": feasibility,
                    "duties": len(duties), "unfilled": len(unfilled), "seed": seed}, actor)
        return aid

    def duties(self, aid: int) -> List[Dict[str, Any]]:
        q = ("SELECT d.*, f.name AS faculty, f.department, s.label AS shift, s.start_time, s.end_time "
             "FROM exm_duty d JOIN exm_faculty f ON f.id=d.faculty_id "
             "JOIN exm_shift s ON s.id=d.shift_id WHERE d.allocation_id=? "
             "ORDER BY d.exam_date, s.position, d.room, d.seat")
        return [dict(r) for r in self.conn.execute(q, (aid,))]

    def unfilled(self, aid: int) -> List[Dict[str, Any]]:
        q = ("SELECT u.*, s.label AS shift FROM exm_unfilled u JOIN exm_shift s ON s.id=u.shift_id "
             "WHERE u.allocation_id=? ORDER BY u.exam_date, s.position, u.room, u.seat")
        return [dict(r) for r in self.conn.execute(q, (aid,))]

    def set_allocation_status(self, aid: int, status: str) -> None:
        duty_status = {"draft": "draft", "approved": "approved", "completed": "completed",
                       "superseded": "cancelled", "cancelled": "cancelled"}[status]
        self.conn.execute("UPDATE exm_allocation SET status=?, approved_at=CASE WHEN ?='approved' "
                          "THEN ? ELSE approved_at END WHERE id=?", (status, status, now(), aid))
        self.conn.execute("UPDATE exm_duty SET status=? WHERE allocation_id=?", (duty_status, aid))

    # ----------------------------------------------------- commitments
    def other_session_duties(self, session_id: int, dates: Sequence[date]) -> List[Dict[str, Any]]:
        """Approved/completed duties of OTHER sessions on these dates (cross-session conflicts)."""

        if not dates:
            return []
        qs = ",".join("?" * len(dates))
        q = (f"SELECT d.faculty_id, d.exam_date, s.start_time, s.end_time FROM exm_duty d "
             f"JOIN exm_allocation a ON a.id=d.allocation_id JOIN exm_shift s ON s.id=d.shift_id "
             f"WHERE a.session_id<>? AND a.status IN ('approved','completed') "
             f"AND d.status IN ('approved','completed') AND d.exam_date IN ({qs})")
        return [dict(r) for r in self.conn.execute(q, (session_id, *[d.isoformat() for d in dates]))]

    def commitments(self, dates: Sequence[date]) -> List[Dict[str, Any]]:
        if not dates:
            return []
        qs = ",".join("?" * len(dates))
        return [dict(r) for r in self.conn.execute(
            f"SELECT * FROM exm_commitment WHERE status='active' AND commit_date IN ({qs})",
            tuple(d.isoformat() for d in dates))]

    def add_commitment(self, faculty_id: int, on: date, start: str, end: str, label: str = "") -> int:
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO exm_commitment(faculty_id,commit_date,start_time,end_time,label) VALUES (?,?,?,?,?)",
                (faculty_id, on.isoformat(), start, end, label))
            self.audit("add_commitment", "commitment", cur.lastrowid,
                       {"faculty": faculty_id, "date": on.isoformat(), "label": label})
        return cur.lastrowid

    # ------------------------------------------------- admin duties
    def add_responsibility(self, faculty_id: int, role: str, description: str = "",
                           start: Optional[date] = None, end: Optional[date] = None) -> int:
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT id FROM exm_admin_responsibility WHERE faculty_id=? AND lower(role)=lower(?) "
                "AND active=1 AND COALESCE(start_date,'')=COALESCE(?, '') AND COALESCE(end_date,'')=COALESCE(?, '')",
                (faculty_id, role, start.isoformat() if start else None,
                 end.isoformat() if end else None)).fetchone()
            if row:
                return row["id"]
            cur = self.conn.execute(
                "INSERT INTO exm_admin_responsibility(faculty_id,role,description,start_date,end_date,active,created_at)"
                " VALUES (?,?,?,?,?,1,?)",
                (faculty_id, role, description, start.isoformat() if start else None,
                 end.isoformat() if end else None, now()))
            self.audit("add_responsibility", "responsibility", cur.lastrowid,
                       {"faculty": faculty_id, "role": role})
        return cur.lastrowid

    def end_responsibility(self, faculty_id: int, role: Optional[str] = None) -> int:
        with transaction(self.conn):
            q = "UPDATE exm_admin_responsibility SET active=0 WHERE faculty_id=? AND active=1"
            args: List[Any] = [faculty_id]
            if role:
                q += " AND lower(role)=lower(?)"
                args.append(role)
            n = self.conn.execute(q, args).rowcount
            self.audit("end_responsibility", "responsibility", faculty_id, {"role": role, "rows": n})
        return n

    def responsibilities(self, active_only: bool = True) -> List[Dict[str, Any]]:
        q = ("SELECT r.*, f.name AS faculty, f.department FROM exm_admin_responsibility r "
             "JOIN exm_faculty f ON f.id=r.faculty_id")
        if active_only:
            q += " WHERE r.active=1"
        return [dict(r) for r in self.conn.execute(q + " ORDER BY f.name")]

    # ---------------------------------------------------- settings
    def set_setting(self, key: str, value: Any) -> None:
        with transaction(self.conn):
            self.conn.execute("INSERT INTO exm_settings(key,value) VALUES (?,?) "
                              "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                              (key, json.dumps(value)))
            self.audit("set_setting", "settings", None, {"key": key, "value": value})