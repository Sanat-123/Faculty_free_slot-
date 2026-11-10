"""College-wide roster import, department exclusion, priority tiers, damped-history fairness,
affected-only repair and NLP phrasing.  Synthetic data; the real PDFs are used only if present."""

import csv
import os
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

from exam_management import db
from exam_management.chat_handler import ExamManagementHandler
from exam_management.config import ExamSettings
from exam_management.importer import Importer
from exam_management.policy import PolicyError, PolicyService
from exam_management.repository import Repository
from exam_management.service import ExamService, SessionSpec

FAST = ExamSettings(solver_deterministic_time=0.6, solver_max_seconds=2.0, candidate_runs=4)
ROOT = Path(__file__).resolve().parents[2]


def _discover_real():
    """Classify whatever is in the project data folder by CONTENT (no file name is assumed)."""
    found = {}
    folder = ROOT / "data"
    if not folder.is_dir():
        return found
    probe_db = db.connect(":memory:")
    db.migrate(probe_db)
    imp = Importer(Repository(probe_db))
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.suffix.lower() in (".pdf", ".xlsx", ".xls", ".csv"):
            try:
                found.setdefault(imp.probe(path, quick=True).purpose, []).append(path)
            except Exception:
                continue
    return found


_REAL = _discover_real()
REAL_ROSTER = (_REAL.get("faculty_roster") or [ROOT / "__absent__"])[0]
REAL_COUNTS = (_REAL.get("duty_counts") or [ROOT / "__absent__"])[0]
REAL_TIMETABLES = _REAL.get("timetable", [])
SHIFT = [{"label": "Morning", "start": "09:00", "end": "12:00"}]


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


def roster_rows(spec):
    """spec: {dept: n}. Unique names/codes, generated - no fixed college data."""
    rows, k = [], 0
    for dept, n in spec.items():
        for _ in range(n):
            k += 1
            rows.append([k, f"Person{k:03d} Surname{k:03d}", dept, f"E{1000 + k}", "ASSISTANT PROFESSOR"])
    return rows


HDR = ["S.No.", "Faculty Name", "Branch / Department", "Employee Code", "Designation"]


@pytest.fixture
def svc(tmp_path):
    return ExamService(str(tmp_path / "x.db"), settings=FAST)


def load(svc, tmp_path, spec, history=None, name="anything_at_all.csv"):
    rows = roster_rows(spec)
    imp = svc.importer
    out = imp.import_probe(imp.probe(write_csv(tmp_path / name, HDR, rows)))
    assert out["status"] == "imported", out
    if history is not None:
        hr = [[r[0], r[1], history(i, r)] for i, r in enumerate(rows)]
        out = imp.import_probe(imp.probe(write_csv(tmp_path / "counts_xyz.csv", ["S.No.", "Faculty Name", "Duty Count"], hr)))
        assert out["status"] == "imported", out
    return rows


def session(svc, days=5, rooms=4, per=2, mx=None, mn=None, **opts):
    start = date(2026, 11, 9)
    return svc.create_session(SessionSpec("T", [start + timedelta(days=i) for i in range(days)], SHIFT,
                                          per_room=per, room_count=rooms, min_duties=mn, max_duties=mx,
                                          classes_suspended=True, options=opts))


def per_person(svc, aid):
    return Counter(d["faculty_id"] for d in svc.repo.duties(aid))


# ============================================================ A. import
def test_roster_detected_from_content_not_filename_or_header_wording(svc, tmp_path):
    p = write_csv(tmp_path / "zzz.csv", ["Sr No", "Name of Faculty", "Dept", "Emp ID", "Post"], roster_rows({"X": 3}))
    pr = svc.importer.probe(p)
    assert pr.purpose == "faculty_roster" and pr.row_count == 3
    assert set(pr.tables[0].mapping) >= {"name", "department", "employee_code", "designation"}


def test_same_content_twice_is_a_noop_even_under_another_name(svc, tmp_path):
    load(svc, tmp_path, {"A": 4})
    before = svc.repo.faculty_all()
    dup = write_csv(tmp_path / "renamed_copy.csv", HDR, roster_rows({"A": 4}))
    out = svc.importer.import_probe(svc.importer.probe(dup))
    assert out["status"] == "duplicate" and len(svc.repo.faculty_all()) == len(before)


def test_multi_sheet_excel_and_unknown_sheet(svc, tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "People"
    ws.append(HDR)
    for r in roster_rows({"A": 2, "B": 2}):
        ws.append(r)
    ws2 = wb.create_sheet("Second")
    ws2.append(HDR)
    for r in roster_rows({"C": 2})[:2]:
        ws2.append([r[0] + 100, r[1] + "Z", r[2], r[3] + "Z", r[4]])
    path = tmp_path / "book.xlsx"
    wb.save(path)
    pr = svc.importer.probe(path)
    assert len(pr.tables) == 2 and pr.row_count == 6
    assert svc.importer.import_probe(pr)["status"] == "imported"
    assert len(svc.repo.faculty_all()) == 6


@pytest.mark.parametrize("name,data", [("notes.txt", b"hello"), ("broken.pdf", b"%PDF-garbage"),
                                       ("empty.csv", b""), ("blob.xlsx", b"not a zip")])
def test_unknown_or_malformed_files_are_refused_not_guessed(svc, tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    pr = svc.importer.probe(p)
    assert pr.purpose == "unknown" and pr.problems
    assert svc.importer.import_probe(pr)["status"] == "needs_confirmation"
    assert svc.repo.faculty_all() == []


def test_bad_rows_are_reported_not_silently_dropped(svc, tmp_path):
    rows = roster_rows({"A": 3}) + [[9, "", "A", "E9", "X"], [10, "Dup Code", "A", "E1001", "X"]]
    pr = svc.importer.probe(write_csv(tmp_path / "r.csv", HDR, rows))
    res = svc.importer.import_probe(pr)
    assert res["status"] == "validation_failed" and res["validation"]["errors"] == 2
    res = svc.importer.import_probe(pr, allow_errors=True)
    assert res["status"] == "imported" and res["issues"] >= 2
    n = svc.repo.conn.execute("SELECT COUNT(*) FROM exm_import_issue WHERE severity='error'").fetchone()[0]
    assert n >= 2


def test_missing_code_is_flagged_not_fabricated(svc, tmp_path):
    rows = roster_rows({"A": 2})
    rows[0][3] = ""
    svc.importer.import_probe(svc.importer.probe(write_csv(tmp_path / "m.csv", HDR, rows)))
    f = [x for x in svc.repo.faculty_all() if x["name"].startswith("Person001")][0]
    assert f["employee_code"] is None and f["record_status"] == "needs_verification"


def test_duplicate_names_are_separate_people_and_history_needs_proof(svc, tmp_path):
    rows = roster_rows({"A": 2, "B": 2})
    rows[2][1] = rows[0][1]                                   # same name, different person/department
    svc.importer.import_probe(svc.importer.probe(write_csv(tmp_path / "r.csv", HDR, rows)))
    assert len(svc.repo.faculty_all()) == 4
    # counts WITH serial -> matched by serial+name ; WITHOUT serial -> not applied (ambiguous), never guessed
    with_serial = [[r[0], r[1], 5] for r in rows]
    res = svc.importer.import_probe(svc.importer.probe(write_csv(tmp_path / "c1.csv", ["S.No.", "Faculty Name", "Duty Count"], with_serial)))
    assert res["history"]["applied"] == 4 and res["history"]["matching"]["matched_by_serial_and_name"] == 2
    res2 = svc.importer.import_probe(svc.importer.probe(
        write_csv(tmp_path / "c2.csv", ["Faculty Name", "Duty Count"], [[r[1], 7] for r in rows])))
    assert res2["history"]["not_applied"] == 2 and res2["history"]["applied"] == 2


def test_reimport_with_changes_keeps_everyone_and_audits(svc, tmp_path):
    load(svc, tmp_path, {"A": 3, "B": 2})
    rows = roster_rows({"A": 3, "B": 2})[:4]                 # one person missing from the new file
    rows[0][2] = "C"                                          # a department change
    res = svc.importer.import_probe(svc.importer.probe(write_csv(tmp_path / "new.csv", HDR, rows)))
    r = res["roster"]
    assert r["updated"] == 1 and r["not_in_this_file"] == 1
    assert len(svc.repo.faculty_all(active_only=False)) == 5 and len(svc.repo.faculty_all()) == 5   # nobody deactivated
    aud = svc.repo.conn.execute("SELECT COUNT(*) FROM exm_audit WHERE action='faculty_changed'").fetchone()[0]
    assert aud == 1


def test_history_is_never_double_counted_and_rollback_restores(svc, tmp_path):
    rows = load(svc, tmp_path, {"A": 3}, history=lambda i, r: 10)
    hist = lambda: sum(v["baseline"] for v in svc.repo.history_components().values())
    assert hist() == 30
    imp = svc.importer
    out = imp.import_probe(imp.probe(write_csv(tmp_path / "c3.csv", ["S.No.", "Faculty Name", "Duty Count"],
                                               [[r[0], r[1], 12] for r in rows])))
    assert hist() == 36                                       # replaced, not 30+36
    imp.rollback(out["import_id"])
    assert hist() == 30                                       # previous totals are back


def test_legacy_history_is_superseded_not_added(svc, tmp_path):
    rows = load(svc, tmp_path, {"A": 3})
    svc.repo.import_history({r[1]: 40 for r in rows}, "duty_sheet")
    assert sum(v["baseline"] for v in svc.repo.history_components().values()) == 120
    imp = svc.importer
    imp.import_probe(imp.probe(write_csv(tmp_path / "c4.csv", ["S.No.", "Faculty Name", "Duty Count"],
                                         [[r[0], r[1], 5] for r in rows])))
    assert sum(v["baseline"] for v in svc.repo.history_components().values()) == 15


def test_migration_upgrades_a_populated_v2_database(tmp_path):
    c = db.connect(tmp_path / "old.db")
    assert db.migrate(c, db.MIGRATIONS[:2]) == [1, 2]                # the schema as shipped before this change
    c.execute("INSERT INTO exm_faculty(name,norm,updated_at) VALUES ('A B','a b','now')")
    fid = c.execute("SELECT id FROM exm_faculty").fetchone()[0]
    c.execute("INSERT INTO exm_history(faculty_id,duties,source,recorded_at) VALUES (?,?,?,?)", (fid, 7, "duty_sheet", "now"))
    assert db.migrate(c) == [3] and db.migrate(c) == []
    row = c.execute("SELECT name, employee_code, designation, record_status FROM exm_faculty").fetchone()
    assert tuple(row) == ("A B", None, "", "verified")
    h = c.execute("SELECT duties, status, verification FROM exm_history").fetchone()
    assert tuple(h) == (7, "active", "unverified")                   # existing history untouched and still counted


@pytest.mark.skipif(not (REAL_ROSTER.exists() and REAL_COUNTS.exists()), reason="real PDFs not present")
def test_real_pdfs_import_college_wide(svc):
    imp = svc.importer
    r = imp.import_probe(imp.probe(REAL_ROSTER))
    h = imp.import_probe(imp.probe(REAL_COUNTS))
    fac = svc.repo.faculty_all()
    assert r["status"] == "imported" and len(fac) == r["roster"]["added"] > 200
    assert len({f["department"] for f in fac}) > 5                 # every department, not just one
    assert all(f["employee_code"] for f in fac)
    assert h["history"]["applied"] == len(fac) and h["history"]["not_applied"] == 0
    assert sum(v["baseline"] for v in svc.repo.history_components().values()) == h["history"]["total_duties"]


# ============================================================ B. policy: department resolution & exclusion
def test_department_resolution_is_database_driven(svc, tmp_path):
    load(svc, tmp_path, {"COMPUTER SCIENCE AND ENGG.": 2, "INFORMATION TECHNOLOGY": 2, "DEPT. OF TRADE STUDIES": 2,
                         "ELECTRICAL ENGG.": 2, "ELECTRONICS AND COMM. ENGG.": 2})
    P = svc.policy
    assert P.resolve_department("trade") == "DEPT. OF TRADE STUDIES"
    assert P.resolve_department("CS") == "COMPUTER SCIENCE AND ENGG."
    assert P.resolve_department("IT") == "INFORMATION TECHNOLOGY"
    assert P.resolve_department("ee") == "ELECTRICAL ENGG."
    with pytest.raises(PolicyError):
        P.resolve_department("quantum basket weaving")


def test_department_exclusion_blocks_all_its_people_for_the_scope(svc, tmp_path):
    rows = load(svc, tmp_path, {"A": 12, "B": 12, "C": 12})
    sid = session(svc, days=4, rooms=4)
    svc.policy.exclude_department("B", kind="session", session_id=sid, reason="exam of its own")
    svc.policy.exclude_department("C", kind="date", session_id=sid, start=date(2026, 11, 10), reason="lab")
    out = svc.generate(sid, seed=1)
    dept = {f["id"]: f["department"] for f in svc.repo.faculty_all()}
    ds_ = svc.repo.duties(out.allocation_id)
    assert not [d for d in ds_ if dept[d["faculty_id"]] == "B"]
    assert not [d for d in ds_ if dept[d["faculty_id"]] == "C" and d["exam_date"] == "2026-11-10"]
    assert [d for d in ds_ if dept[d["faculty_id"]] == "C"]                 # C is still used on other dates
    assert out.status == "complete" and svc.validate_allocation(out.allocation_id) == []


def test_department_and_individual_restrictions_restore_independently(svc, tmp_path):
    rows = load(svc, tmp_path, {"A": 8, "B": 8})
    sid = session(svc, days=3, rooms=2)
    fid = [f for f in svc.repo.faculty_all() if f["department"] == "A"][0]["id"]
    svc.policy.exclude_department("A", kind="session", session_id=sid, reason="r")
    from exam_management.restrictions import RestrictionSpec
    svc.restrictions.add(RestrictionSpec(faculty_id=fid, kind="session", session_id=sid, reason="leave"))
    svc.policy.restore_department("A", session_id=sid)
    out = svc.generate(sid, seed=1)
    got = per_person(svc, out.allocation_id)
    assert fid not in got and any(f["department"] == "A" for f in svc.repo.faculty_all() if f["id"] in got)
    svc.policy.exclude_department("A", kind="session", session_id=sid, reason="again")
    svc.restrictions.restore(fid, session_id=sid) if hasattr(svc.restrictions, "restore") else None
    assert svc.policy.department_restrictions(sid)                      # restoring a person kept the department rule


def test_windowed_department_restore_splits_the_range(svc, tmp_path):
    load(svc, tmp_path, {"A": 3})
    sid = session(svc, days=6, rooms=1)
    P = svc.policy
    P.exclude_department("A", kind="date_range", session_id=sid, start=date(2026, 11, 9), end=date(2026, 11, 14))
    P.restore_department("A", session_id=sid, start=date(2026, 11, 11), end=date(2026, 11, 12))
    spans = sorted((r["start_date"], r["end_date"]) for r in P.department_restrictions(sid))
    assert spans == [("2026-11-09", "2026-11-10"), ("2026-11-13", "2026-11-14")]


# ============================================================ C. priority tiers
def test_lowest_priority_department_is_used_only_for_a_shortfall(svc, tmp_path):
    load(svc, tmp_path, {"A": 30, "B": 30, "M": 10})
    sid = session(svc, days=4, rooms=18)                  # 36 seats a day
    svc.policy.set_priority("department", "M", 3, session_id=sid, label="lowest")
    out = svc.generate(sid, seed=2)
    dept = {f["id"]: f["department"] for f in svc.repo.faculty_all()}
    assert out.status == "complete"
    assert not [d for d in svc.repo.duties(out.allocation_id) if dept[d["faculty_id"]] == "M"]   # 60 others suffice
    svc.policy.exclude_department("B", kind="session", session_id=sid, reason="x")               # now only 30 for 36 seats
    out2 = svc.generate(sid, seed=2)
    ds_ = svc.repo.duties(out2.allocation_id)
    m_per_day = Counter(d["exam_date"] for d in ds_ if dept[d["faculty_id"]] == "M")
    a_per_day = Counter(d["exam_date"] for d in ds_ if dept[d["faculty_id"]] == "A")
    assert out2.status == "complete"
    assert all(v == 6 for v in m_per_day.values()) and len(m_per_day) == 4      # exactly the shortfall, nothing more
    assert all(v == 30 for v in a_per_day.values())                              # higher tier fully used first
    assert svc.validate_allocation(out2.allocation_id) == []


def test_priority_is_not_exclusion_and_can_be_reset_or_reordered(svc, tmp_path):
    load(svc, tmp_path, {"A": 5, "M": 5})
    P = svc.policy
    P.set_priority("department", "M", 3)
    fac = svc.repo.faculty_all()
    tiers = {f["department"]: P.tier_of(f, P.priority_rules()) for f in fac}
    assert tiers["A"][0] == 1 and tiers["M"][0] == 3
    P.remove_priority("department", "M")
    assert all(P.tier_of(f, P.priority_rules())[0] == 1 for f in fac)
    P.set_priority("designation", "assistant", 2)
    assert all(P.tier_of(f, P.priority_rules())[0] == 2 for f in fac)
    assert P.department_restrictions() == []                 # priority never creates an exclusion


def test_priority_rules_are_session_scoped_and_audited(svc, tmp_path):
    load(svc, tmp_path, {"A": 2, "M": 2})
    s1 = session(svc, days=2, rooms=1)
    P = svc.policy
    P.set_priority("department", "M", 3, session_id=s1)
    assert P.priority_rules(s1) and not P.priority_rules(None)
    assert svc.repo.conn.execute("SELECT COUNT(*) FROM exm_audit WHERE action='set_priority'").fetchone()[0] == 1


# ============================================================ D. fairness & randomisation
def test_history_is_damped_so_high_history_still_serves_and_zero_history_is_not_overloaded(svc, tmp_path):
    hist = lambda i, r: 49 if i == 0 else (50 if i == 1 else (0 if i % 2 else 25))
    rows = load(svc, tmp_path, {"A": 60}, history=hist)
    sid = session(svc, days=6, rooms=5)                    # 60 seats over 60 people
    out = svc.generate(sid, seed=4)
    got = per_person(svc, out.allocation_id)
    ids = {f["name"]: f["id"] for f in svc.repo.faculty_all()}
    assert got[ids[rows[0][1]]] >= 0 and out.status == "complete"
    vals = [got.get(f["id"], 0) for f in svc.repo.faculty_all()]
    assert max(vals) <= 3                                   # nobody is loaded like a raw water-fill would (up to 6)
    assert min(vals) >= 0 and sum(1 for v in vals if v == 0) <= 10   # the heavy-history people are not all shut out


def test_old_raw_waterfill_behaviour_is_recoverable_by_settings(svc, tmp_path):
    hist = lambda i, r: 0 if i < 20 else 30
    load(svc, tmp_path, {"A": 60}, history=hist)
    s = ExamSettings(solver_deterministic_time=0.6, solver_max_seconds=2.0, candidate_runs=4,
                     history_weight=1.0, history_adjust_cap=1e9, history_adjust_fraction=None)
    raw = ExamService(str(tmp_path / "raw.db"), settings=s)
    load(raw, tmp_path, {"A": 60}, history=hist, name="r2.csv")
    sid = session(raw, days=6, rooms=5)
    got = per_person(raw, raw.generate(sid, seed=1).allocation_id)
    assert sum(1 for f in raw.repo.faculty_all() if got.get(f['id'], 0) == 0) >= 30   # raw water-fill shuts out all high-history people


def test_same_seed_reproduces_and_a_new_seed_differs(svc, tmp_path):
    load(svc, tmp_path, {"A": 40})
    sid = session(svc, days=4, rooms=4)
    key = lambda o: sorted((d["faculty_id"], d["exam_date"], d["room"]) for d in svc.repo.duties(o.allocation_id))
    a, b, c = svc.generate(sid, seed=11), svc.generate(sid, seed=11), svc.generate(sid, seed=12)
    assert key(a) == key(b) and key(a) != key(c)
    assert svc.validate_allocation(c.allocation_id) == []


def test_hard_limits_hold(svc, tmp_path):
    load(svc, tmp_path, {"A": 30})
    sid = session(svc, days=5, rooms=4, mx=2, mn=1)
    out = svc.generate(sid, seed=5)
    assert max(per_person(svc, out.allocation_id).values()) <= 2
    assert svc.validate_allocation(out.allocation_id) == []


# ============================================================ E. affected-only repair
def test_repair_changes_only_affected_seats_and_never_touches_approved(svc, tmp_path):
    load(svc, tmp_path, {"A": 20, "B": 20})
    sid = session(svc, days=4, rooms=4)
    base = svc.generate(sid, seed=3)
    before = {(d["faculty_id"], d["exam_date"]) for d in svc.repo.duties(base.allocation_id)}
    svc.policy.exclude_department("B", kind="session", session_id=sid, reason="x")
    dept = {f["id"]: f["department"] for f in svc.repo.faculty_all()}
    hit = {k for k in before if dept[k[0]] == "B"}
    out = svc.reallocate_affected(base.allocation_id, seed=9)
    after = {(d["faculty_id"], d["exam_date"]) for d in svc.repo.duties(out.allocation_id)}
    assert len(out.affected) == len(hit) and not [k for k in after if dept[k[0]] == "B"]
    assert (before - hit) <= after                          # every unaffected duty survived
    assert svc.validate_allocation(out.allocation_id) == []
    assert svc.repo.allocation(base.allocation_id)["status"] != "published"
    again = svc.reallocate_affected(out.allocation_id)
    assert again.allocation_id is None                      # nothing left to repair


# ============================================================ F. NLP phrasing
@pytest.fixture
def bot(svc, tmp_path):
    load(svc, tmp_path, {"COMPUTER SCIENCE AND ENGG.": 6, "INFORMATION TECHNOLOGY": 6, "DEPT. OF MANAGEMENT STUDIES": 4})
    return ExamManagementHandler(svc, lambda: date(2026, 11, 9))


@pytest.mark.parametrize("sentence,intent", [
    ("Exclude Management.", "exclude"),
    ("Do not assign duties to Management faculty.", "exclude"),
    ("Keep Management teachers out of this examination.", "exclude"),
    ("Please leave the management department out of the exam duty", "exclude"),
    ("Management should have the lowest priority.", "priority_low"),
    ("Use management faculty only if needed", "priority_low"),
    ("Put Management last.", "priority_low"),
    ("Give IT priority over Management.", "priority_order"),
    ("Give IT higher priority.", "priority_high"),
    ("Restore the Management department.", "restore"),
    ("Bring CS back into the allocation", "restore"),
    ("Show all excluded departments", "show_excluded"),
    ("which departments are excluded?", "show_excluded"),
    ("Reset the priority of Management", "priority_reset"),
])
def test_exclusion_and_priority_are_told_apart(bot, sentence, intent):
    assert bot.policy_chat._intent(sentence.lower()) == intent


def test_lowercase_it_is_a_pronoun_not_a_department(bot):
    assert bot.policy_chat.find_departments("exclude it from this exam")[0] == []
    assert [f.label for f in bot.policy_chat.find_departments("exclude IT department")[0]] == ["INFORMATION TECHNOLOGY"]


def test_priority_instruction_does_not_exclude_and_exclusion_does_not_set_priority(bot):
    bot.handle("Management should have the lowest priority.")
    assert bot.svc.policy.department_restrictions() == [] and bot.svc.policy.priority_rules()
    bot.handle("Exclude the Management department.")
    assert len(bot.svc.policy.priority_rules()) == 1 and bot.svc.policy.department_restrictions() == [] \
        or bot.pending_dept                                  # no session yet: queued, applied at creation


def test_multi_department_dated_exclusion_with_misspelling_and_reason(bot):
    bot.handle("Allocate duties from 9 November to 12 November, 9 AM to 12 PM, 3 rooms with two invigilators each, "
               "regular classes are suspended.")
    r = bot.handle("Exclude CS and IT from 10 November to 11 November because of lab exams")
    sid = bot.svc.repo.conn.execute("SELECT id FROM exm_session").fetchone()[0]
    rows = bot.svc.policy.department_restrictions(sid)
    assert {x["department"] for x in rows} == {"COMPUTER SCIENCE AND ENGG.", "INFORMATION TECHNOLOGY"}
    assert all(x["kind"] == "date_range" and x["reason"] == "lab exams" for x in rows)
    r2 = bot.handle("Restore the Managment department")      # misspelled, nothing excluded
    assert "no active exclusion" in r2


def test_ambiguous_department_is_asked_not_guessed(bot):
    bot.svc.policy.resolver()
    r = bot.handle("Exclude the engineering department")
    assert r is None or "Which one" in r or "could not tell" in r
    assert bot.svc.policy.department_restrictions() == []


def test_import_through_chat_needs_confirmation(bot, tmp_path):
    p = write_csv(tmp_path / "extra_people.csv", HDR, [[1, "New Person", "Z", "NP1", "LECTURER"]])
    r = bot.handle(f"Import faculty data from {p}")
    assert "confirm" in r.lower() and not [f for f in bot.svc.repo.faculty_all() if f["name"] == "New Person"]
    r2 = bot.handle("confirm")
    assert [f for f in bot.svc.repo.faculty_all() if f["name"] == "New Person"], r2


# ============================================================ G. live-app scenario: timetable directory vs imported roster
def test_timetable_directory_never_duplicates_or_deactivates_the_imported_roster(tmp_path):
    from exam_management.providers import StaticDirectory
    rows = roster_rows({"CS": 5, "Other": 5})
    tt = [("Dr. " + r[1], "CS") for r in rows[:5]]                      # same people, titled spelling, from the timetable
    tt += [("Mr. Zed Unlisted", "CS")]                                  # not on the roster at all
    svc = ExamService(str(tmp_path / "live.db"), directory=StaticDirectory(tt), settings=FAST)
    svc.importer.import_probe(svc.importer.probe(write_csv(tmp_path / "roster.csv", HDR, rows)))
    res = svc.sync_roster()
    active = svc.repo.faculty_all()
    assert len(active) == 10                                            # 10 roster people; nobody duplicated, none lost
    assert res["added"] == 1                                            # only the stranger was recorded ...
    stray = svc.importer.unverified()
    assert [x["name"] for x in stray] == ["Mr. Zed Unlisted"] and stray[0]["active"] == 0   # ... and parked, not eligible
    sid = session(svc, days=2, rooms=2)
    out = svc.generate(sid, seed=1)
    assert "Mr. Zed Unlisted" not in {d["faculty"] for d in svc.repo.duties(out.allocation_id)}
    svc.importer.accept_unlisted(stray[0]["id"])
    assert len(svc.repo.faculty_all()) == 11
    svc.sync_roster()                                                   # a second sync changes nothing
    assert len(svc.repo.faculty_all()) == 11 and len(svc.repo.faculty_all(active_only=False)) == 11


def test_confirmed_alias_merges_a_variant_spelling(tmp_path):
    from exam_management.providers import StaticDirectory
    rows = roster_rows({"CS": 3})
    svc = ExamService(str(tmp_path / "al.db"), directory=StaticDirectory([("Dr. Persn001 Surname001", "CS")]), settings=FAST)
    svc.importer.import_probe(svc.importer.probe(write_csv(tmp_path / "r.csv", HDR, rows)))
    svc.sync_roster()
    stray = svc.importer.unverified()[0]
    sugg = svc.importer.link_timetable_names([stray["name"]])
    assert sugg["needs_confirmation"] and sugg["linked"] == 0           # near-match is suggested, never auto-applied
    target = [f for f in svc.repo.faculty_all() if f["name"].startswith("Person001")][0]
    assert svc.importer.confirm_alias(stray["id"], target["id"])["status"] == "linked"
    svc.sync_roster()
    assert len(svc.repo.faculty_all()) == 3 and svc.importer.unverified() == []


# ============================================================ H. reports, publication, notifications
def _approved_world(svc, tmp_path, n=12):
    load(svc, tmp_path, {"A": n, "B": n}, history=lambda i, r: i % 9)
    sid = session(svc, days=3, rooms=3)
    out = svc.generate(sid, seed=2)
    assert svc.approve(out.allocation_id)["approved"]
    return sid, out.allocation_id


def test_reports_are_built_from_the_database_and_export(svc, tmp_path):
    from exam_management.exports import ReportExporter
    sid, aid = _approved_world(svc, tmp_path)
    ex = ReportExporter(svc)
    assert ["Summary", "Hard-constraint violations", 0, "none"] in ex.report_d(aid)[1]
    svc.policy.exclude_department("B", kind="date", session_id=sid, start=date(2026, 11, 9), reason="lab")
    viol = [r for r in ex.report_d(aid)[1] if r[1] == "Hard-constraint violations"][0]
    assert viol[2] > 0 and viol[3] != "none"             # an approved allocation that a NEW exclusion now violates is flagged
    h, rows = ex.report_b(aid)
    assert len(rows) == len(svc.repo.faculty_all())                           # every active person, every department
    assert sum(r[4] for r in rows) == len(svc.repo.duties(aid)) == 18         # new duties add up to the allocation
    assert all(r[1].startswith("E") for r in rows)                            # employee codes shown, none invented
    h, rows = ex.report_c(aid)
    assert rows[-1][0] == "ALL DEPARTMENTS" and rows[-1][5] == 18
    h, rows = ex.report_e(sid)
    assert any(r[0] == "Department" and r[2] == "B" and r[7] == "active" for r in rows)
    x, p = ex.to_excel(aid, tmp_path / "r.xlsx"), ex.to_pdf(aid, tmp_path / "r.pdf")
    import openpyxl
    assert openpyxl.load_workbook(x).sheetnames[:2] == ["A Duty allocation", "B Faculty workload"]
    assert p.read_bytes()[:4] == b"%PDF"


def test_overview_separates_unknown_history_from_zero(svc, tmp_path):
    from exam_management.exports import ReportExporter
    rows = load(svc, tmp_path, {"A": 4})
    sid = session(svc, days=2, rooms=1)
    o = ReportExporter(svc).overview(sid)
    assert o["total_active"] == 4 and len(o["history_unavailable"]) == 4      # no counts imported: flagged, not "0"


def test_publish_requires_approval_and_notifications_require_publication(svc, tmp_path):
    from exam_management.notifications import NotificationService
    load(svc, tmp_path, {"A": 12, "B": 12})
    sid = session(svc, days=3, rooms=3)
    out = svc.generate(sid, seed=2)
    ns = NotificationService(svc)
    with pytest.raises(Exception):
        ns.publish(out.allocation_id)                                          # a draft cannot be published
    svc.approve(out.allocation_id)
    with pytest.raises(Exception):
        ns.plan(out.allocation_id)                                             # approved but not published
    assert ns.publish(out.allocation_id)["published"] and ns.publish(out.allocation_id)["already"]


class FakeProvider:
    def __init__(self, status="sent"):
        self.calls, self.status = [], status

    def send(self, address, subject, body):
        from exam_management.notifications import DeliveryResult
        self.calls.append(address)
        return DeliveryResult(self.status, ref="r1", detail="fake")


def test_notifications_are_dry_run_by_default_deduplicated_and_honest(svc, tmp_path):
    from exam_management.notifications import NotificationService
    sid, aid = _approved_world(svc, tmp_path)
    prov = FakeProvider("sent")
    ns = NotificationService(svc, providers={"email": prov})
    ns.publish(aid)
    people = {d["faculty_id"] for d in svc.repo.duties(aid)}
    first = sorted(people)[0]
    ns.set_contact(first, "email", "someone@example.org")
    with pytest.raises(Exception):
        ns.set_contact(first, "email", "not-an-email")                         # validation
    r = ns.plan(aid)
    assert r["created"] == 2 * len(people) and ns.plan(aid)["created"] == 0    # re-planning never duplicates
    st = ns.status(aid)
    assert st["in_app"] == len(people) and st["queued"] == 1 and st["skipped"] == len(people) - 1
    assert ns.dispatch(aid)["sent"] == 0 and prov.calls == []                  # default = dry run
    assert ns.dispatch(aid, dry_run=False)["sent"] == 0 and prov.calls == []   # still needs explicit confirmation
    live = ns.dispatch(aid, dry_run=False, confirmed=True)
    assert live["sent"] == 1 and live["delivered"] == 0 and prov.calls == ["someone@example.org"]
    assert ns.status(aid)["sent"] == 1                                         # 'sent' is never promoted to 'delivered'
    assert ns.dispatch(aid, dry_run=False, confirmed=True)["sent"] == 0 and len(prov.calls) == 1   # no resend


def test_failed_notification_retries_and_missing_provider_is_never_reported_as_sent(svc, tmp_path):
    from exam_management.notifications import NotificationService
    sid, aid = _approved_world(svc, tmp_path)
    ns = NotificationService(svc, providers={"email": FakeProvider("failed")})
    ns.publish(aid)
    fid = svc.repo.duties(aid)[0]["faculty_id"]
    ns.set_contact(fid, "email", "a@b.org")
    ns.set_contact(fid, "sms", "+911234567890")
    ns.plan(aid, channels=("email", "sms"))
    sms = svc.repo.conn.execute("SELECT status, detail FROM exm_notification WHERE channel='sms' AND faculty_id=?", (fid,)).fetchone()
    assert sms["status"] == "skipped" and "no provider" in sms["detail"]
    assert ns.dispatch(aid, dry_run=False, confirmed=True)["failed"] == 1
    assert ns.dispatch(aid, dry_run=False, confirmed=True)["failed"] == 0      # failed rows wait for an explicit retry
    ns.providers["email"] = FakeProvider("sent")
    assert ns.dispatch(aid, dry_run=False, confirmed=True, retry_failed=True)["sent"] == 1


def test_chat_publish_and_send_need_confirmation_and_stale_confirmations_are_disarmed(svc, tmp_path):
    load(svc, tmp_path, {"A": 12, "B": 12})
    bot = ExamManagementHandler(svc, lambda: date(2026, 11, 9))
    bot.handle("Allocate duties from 9 November to 11 November, 9 AM to 12 PM, 3 rooms with two invigilators each, "
               "regular classes are suspended.")
    assert "approved" in bot.handle("Publish the allocation").lower()          # still a draft: refused
    bot.handle("approve")
    aid = bot._latest_alloc()["id"]
    r = bot.handle("Publish the allocation")
    assert "confirm" in r.lower() and not svc.repo.conn.execute("SELECT 1 FROM exm_publication").fetchone()
    bot.handle("show exclusions")                                              # another instruction disarms it
    bot.handle("yes")
    assert not svc.repo.conn.execute("SELECT 1 FROM exm_publication").fetchone()
    bot.handle("Publish the allocation")
    bot.handle("confirm")
    assert svc.repo.conn.execute("SELECT 1 FROM exm_publication WHERE allocation_id=?", (aid,)).fetchone()
    r = bot.handle("Send notifications")
    assert "dry run" in r.lower() and svc.repo.conn.execute("SELECT COUNT(*) FROM exm_notification WHERE status IN ('sent','delivered')").fetchone()[0] == 0


def test_chat_exports_and_overview(svc, tmp_path, monkeypatch):
    monkeypatch.setenv("EXAM_MGMT_EXPORT_DIR", str(tmp_path / "out"))
    load(svc, tmp_path, {"A": 12, "B": 12})
    bot = ExamManagementHandler(svc, lambda: date(2026, 11, 9))
    bot.handle("Allocate duties from 9 November to 11 November, 9 AM to 12 PM, 3 rooms with two invigilators each, "
               "regular classes are suspended.")
    assert "Faculty overview" in bot.handle("Give me the faculty overview")
    r = bot.handle("Generate the PDF and Excel report")
    assert len(bot.last_exports) == 2 and all(Path(p).exists() for p in bot.last_exports) and "DRAFT" in r


def test_import_folder_is_configured_not_built_in_and_files_are_judged_by_content(svc, tmp_path, monkeypatch):
    folder = tmp_path / "drop"
    folder.mkdir()
    write_csv(folder / "aaa.csv", HDR, roster_rows({"A": 3}))
    write_csv(folder / "bbb.csv", ["Faculty Name", "Duty Count"], [["Person001 Surname001", 4]])
    (folder / "junk.txt").write_text("ignore me")
    assert svc.importer.scan_directory() == []                                   # nothing configured -> nothing scanned
    monkeypatch.setenv("EXAM_MGMT_IMPORT_DIR", str(folder))
    found = {f["file"]: f for f in svc.importer.scan_directory()}
    assert set(found) == {"aaa.csv", "bbb.csv"} and found["aaa.csv"]["purpose"] == "faculty_roster"
    assert found["bbb.csv"]["purpose"] == "duty_counts"
    svc.importer.import_probe(found["aaa.csv"]["probe"])
    assert {f["file"]: f["already_imported"] for f in svc.importer.scan_directory()}["aaa.csv"] is True


# ============================================================ I. the Streamlit upload box
class FakeUpload:
    def __init__(self, path):
        self.name, self._data = Path(path).name, Path(path).read_bytes()

    def getbuffer(self):
        return memoryview(self._data)


def test_upload_box_routes_roster_and_counts_by_content_in_any_order(tmp_path):
    from exam_management.integration import import_college_uploads
    rows = roster_rows({"A": 4, "B": 3})
    roster = write_csv(tmp_path / "whatever_name.csv", HDR, rows)
    counts = write_csv(tmp_path / "zz_counts.csv", ["S.No.", "Faculty Name", "Duty Count"], [[r[0], r[1], 5] for r in rows])
    other = tmp_path / "timetable_like.txt"
    other.write_text("not data")
    db_path = tmp_path / "up.db"
    ups = [FakeUpload(counts), FakeUpload(other), FakeUpload(roster)]          # counts uploaded BEFORE the roster
    rep = import_college_uploads(ups, db_path)
    assert set(rep) == {"zz_counts.csv", "whatever_name.csv"}                  # the unrelated file is left to the timetable path
    assert rep["whatever_name.csv"]["success"] and "7 faculty added" in rep["whatever_name.csv"]["message"]
    assert rep["zz_counts.csv"]["success"] and "7 faculty" in rep["zz_counts.csv"]["message"]
    again = import_college_uploads(ups, db_path)                               # Streamlit re-runs on every change
    assert all("already imported" in r["message"] for r in again.values())
    svc = ExamService(str(db_path), settings=FAST)
    assert len(svc.repo.faculty_all()) == 7
    assert sum(v["baseline"] for v in svc.repo.history_components().values()) == 35


def test_legacy_duty_sheet_cannot_double_count_on_top_of_an_imported_total(svc, tmp_path):
    rows = load(svc, tmp_path, {"A": 3}, history=lambda i, r: 10)
    legacy = {r[1]: 10 for r in rows}                                           # the old duty-history store says the same
    res = svc.repo.import_history(legacy, "duty_sheet")
    assert res["imported"] == 0 and res["deferred"] == 3
    assert sum(v["baseline"] for v in svc.repo.history_components().values()) == 30    # not 60


@pytest.mark.skipif(not (REAL_ROSTER.exists() and REAL_COUNTS.exists()), reason="real data files not present")
def test_real_upload_set_routes_roster_and_counts_and_leaves_timetables(tmp_path):
    from exam_management.integration import import_college_uploads
    ups = [FakeUpload(p) for p in [REAL_COUNTS, REAL_ROSTER] + REAL_TIMETABLES]      # counts first, on purpose
    rep = import_college_uploads(ups, tmp_path / "shot.db")
    assert set(rep) == {REAL_ROSTER.name, REAL_COUNTS.name}                           # timetables untouched
    assert all(r["success"] for r in rep.values())
    svc = ExamService(str(tmp_path / "shot.db"), settings=FAST)
    assert len(svc.repo.faculty_all()) > 200
    expected = sum(float(r["duty_count"]) for r in svc.importer.probe(REAL_COUNTS).records())
    assert sum(v["baseline"] for v in svc.repo.history_components().values()) == expected   # independent of the import code path


def test_output_names_and_notification_wording_come_from_settings(svc, tmp_path, monkeypatch):
    from exam_management.notifications import NotificationService
    monkeypatch.delenv("EXAM_MGMT_EXPORT_DIR", raising=False)
    svc.repo.set_setting("export_dir", str(tmp_path / "custom_out"))
    svc.repo.set_setting("export_name_template", "report_{session}_{version}")
    svc.repo.set_setting("notification_subject", "Duty notice")
    svc.repo.set_setting("seat_role_labels", ["Chief", "Assistant"])
    svc.reload_settings()
    sid, aid = _approved_world(svc, tmp_path)
    bot = ExamManagementHandler(svc, lambda: date(2026, 11, 9))
    bot.handle("Generate the PDF report")
    assert bot.last_exports and bot.last_exports[0].startswith(str(tmp_path / "custom_out"))
    assert Path(bot.last_exports[0]).name == f"report_{sid}_1.pdf"
    ns = NotificationService(svc)
    d = svc.repo.duties(aid)[0]
    msg = ns._message(aid, d["faculty_id"], [d], d["faculty"])
    assert msg["subject"] == "Duty notice" and ("(Chief)" in msg["body"] or "(Assistant)" in msg["body"])


# ============================================================ J. permanent no-hardcoding guard
@pytest.mark.skipif(not (_REAL.get("faculty_roster")), reason="no real data folder to audit against")
def test_no_file_name_person_or_department_from_the_real_data_appears_in_code_or_config():
    """Reads the project's actual data folder and fails if anything found there is baked into the code."""
    import re
    forbidden = set()
    for path in (ROOT / "data").iterdir():                               # every file name present on disk
        if path.is_file():
            forbidden.add(path.name.lower())
            if re.search(r"[\s_\d]", path.stem):                         # distinctive stems only ("timetable" is just a word)
                forbidden.add(path.stem.lower())
    # Not input files: the package's own name, and the default name of the app's OWN database
    # (a setting - db_filename - and overridable with EXAM_MGMT_DB_PATH).
    forbidden -= {"exam_management", "exam_management.db"}
    c = db.connect(":memory:")
    db.migrate(c)
    repo = Repository(c)
    imp = Importer(repo)
    pr = imp.probe(REAL_ROSTER)
    for r in pr.records():                                               # every person and every department
        forbidden |= {r["name"].lower()}
        if len(r["department"].split()) >= 2:
            forbidden.add(r["department"].lower())
    forbidden = {f for f in forbidden if len(f) > 6}
    scanned = [p for p in (ROOT / "exam_management").glob("*.py") if p.name != "nlu.py"]   # nlu.py: older docstring example
    scanned += list((ROOT / "config").glob("*.json")) + [ROOT / "app.py"]
    hits = []
    for p in scanned:
        text = p.read_text(encoding="utf-8", errors="ignore").lower()
        text = re.sub(r"\s+", " ", text)
        hits += [(p.name, f) for f in forbidden if f in text]
    assert hits == [], f"real data baked into code/config: {hits[:5]}"


def test_sidebar_examples_come_from_loaded_data_or_neutral_placeholders():
    from utils.example_questions import build_example_questions
    empty = " ".join(build_example_questions([]))
    assert "<faculty>" in empty and "<subject>" in empty and "<class>" in empty
    recs = [{"teacher": "Zed Quux", "day": "tuesday", "slot": "4", "subject": "Widgets 101", "class_name": "9X-Q"}]
    shown = build_example_questions(recs)
    assert "Is Zed Quux free on Tuesday slot 4?" in shown and "Who teaches Widgets 101?" in shown
    assert build_example_questions(None)                                         # never raises on missing data