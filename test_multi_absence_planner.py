"""
Manual verification script for scheduling.multi_absence_planner.
MultiAbsenceCoordinator.

Run standalone:
    python3 test_multi_absence_planner.py

Run under pytest:
    python -m pytest test_multi_absence_planner.py -q
    python -m pytest -q          (collects it along with everything else)

This matches this repository's existing test_*.py convention
(runnable, print-based verification scripts against the REAL
loaded timetable data, e.g. test_faculty_fixes.py) rather than a
pytest suite - there is no existing pytest-style coverage for
scheduling/ to stay consistent with. Every scenario below prints
an explicit PASS/FAIL line.

All of the actual scenario logic lives in main(), which RETURNS
an exit code (0 = all checks passed, 1 = at least one failed)
instead of calling sys.exit() itself. This is what makes the file
safe for pytest collection: pytest imports every test_*.py module
it discovers, and a bare `sys.exit(...)` sitting at module level
(rather than behind `if __name__ == "__main__":`) would raise
SystemExit during that import/collection step itself, which
pytest cannot recover from. Two thin wrappers call main():

    - `if __name__ == "__main__": sys.exit(main())` for standalone
      command-line use (`python test_multi_absence_planner.py`),
      which still exits with a non-zero status on failure exactly
      as before.
    - `test_multi_absence_scenarios()`, a normal pytest test
      function (pytest discovers it by its `test_` prefix) that
      calls main() and asserts its return code is 0. Because this
      only runs when pytest actually EXECUTES the test - never at
      import/collection time - it cannot cause a collection error.

DATA SAFETY
-----------
Every scenario that needs to CONFIRM an assignment, or needs to
simulate an assignment already being confirmed, uses a throwaway
FacultyAssignmentEngine backed by a temporary AssignmentStore
file inside a tempfile.mkdtemp() directory - never the real
data/assignments.json.

Scenario 12 is the only one that touches the real store, and it
only ever calls plan() (read-only) against it - never confirm().
The scenario explicitly hashes data/assignments.json before and
after to prove it is byte-for-byte unchanged.

No faculty name, class name, subject, or slot used below is
invented - every one was discovered by inspecting the actual
loaded timetable data before writing this script (see the
Phase 2 write-up for how each was found).
"""

import shutil
import sys
import tempfile
from pathlib import Path

from faculty_chatbot import FacultyAIChatbot
from scheduling.assignment_store import AssignmentStore
from scheduling.assignment_engine import FacultyAssignmentEngine
from scheduling.conflict_engine import FacultyConflictEngine
from scheduling.multi_absence_planner import MultiAbsenceCoordinator


def main() -> int:
    """
    Runs the full real-data scenario suite and returns a
    process-style exit code (0 = all checks passed, 1 =
    at least one check failed). Never calls sys.exit() itself,
    so it is safe to call from a pytest test function as well
    as from the __main__ guard below - see
    test_multi_absence_scenarios().
    """

    RESULTS = []


    def check(name, condition, details=""):
        if condition:
            RESULTS.append((name, True, ""))
            print(f"PASS - {name}")
        else:
            RESULTS.append((name, False, details))
            print(f"FAIL - {name}  {details}")


    print("=" * 70)
    print("Loading real timetable data and building engines...")
    print("=" * 70)

    bot = FacultyAIChatbot()

    TMP_DIR = Path(tempfile.mkdtemp(prefix="multi_absence_test_"))
    print(f"\nUsing temporary directory for test assignment stores: {TMP_DIR}")

    _counter = {"n": 0}


    def fresh_engine():
        """
        Returns (FacultyAssignmentEngine, AssignmentStore) backed by a
        brand-new, empty, throwaway JSON file inside TMP_DIR. Never
        touches data/assignments.json.
        """
        _counter["n"] += 1
        path = TMP_DIR / f"assignments_{_counter['n']}.json"
        store = AssignmentStore(path=path)
        engine = FacultyAssignmentEngine(
            bot.query_engine,
            bot.absence_engine,
            assignment_store=store,
        )
        return engine, store


    # ================================================================
    # SCENARIO 1 - zero absences
    # ================================================================

    print("\n" + "=" * 70)
    print("SCENARIO 1: zero absences")
    print("=" * 70)

    engine1, store1 = fresh_engine()
    coord1 = MultiAbsenceCoordinator(
        bot.absence_engine, engine1, bot.workload_engine
    )

    plan1 = coord1.plan([])

    check(
        "S1: empty absence list produces an empty, valid plan",
        plan1["covered_count"] == 0
        and plan1["uncovered_count"] == 0
        and plan1["covered"] == []
        and plan1["uncovered"] == [],
        str(plan1),
    )

    check(
        "S1: plan() with zero absences is side-effect free",
        store1.all() == [],
    )

    # ================================================================
    # SCENARIO 2 - one absence (must be at least as good as the
    # existing single-absence best_replacements())
    # ================================================================

    print("\n" + "=" * 70)
    print("SCENARIO 2: one absence - Mr. Rajesh Rajaan / Monday")
    print("=" * 70)

    engine2, store2 = fresh_engine()
    coord2 = MultiAbsenceCoordinator(
        bot.absence_engine, engine2, bot.workload_engine
    )

    plan2 = coord2.plan([
        {"teacher": "Mr. Rajesh Rajaan", "day": "Monday"},
    ])

    best2 = bot.absence_engine.best_replacements(
        "Mr. Rajesh Rajaan", "Monday"
    )

    for item in plan2["covered"]:
        print(
            f"  block {item['slots']} ({item['class_name']}) -> "
            f"{item['replacement_teacher']} "
            f"(priority {item['priority']})"
        )

    check(
        "S2: covers every block best_replacements() covers",
        plan2["covered_count"] == best2["block_count"],
        f"plan={plan2['covered_count']} best_replacements="
        f"{best2['block_count']}",
    )

    total_priority_plan = sum(
        item["priority"] for item in plan2["covered"]
    )

    total_priority_best = sum(
        rec["priority"]
        for rec in best2["recommendations"]
        if rec.get("priority") is not None
    )

    check(
        "S2: total candidate quality is at least as good as "
        "best_replacements() (lower total priority is better)",
        total_priority_plan <= total_priority_best,
        f"plan_total_priority={total_priority_plan} "
        f"best_replacements_total_priority={total_priority_best}",
    )

    check(
        "S2: plan() is side-effect free (no store writes)",
        store2.all() == [],
    )

    # ================================================================
    # SCENARIO 3 & 6 - two absences with NON-overlapping blocks
    # ================================================================

    print("\n" + "=" * 70)
    print(
        "SCENARIO 3 & 6: two absences, non-overlapping blocks - "
        "Mr. Rajesh Rajaan (slots 4,6,7) + Dr. Aakriti Sharma "
        "(slots 1,5) / Monday"
    )
    print("=" * 70)

    engine3, store3 = fresh_engine()
    coord3 = MultiAbsenceCoordinator(
        bot.absence_engine, engine3, bot.workload_engine
    )

    plan3 = coord3.plan([
        {"teacher": "Mr. Rajesh Rajaan", "day": "Monday"},
        {"teacher": "Dr. Aakriti Sharma", "day": "Monday"},
    ])

    for item in plan3["covered"]:
        print(
            f"  {item['absent_teacher']} block {item['slots']} -> "
            f"{item['replacement_teacher']}"
        )

    rajaan_slots = {
        slot
        for item in plan3["covered"]
        if item["absent_teacher"] == "Mr. Rajesh Rajaan"
        for slot in item["slots"]
    }

    aakriti_slots = {
        slot
        for item in plan3["covered"]
        if item["absent_teacher"] == "Dr. Aakriti Sharma"
        for slot in item["slots"]
    }

    check(
        "S3/6: the two absent teachers' blocks do not share any slot "
        "(genuinely non-overlapping input)",
        not (rajaan_slots & aakriti_slots),
        f"rajaan_slots={rajaan_slots} aakriti_slots={aakriti_slots}",
    )

    check(
        "S3/6: both absent teachers get every block covered "
        "independently",
        plan3["covered_count"] == 4 and plan3["uncovered_count"] == 0,
        str(plan3["uncovered"]),
    )

    check(
        "S3/6: plan() is side-effect free",
        store3.all() == [],
    )

    # ================================================================
    # SCENARIO 7 - multi-period block preserved as one unit
    # ================================================================

    print("\n" + "=" * 70)
    print(
        "SCENARIO 7: multi-period block (Mr. Rajesh Rajaan's "
        "2-period [6, 7] block) is never split"
    )
    print("=" * 70)

    multi_block_item = next(
        (
            item
            for item in plan2["covered"]
            if item["slots"] == [6, 7]
        ),
        None,
    )

    check(
        "S7: the [6, 7] block exists in the plan as ONE covered "
        "entry with both slots together",
        multi_block_item is not None
        and multi_block_item["period_count"] == 2,
        str(multi_block_item),
    )

    # Confirm it end-to-end through the EXISTING assignment engine
    # and verify the persisted assignment also kept both slots
    # together (uses its own fresh temp store).

    engine7, store7 = fresh_engine()
    coord7 = MultiAbsenceCoordinator(
        bot.absence_engine, engine7, bot.workload_engine
    )

    plan7 = coord7.plan([
        {"teacher": "Mr. Rajesh Rajaan", "day": "Monday"},
    ])

    confirm7 = coord7.confirm(plan7)

    persisted_multi_block = next(
        (
            a
            for a in confirm7["confirmed"]
            if a.get("slots") == [6, 7]
        ),
        None,
    )

    check(
        "S7: confirm() persists the multi-period block as a single "
        "assignment with slots [6, 7] (never split into two "
        "single-slot assignments)",
        persisted_multi_block is not None
        and persisted_multi_block.get("period_count") == 2,
        str(confirm7),
    )

    check(
        "S7: confirm() reused assignment_engine.assign_recommendation "
        "- no failures",
        confirm7["failed_count"] == 0,
        str(confirm7["failed"]),
    )

    # ================================================================
    # SCENARIO 4, 5, 8, 10 - real candidate contention
    #
    # Discovered by inspection: Dr. Aakriti Sharma, Dr. Arpita
    # Sharma, and Ms.Allisa Goyal each have a Monday block whose
    # single BEST (tier-1) candidate is the SAME person -
    # Ms. Kiran Aahuja - even though the blocks themselves are at
    # different slots. Because this planner matches each candidate
    # to at most one block per plan, this is genuine, real
    # contention: at most one of these three blocks can actually
    # receive Ms. Kiran Aahuja.
    # ================================================================

    print("\n" + "=" * 70)
    print(
        "SCENARIO 4, 5, 8, 10: real multi-way contention - three "
        "absences whose top candidate is the same person "
        "(Ms. Kiran Aahuja) / Monday"
    )
    print("=" * 70)

    engine4, store4 = fresh_engine()
    coord4 = MultiAbsenceCoordinator(
        bot.absence_engine, engine4, bot.workload_engine
    )

    contended_absences = [
        {"teacher": "Dr. Aakriti Sharma", "day": "Monday"},
        {"teacher": "Dr. Arpita Sharma", "day": "Monday"},
        {"teacher": "Ms.Allisa Goyal", "day": "Monday"},
    ]

    plan4 = coord4.plan(contended_absences)

    for item in plan4["covered"]:
        print(
            f"  {item['absent_teacher']} block {item['slots']} -> "
            f"{item['replacement_teacher']} "
            f"(priority {item['priority']})"
        )

    for item in plan4["uncovered"]:
        print(
            f"  UNCOVERED {item['absent_teacher']} block "
            f"{item['slots']}: {item['reason']}"
        )

    kiran_uses = [
        item
        for item in plan4["covered"]
        if item["replacement_teacher"] == "Ms. Kiran Aahuja"
    ]

    check(
        "S4/8: Ms. Kiran Aahuja is used for AT MOST ONE block across "
        "the whole plan (never double-booked across absences)",
        len(kiran_uses) <= 1,
        str(kiran_uses),
    )

    replacement_names = [
        item["replacement_teacher"] for item in plan4["covered"]
    ]

    check(
        "S5/10: no replacement teacher appears more than once in "
        "this plan (each candidate matched to at most one block)",
        len(replacement_names) == len(set(replacement_names)),
        str(replacement_names),
    )

    expected_total_blocks = sum(
        len(
            bot.absence_engine.replacement_candidates(
                absence["teacher"], absence["day"]
            )["blocks"]
        )
        for absence in contended_absences
    )

    check(
        "S4/8/10: real alternative candidates existed, so the "
        "contention is resolved rather than leaving blocks "
        "uncovered (every affected block across all three "
        "absences was covered)",
        plan4["uncovered_count"] == 0
        and plan4["covered_count"] == expected_total_blocks,
        f"covered={plan4['covered_count']} "
        f"expected_total_blocks={expected_total_blocks} "
        f"uncovered={plan4['uncovered']}",
    )

    check(
        "S4/5/8/10: plan() is side-effect free",
        store4.all() == [],
    )

    # ================================================================
    # SCENARIO 9 - replacement-assignment conflict (a CONFIRMED
    # assignment must exclude an otherwise-eligible candidate)
    # ================================================================

    print("\n" + "=" * 70)
    print(
        "SCENARIO 9: a pre-existing CONFIRMED assignment must "
        "exclude that candidate from a new plan"
    )
    print("=" * 70)

    engine9, store9 = fresh_engine()

    seed_result = engine9.assign(
        "Ms. Kiran Aahuja",
        "Monday",
        [1],
        absent_teacher="Test Seed (scenario 9)",
        subject="Seed",
        class_name="SEED",
        period_count=1,
    )

    check(
        "S9 setup: seed confirmed assignment for Ms. Kiran Aahuja / "
        "Monday slot 1 was created in the TEMP store",
        seed_result.get("success") is True,
        str(seed_result),
    )

    coord9 = MultiAbsenceCoordinator(
        bot.absence_engine, engine9, bot.workload_engine
    )

    plan9 = coord9.plan([
        {"teacher": "Dr. Aakriti Sharma", "day": "Monday"},
    ])

    slot1_item = next(
        (
            item
            for item in plan9["covered"] + plan9["uncovered"]
            if item["slots"] == [1]
        ),
        None,
    )

    print(f"  Dr. Aakriti Sharma slot-1 block result: {slot1_item}")

    check(
        "S9: Ms. Kiran Aahuja (already confirmed elsewhere) is NOT "
        "chosen again for Dr. Aakriti Sharma's slot-1 block",
        slot1_item is not None
        and slot1_item.get("replacement_teacher") != "Ms. Kiran Aahuja",
        str(slot1_item),
    )

    check(
        "S9: plan() itself added no NEW entries to the store (still "
        "only the one seeded assignment)",
        len(store9.all()) == 1,
        str(store9.all()),
    )

    # ================================================================
    # SCENARIO 11 - impossible schedule
    #
    # Discovered by inspection: Mr. Kapil Sharma's Monday block
    # (slots [1, 2, 3], a 3-period lab block) has exactly two
    # real qualified candidates: Dr. Rashmi Kaushik and
    # Ms. Kiran Aahuja. Pre-confirming BOTH of them at slot 1 (part
    # of that block) makes the block genuinely impossible to cover.
    # ================================================================

    print("\n" + "=" * 70)
    print(
        "SCENARIO 11: impossible schedule - both of a block's only "
        "two real candidates are already confirmed elsewhere"
    )
    print("=" * 70)

    engine11, store11 = fresh_engine()

    seed_a = engine11.assign(
        "Dr. Rashmi Kaushik",
        "Monday",
        [1],
        absent_teacher="Test Seed (scenario 11)",
        subject="Seed",
        class_name="SEED",
        period_count=1,
    )

    seed_b = engine11.assign(
        "Ms. Kiran Aahuja",
        "Monday",
        [1],
        absent_teacher="Test Seed (scenario 11)",
        subject="Seed",
        class_name="SEED",
        period_count=1,
    )

    check(
        "S11 setup: both seed assignments created successfully in "
        "the TEMP store",
        seed_a.get("success") is True and seed_b.get("success") is True,
        f"{seed_a} / {seed_b}",
    )

    coord11 = MultiAbsenceCoordinator(
        bot.absence_engine, engine11, bot.workload_engine
    )

    plan11 = coord11.plan([
        {"teacher": "Mr. Kapil Sharma", "day": "Monday"},
    ])

    print(f"  covered={plan11['covered']}")
    print(f"  uncovered={plan11['uncovered']}")

    check(
        "S11: the block is reported uncovered rather than partially/"
        "silently assigned",
        plan11["covered_count"] == 0 and plan11["uncovered_count"] == 1,
        str(plan11),
    )

    check(
        "S11: the uncovered reason is 'no_qualified_candidate' "
        "(both real candidates were filtered out before matching)",
        plan11["uncovered"]
        and plan11["uncovered"][0]["reason"] == "no_qualified_candidate",
        str(plan11["uncovered"]),
    )

    check(
        "S11: the uncovered block still reports the full, unsplit "
        "3-period block ([1, 2, 3])",
        plan11["uncovered"]
        and plan11["uncovered"][0]["slots"] == [1, 2, 3]
        and plan11["uncovered"][0]["period_count"] == 3,
        str(plan11["uncovered"]),
    )

    check(
        "S11: plan() added no new entries to the store beyond the "
        "two seeded ones (still side-effect free)",
        len(store11.all()) == 2,
        str(store11.all()),
    )

    # ================================================================
    # SCENARIO 12 - existing (REAL) assignments already present
    #
    # Uses the REAL bot.assignment_engine / data/assignments.json,
    # read-only (plan() only, never confirm()). The repository's
    # committed data/assignments.json already contains one real
    # confirmed assignment: Ms. Nidhi Srivastav replacing
    # Mr. Rajesh Rajaan on Monday, slot 4.
    # ================================================================

    print("\n" + "=" * 70)
    print(
        "SCENARIO 12: existing REAL confirmed assignment must be "
        "respected (read-only against data/assignments.json)"
    )
    print("=" * 70)

    real_store_path = Path("data/assignments.json")

    before_bytes = (
        real_store_path.read_bytes()
        if real_store_path.exists()
        else b""
    )

    real_assignments_before = bot.assignment_engine.assignments()

    print(f"  real store currently contains: {real_assignments_before}")

    check(
        "S12 precondition: the real store currently contains the "
        "known confirmed assignment (Ms. Nidhi Srivastav / "
        "Mr. Rajesh Rajaan / Monday / slot 4)",
        any(
            a.get("replacement_teacher") == "Ms. Nidhi Srivastav"
            and a.get("day", "").lower() == "monday"
            and 4 in (a.get("slots") or [])
            for a in real_assignments_before
        ),
        str(real_assignments_before),
    )

    coord12 = MultiAbsenceCoordinator(
        bot.absence_engine, bot.assignment_engine, bot.workload_engine
    )

    plan12 = coord12.plan([
        {"teacher": "Mr. Rajesh Rajaan", "day": "Monday"},
    ])

    after_bytes = (
        real_store_path.read_bytes()
        if real_store_path.exists()
        else b""
    )

    for item in plan12["covered"]:
        print(
            f"  block {item['slots']} -> {item['replacement_teacher']}"
        )

    slot4_item = next(
        (item for item in plan12["covered"] if item["slots"] == [4]),
        None,
    )

    check(
        "S12: Ms. Nidhi Srivastav (already confirmed for this exact "
        "slot) is not proposed again for the slot-4 block",
        slot4_item is not None
        and slot4_item["replacement_teacher"] != "Ms. Nidhi Srivastav",
        str(slot4_item),
    )

    check(
        "S12: data/assignments.json is byte-for-byte unchanged after "
        "plan() (no side effects on the real store)",
        before_bytes == after_bytes,
    )

    # ================================================================
    # SUMMARY
    # ================================================================

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = total - passed

    print(f"{passed}/{total} checks passed.")

    if failed:
        print(f"\n{failed} FAILURE(S):")
        for name, ok, details in RESULTS:
            if not ok:
                print(f"  - {name}  {details}")

    shutil.rmtree(TMP_DIR, ignore_errors=True)

    return 1 if failed else 0


def test_multi_absence_scenarios():
    """
    Pytest entry point. Runs the exact same real-data
    scenario suite as `python test_multi_absence_planner.py`
    (via main()) and asserts every check passed. main() never
    calls sys.exit(), so this behaves as a normal pytest test
    rather than aborting the test process.
    """

    exit_code = main()

    assert exit_code == 0, "one or more multi-absence planner checks failed "\
        "(see printed output above for details)"


if __name__ == "__main__":
    sys.exit(main())