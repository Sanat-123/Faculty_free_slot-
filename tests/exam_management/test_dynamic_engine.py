"""
Allocation correctness behind the natural-language layer: who may be used, how counts are judged, that every date and
shift is its own session, that validation really re-checks the data, and that nothing is hardcoded.
"""
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import pytest

from exam_management import dashboard as dash
from exam_management import official_export as oe
from exam_management import validation as vl
from exam_management.chat_handler import ExamManagementHandler
from tests.exam_management import dataset as ds
from tests.exam_management.conftest import FAST, World

TODAY = date(2026, 11, 6)
EXAM_DAY = date(2026, 11, 10)                                  # a Tuesday
WEEKDAY = EXAM_DAY.strftime("%A")


class ClassTT:
    """Teaching table: name -> [(weekday, start, end, class)].  People absent from it have NO timetable on record."""

    def __init__(self, table):
        self.table = table

    def busy(self, on, start, end):
        return self.busy_outside(on, start, end, ())

    def busy_outside(self, on, start, end, suspended):
        out = set()
        for name, rows in self.table.items():
            for wd, s, e, cls in rows:
                if wd == on.strftime("%A") and s < end and start < e and cls not in set(suspended):
                    out.add(name)
        return out

    def known_names(self):
        return set(self.table)


class Catalog:
    def __init__(self, classes):
        self.c = classes                                      # {"3A": ("Mechanical", 3), ...}

    def classes(self):
        return sorted(self.c)

    def semester_of(self, name):
        return self.c[name][1]

    def department_of(self, name):
        return self.c[name][0]


@pytest.fixture
def college(tmp_path):
    rows = ds.roster(12)
    mech = [n for n, d in rows if d == "Mechanical"]
    sem3, sem5 = mech[:4], mech[4:8]
    table = {n: [(WEEKDAY, "09:00", "12:00", "3M")] for n in sem3}
    table.update({n: [(WEEKDAY, "09:00", "12:00", "5M")] for n in sem5})
    w = World(tmp_path / "e.db", rows, settings=FAST.with_overrides(own_all_allocation_requests=True), timetable=ClassTT(table))
    bot = ExamManagementHandler(w.svc, lambda: TODAY, catalog=Catalog({"3M": ("Mechanical", 3), "5M": ("Mechanical", 5)}))
    return type("C", (), {"w": w, "bot": bot, "sem3": sem3, "sem5": sem5, "rows": rows, "mech": mech})()


def assigned(bot):
    aid = bot._latest_alloc()["id"]
    return [d["faculty"] for d in bot.repo.duties(aid)]


# ------------------------------------------------------------------------------------------- scoped suspension
def test_only_the_examined_semester_stops_teaching(college):
    c = college
    c.bot.handle("Exam of 3rd semester on 10 November 9 to 12, 2 rooms, 1 per room, Mechanical faculty")
    got = set(assigned(c.bot))
    assert got and got <= set(c.sem3)                          # sem-3 teachers are free; sem-5 teachers still teach
    d = c.bot.consume_dashboard()
    rows = {r[0]: r for r in d["eligibility"]["rows"]}
    for n in c.sem5:
        assert "Teaching" in rows[n][3] or n in got is False


def test_faculty_without_a_timetable_are_unverified_not_free(college):
    c = college
    c.bot.handle("Exam of 3rd semester on 10 November 9 to 12, 6 rooms, 2 per room")
    d = c.bot.consume_dashboard()
    unknown = [n for n, _ in c.rows if n not in c.sem3 + c.sem5]
    assert not set(assigned(c.bot)) & set(unknown)
    status = {r[0]: r[2] for r in d["eligibility"]["rows"]}
    assert all(status[n] == "Unverified" for n in unknown[:10])
    assert d["unfilled"]["rows"], "12 seats cannot be filled from 4 verified people"
    assert any(x["check"].startswith("Every room") and x["status"] == "Failed" for x in d["validation"])


def test_unverified_faculty_can_be_included_but_validation_says_not_verified(college):
    c = college
    c.bot.handle("Exam of 3rd semester on 10 November 9 to 12, 6 rooms, 2 per room. "
                 "Include faculty whose timetable is missing.")
    d = c.bot.consume_dashboard()
    assert set(assigned(c.bot)) - set(c.sem3)                  # unknown people were used
    chk = {x["check"]: x for x in d["validation"]}["Teaching timetable conflicts"]
    assert chk["status"] == "Not verified" and "no timetable on record" in chk["detail"]


# ------------------------------------------------------------------------------------------- duty count rule
def prior(c, name):
    comps = c.w.svc.repo.history_components()
    return comps.get(c.w.ids[name], {}).get("baseline", 0.0)


def test_required_duty_rule_excludes_everyone_who_fails_it(college):
    c = college
    limit = 5
    c.bot.handle(f"Exam on 10 November 9 to 12, 8 rooms, 2 per room. Regular classes are suspended. "
                 f"Assign faculty who have a duty count less than {limit}.")
    got = assigned(c.bot)
    assert got and all(prior(c, n) < limit for n in got)
    d = c.bot.consume_dashboard()
    barred = [r for r in d["eligibility"]["rows"] if prior(c, r[0]) >= limit]
    assert barred and all(r[2] == "Not eligible" and "Duty count" in r[3] for r in barred)
    assert {x["check"]: x["status"] for x in d["validation"]}["Duty-count condition"] == "Passed"


def test_preferred_duty_rule_is_used_first_but_never_blocks(college):
    c = college
    c.bot.handle("Exam on 10 November 9 to 12, 3 rooms, 2 per room. Regular classes are suspended. "
                 "Give preference to faculty having fewer than 6 duties.")
    assert all(prior(c, n) < 6 for n in assigned(c.bot))       # enough low-count people exist
    many = sum(1 for n, _ in c.rows if prior(c, n) < 6)
    c.bot.handle(f"Exam on 17 November 9 to 12, {many}, rooms, 2 per room. Regular classes are suspended. "
                 "Prefer faculty having fewer than 6 duties.".replace(f"{many}, rooms", f"{many} rooms"))
    assert len(assigned(c.bot)) == many * 2                    # nobody blocked: the shortage went to others, all seats filled


def test_a_total_duty_cap_is_enforced_per_person(college):
    c = college
    c.bot.handle("Exam on 10, 11, 12 and 13 November 9 to 12, 10 rooms, 2 per room. Regular classes are suspended. "
                 "Nobody should exceed 9 duties in total.")
    total = Counter(assigned(c.bot))
    assert total and all(prior(c, n) + k <= 9 for n, k in total.items())
    d = c.bot.consume_dashboard()
    assert {x["check"]: x["status"] for x in d["validation"]}["Duty-count condition"] == "Passed"


def test_lowest_duty_first_favours_low_history(college):
    c = college
    c.bot.handle("Exam on 10 November 9 to 12, 4 rooms, 2 per room. Regular classes are suspended. "
                 "Select faculty with the lowest duty count.")
    got = assigned(c.bot)
    ranked = sorted((prior(c, n) for n, _ in c.rows))
    assert max(prior(c, n) for n in got) <= ranked[len(got) + 2]


# ------------------------------------------------------------------------------------------- dates x shifts
def test_every_date_and_shift_is_its_own_session(college):
    c = college
    c.bot.handle("Exam on 10, 11 and 12 November, morning 9 to 12 and afternoon 2 to 5. 5 rooms, 2 per room. "
                 "Regular classes are suspended. Nobody should have two duties on the same day.")
    d = c.bot.consume_dashboard()
    assert len(d["roster"]) == 3 and all(len(day["shifts"]) == 2 for day in d["roster"])
    assert {c_["label"]: c_["value"] for c_ in d["cards"]}["Total shifts"].startswith("6")
    aid = c.bot._latest_alloc()["id"]
    per_slot = defaultdict(list)
    per_day = defaultdict(list)
    for x in c.bot.repo.duties(aid):
        per_slot[(x["exam_date"], x["shift"])].append(x["faculty_id"])
        per_day[x["exam_date"]].append(x["faculty_id"])
    assert len(per_slot) == 6
    assert all(len(v) == len(set(v)) for v in per_slot.values())
    assert all(len(v) == len(set(v)) for v in per_day.values())          # the same-day limit was applied


# ------------------------------------------------------------------------------------------- dashboard = database
def test_dashboard_numbers_come_from_the_database(college):
    c = college
    c.bot.handle("Exam of 3rd semester on 10 and 11 November 9 to 12, 3 rooms, 2 per room. Include faculty whose timetable is missing.")
    d = c.bot.consume_dashboard()
    aid = c.bot._latest_alloc()["id"]
    duties, unfilled = c.bot.repo.duties(aid), c.bot.repo.unfilled(aid)
    cards = {x["label"]: x["value"] for x in d["cards"]}
    assert cards["Positions required"] == 2 * 3 * 2
    assert cards["Positions assigned"] == len(duties) and cards["Positions unfilled"] == len(unfilled)
    rows = d["duty_distribution"]["rows"]
    assert sum(r[3] for r in rows) == len(duties)
    for name, _, previous, new, total in rows:
        assert previous + new == total and previous == prior(c, name)
    shown = [cell for day in d["roster"] for sh in day["shifts"] for r in sh["rows"] for cell in r[1::2]]
    assert sum(1 for x in shown if "UNFILLED" not in x) == len(duties)


# ------------------------------------------------------------------------------------------- validation really re-checks
def build_roster(c):
    c.bot.handle("Exam on 10 November 9 to 12, 3 rooms, 2 per room. Regular classes are suspended.")
    c.bot.consume_dashboard()
    return c.bot._latest_alloc()["id"]


def test_validation_detects_overlapping_assignments_across_shifts(college):
    """The database already forbids one person twice in a shift; overlapping SHIFTS (9-12 and 11-1) are checked by time."""

    c = college
    c.bot.handle("Exam on 10 November, shift A 9 to 12 and shift B 11 to 1. 3 rooms, 2 per room. Regular classes are suspended.")
    aid = c.bot._latest_alloc()["id"]
    rows = c.bot.repo.duties(aid)
    a_row = rows[0]
    b_row = next(r for r in rows if r["shift_id"] != a_row["shift_id"] and r["faculty_id"] != a_row["faculty_id"])
    c.bot.repo.conn.execute("UPDATE exm_duty SET faculty_id=? WHERE id=?", (a_row["faculty_id"], b_row["id"]))
    checks = {x.check: x for x in vl.validate_allocation(c.w.svc, aid)}
    assert checks["No faculty in two rooms at the same time"].status == "Failed"


def test_the_database_itself_forbids_a_duplicate_in_one_shift(college):
    import sqlite3
    c = college
    aid = build_roster(c)
    rows = c.bot.repo.duties(aid)
    same_shift = [r for r in rows if r["shift_id"] == rows[0]["shift_id"] and r["faculty_id"] != rows[0]["faculty_id"]]
    with pytest.raises(sqlite3.IntegrityError):
        c.bot.repo.conn.execute("UPDATE exm_duty SET faculty_id=? WHERE id=?", (rows[0]["faculty_id"], same_shift[0]["id"]))


def test_validation_detects_a_teaching_clash_the_solver_did_not_see(tmp_path):
    rows = ds.roster(6)
    table = {rows[0][0]: [(WEEKDAY, "09:00", "12:00", "5M")]}
    w = World(tmp_path / "v.db", rows, settings=FAST.with_overrides(own_all_allocation_requests=True), timetable=ClassTT(table))
    bot = ExamManagementHandler(w.svc, lambda: TODAY, catalog=Catalog({"5M": ("Mechanical", 5)}))
    bot.handle("Exam of 3rd semester on 10 November 9 to 12, 1 room, 1 per room, include faculty whose timetable is missing")
    aid = bot._latest_alloc()["id"]
    duty = bot.repo.duties(aid)[0]
    bot.repo.conn.execute("UPDATE exm_duty SET faculty_id=? WHERE id=?", (w.ids[rows[0][0]], duty["id"]))
    status = {x.check: x.status for x in vl.validate_allocation(w.svc, aid)}
    assert status["Teaching timetable conflicts"] == "Failed"


def test_validation_marks_conditions_not_requested_as_not_applicable(college):
    c = college
    aid = build_roster(c)
    st = {x.check: x.status for x in vl.validate_allocation(c.w.svc, aid)}
    assert st["Duty-count condition"] == "Not applicable" and st["Teaching timetable conflicts"] == "Not applicable"
    assert st["No faculty in two rooms at the same time"] == "Passed"


def test_a_requested_period_the_data_cannot_separate_is_not_verified(college):
    c = college
    c.bot.handle("Exam on 10 November 9 to 12, 3 rooms, 2 per room. Regular classes are suspended. "
                 "Faculty with fewer than 20 duties this semester only.")
    d = c.bot.consume_dashboard()
    chk = {x["check"]: x for x in d["validation"]}["Duty-count condition"]
    assert chk["status"] == "Not verified" and "no dates" in chk["detail"]


# ------------------------------------------------------------------------------------------- follow-ups
def test_follow_ups_modify_the_same_request_and_keep_exclusions(college):
    c = college
    c.bot.handle("Exam on 10 and 11 November 9 to 12, 4 rooms, 2 per room. Regular classes are suspended. "
                 "Use Civil and Electrical faculty only.")
    c.bot.consume_dashboard()
    victim = assigned(c.bot)[0]
    c.bot.handle(f"Now exclude {victim}")
    c.bot.handle("Make it three per room")
    d = c.bot.consume_dashboard()
    cards = {x["label"]: x["value"] for x in d["cards"]}
    assert cards["Invigilators per room"] == 3 and cards["Examination dates"] == "10-11 November 2026"
    assert cards["Faculty departments"] == "Civil, Electrical"
    assert victim not in assigned(c.bot)                       # the exclusion survived the rebuild
    c.bot.handle("Give preference to faculty members having fewer than 8 duties")
    d = c.bot.consume_dashboard()
    assert any(u["item"] == "Duty-count condition" for u in d["understood"])
    assert {x["label"]: x["value"] for x in d["cards"]}["Invigilators per room"] == 3
    assert victim not in assigned(c.bot)


def test_the_superseded_draft_is_retired(college):
    c = college
    c.bot.handle("Exam on 10 November 9 to 12, 3 rooms, 2 per room. Regular classes are suspended.")
    first = c.bot._session()["id"]
    c.bot.handle("Make it three per room")
    assert c.bot.repo.session(first)["status"] == "cancelled" and c.bot._session()["id"] != first


# ------------------------------------------------------------------------------------------- exports
def test_exports_match_the_reviewed_roster(college, tmp_path):
    c = college
    c.bot.handle("Exam on 10 and 11 November 9 to 12, 3 rooms, 2 per room. Regular classes are suspended.")
    d = c.bot.consume_dashboard()
    xl, pdf = oe.to_excel(d, tmp_path / "r.xlsx"), oe.to_pdf(d, tmp_path / "r.pdf")
    import openpyxl
    wb = openpyxl.load_workbook(xl)
    assert {"Examination summary", "Faculty duty distribution", "Faculty eligibility report", "Unfilled positions",
            "Validation report"} <= set(wb.sheetnames)
    ws = wb["Roster (date - shift - room)"]
    names = [v for row in ws.iter_rows(values_only=True) for v in row[1::2] if v and str(v).startswith(("Dr.", "Prof."))]
    assert len(names) == len(c.bot.repo.duties(c.bot._latest_alloc()["id"]))
    assert pdf.read_bytes()[:4] == b"%PDF"
    data, name, mime = c.bot.export("xlsx")
    assert name.endswith(".xlsx") and data[:2] == b"PK"


# ------------------------------------------------------------------------------------------- no hardcoding
FORBIDDEN = [r"""["'](?:CS|CSE|IT|ECE|EE|ME|CE|Computer Science|Information Technology|Electronics|Mechanical|Civil)["']""",
             r"""["'](?:A10\d|Room\s?10\d|[A-Z]\d{3})["']""", r"""["']Dr\.\s+[A-Z][a-z]+["']"""]


def test_no_college_specific_values_in_the_source():
    root = Path(__file__).resolve().parent.parent.parent / "exam_management"
    hits = []
    for f in root.glob("*.py"):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith(("#", '"""')):
                continue
            for pat in FORBIDDEN:
                if re.search(pat, line):
                    hits.append(f"{f.name}:{i}: {line.strip()[:90]}")
    assert not hits, "college-specific literals found:\n" + "\n".join(hits)


def test_lexicon_holds_language_only():
    import json
    text = (Path(__file__).resolve().parent.parent.parent / "config" / "exam_lexicon.json").read_text(encoding="utf-8")
    json.loads(text)
    for forbidden in ("Computer", "Mechanical", "Civil", "Electronics", "Sharma", "Room 1"):
        assert forbidden not in text