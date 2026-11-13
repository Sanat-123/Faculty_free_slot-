"""
Dynamic file import for the college-wide Faculty Master.

Nothing here knows a file name, a path, a department or a person.  A file is
READ, its columns are recognised from the header words in config/import_columns.json,
its PURPOSE is inferred from which columns it has, the administrator sees a
PREVIEW, and only then is it imported (idempotently, with an audit trail).

    probe = Importer.probe(path)            # detect type + purpose + column mapping, no writes
    svc_importer.import_probe(probe, ...)   # validated, transactional import

Purposes:  faculty_roster | duty_counts | roster_with_counts | timetable | unknown
An `unknown` purpose is never guessed: the caller must ask the administrator.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .db import transaction
from .repository import Repository, norm_name, now

_READERS = (".pdf", ".xlsx", ".xls", ".csv")            # formats the code can actually read


def _supported() -> tuple:
    """Extensions accepted = those listed in config/import_columns.json that a reader exists for."""
    try:
        want = json.loads(_columns_file().read_text(encoding="utf-8")).get("supported_extensions")
    except Exception:
        want = None
    return tuple(e for e in (want or _READERS) if e in _READERS) or _READERS


SUPPORTED = _supported()


# ---------------------------------------------------------------- configuration
def _columns_file() -> Path:
    env = os.environ.get("EXAM_MGMT_COLUMNS_FILE")
    return Path(env) if env else Path(__file__).resolve().parent.parent / "config" / "import_columns.json"


def load_columns() -> Dict[str, Any]:
    try:
        return json.loads(_columns_file().read_text(encoding="utf-8"))
    except Exception:
        return {"fields": {}, "timetable_markers": [], "titles": [], "fuzzy_header_threshold": 88}


def _key(text: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9#]+", str(text or "").lower()))


def strip_titles(name: str, titles: Sequence[str]) -> str:
    low = set(titles)
    return " ".join(t for t in re.findall(r"[a-z0-9]+", str(name or "").lower()) if t not in low)


# -------------------------------------------------------------------- data model
@dataclass
class Table:
    label: str                       # sheet name / "page 3" ...
    header: List[str]
    rows: List[List[str]]
    mapping: Dict[str, int] = field(default_factory=dict)   # field -> column index


@dataclass
class Probe:
    filename: str
    sha256: str
    file_type: str
    purpose: str                     # see module docstring
    confidence: float
    tables: List[Table]
    problems: List[str] = field(default_factory=list)
    unmapped: List[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return sum(len(t.rows) for t in self.tables)

    def records(self) -> List[Dict[str, Any]]:
        """Rows as dicts keyed by FIELD name (only mapped fields)."""
        out = []
        for t in self.tables:
            for i, r in enumerate(t.rows):
                rec = {f: (r[c].strip() if c < len(r) and r[c] is not None else "") for f, c in t.mapping.items()}
                rec["_ref"] = f"{t.label} row {i + 1}"
                out.append(rec)
        return out

    def preview(self, n: int = 5) -> Dict[str, Any]:
        return {"filename": self.filename, "type": self.file_type, "purpose": self.purpose,
                "confidence": round(self.confidence, 2), "rows": self.row_count,
                "columns": {t.label: {f: t.header[c] for f, c in t.mapping.items()} for t in self.tables},
                "unmapped_columns": self.unmapped, "problems": self.problems,
                "sample": self.records()[:n]}


# ----------------------------------------------------------------------- readers
def _read_pdf(data: bytes, max_pages: Optional[int] = None) -> List[Tuple[str, List[List[str]]]]:
    import pdfplumber
    out = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            if max_pages and i > max_pages:
                break
            for tb in page.extract_tables() or []:
                rows = [[("" if c is None else str(c).replace("\n", " ").strip()) for c in r] for r in tb]
                if rows:
                    out.append((f"page {i}", rows))
    return out


def _read_excel(data: bytes, suffix: str) -> List[Tuple[str, List[List[str]]]]:
    import pandas as pd
    sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str)  # every sheet, any count
    out = []
    for name, df in sheets.items():
        rows = [["" if (c is None or str(c) == "nan") else str(c).strip() for c in r] for r in df.values.tolist()]
        rows = [r for r in rows if any(r)]
        if rows:
            out.append((str(name), rows))
    return out


def _read_csv(data: bytes) -> List[Tuple[str, List[List[str]]]]:
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except Exception:
            continue
    else:
        raise ValueError("cannot decode CSV text")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except Exception:
        dialect = csv.excel
    rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    return [("csv", rows)] if rows else []


def read_tables(data: bytes, suffix: str, max_pdf_pages: Optional[int] = None) -> List[Tuple[str, List[List[str]]]]:
    if suffix == ".pdf":
        return _read_pdf(data, max_pdf_pages)
    if suffix in (".xlsx", ".xls"):
        return _read_excel(data, suffix)
    if suffix == ".csv":
        return _read_csv(data)
    raise ValueError(f"unsupported file type '{suffix}' (supported: {', '.join(SUPPORTED)})")


# ----------------------------------------------------------------- column mapping
def map_header(header: Sequence[str], cfg: Dict[str, Any]) -> Tuple[Dict[str, int], List[str]]:
    """field -> column index by synonym / fuzzy match.  Ambiguous columns stay unmapped."""

    from rapidfuzz import fuzz
    fields: Dict[str, List[str]] = cfg.get("fields", {})
    thr = cfg.get("fuzzy_header_threshold", 88)
    keys = [_key(h) for h in header]
    mapping: Dict[str, int] = {}
    taken: set = set()
    for f, syns in fields.items():                       # exact synonym pass
        for i, k in enumerate(keys):
            if i not in taken and k and k in {_key(s) for s in syns}:
                mapping.setdefault(f, i)
                taken.add(i)
                break
    for f, syns in fields.items():                       # fuzzy pass for what is still missing
        if f in mapping:
            continue
        best, best_i = 0.0, -1
        for i, k in enumerate(keys):
            if i in taken or not k:
                continue
            score = max(fuzz.ratio(k, _key(s)) for s in syns)
            if score > best:
                best, best_i = score, i
        if best >= thr:
            mapping[f] = best_i
            taken.add(best_i)
    unmapped = [header[i] for i in range(len(header)) if i not in taken and str(header[i]).strip()]
    return mapping, unmapped


def _find_header(rows: List[List[str]], cfg: Dict[str, Any]) -> Optional[int]:
    best, best_i = 1, None
    for i, r in enumerate(rows[:12]):
        m, _ = map_header(r, cfg)
        if len(m) > best or (len(m) == best and best_i is None and len(m) >= 2):
            best, best_i = len(m), i
    return best_i


def _purpose(fields: set, header_text: str, cfg: Dict[str, Any]) -> Tuple[str, float]:
    days = {_key(d) for d in cfg.get("timetable_markers", [])}
    if len(days & set(header_text.split())) >= 3:
        return "timetable", 0.9
    has_id = "name" in fields or "employee_code" in fields
    if has_id and "duty_count" in fields and ({"department", "designation"} & fields):
        return "roster_with_counts", 0.9
    if has_id and "duty_count" in fields:
        return "duty_counts", 0.95
    if "name" in fields and ({"department", "employee_code", "designation"} & fields):
        score = 0.6 + 0.1 * len({"department", "employee_code", "designation"} & fields)
        return "faculty_roster", min(score, 0.95)
    return "unknown", 0.0


# --------------------------------------------------------------------- the Importer
class Importer:
    """Reads, classifies and imports; holds no data of its own."""

    def __init__(self, repo: Repository, actor: str = "administrator"):
        self.repo = repo
        self.conn = repo.conn
        self.actor = actor
        self.cfg = load_columns()

    # ---------------------------------------------------------------- probing
    def probe(self, source: Any, filename: Optional[str] = None,
              mapping_override: Optional[Dict[str, str]] = None, quick: bool = False) -> Probe:
        """Inspect a file (path or bytes).  Never writes.  The file name is not used to decide anything."""

        if isinstance(source, (bytes, bytearray)):
            data, name = bytes(source), filename or "upload"
        else:
            path = Path(source)
            data, name = path.read_bytes(), filename or path.name
        suffix = Path(name).suffix.lower()
        sha = hashlib.sha256(data).hexdigest()
        problems: List[str] = []
        if suffix not in SUPPORTED:
            return Probe(name, sha, suffix or "?", "unknown", 0.0, [],
                         [f"Unsupported file type '{suffix}'. Supported: {', '.join(SUPPORTED)}"])
        try:
            raw = read_tables(data, suffix, 2 if quick else None)   # quick: classify from the first pages only
        except Exception as exc:                          # malformed / unreadable
            return Probe(name, sha, suffix, "unknown", 0.0, [], [f"Could not read the file: {exc}"])
        if not raw:
            return Probe(name, sha, suffix, "unknown", 0.0, [],
                         ["No tables or rows were found (a scanned image PDF needs OCR first)."])

        tables: List[Table] = []
        unmapped_all: List[str] = []
        header_ref: Optional[List[str]] = None
        no_header: List[str] = []
        for label, rows in raw:
            hi = _find_header(rows, self.cfg)
            if hi is None and header_ref is not None:     # continuation page without a header row
                hdr, body = header_ref, rows
            elif hi is None:
                no_header.append(label)
                continue
            else:
                hdr, body = rows[hi], rows[hi + 1:]
                header_ref = hdr
            mapping, unm = map_header(hdr, self.cfg)
            if mapping_override:                          # administrator-confirmed mapping wins
                for f, col in mapping_override.items():
                    idx = next((i for i, h in enumerate(hdr) if _key(h) == _key(col)), None)
                    if idx is not None:
                        mapping[f] = idx
            body = [r for r in body if any(c.strip() for c in r)]
            tables.append(Table(label, list(hdr), body, mapping))
            unmapped_all += [u for u in unm if u not in unmapped_all]

        if no_header:
            problems.append(f"No recognisable header row on {len(no_header)} table(s) (first: {no_header[0]}).")
        if not tables:
            if suffix == ".pdf" and self._looks_like_timetable(data):
                return Probe(name, sha, suffix, "timetable", 0.8, [], ["Teaching timetable detected."], unmapped_all)
            return Probe(name, sha, suffix, "unknown", 0.0, [], problems or ["No usable table found"], unmapped_all)

        fields = set().union(*(set(t.mapping) for t in tables))
        header_text = " ".join(_key(h) for t in tables for h in t.header)
        purpose, conf = _purpose(fields, header_text, self.cfg)
        if purpose == "unknown":
            problems.append("The purpose of this file is unclear: confirm what it contains and the column mapping.")
        return Probe(name, sha, suffix, purpose, conf, tables, problems, unmapped_all)

    def _looks_like_timetable(self, data: bytes) -> bool:
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                text = " ".join((pg.extract_text() or "") for pg in pdf.pages[:3]).lower()
        except Exception:
            return False
        days = {d for d in self.cfg.get("timetable_markers", []) if re.search(rf"\b{d}\b", text)}
        return len(days) >= 3

    # ------------------------------------------------------------- validation
    def validate(self, probe: Probe) -> Dict[str, Any]:
        recs = probe.records()
        issues: List[Dict[str, str]] = []
        seen_codes: Dict[str, str] = {}
        for r in recs:
            if not r.get("name") and not r.get("employee_code"):
                issues.append({"ref": r["_ref"], "severity": "error", "message": "row has no name and no employee code"})
                continue
            if "name" in r and not r["name"]:
                issues.append({"ref": r["_ref"], "severity": "error", "message": "missing faculty name"})
            code = r.get("employee_code", "")
            if code:
                if code in seen_codes:
                    issues.append({"ref": r["_ref"], "severity": "error",
                                   "message": f"employee code {code} repeats ({seen_codes[code]})"})
                seen_codes[code] = r["_ref"]
            elif "employee_code" in r:
                issues.append({"ref": r["_ref"], "severity": "warning", "message": "missing employee code"})
            if "duty_count" in r and r["duty_count"] != "":
                try:
                    if float(r["duty_count"]) < 0:
                        raise ValueError
                except ValueError:
                    issues.append({"ref": r["_ref"], "severity": "error",
                                   "message": f"invalid duty count '{r['duty_count']}'"})
        return {"rows": len(recs), "errors": sum(i["severity"] == "error" for i in issues),
                "warnings": sum(i["severity"] == "warning" for i in issues), "issues": issues}

    # -------------------------------------------------------------- bookkeeping
    def _previous(self, sha: str, purpose: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM exm_import_file WHERE sha256=? AND purpose=? AND status='imported'",
                                (sha, purpose)).fetchone()
        return dict(row) if row else None

    def _log(self, probe: Probe, purpose: str, summary: Dict[str, Any], issues: List[Dict[str, str]]) -> int:
        cur = self.conn.execute(
            "INSERT INTO exm_import_file(sha256,filename,file_kind,purpose,row_count,summary,imported_by,imported_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (probe.sha256, probe.filename, probe.file_type, purpose, probe.row_count,
             json.dumps(summary, default=str, ensure_ascii=False), self.actor, now()))
        iid = cur.lastrowid
        for i in issues:
            self.conn.execute("INSERT INTO exm_import_issue(import_id,row_ref,severity,message,raw) VALUES (?,?,?,?,?)",
                              (iid, i.get("ref", ""), i.get("severity", "warning"), i["message"], i.get("raw", "")))
        return iid

    # ---------------------------------------------------------------- the import
    def import_probe(self, probe: Probe, purpose: Optional[str] = None, *, mode: str = "replace",
                     allow_errors: bool = False) -> Dict[str, Any]:
        """
        Import a probed file.  `purpose` is the administrator's confirmation (defaults to the
        detected one; an unknown purpose is refused).  Importing identical bytes twice is a no-op.
        """

        purpose = purpose or probe.purpose
        if purpose in ("unknown", "timetable"):
            return {"status": "needs_confirmation" if purpose == "unknown" else "use_timetable_import",
                    "message": ("Purpose unclear - please confirm what this file contains."
                                if purpose == "unknown" else
                                "This is a teaching timetable: import it with the timetable importer.")}
        prev = self._previous(probe.sha256, purpose)
        if prev:
            return {"status": "duplicate", "import_id": prev["id"],
                    "message": f"Identical file already imported on {prev['imported_at']} (import #{prev['id']}); nothing changed."}
        report = self.validate(probe)
        if report["errors"] and not allow_errors:
            return {"status": "validation_failed", "validation": report,
                    "message": f"{report['errors']} row(s) have errors; fix them or import with errors allowed "
                               f"(the faulty rows are skipped and listed, never silently dropped)."}
        with transaction(self.conn):
            issues: List[Dict[str, str]] = list(report["issues"])
            summary: Dict[str, Any] = {"purpose": purpose}
            iid = self._log(probe, purpose, summary, [])
            if purpose in ("faculty_roster", "roster_with_counts"):
                summary["roster"] = self._apply_roster(probe, iid, issues)
            if purpose in ("duty_counts", "roster_with_counts"):
                summary["history"] = self._apply_counts(probe, iid, issues, mode)
            for i in issues:
                self.conn.execute("INSERT INTO exm_import_issue(import_id,row_ref,severity,message,raw) VALUES (?,?,?,?,?)",
                                  (iid, i.get("ref", ""), i.get("severity", "warning"), i["message"], i.get("raw", "")))
            nothing = purpose == "duty_counts" and summary["history"]["applied"] == 0
            self.conn.execute("UPDATE exm_import_file SET summary=?, status=? WHERE id=?",
                              (json.dumps(summary, default=str, ensure_ascii=False),
                               "rolled_back" if nothing else "imported", iid))
            self.repo.audit("import_file", "import", iid, {"file": probe.filename, "purpose": purpose,
                                                           "rows": probe.row_count, "applied_nothing": nothing}, actor=self.actor)
        if nothing:                               # not recorded as imported, so uploading it again later is NOT a duplicate
            return {"status": "nothing_applied", "import_id": iid, "issues": len(issues), **summary,
                    "message": f"None of the {summary['history']['not_applied']} duty counts could be matched to a faculty "
                               f"member - load the faculty roster first, then upload this file again."}
        return {"status": "imported", "import_id": iid, "issues": len(issues), **summary}

    # --------------------------------------------------------------- roster
    def _unique_display_name(self, name: str, code: str) -> str:
        if not self.conn.execute("SELECT 1 FROM exm_faculty WHERE name=?", (name,)).fetchone():
            return name
        return f"{name} (EC {code})" if code else f"{name} (unverified {now()[11:19]})"

    def _apply_roster(self, probe: Probe, iid: int, issues: List[Dict[str, str]]) -> Dict[str, Any]:
        titles = self.cfg.get("titles", [])
        added: List[str] = []
        changed: List[Dict[str, Any]] = []
        unchanged = linked = 0
        seen_ids: set = set()
        for r in probe.records():
            name = " ".join(r.get("name", "").split())
            code = r.get("employee_code", "").strip() or None
            dept = " ".join(r.get("department", "").split())
            desig = " ".join(r.get("designation", "").split())
            serial = int(r["serial"]) if re.fullmatch(r"\d+", r.get("serial", "") or "") else None
            if not name:
                if not any(i["ref"] == r["_ref"] and i["severity"] == "error" for i in issues):
                    issues.append({"ref": r["_ref"], "severity": "error", "message": "row skipped: no faculty name"})
                continue
            row = self.repo.faculty_by_code(code) if code else None
            if row is None:                                # adopt an existing code-less record, never duplicate it
                cands = [dict(x) for x in self.conn.execute(
                    "SELECT * FROM exm_faculty WHERE employee_code IS NULL")]
                match = [c for c in cands if strip_titles(c["name"], titles) == strip_titles(name, titles)]
                if len(match) > 1 and dept:
                    narrowed = [c for c in match if (c["department"] or "").lower() == dept.lower()]
                    match = narrowed or match
                if len(match) == 1 and code:
                    row = match[0]
                    linked += 1
                elif len(match) > 1:
                    issues.append({"ref": r["_ref"], "severity": "warning",
                                   "message": f"'{name}' matches {len(match)} existing code-less records; "
                                              f"created as a new record - please review/merge"})
            status = "verified" if (code and dept) else "needs_verification"
            if row is None:
                disp = self._unique_display_name(name, code or "")
                cur = self.conn.execute(
                    "INSERT INTO exm_faculty(name,norm,department,active,source,updated_at,employee_code,designation,"
                    "record_status,roster_serial) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (disp, norm_name(disp), dept, 1, f"import:{iid}", now(), code, desig, status, serial))
                fid = cur.lastrowid
                added.append(disp)
            else:
                fid = row["id"]
                new_dept = dept
                ov = self.conn.execute("SELECT department FROM exm_dept_override WHERE faculty_id=?", (fid,)).fetchone()
                if ov:
                    new_dept = ov["department"]            # an administrator's correction wins
                diffs = {}
                for col, new in (("department", new_dept), ("designation", desig), ("employee_code", code)):
                    old = row.get(col)
                    if new not in (None, "") and (old or "") != new:
                        diffs[col] = {"from": old, "to": new}
                if not row["active"]:
                    diffs["active"] = {"from": 0, "to": 1}
                if diffs:
                    self.conn.execute(
                        "UPDATE exm_faculty SET department=COALESCE(NULLIF(?, ''),department), "
                        "designation=COALESCE(NULLIF(?, ''),designation), employee_code=COALESCE(?,employee_code), "
                        "active=1, record_status=?, roster_serial=?, source=?, updated_at=? WHERE id=?",
                        (new_dept, desig, code, status, serial, f"import:{iid}", now(), fid))
                    changed.append({"faculty_id": fid, "name": row["name"], "changes": diffs})
                    self.repo.audit("faculty_changed", "faculty", fid, {"import": iid, "changes": diffs}, actor=self.actor)
                else:
                    self.conn.execute("UPDATE exm_faculty SET roster_serial=?, source=? WHERE id=?", (serial, f"import:{iid}", fid))
                    unchanged += 1
            seen_ids.add(fid)
            alias = strip_titles(name, titles)
            if alias:
                self.conn.execute("INSERT OR IGNORE INTO exm_faculty_alias(alias_norm,faculty_id,source,created_at) "
                                  "VALUES (?,?,?,?)", (alias, fid, f"import:{iid}", now()))
        # people already in the database but absent from this file: REPORTED, never deleted.  Roster-imported people
        # missing from a NEWER file stay as they are; people who were never on any imported roster are PARKED.
        missing = [dict(x) for x in self.conn.execute(
            "SELECT id,name,department,employee_code FROM exm_faculty WHERE active=1 AND source LIKE 'import:%'")
            if x["id"] not in seen_ids]
        parked = self.repo.park_unlisted()
        return {"added": len(added), "updated": len(changed), "unchanged": unchanged,
                "linked_existing_records": linked, "not_in_this_file": len(missing),
                "parked_not_on_roster": len(parked), "parked_sample": [r["name"] for r in parked[:10]],
                "not_in_this_file_sample": missing[:10], "changes": changed[:50]}

    # --------------------------------------------------------- duty counts
    def _match_person(self, rec: Dict[str, Any], by_norm: Dict[str, List[Dict[str, Any]]],
                      titles: Sequence[str]) -> Tuple[Optional[Dict[str, Any]], str]:
        code = rec.get("employee_code", "").strip()
        if code:
            row = self.repo.faculty_by_code(code)
            return (row, "matched_by_code") if row else (None, "unknown_code")
        key = strip_titles(rec.get("name", ""), titles)
        cands = by_norm.get(key, [])
        if len(cands) == 1:
            return cands[0], "matched_by_unique_name"
        if len(cands) > 1:                                  # same name, no code: verify by serial position
            serial = rec.get("serial", "")
            if re.fullmatch(r"\d+", serial or ""):
                hit = [c for c in cands if c.get("roster_serial") == int(serial)]
                if len(hit) == 1:
                    return hit[0], "matched_by_serial_and_name"
            return None, "ambiguous_name"
        return None, "unknown_name"

    def _apply_counts(self, probe: Probe, iid: int, issues: List[Dict[str, str]], mode: str) -> Dict[str, Any]:
        titles = self.cfg.get("titles", [])
        by_norm: Dict[str, List[Dict[str, Any]]] = {}
        for f in self.repo.faculty_all(active_only=False):
            base = strip_titles(re.sub(r"\s*\((EC|unverified)[^)]*\)", "", f["name"]), titles)
            by_norm.setdefault(base, []).append(f)
        source = f"import:{iid}"
        matched: List[Tuple[int, float, str]] = []
        skipped = 0
        how: Dict[str, int] = {}
        for r in probe.records():
            if r.get("duty_count", "") == "":
                continue
            try:
                value = float(r["duty_count"])
            except ValueError:
                continue                                    # already reported by validate()
            person, method = self._match_person(r, by_norm, titles)
            how[method] = how.get(method, 0) + 1
            if person is None:
                skipped += 1
                issues.append({"ref": r["_ref"], "severity": "warning" if method != "ambiguous_name" else "error",
                               "message": f"duty count NOT applied ({method.replace('_', ' ')}): "
                                          f"'{r.get('name') or r.get('employee_code')}' = {r['duty_count']}",
                               "raw": json.dumps({k: v for k, v in r.items() if k != '_ref'})})
                continue
            matched.append((person["id"], value, method))
        superseded = 0
        if mode == "replace":                               # a cumulative total replaces (never adds to) older ones
            ids = [m[0] for m in matched]
            for chunk in range(0, len(ids), 500):
                part = ids[chunk:chunk + 500]
                q = ",".join("?" * len(part))
                superseded += self.conn.execute(
                    f"UPDATE exm_history SET status='superseded' WHERE status='active' AND faculty_id IN ({q})",
                    part).rowcount
        for fid, value, method in matched:
            verification = "verified" if method in ("matched_by_code", "matched_by_unique_name",
                                                    "matched_by_serial_and_name") else "unverified"
            self.conn.execute(
                "INSERT INTO exm_history(faculty_id,duties,source,recorded_at,status,verification,import_id) "
                "VALUES (?,?,?,?, 'active', ?, ?)", (fid, value, source, now(), verification, iid))
        return {"applied": len(matched), "not_applied": skipped, "matching": how,
                "mode": mode, "superseded_rows": superseded, "total_duties": sum(m[1] for m in matched)}

    # ------------------------------------------------------------- rollback
    def rollback(self, import_id: int) -> Dict[str, Any]:
        """Undo the HISTORY effect of an import (rows are marked, not deleted; superseded rows come back)."""

        row = self.conn.execute("SELECT * FROM exm_import_file WHERE id=?", (import_id,)).fetchone()
        if not row or row["status"] != "imported":
            return {"status": "nothing_to_roll_back"}
        with transaction(self.conn):
            mine = self.conn.execute("UPDATE exm_history SET status='superseded' WHERE import_id=? AND status='active'",
                                     (import_id,)).rowcount
            restored = 0
            for h in self.conn.execute("SELECT DISTINCT faculty_id FROM exm_history WHERE import_id=?", (import_id,)).fetchall():
                prev = self.conn.execute(
                    "SELECT MAX(id) m FROM exm_history WHERE faculty_id=? AND status='superseded' "
                    "AND COALESCE(import_id,0)<>? AND source NOT LIKE 'rolled%'", (h["faculty_id"], import_id)).fetchone()
                if prev and prev["m"]:
                    restored += self.conn.execute("UPDATE exm_history SET status='active' WHERE id=?", (prev["m"],)).rowcount
            self.conn.execute("UPDATE exm_import_file SET status='rolled_back' WHERE id=?", (import_id,))
            self.repo.audit("import_rolled_back", "import", import_id, {"history_rows": mine, "restored": restored}, actor=self.actor)
        return {"status": "rolled_back", "history_rows_deactivated": mine, "previous_rows_restored": restored,
                "note": "Faculty records created by the import are kept (never deleted); review them in the roster."}

    # ------------------------------------------------------- timetable-name links
    def link_timetable_names(self, names: Iterable[str]) -> Dict[str, Any]:
        """Exact normalised matches are linked as aliases; near matches are SUGGESTED, never auto-applied."""

        from rapidfuzz import process, fuzz
        titles = self.cfg.get("titles", [])
        faculty = [f for f in self.repo.faculty_all(active_only=False)
                   if f.get("record_status") not in ("needs_verification", "merged")]      # roster people only
        keys = {f["id"]: strip_titles(re.sub(r"\s*\((EC|unverified)[^)]*\)", "", f["name"]), titles) for f in faculty}
        inv: Dict[str, List[int]] = {}
        for fid, k in keys.items():
            inv.setdefault(k, []).append(fid)
        linked, suggest, unmatched = 0, [], []
        with transaction(self.conn):
            for n in names:
                k = strip_titles(n, titles)
                ids = inv.get(k, [])
                if len(ids) == 1:
                    self.conn.execute("INSERT OR IGNORE INTO exm_faculty_alias(alias_norm,faculty_id,source,created_at) "
                                      "VALUES (?,?,?,?)", (k, ids[0], "timetable", now()))
                    linked += 1
                    continue
                best = process.extract(k, list(inv), scorer=fuzz.token_sort_ratio, limit=2, score_cutoff=80)
                if best:
                    suggest.append({"timetable_name": n, "candidates": [
                        {"name": next(f["name"] for f in faculty if f["id"] == inv[c][0]), "score": round(s)}
                        for c, s, _ in best]})
                else:
                    unmatched.append(n)
        return {"linked": linked, "needs_confirmation": suggest, "unmatched": unmatched}

    # ----------------------------------------------- verification of unlisted names
    def unverified(self) -> List[Dict[str, Any]]:
        """Records that came from a timetable but are not on the imported roster (parked, not eligible)."""
        return [dict(r) for r in self.conn.execute(
            "SELECT id,name,department,active,source FROM exm_faculty WHERE record_status='needs_verification' ORDER BY name")]

    def confirm_alias(self, unverified_id: int, roster_faculty_id: int) -> Dict[str, Any]:
        """The administrator says this timetable spelling IS that roster person: future syncs recognise it."""
        row = self.conn.execute("SELECT * FROM exm_faculty WHERE id=?", (unverified_id,)).fetchone()
        tgt = self.conn.execute("SELECT * FROM exm_faculty WHERE id=?", (roster_faculty_id,)).fetchone()
        if not row or not tgt or unverified_id == roster_faculty_id:
            return {"status": "invalid"}
        with transaction(self.conn):
            key = strip_titles(row["name"], self.cfg.get("titles", []))
            self.conn.execute("INSERT OR REPLACE INTO exm_faculty_alias(alias_norm,faculty_id,source,created_at) VALUES (?,?,?,?)",
                              (key, roster_faculty_id, f"confirmed by {self.actor}", now()))
            self.conn.execute("UPDATE exm_faculty SET active=0, record_status='merged' WHERE id=?", (unverified_id,))
            self.repo.audit("alias_confirmed", "faculty", roster_faculty_id,
                            {"alias": row["name"], "merged_record": unverified_id}, actor=self.actor)
        return {"status": "linked", "alias": row["name"], "person": tgt["name"]}

    def accept_unlisted(self, faculty_id: int) -> Dict[str, Any]:
        """The administrator confirms an unlisted person is real and eligible."""
        with transaction(self.conn):
            self.conn.execute("UPDATE exm_faculty SET active=1, record_status='accepted' WHERE id=? AND record_status='needs_verification'",
                              (faculty_id,))
            self.repo.audit("unlisted_accepted", "faculty", faculty_id, {}, actor=self.actor)
        return {"status": "accepted"}

    # ----------------------------------------------------- configurable import folder
    @staticmethod
    def import_dir(explicit: Optional[str] = None) -> Optional[Path]:
        """The folder to scan: an explicit argument, else EXAM_MGMT_IMPORT_DIR. Never a built-in path."""
        raw = explicit or os.environ.get("EXAM_MGMT_IMPORT_DIR")
        return Path(raw) if raw else None

    def scan_directory(self, folder: Optional[str] = None) -> List[Dict[str, Any]]:
        """Preview every supported file in the folder (no writes). Each file is judged on its own content;
        files whose identical bytes were already imported are marked so and will not be imported again."""
        d = self.import_dir(folder)
        if d is None or not d.is_dir():
            return []
        out = []
        for path in sorted(d.iterdir()):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED:
                continue
            probe = self.probe(path)
            prev = self._previous(probe.sha256, probe.purpose)
            out.append({"file": path.name, "purpose": probe.purpose, "confidence": round(probe.confidence, 2),
                        "rows": probe.row_count, "problems": probe.problems,
                        "already_imported": bool(prev), "probe": probe})
        return out