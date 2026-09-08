"""
Focused regression test for the room-availability vs lab-shift
routing bug in faculty_chatbot.py's process_query().

Covers all 12 scenarios required by the bug report, using real
uploaded data (the default FacultyAIChatbot() dataset) wherever
that data can exercise the behavior, and a synthetic-but-real
CanonicalEventMatcher-based dataset (built through the ACTUAL
canonical architecture, not hand-injected into matcher internals)
for the room-availability POSITIVE-PATH cases, since the default
dataset's uploaded files do not (in this checkpoint) produce any
room-free-slot data on their own.
"""

import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent


def main() -> int:
    """
    Runs the full room-availability-vs-lab-shift routing
    regression suite and returns a process-style exit code
    (0 = all checks passed, 1 = at least one failed). Never
    calls sys.exit() itself, so it is safe to call from a
    pytest test function as well as from the __main__ guard
    below - see test_room_intent_routing_scenarios().
    """

    RESULTS = []


    def check(name, condition, details=""):
        if condition:
            RESULTS.append((name, True, ""))
            print(f"PASS - {name}")
        else:
            RESULTS.append((name, False, details))
            print(f"FAIL - {name}  {details}")


    def hash_file(path):
        p = Path(path)
        if not p.exists():
            return None
        return hashlib.sha256(p.read_bytes()).hexdigest()


    before_assignments = hash_file(REPO / "data/assignments.json")
    before_room_shifts = hash_file(REPO / "data/room_shifts.json")
    before_exam_duties = hash_file(REPO / "data/exam_duties.json")


    print("=" * 70)
    print("PART A - ROOM AVAILABILITY, POSITIVE PATH")
    print("(synthetic-but-real records through the ACTUAL")
    print("CanonicalEventMatcher pipeline, not hardcoded institutional")
    print("data - proves the routing AND the room_free_slots()/")
    print("room_free_for_period() formatting both work correctly)")
    print("=" * 70)

    from data_engine.canonical_event_matcher import CanonicalEventMatcher
    from faculty_chatbot import FacultyAIChatbot

    synthetic_records = [
        {
            "room": "TestRoom-201", "day": "Monday", "slot": 3,
            "slot_time": "10:15 - 11:15", "subject": "", "teacher": "",
            "class_name": "", "source_file": "Sample Location wise TT.pdf",
        },
        {
            "room": "TestRoom-202", "day": "Monday", "slot": 3,
            "slot_time": "10:15 - 11:15", "subject": "DBMS",
            "teacher": "Mr. Test Teacher", "class_name": "3CS-A",
            "source_file": "Sample Location wise TT.pdf",
        },
        {
            "room": "TestRoom-201", "day": "Monday", "slot": 4,
            "slot_time": "11:15 - 12:15", "subject": "", "teacher": "",
            "class_name": "", "source_file": "Sample Location wise TT.pdf",
        },
        {
            "room": "TestRoom-202", "day": "Monday", "slot": 4,
            "slot_time": "11:15 - 12:15", "subject": "", "teacher": "",
            "class_name": "", "source_file": "Sample Location wise TT.pdf",
        },
        {
            "room": "TestRoom-301", "day": "Tuesday", "slot": 4,
            "slot_time": "11:15 - 12:15", "subject": "", "teacher": "",
            "class_name": "", "source_file": "Sample Location wise TT.pdf",
        },
        {
            "room": "TestRoom-302", "day": "Tuesday", "slot": 4,
            "slot_time": "11:15 - 12:15", "subject": "OS",
            "teacher": "Mr. Test Teacher 2", "class_name": "4CS-B",
            "source_file": "Sample Location wise TT.pdf",
        },
        {
            "room": "TestRoom-401", "day": "Wednesday", "slot": 3,
            "slot_time": "10:15 - 11:15", "subject": "", "teacher": "",
            "class_name": "", "source_file": "Sample Location wise TT.pdf",
        },
        {
            "room": "TestRoom-401", "day": "Wednesday", "slot": 4,
            "slot_time": "11:15 - 12:15", "subject": "", "teacher": "",
            "class_name": "", "source_file": "Sample Location wise TT.pdf",
        },
        {
            "room": "TestRoom-402", "day": "Wednesday", "slot": 3,
            "slot_time": "10:15 - 11:15", "subject": "AI",
            "teacher": "Mr. Test Teacher 3", "class_name": "5CS-C",
            "source_file": "Sample Location wise TT.pdf",
        },
        {
            "room": "TestRoom-402", "day": "Wednesday", "slot": 4,
            "slot_time": "11:15 - 12:15", "subject": "", "teacher": "",
            "class_name": "", "source_file": "Sample Location wise TT.pdf",
        },
    ]

    synthetic_matcher = CanonicalEventMatcher(synthetic_records)
    synthetic_matcher.match()

    check(
        "SETUP: synthetic matcher produced non-zero room free slots",
        len(synthetic_matcher.room_free_slots) > 0,
        str(synthetic_matcher.summary())
    )

    synthetic_bot = FacultyAIChatbot(matcher=synthetic_matcher)

    # --- Scenario 1 ---
    r1 = synthetic_bot.process_query(
        "Which rooms are free on Monday slot 3?"
    )
    print("\n1.", repr(r1))
    check(
        "1: 'Which rooms are free on Monday slot 3?' routes to room "
        "availability and returns the real free room, excluding the "
        "busy one",
        "TestRoom-201" in r1
        and "TestRoom-202" not in r1
        and "Please specify the faculty" not in r1
    )

    # --- Scenario 2 ---
    r2 = synthetic_bot.process_query(
        "Which rooms are available on Monday slot 3?"
    )
    print("2.", repr(r2))
    check(
        "2: 'Which rooms are available on Monday slot 3?' routes to "
        "room availability",
        "TestRoom-201" in r2
        and "Please specify the faculty" not in r2
    )

    # --- Scenario 3 ---
    r3 = synthetic_bot.process_query(
        "What rooms are free on Tuesday slot 4?"
    )
    print("3.", repr(r3))
    check(
        "3: 'What rooms are free on Tuesday slot 4?' routes to room "
        "availability and excludes the busy room",
        "TestRoom-301" in r3
        and "TestRoom-302" not in r3
        and "Please specify the faculty" not in r3
    )

    # --- Scenario 4 ---
    r4 = synthetic_bot.process_query(
        "Which rooms are free from 10:15 to 12:15 on Wednesday?"
    )
    print("4.", repr(r4))
    check(
        "4: time-range room query routes to room availability and "
        "requires the room free across the WHOLE range (TestRoom-401 "
        "free both slots; TestRoom-402 busy in slot 3 so excluded)",
        "TestRoom-401" in r4
        and "TestRoom-402" not in r4
        and "Please specify the faculty" not in r4
    )


    print("\n" + "=" * 70)
    print("PART B - LAB SHIFTING STILL ROUTES CORRECTLY (real data)")
    print("=" * 70)

    real_bot = FacultyAIChatbot()
    qe = real_bot.query_engine

    # Discovered dynamically: a real teacher/day/slot with an actual
    # Lab-type record in the uploaded data.
    lab_record = next(
        r for r in qe._faculty_records()
        if "lab" in str(qe._get(r, "type")).lower()
    )
    lab_teacher = lab_record["teacher"]
    lab_day = lab_record["day"]
    lab_slot = lab_record["slot"]

    print(f"Using real lab record: {lab_teacher} / {lab_day} / slot {lab_slot}")

    # --- Scenario 5 ---
    r5 = real_bot.process_query(
        f"Move {lab_teacher}'s lab to another room."
    )
    print("\n5.", repr(r5)[:200])
    check(
        "5: 'Move <teacher>'s lab to another room.' still routes to "
        "lab shifting (not room availability)",
        "Room availability cannot be determined" not in r5
    )

    # --- Scenario 6 ---
    r6 = real_bot.process_query(
        f"Which rooms are available for {lab_teacher}'s lab on "
        f"{lab_day.capitalize()} slot {lab_slot}?"
    )
    print("6.", repr(r6)[:300])
    check(
        "6: 'Which rooms are available for <teacher>'s lab on "
        "<day> slot <slot>?' still routes to lab shifting and "
        "returns a real lab-shift-shaped answer (not the room-"
        "availability branch, not an invented room list)",
        "Room availability cannot be determined" not in r6
        and (
            "Available rooms for" in r6
            or "No alternative room is free" in r6
        )
    )

    # --- Scenario 7 ---
    r7 = real_bot.process_query("What if I move this lab?")
    print("7.", repr(r7)[:200])
    check(
        "7: 'What if I move this lab?' does not fall into the plain "
        "room-availability branch",
        "Rooms free on" not in r7
    )


    print("\n" + "=" * 70)
    print("PART C - EXISTING FUNCTIONALITY (real data, no regression)")
    print("=" * 70)

    # --- Scenario 8: existing faculty availability query ---
    r8 = real_bot.process_query(
        "Is Mr. Rajesh Rajaan free on Monday from 9:15 to 11:15?"
    )
    print("\n8.", repr(r8)[:200])
    check(
        "8: existing faculty availability query still answers about "
        "the faculty member, not rooms",
        "Mr. Rajesh Rajaan" in r8
    )

    # --- Scenario 9: existing class timetable query ---
    known_classes = sorted({
        str(qe._get(r, "class_name", "class"))
        for r in qe._faculty_records()
        if qe._get(r, "class_name", "class")
    })
    target_class = known_classes[0]

    r9 = real_bot.process_query(f"Show timetable of {target_class}")
    print("9.", repr(r9)[:200])
    check(
        "9: existing class timetable query still returns a real "
        "answer",
        isinstance(r9, str) and len(r9.strip()) > 0
    )

    # --- Scenario 10: existing workload query ---
    r10 = real_bot.process_query(
        "What is the workload of Dr. Chothmal Choudhary on Wednesday?"
    )
    print("10.", repr(r10)[:200])
    check(
        "10: existing workload query still answers about workload, "
        "not rooms",
        "Dr. Chothmal Choudhary" in r10
    )

    # --- Scenario 11: existing absence/what-if query ---
    r11 = real_bot.process_query(
        "What if Mr. Rajesh Rajaan is absent on Thursday?"
    )
    print("11.", repr(r11)[:300])
    check(
        "11: existing what-if absence query still returns a "
        "simulation answer",
        "simulation" in r11.lower() or "affected" in r11.lower()
    )

    # --- Scenario 12: existing exam-duty query ---
    r12 = real_bot.process_query(
        "Suggest 2 faculty for exam duty on 2026-09-07 from "
        "09:15 to 10:15"
    )
    print("12.", repr(r12)[:300])
    check(
        "12: existing exam-duty query still returns a proposal, not "
        "a room-availability response",
        "proposed" in r12.lower() and '"confirm"' in r12.lower()
    )


    print("\n" + "=" * 70)
    print("PART D - THE EXACT REPORTED FAILING QUERY")
    print("=" * 70)

    r_original = real_bot.process_query(
        "Which rooms are free on Monday slot 3?"
    )
    print("\nOriginal failing query:", repr(r_original))

    check(
        "D: the exact reported query no longer returns the lab-shift "
        "prompt",
        "Please specify the faculty, day, and slot for the lab" not in (
            r_original
        )
    )

    check(
        "D: the exact reported query returns a genuine room-"
        "availability response - either a real room list (this "
        "dataset now has room data, via the separately-preserved "
        "PDF-import fix) or, if room data were unavailable, a "
        "truthful 'cannot be determined' message - but never the "
        "lab-shift prompt and never an invented room",
        "Room availability cannot be determined" in r_original
        or "Rooms free on" in r_original
    )


    print("\n" + "=" * 70)
    print("PART E - PERSISTENCE SAFETY")
    print("=" * 70)

    after_assignments = hash_file(REPO / "data/assignments.json")
    after_room_shifts = hash_file(REPO / "data/room_shifts.json")
    after_exam_duties = hash_file(REPO / "data/exam_duties.json")

    check(
        "E: data/assignments.json byte-for-byte unchanged",
        after_assignments == before_assignments
    )

    check(
        "E: data/room_shifts.json byte-for-byte unchanged",
        after_room_shifts == before_room_shifts
    )

    check(
        "E: data/exam_duties.json byte-for-byte unchanged (proposal, "
        "not confirmed)",
        after_exam_duties == before_exam_duties
    )


    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"{passed}/{total} checks passed.")

    if passed != total:
        print("\nFAILURES:")
        for name, ok, details in RESULTS:
            if not ok:
                print(f"  - {name}: {details}")
        return 1

    return 0


def test_room_intent_routing_scenarios():
    """
    Pytest entry point. Runs the exact same room-
    availability-vs-lab-shift routing regression suite as
    `python test_room_intent_regression.py` (via main()) and
    asserts every check passed. main() never calls
    sys.exit(), so this behaves as a normal pytest test
    rather than aborting the test process.
    """

    exit_code = main()

    assert exit_code == 0, "one or more room-intent routing checks failed "\
        "(see printed output above for details)"


if __name__ == "__main__":
    sys.exit(main())