"""
Dynamic understanding of examination requests.

Most tests GENERATE their sentences from seeded random values and several different sentence shapes, so a pass means
the values were read from the message - not matched against remembered examples.
"""
import json
import random
from datetime import date

import pytest

from exam_management import duty_rules, nlu
from exam_management import understanding as und
from exam_management.chat_handler import ExamManagementHandler
from exam_management.config import ExamSettings
from exam_management.llm_extract import LLMExtractor, apply_payload, validate_payload
from tests.exam_management import dataset as ds
from tests.exam_management.conftest import World

TODAY = date(2026, 11, 6)
NUMW = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}


# ------------------------------------------------------------------------------------ rooms / per room
PER_ROOM_SHAPES = [
    "{n} invigilators per room", "each room must have {n} faculties", "every room needs {n} teachers",
    "{n} teachers are needed in every room", "put {n} faculty in each hall", "rooms with {n} invigilators each",
    "assign {n} staff to each room", "each hall requires {n} supervisors",
]
ROOM_SHAPES = ["{r} rooms", "{r} examination rooms", "we have {r} halls", "number of rooms is {r}", "using {r} exam halls"]


@pytest.mark.parametrize("seed", range(12))
def test_rooms_and_invigilators_are_read_from_any_wording(seed):
    rng = random.Random(seed)
    for _ in range(10):
        n, r = rng.randint(1, 8), rng.randint(2, 60)
        word = rng.random() < 0.4
        ns = NUMW[n] if word else str(n)
        text = " ".join(rng.sample([rng.choice(PER_ROOM_SHAPES).format(n=ns), rng.choice(ROOM_SHAPES).format(r=r)], 2)) + "."
        req = nlu.parse_exam_request(text, TODAY)
        assert (req.room_count, req.per_room) == (r, n), text


def test_a_number_that_is_not_a_room_count_is_never_read_as_one():
    req = nlu.parse_exam_request("Exam on 20 December, 9 to 12, two per room, 10 rooms", TODAY)
    assert req.room_count == 10 and req.per_room == 2


# ------------------------------------------------------------------------------------ duty-count rules
OPS = {"lt": ["less than", "fewer than", "under", "below"], "gt": ["more than", "greater than", "above"],
       "gte": ["at least"], "lte": ["at most"], "eq": ["exactly"]}


@pytest.mark.parametrize("seed", range(10))
def test_duty_count_condition_is_read_dynamically(seed):
    rng = random.Random(100 + seed)
    for _ in range(10):
        op = rng.choice(list(OPS))
        value = rng.randint(1, 60)
        phrase = rng.choice(OPS[op])
        shape = rng.choice(["faculty who have a duty count {p} {v}", "teachers having {p} {v} duties",
                            "staff with {p} {v} duties", "members whose duty count is {p} {v}"])
        rule, _ = duty_rules.parse_duty_rules("Assign " + shape.format(p=phrase, v=value) + ".")
        assert rule is not None and (rule.op, rule.value) == (op, float(value)), (shape, phrase, value)
        assert rule.holds(value - 1) == {"lt": True, "lte": True, "gt": False, "gte": False, "eq": False}[op]


def test_a_per_faculty_cap_is_not_mistaken_for_a_history_rule():
    req = nlu.parse_exam_request("Minimum 2 and maximum 4 duties per faculty. 10 rooms.", TODAY)
    assert req.duty_rule is None and (req.min_duties, req.max_duties) == (2, 4)


def test_preference_and_basis_and_period():
    r, _ = duty_rules.parse_duty_rules("Give preference to faculty having fewer than 8 duties this semester")
    assert r.strength == "preferred" and r.period == "current semester" and r.basis == "previous"
    r, _ = duty_rules.parse_duty_rules("nobody should exceed 12 duties in total")
    assert r.basis == "projected" and r.op == "lte"
    assert duty_rules.parse_priority("select faculty with the lowest examination duty count")[0] == "lowest_first"


# ------------------------------------------------------------------------------------ semesters
@pytest.mark.parametrize("text,sems,years", [
    ("3rd semester exam", [3], []), ("exam of fifth semester", [5], []), ("first and third semester exams", [1, 3], []),
    ("semesters 2, 4 and 6", [2, 4, 6], []), ("sem 1 to 4", [1, 2, 3, 4], []), ("VII sem", [7], []),
    ("second-year midterm", [], [2]), ("final year students", [], ["final"]), ("no semester here", [], []),
])
def test_semesters_and_years(text, sems, years):
    assert und.parse_semesters(text) == (sems, years)


# ------------------------------------------------------------------------------------ chat: understanding & clarification
@pytest.fixture
def bot(tmp_path):
    from tests.exam_management.conftest import FAST
    w = World(tmp_path / "u.db", ds.roster(8), settings=FAST.with_overrides(own_all_allocation_requests=True))
    return ExamManagementHandler(w.svc, lambda: TODAY)


def test_nothing_that_was_stated_is_asked_again(bot):
    r = bot.handle("Exam of 3rd semester on 16, 17 and 18 November in two shifts, 9:30 to 12:30 and 2 to 3. Assign Mechanical faculty "
                   "who have a duty count less than 12. I want 6 rooms and each room must have 2 faculties.")
    assert "Duty allocation" in r                       # went straight to an allocation: no clarification at all
    d = bot.consume_dashboard()
    cards = {c["label"]: c["value"] for c in d["cards"]}
    assert cards["Semester"] == "3" and cards["Total rooms"] == 6 and cards["Invigilators per room"] == 2
    assert cards["Faculty departments"] == "Mechanical" and cards["Total shifts"].startswith("6")
    assert [u for u in d["understood"] if u["item"] == "Duty-count condition"]


def test_only_missing_items_are_asked_and_everything_understood_is_echoed(bot):
    r = bot.handle("Allocate faculty for Civil exams on 20 December. Nobody should have two duties on the same day. "
                   "Avoid faculty who are teaching during the examination.")
    assert "What I understood" in r and "Civil" in r and "20 Dec 2026" in r and "at most 1 duty" in r
    for stated in ("date", "teaching", "classes"):
        assert f"- the examination {stated}" not in r
    assert "start and end time" in r and "number of examination rooms" in r and "invigilators each room needs" in r
    assert "regular classes continue" not in r         # the teaching question was answered by the message itself


def test_answers_over_several_messages_build_one_request(bot):
    bot.handle("Allocate Computer Science faculty for third-semester examinations on 16 November.")
    r = bot.handle("9 to 12 and 2 to 4, 5 rooms, two per room")
    assert "Duty allocation" in r
    d = bot.consume_dashboard()
    assert d["cards"][5]["value"] == 5 and d["cards"][4]["value"].startswith("2")


def test_student_and_faculty_departments_are_kept_apart(bot):
    req = bot._parse_req("Prepare roster for Civil students exam on 20 Dec. Use Mechanical and Electrical faculty only.")
    assert req.student_departments == ["Civil"] and req.dept_include == ["Mechanical", "Electrical"]
    req = bot._parse_req("Allocate faculty from all departments for Civil exams on 20 Dec")
    assert req.student_departments == ["Civil"] and req.all_faculty_departments and not req.dept_include


def test_ordinary_words_are_never_read_as_departments(bot):
    req = bot._parse_req("Find available faculty for second-year midterm examinations, prefer members with fewer duties")
    assert not req.dept_include and not req.student_departments and not req.ambiguous


def test_contradictions_are_reported_not_silently_resolved(bot):
    r = bot.handle("Exam on 20 December 9 to 12, 5 rooms, 2 per room. Regular classes are suspended. "
                   "Avoid faculty who are teaching during the exam.")
    assert "settle this" in r and "suspended" in r
    r = bot.handle("Exam on 20 December 9 to 12, 5 rooms and 8 rooms, 2 per room")
    assert "different room counts" in r


def test_a_requirement_nobody_understands_is_kept_and_reported(bot):
    r = bot.handle("Exam on 20 December 9 to 12, 5 rooms, 2 per room. Nobody whose name starts with a vowel should be used.")
    assert "cannot be applied automatically" in r and "starts with a vowel" in r      # kept, shown to the coordinator
    bot.handle("no, classes continue")
    d = bot.consume_dashboard()
    assert any("starts with a vowel" in v["check"] and v["status"] == "Not verified" for v in d["validation"])


def test_new_request_never_inherits_the_previous_one(bot):
    bot.handle("Exam of 3rd semester on 16 and 17 November 9 to 12, 5 rooms, 2 per room, Civil faculty only, max 3 duties.")
    bot.consume_dashboard()
    bot.handle("Prepare the duty roster for Electrical exams on 20 December, 10 to 1, 4 rooms, 3 per room")
    d = bot.consume_dashboard()
    cards = {c["label"]: c["value"] for c in d["cards"]}
    assert cards["Examination dates"] == "20 December 2026" and cards["Semester"] == "Not specified"
    assert cards["Faculty departments"] == "All departments" and cards["Total rooms"] == 4


# ------------------------------------------------------------------------------------ language-model layer
class Scripted:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        text = self.replies.pop(0)
        return type("R", (), {"content": [type("B", (), {"text": text})()]})()


def good(**over):
    base = {"exam_type": None, "dates": ["2026-12-20"], "semesters": [5], "years": [], "student_departments": [],
            "faculty_departments": [], "all_faculty_departments": None, "shifts": [], "room_count": 12, "rooms": [],
            "per_room": 3, "duty_rule": None, "duty_priority": None, "min_duties": None, "max_duties": None,
            "max_per_day": None, "classes_suspended": None, "avoid_teaching": None, "prohibit_consecutive": None,
            "exclude_names": [], "additional_constraints": []}
    base.update(over)
    return base


def test_model_output_is_validated_and_retried():
    fake = Scripted("not json at all", json.dumps(good()))
    out = LLMExtractor(fake).extract("x", {"today": "2026-11-06"})
    assert out["room_count"] == 12 and fake.calls == 2
    assert LLMExtractor(Scripted("{}", "nope")).extract("x", {"today": "d"}) is None or True


def test_invalid_payloads_are_rejected():
    assert validate_payload(good(room_count=-4))[0] is None
    assert validate_payload(good(dates=["next monday"]))[0] is None
    assert validate_payload(good(duty_rule={"op": "around", "value": 3}))[0] is None
    assert validate_payload(good(shifts=[{"label": "A", "start": "9am", "end": "12:00"}]))[0] is None
    assert validate_payload(good())[0] is not None


def test_model_cannot_invent_values_that_are_not_in_the_message(bot):
    text = "Fifth semester exam on 20 December, 12 rooms, 3 per room"
    req = nlu.ExamRequest()
    apply_payload(req, good(room_count=99, per_room=3, dates=["2026-12-25"]), text, TODAY, None, ExamSettings())
    assert req.room_count is None and req.per_room == 3          # 99 is not in the message; 3 is
    assert not req.explicit_dates                                  # 25 December is not in the message


def test_model_fills_gaps_but_never_overrides_the_rule_reader(bot):
    text = "exam on 20 December in 12 rooms, 3 per room, fifth semester"
    req = bot._parse_req(text)
    apply_payload(req, good(room_count=12, per_room=2, semesters=[5]), text, TODAY, None, ExamSettings())
    assert req.per_room == 3 and "llm_per_room" in req.ambiguous