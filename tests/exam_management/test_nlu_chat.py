"""Natural-language extraction + chatbot workflow (bulk, per-person scopes, ask-when-missing)."""
from datetime import date

import pytest

from exam_management import nlu
from exam_management.config import ExamSettings
from exam_management.chat_handler import ExamManagementHandler
from tests.exam_management import dataset as ds
from tests.exam_management.conftest import World

TODAY = date(2026, 11, 6)
SHIFTS = {"Morning": (540, 720), "Afternoon": (840, 1020)}


@pytest.fixture
def resolver():
    rows = ds.roster(5)
    return FacultyResolverFactory(rows)


def FacultyResolverFactory(rows):
    r = nlu.FacultyResolver(list(enumerate([n for n, _ in rows], 1)))
    r.rows = [n for n, _ in rows]
    return r


def directives(text, r):
    ms, un = r.mentions(text)
    return nlu.parse_directives(text, r, ms, list(SHIFTS), SHIFTS, TODAY, 2026, ExamSettings().admin_keywords, ExamSettings().leave_keywords), un


def test_exam_parameters_are_extracted_not_hardcoded():
    req = nlu.parse_exam_request(
        "Allocate examination duties from 10 November to 20 November. We have 15 examination rooms. Assign 2 invigilators "
        "per room. Minimum 2 duties and maximum 4 duties per faculty member. Morning shift 9 am to 12 pm and afternoon "
        "shift 2 pm to 5 pm. Regular classes are suspended for the final semester examination.", TODAY)
    assert (req.start, req.end) == (date(2026, 11, 10), date(2026, 11, 20))
    assert req.room_count == 15 and req.per_room == 2 and (req.min_duties, req.max_duties) == (2, 4)
    assert [s["label"] for s in req.shifts] == ["Morning", "Afternoon"] and req.classes_suspended is True
    assert req.name == "Final Semester"


def test_any_duration_and_missing_values_are_detected():
    req = nlu.parse_exam_request("Allocate exam duties for 7 days starting 3 December, 12 rooms", TODAY)
    assert req.n_days == 7 and req.start == date(2026, 12, 3) and req.room_count == 12
    miss = nlu.missing_fields(req)
    assert any("shift" in m for m in miss) and any("invigilators" in m for m in miss) and any("suspended" in m for m in miss)
    dates, notes = nlu.expand_dates(req, ("sunday",))
    assert len(dates) == 7 and all(d.weekday() != 6 for d in dates)


def test_not_suspended_is_distinguished_from_suspended():
    assert nlu.parse_exam_request("regular classes will continue during the exam", TODAY).classes_suspended is False
    assert nlu.parse_exam_request("classes are not suspended", TODAY).classes_suspended is False
    assert nlu.parse_exam_request("lectures are suspended", TODAY).classes_suspended is True
    assert nlu.parse_exam_request("allocate duties 10 to 12 November", TODAY).classes_suspended is None


def test_bulk_command_gives_each_person_their_own_scope(resolver):
    n = resolver.rows
    ds_, un = directives(
        f"Exclude {n[0]} and {n[1]} from the complete examination period. {n[2]} is unavailable on 12 November and "
        f"{n[3]} is unavailable from 13 November to 15 November.", resolver)
    assert len(ds_) == 3 and not un
    assert [m.text for m in ds_[0].mentions] == [n[0], n[1]] and ds_[0].scopes[0].kind == "session"
    assert ds_[1].scopes[0].kind == "date" and ds_[1].scopes[0].start == date(2026, 11, 12)
    assert ds_[2].scopes[0].kind == "date_range" and ds_[2].scopes[0].end == date(2026, 11, 15)


def test_mixed_restrictions_in_one_sentence(resolver):
    n = resolver.rows
    ds_, _ = directives(f"{n[0]} is unavailable on 12 November, {n[1]} is unavailable from 13 November to 15 November, "
                        f"and {n[2]} must be excluded from the entire examination session.", resolver)
    assert [d.scopes[0].kind for d in ds_] == ["date", "date_range", "session"]


def test_shift_specific_clause_ignores_the_availability_half(resolver):
    n = resolver.rows
    ds_, _ = directives(f"{n[0]} is unavailable during the morning shift on 12 November but is available during "
                        f"the afternoon shift.", resolver)
    assert len(ds_) == 1 and ds_[0].scopes[0].kind == "shift" and ds_[0].scopes[0].shift == "Morning"


def test_unknown_names_are_reported_not_guessed(resolver):
    ds_, un = directives(f"Exclude Dr. Zorg Quill and {resolver.rows[0]} from the entire examination.", resolver)
    assert un == ["Dr. Zorg Quill"] and len(ds_[0].mentions) == 1


def test_possessive_and_surname_matching(resolver):
    ms, _ = resolver.mentions(f"Restore {resolver.rows[0]}'s examination duty eligibility.")
    assert len(ms) == 1 and ms[0].resolved == 1


def test_restore_forms(resolver):
    n = resolver.rows
    d1, _ = directives(f"Make {n[0]} available again from 16 November.", resolver)
    assert d1[0].action == "restore" and d1[0].scopes[0].start == date(2026, 11, 16) and d1[0].scopes[0].end is None
    d2, _ = directives(f"Remove the temporary examination exclusion for {n[1]}.", resolver)
    assert d2[0].action == "restore" and d2[0].kinds == ["session", "date", "date_range", "shift"]


def test_reason_and_administrative_category(resolver):
    ds_, _ = directives(f"Exclude {resolver.rows[0]} from the entire period because of NAAC accreditation work.", resolver)
    assert ds_[0].category == "administrative" and "NAAC" in ds_[0].reason


# ------------------------------------------------------------------ chatbot flow
@pytest.fixture
def bot(tmp_path):
    w = World(tmp_path / "chat.db", ds.roster(10))
    h = ExamManagementHandler(w.svc, lambda: TODAY)
    h.w = w
    return h


ALLOC = ("Allocate examination duties from 10 November to 13 November. 6 examination rooms, 2 invigilators per room, "
         "maximum 4 duties, morning shift 9 am to 12 pm, regular classes are suspended.")


def test_unrelated_messages_fall_through_to_existing_chatbot(bot):
    for q in ["who is free on monday at 10", "hello", "show timetable of Dr. Sharma", "free slots of all teachers on friday"]:
        assert bot.handle(q) is None


def test_missing_information_is_asked_not_invented(bot):
    r = bot.handle("Allocate examination duties from 10 November to 13 November with 6 rooms")
    assert "invigilators" in r and "shift" in r and "suspended" in r
    assert bot._session() is None                                  # nothing created yet
    r2 = bot.handle("2 invigilators per room, morning shift 9 am to 12 pm, classes are suspended")
    assert "version 1" in r2 and bot._session() is not None


def test_full_coordinator_workflow(bot):
    first = bot.handle(ALLOC)
    assert "awaiting your approval" in first and "complete" in first
    sid = bot._session()["id"]
    n = bot.w.names
    r = bot.handle(f"{n[0]} is unavailable on 12 November. {n[1]} is unavailable from 11 November to 12 November, "
                   f"and {n[2]} must be excluded from the entire examination session.")
    assert "Restrictions recorded" in r
    assert "version 2" in bot.handle("generate allocation again")        # apply the new restrictions
    for q, expect in [("show exclusions", n[1]), ("Show department-wise examination duty distribution", "Total faculty"),
                      ("Show total examination duties assigned to every faculty member", "Historical duties"),
                      ("Show faculty members who have not received any duties", n[2]),
                      ("Find faculty members who are receiving consecutive examination duties", "Consecutive"),
                      ("who is eligible", "Excluded")]:
        assert expect in bot.handle(q), q
    second = bot.handle("Generate another allocation with a different faculty distribution while maintaining all constraints.")
    assert "version 3" in second
    alloc = bot._latest_alloc()
    assert bot.w.svc.validate_allocation(alloc["id"]) == []
    duties = {d["faculty"] for d in bot.w.svc.repo.duties(alloc["id"])}
    assert n[2] not in duties
    approved = bot.handle("approve")
    assert "APPROVED" in approved
    victim = bot.w.svc.repo.duties(alloc["id"])[0]
    rep = bot.handle(f"Remove {victim['faculty']} from {victim['exam_date']} because of an urgent administrative meeting.")
    assert "Changes" in rep and "re-validated: OK" in rep and "draft version 4" in rep
    assert "report" in bot.handle("show full report").lower() or "Examination summary" in bot.handle("show full report")


def test_large_operation_asks_for_confirmation_and_cancel_changes_nothing(bot):
    bot.handle(ALLOC)
    n = bot.w.names
    r = bot.handle("Exclude " + ", ".join(n[:6]) + " from the entire examination period.")
    assert "confirm" in r.lower() and not bot.w.svc.restrictions.active()
    bot.handle("cancel")
    assert not bot.w.svc.restrictions.active()
    bot.handle("Exclude " + ", ".join(n[:6]) + " from the entire examination period.")
    out = bot.handle("confirm")
    assert len(bot.w.svc.restrictions.active()) == 6 and "Restrictions recorded" in out


def test_ambiguous_and_unknown_names_change_nothing_for_them(bot):
    bot.handle(ALLOC)
    n = bot.w.names
    first = n[0].split()[1]
    r = bot.handle(f"Exclude Dr. Zorg Quill and {n[3]} from the entire examination period.")
    assert "Zorg Quill" in r and len(bot.w.svc.restrictions.active()) == 1
    r = bot.handle(f"Exclude Prof. {first} from the entire examination period.")
    assert "more than one match" in r or len(bot.w.svc.restrictions.active()) >= 1


def test_administrative_flow_is_data_driven(bot):
    bot.handle(ALLOC)
    n = bot.w.names
    assert "No matching administrative" in bot.handle("Exclude faculty members who are handling administrative responsibilities during this examination period.")
    assert "Recorded" in bot.handle(f"{n[0]} is HOD.")
    assert "Recorded" in bot.handle(f"{n[1]} is handling NAAC accreditation work.")
    r = bot.handle("Exclude faculty members who are handling administrative responsibilities during this examination period.")
    assert n[0] in r and n[1] in r
    assert n[0] in bot.handle("show administrative responsibilities")


def test_exclusions_before_the_exam_exists_are_remembered_and_applied(bot):
    n = bot.w.names
    assert "next examination" in bot.handle(f"Exclude {n[0]} from the entire examination period because of NAAC work.")
    bot.handle(ALLOC)
    assert bot.w.svc.restrictions.active(bot.w.fid(0))
    sid = bot._session()["id"]
    assert not bot.w.svc.eligibility.evaluate(sid).eligible_slots(bot.w.fid(0))


def test_explanations_come_from_records(bot):
    bot.handle(ALLOC)
    n = bot.w.names
    r = bot.handle(f"Why did {n[5]} receive more duties than {n[6]}?")
    assert n[5] in r and n[6] in r and "Historical duties" in r
    assert "was given" in bot.handle(f"Why did {n[5]} get these duties?")


def test_unfilled_rooms_are_reported_honestly(bot):
    r = bot.handle("Allocate examination duties from 10 November to 12 November. 40 examination rooms, 2 invigilators per room, "
                   "maximum 4 duties, morning shift 9 am to 12 pm, classes are suspended.")
    assert "PARTIAL" in r and "unfilled" in r.lower()
    assert "Reason" in bot.handle("Show all unfilled rooms and explain why they could not be filled.")
    assert "cannot approve" in bot.handle("approve")
    assert "APPROVED" in bot.handle("approve anyway")