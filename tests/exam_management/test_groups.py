"""CS / non-CS style division of duties - nothing about 'CS' is built into the engine."""
from collections import Counter, defaultdict
from datetime import date

import pytest

from exam_management import nlu
from exam_management.chat_handler import ExamManagementHandler
from exam_management.groups import normalise_spec, resolve_group, seat_quota
from exam_management.providers import StaticDirectory
from exam_management.reports import Reporter
from tests.exam_management import dataset as ds
from tests.exam_management.conftest import World

TODAY = date(2026, 11, 6)
ALLOC = ("Allocate examination duties from 10 November to 14 November. 6 examination rooms, 2 invigilators per room, "
         "maximum 4 duties, morning shift 9 am to 12 pm, regular classes are suspended. "
         "Divide the duties equally between CS and non-CS faculty.")


def all_one_dept(n=40):
    """Like the real timetable: every faculty member is labelled with the same department."""
    return [(name, "CS") for name, _ in ds.roster(n // 6 + 1)[:n]]


@pytest.fixture
def bot(tmp_path):
    w = World(tmp_path / "g.db", all_one_dept(40))
    h = ExamManagementHandler(w.svc, lambda: TODAY)
    h.w = w
    return h


def group_of_duties(h, aid, spec_names):
    out = defaultdict(Counter)
    for d in h.repo.duties(aid):
        out[(d["exam_date"], d["shift"])][spec_names[d["faculty_id"]]] += 1
    return out


def test_quota_math_is_exact_and_share_based():
    assert seat_quota(12, {"A": 1, "B": 1}) == {"A": 6, "B": 6}
    assert seat_quota(13, {"A": 3, "B": 1}) == {"A": 10, "B": 3}
    assert sum(seat_quota(7, {"A": 1, "B": 1, "C": 1}).values()) == 7


def test_group_resolution_uses_labels_and_catch_all():
    spec = normalise_spec({"groups": {"X": ["alpha"], "Y": ["*"]}})
    assert resolve_group("Alpha", spec) == "X" and resolve_group("whatever", spec) == "Y" and resolve_group("", spec) == "Y"


def test_split_phrases_are_parsed():
    p = nlu.parse_group_split("divide the duties equally between CS and non-CS faculty")
    assert p["base"] == "cs" and p["second"] is None and p["shares"] == {}
    p = nlu.parse_group_split("split duties 60% to CS and 40% to non-CS")
    assert p["shares"] == {"first": 60.0, "second": 40.0}
    p = nlu.parse_group_split("share the duties between Mechanical and Civil faculty in 3:1 ratio")
    assert (p["base"], p["second"], p["shares"]) == ("mechanical", "civil", {"first": 3.0, "second": 1.0})
    assert nlu.parse_group_split("allocate duties from 10 to 14 November with 6 rooms") is None


def test_department_assignment_phrases():
    d = nlu.parse_dept_assignment("Dr. A and Dr. B are non-CS faculty", ["CS"])
    assert d["label"] == "Non-CS"
    d = nlu.parse_dept_assignment("treat all MTECH faculty as non-CS", ["CS", "MTECH"])
    assert d == {"label": "Non-CS", "source": "mtech"}
    assert nlu.parse_dept_assignment("Dr. A belongs to the Mathematics department", [])["label"] == "Mathematics"


def test_full_flow_with_everyone_labelled_cs(bot):
    names = bot.w.names
    reply = bot.handle(f"{', '.join(names[:12])} are non-CS faculty.")
    assert "Recorded 12" in reply and "Non-CS" in reply
    first = bot.handle(ALLOC)
    assert "Duty split" in first and "CS" in first and "awaiting your approval" in first
    sess = bot._session()
    assert sess["options"]["group_split"]["shares"] == {"CS": 1.0, "Non-CS": 1.0}
    aid = bot._alloc()["id"]
    dept = {f["id"]: f["department"] for f in bot.repo.faculty_all()}
    for slot, cnt in group_of_duties(bot, aid, dept).items():
        assert cnt["CS"] == cnt["Non-CS"] == 6, (slot, cnt)                # equal share of 12 seats
    assert bot.w.svc.validate_allocation(aid) == []
    # every room has one of each group (equal split, 2 invigilators per room)
    rooms = defaultdict(set)
    for d in bot.repo.duties(aid):
        rooms[(d["exam_date"], d["room"])].add(dept[d["faculty_id"]])
    assert all(v == {"CS", "Non-CS"} for v in rooms.values())
    rep = bot.handle("show CS and non-CS distribution")
    assert "Seats planned" in rep and "Non-CS" in rep
    assert "Group split" in bot.handle("show full report")


def test_override_survives_roster_sync(bot):
    bot.handle(f"{bot.w.names[0]} is non-CS faculty.")
    bot.w.svc.sync_roster()
    assert bot.repo.faculty_by_name(bot.w.names[0])["department"] == "Non-CS"
    assert "Non-CS" in bot.repo.departments()


def test_fairness_is_computed_inside_each_group(bot):
    names = bot.w.names
    bot.handle(f"{', '.join(names[:10])} are non-CS faculty.")
    bot.handle(ALLOC)
    aid = bot._alloc()["id"]
    per = Counter(d["faculty_id"] for d in bot.repo.duties(aid))
    non = [per[bot.w.ids[n]] for n in names[:10]]
    cs = [per[bot.w.ids[n]] for n in names[10:40]]
    assert sum(non) == sum(cs) == 30                       # equal seats, though groups differ in size
    assert max(non) <= 4 and max(cs) <= 4
    assert sum(non) / 10 > sum(cs) / 30                    # the smaller group carries more each (and is within max)
    assert max(non) - min(non) <= 2


def test_empty_group_is_explained_and_seats_still_filled(bot):
    reply = bot.handle(ALLOC)
    assert "No faculty are in the 'Non-CS' group" in reply
    aid = bot._alloc()["id"]
    assert bot.repo.allocation(aid)["feasibility"] == "complete"      # CS faculty cover the seats
    assert "short of its share" in bot.handle("show CS and non-CS distribution")


def test_unequal_shares_and_replacement_keeps_the_split(bot):
    names = bot.w.names
    bot.handle(f"{', '.join(names[:20])} are non-CS faculty.")
    bot.handle(ALLOC.replace("equally between CS and non-CS faculty", "between CS and non-CS faculty with 75% to CS and 25% to non-CS"))
    aid = bot._alloc()["id"]
    dept = {f["id"]: f["department"] for f in bot.repo.faculty_all()}
    day = group_of_duties(bot, aid, dept)
    assert all(c["CS"] == 9 and c["Non-CS"] == 3 for c in day.values())
    bot.handle("approve")
    victim = next(d for d in bot.repo.duties(aid) if dept[d["faculty_id"]] == "CS")
    out = bot.w.svc.replace_duty(aid, victim["faculty_id"], date.fromisoformat(victim["exam_date"]))
    new_day = group_of_duties(bot, out.allocation_id, dept)
    assert all(c["CS"] == 9 and c["Non-CS"] == 3 for c in new_day.values())


def test_split_can_be_added_or_removed_after_creation(bot):
    names = bot.w.names
    bot.handle(f"{', '.join(names[:15])} are non-CS faculty.")
    bot.handle(ALLOC.replace(" Divide the duties equally between CS and non-CS faculty.", ""))
    assert "group_split" not in bot._session()["options"]
    assert "apply the split" in bot.handle("divide the duties equally between CS and non-CS faculty")
    assert "group_split" in bot._session()["options"]
    bot.handle("generate allocation again")
    assert "Seats planned" in bot.handle("show group-wise distribution")
    assert "Removed the group split" in bot.handle("remove the group split")
    assert "group_split" not in bot._session()["options"]


def test_department_file_import(tmp_path, bot):
    f = tmp_path / "depts.csv"
    names = bot.w.names
    f.write_text("name,department\n" + "\n".join(f"{n},Non-CS" for n in names[:5]) + "\nDr. Nobody Here,Non-CS\n")
    reply = bot.handle(f"import departments from {f}")
    assert "5 faculty" in reply and "Dr. Nobody Here" in reply
    assert bot.repo.faculty_by_name(names[0])["department"] == "Non-CS"


def test_other_department_assignments_are_not_confused_with_admin_roles(bot):
    assert bot.handle(f"{bot.w.names[0]} is HOD of the CS department") is None or \
        "Recorded" in bot.handle(f"{bot.w.names[0]} is HOD of the CS department")
    assert bot.repo.faculty_by_name(bot.w.names[0])["department"] == "CS"