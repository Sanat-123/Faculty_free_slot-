"""
Regression test for the room-value contamination bug: clock-time
tokens (e.g. "9:00", "10:00", "12:45") were leaking into room
candidate lists because PDFImporter.detect_room()'s colon-based
room-code pattern (meant for real codes like "7F:CP7") never
required a letter anywhere in the match, so a bare "H:MM" digit
pair matched it too.

Also re-verifies the room-availability-vs-lab-shift routing fix
(previously committed as db99b7c) still holds on top of this
change, and that lab shifting / what-if / plan-confirm safety are
all still intact.
"""

import hashlib
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent


def main() -> int:
    """
    Runs the full room-value-contamination regression suite
    and returns a process-style exit code (0 = all checks
    passed, 1 = at least one failed). Never calls sys.exit()
    itself, so it is safe to call from a pytest test function
    as well as from the __main__ guard below - see
    test_room_candidate_fix_scenarios().
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


    def looks_like_clock_time(value):
        """
        Generic, format-based check mirroring PDFImporter.
        _looks_like_clock_time() - used here independently (not by
        importing the production helper) so the TEST verifies the
        actual observable behavior, not just that the helper exists.
        Deliberately NOT a list of the five originally-observed
        values - any H:MM / HH:MM token is rejected, regardless of
        which specific time it is.
        """
        return bool(re.fullmatch(r"\d{1,2}:[0-5]\d", str(value).strip()))


    def extract_numbered_rooms(response_text):
        lines = response_text.split("\n")
        rooms = []
        for line in lines:
            match = re.match(r"^\d+\.\s+(.+)$", line.strip())
            if match:
                rooms.append(match.group(1).strip())
        return rooms


    before_assignments = hash_file(REPO / "data/assignments.json")
    before_room_shifts = hash_file(REPO / "data/room_shifts.json")
    before_exam_duties = hash_file(REPO / "data/exam_duties.json")


    print("=" * 70)
    print("PART A - ROOT CAUSE: detect_room() no longer extracts times")
    print("=" * 70)

    from import_engine.pdf_importer import PDFImporter

    # Real cell text taken directly from the actual uploaded PDF data
    # (classwise TT), where the bug was originally observed.
    real_cell_texts = [
        "8:00 - 10:00 Group 1 A-JAVA Lab/Spoken-Java CL-13 SN",
        "10:00 - 11:00 ADBMS 103 ArJ",
        "12:45 - 14:45 Adv. ML CL-10 MB",
        "8:00 - 9:00 ITR VII ME-301 LK",
        "9:00 - 10:00 DSIo T ME-301 ArJ",
    ]

    expected_rooms = ["CL-13", "103", "CL-10", "ME-301", "ME-301"]

    for cell_text, expected in zip(real_cell_texts, expected_rooms):

        detected = PDFImporter.detect_room(cell_text)

        check(
            f"A: detect_room({cell_text!r}) returns the real room "
            f"{expected!r}, not a time token",
            detected == expected,
            f"got {detected!r}"
        )

        check(
            f"A: detect_room({cell_text!r}) result is never itself "
            "a clock-time token",
            not looks_like_clock_time(detected)
        )

    # A real colon-based room code must still be detected correctly
    # (proves the fix didn't overcorrect and start rejecting valid
    # colon-containing room codes).
    colon_room_cases = [
        ("7F:EE-Lab13 something else", "7F:EE-Lab13"),
        ("Class in 7F:CP7 this week", "7F:CP7"),
    ]

    for cell_text, expected in colon_room_cases:

        detected = PDFImporter.detect_room(cell_text)

        check(
            f"A: a genuine colon-based room code {expected!r} is "
            "still detected correctly",
            detected == expected,
            f"got {detected!r}"
        )

    # Generic time-shape check itself, across arbitrary times - not
    # just the five originally observed values.
    generic_time_probes = [
        "0:00", "1:15", "6:30", "9:00", "10:00", "11:00", "12:00",
        "12:45", "13:15", "23:59", "8:05",
    ]

    for probe in generic_time_probes:

        check(
            f"A: detect_room() never returns a bare time token "
            f"{probe!r} as a room when that's ALL a cell contains",
            PDFImporter.detect_room(probe) == ""
        )


    print("\n" + "=" * 70)
    print("PART B - FULL REAL-DATA IMPORT: zero time-token "
          "contamination anywhere")
    print("=" * 70)

    all_rooms_seen = set()

    for fname in [
        "Facultywise TT 20 sep.pdf",
        "classwise TT 27 sep.pdf",
        "Location wise TT 27 sep 2025.pdf",
    ]:
        records = PDFImporter.import_file(
            str(REPO / "data" / fname)
        )

        for record in records:
            room = record.get("room", "")
            if room:
                all_rooms_seen.add(room)

    contaminated = [r for r in all_rooms_seen if looks_like_clock_time(r)]

    check(
        "B: zero clock-time tokens among ALL room values across the "
        "full real uploaded dataset",
        len(contaminated) == 0,
        f"contaminated={contaminated}"
    )

    check(
        "B: real rooms are still present in the dataset (fix did not "
        "remove legitimate data)",
        len(all_rooms_seen) > 0
    )


    print("\n" + "=" * 70)
    print("PART C - CHATBOT: the exact reported query")
    print("=" * 70)

    from faculty_chatbot import FacultyAIChatbot

    real_bot = FacultyAIChatbot()
    qe = real_bot.query_engine

    r_reported = real_bot.process_query(
        "Which rooms are available for Dr. Chothmal Choudhary's lab "
        "on Wednesday slot 6?"
    )
    print("\nReported query response:\n", r_reported)

    rooms_c = extract_numbered_rooms(r_reported)

    check(
        "C: the exact reported query returns a non-empty room list",
        len(rooms_c) > 0,
        r_reported
    )

    bad_rooms_c = [r for r in rooms_c if looks_like_clock_time(r)]

    check(
        "C: NO returned room value is a timetable time token "
        "(generic check, not limited to the 5 originally observed "
        "values)",
        len(bad_rooms_c) == 0,
        f"bad_rooms={bad_rooms_c}"
    )

    check(
        "C: response uses the readable structured format (Day/"
        "Slots/Current room labels, not one giant comma line)",
        "Day:" in r_reported
        and "Slots:" in r_reported
        and "Current room:" in r_reported
    )

    check(
        "C: response does not contain the old one-line comma-joined "
        "room dump format",
        ", ".join(rooms_c) not in r_reported
    )

    check(
        "C: room ordering is deterministic (alphabetical, matching "
        "entity_knowledge()'s sorted() output)",
        rooms_c == sorted(rooms_c, key=lambda x: x.casefold())
    )

    check(
        "C: no duplicate rooms in the response",
        len(rooms_c) == len(set(rooms_c))
    )


    print("\n" + "=" * 70)
    print("PART D - GENERAL ROOM AVAILABILITY QUERIES")
    print("=" * 70)

    queries_and_labels = [
        ("Which rooms are free on Monday slot 3?", "D1"),
        ("Which rooms are available on Tuesday slot 4?", "D2"),
        ("Which rooms are free on Wednesday from 10:15 to 12:15?", "D3"),
    ]

    for query_text, label in queries_and_labels:

        response = real_bot.process_query(query_text)
        print(f"\n{label}. {query_text}\n{response[:300]}")

        check(
            f"{label}: '{query_text}' does not fall into lab "
            "shifting",
            "Please specify the faculty" not in response
        )

        rooms = extract_numbered_rooms(response)

        bad = [r for r in rooms if looks_like_clock_time(r)]

        check(
            f"{label}: no time-token contamination in the room list",
            len(bad) == 0,
            f"bad_rooms={bad}"
        )

        if rooms:

            check(
                f"{label}: no duplicate rooms",
                len(rooms) == len(set(rooms))
            )

            check(
                f"{label}: room ordering is deterministic "
                "(alphabetical)",
                rooms == sorted(rooms, key=lambda x: x.casefold())
            )


    print("\n" + "=" * 70)
    print("PART E - LAB SHIFTING AND WHAT-IF STILL WORK")
    print("=" * 70)

    lab_record = next(
        r for r in qe._faculty_records()
        if "lab" in str(qe._get(r, "type")).lower()
    )
    lab_teacher = lab_record["teacher"]
    lab_day = lab_record["day"]
    lab_slot = lab_record["slot"]

    r_shift = real_bot.process_query(
        f"Move {lab_teacher}'s lab on {lab_day.capitalize()} slot "
        f"{lab_slot} to another room."
    )
    print("\nE1 (lab shift):\n", r_shift[:400])

    check(
        "E1: lab-shift query for a real lab still routes to lab "
        "shifting and returns a clean room list",
        "Please specify the faculty" not in r_shift
    )

    rooms_e1 = extract_numbered_rooms(r_shift)
    bad_e1 = [r for r in rooms_e1 if looks_like_clock_time(r)]

    check(
        "E1: no time-token contamination in the lab-shift room list",
        len(bad_e1) == 0,
        f"bad_rooms={bad_e1}"
    )

    r_whatif = real_bot.process_query("What if I move this lab?")
    print("\nE2 (what-if, no context):\n", r_whatif[:200])

    check(
        "E2: what-if with no lab context asks for details rather "
        "than inventing a room list (side-effect free)",
        "Rooms free on" not in r_whatif
        and "Available rooms for" not in r_whatif
    )


    print("\n" + "=" * 70)
    print("PART F - EXISTING FUNCTIONALITY (real data, no regression)")
    print("=" * 70)

    r_f1 = real_bot.process_query(
        "Is Mr. Rajesh Rajaan free on Monday from 9:15 to 11:15?"
    )
    check(
        "F1: existing faculty availability query still works",
        "Mr. Rajesh Rajaan" in r_f1
    )

    known_classes = sorted({
        str(qe._get(r, "class_name", "class"))
        for r in qe._faculty_records()
        if qe._get(r, "class_name", "class")
    })
    r_f2 = real_bot.process_query(
        f"Show timetable of {known_classes[0]}"
    )
    check(
        "F2: existing class timetable query still works",
        isinstance(r_f2, str) and len(r_f2.strip()) > 0
    )

    r_f3 = real_bot.process_query(
        "What is the workload of Dr. Chothmal Choudhary on Wednesday?"
    )
    check(
        "F3: existing workload query still works",
        "Dr. Chothmal Choudhary" in r_f3
    )

    r_f4 = real_bot.process_query(
        "What if Mr. Rajesh Rajaan is absent on Thursday?"
    )
    check(
        "F4: existing what-if absence query still works",
        "simulation" in r_f4.lower() or "affected" in r_f4.lower()
    )

    r_f5 = real_bot.process_query(
        "Suggest 2 faculty for exam duty on 2026-09-07 from "
        "09:15 to 10:15"
    )
    check(
        "F5: existing exam-duty query still works and is a proposal",
        "proposed" in r_f5.lower() and '"confirm"' in r_f5.lower()
    )


    print("\n" + "=" * 70)
    print("PART G - PERSISTENCE SAFETY")
    print("=" * 70)

    after_assignments = hash_file(REPO / "data/assignments.json")
    after_room_shifts = hash_file(REPO / "data/room_shifts.json")
    after_exam_duties = hash_file(REPO / "data/exam_duties.json")

    check(
        "G: data/assignments.json byte-for-byte unchanged",
        after_assignments == before_assignments
    )

    check(
        "G: data/room_shifts.json byte-for-byte unchanged (querying "
        "available rooms must never itself confirm a shift)",
        after_room_shifts == before_room_shifts
    )

    check(
        "G: data/exam_duties.json byte-for-byte unchanged",
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


def test_room_candidate_fix_scenarios():
    """
    Pytest entry point. Runs the exact same room-value-
    contamination regression suite as
    `python test_room_candidate_fix.py` (via main()) and
    asserts every check passed. main() never calls sys.exit(),
    so this behaves as a normal pytest test rather than
    aborting the test process.
    """

    exit_code = main()

    assert exit_code == 0, "one or more room-candidate checks failed "\
        "(see printed output above for details)"


if __name__ == "__main__":
    sys.exit(main())