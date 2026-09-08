"""
=============================================================
UNISCHED AI - REAL CANONICAL EVENT TEST
=============================================================

Tests the complete pipeline:

    PDF + Excel + CSV
          ↓
    Import Manager
          ↓
    Universal Records
          ↓
    Canonical Event Matcher
          ↓
    Scheduled Events
    Faculty Free Slots
    Class Free Slots
    Room Free Slots
    Contract Records
    Unmatched Records
    Conflicts

=============================================================
"""

from pathlib import Path
import sys

from import_engine.import_manager import ImportManager
from data_engine.canonical_event_matcher import CanonicalEventMatcher


# =============================================================
# PROJECT PATH
# =============================================================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"


# =============================================================
# REAL DATASET FILES
# =============================================================

FILES = [

    "Facultywise TT 20 sep.pdf",

    "classwise TT 27 sep.pdf",

    "Location wise TT 27 sep 2025.pdf",

    "timetable.xlsx",

    "test_timetable.csv",

]

def main() -> int:
    """
    Runs the full real-data canonical-event pipeline test and
    returns a process-style exit code (0 = completed, 1 = no
    records were imported at all). Never calls sys.exit()/
    raises SystemExit itself, so it is safe to call from a
    pytest test function as well as from the __main__ guard
    below - see test_canonical_events_pipeline().
    """


    # =============================================================
    # HEADER
    # =============================================================

    print("=" * 80)

    print(
        "UNISCHED AI - REAL CANONICAL EVENT TEST"
    )

    print("=" * 80)

    print()


    # =============================================================
    # IMPORT MANAGER
    # =============================================================

    manager = ImportManager()


    all_records = []

    successful_files = []


    # =============================================================
    # IMPORT ALL FILES
    # =============================================================

    for filename in FILES:

        print(
            f"IMPORTING: {filename}"
        )

        file_path = DATA_DIR / filename

        # ---------------------------------------------------------
        # Check file
        # ---------------------------------------------------------

        if not file_path.exists():

            print(
                f"ERROR: File not found: {file_path}"
            )

            print()

            continue

        try:

            # -----------------------------------------------------
            # ImportManager supports:
            #
            # import_file(filename, file_path)
            #
            # The exact signature used by the project is handled
            # below.
            # -----------------------------------------------------

            try:

                result = manager.import_file(
                    filename,
                    str(file_path)
                )

            except TypeError:

                # Fallback for ImportManager implementations
                # that accept only file_path.

                result = manager.import_file(
                    str(file_path)
                )

            # -----------------------------------------------------
            # Result should be a dictionary
            # -----------------------------------------------------

            if not isinstance(
                result,
                dict
            ):

                print(
                    "ERROR: Import Manager returned an unexpected result."
                )

                print(
                    "Returned type:",
                    type(result)
                )

                print()

                continue

            # -----------------------------------------------------
            # Check success
            # -----------------------------------------------------

            status = result.get(
                "status",
                "SUCCESS"
            )

            if str(status).upper() not in {
                "SUCCESS",
                "OK",
            }:

                print(
                    "FAILED"
                )

                print(
                    "Status:",
                    status
                )

                print(
                    "Error:",
                    result.get(
                        "error"
                    )
                )

                print()

                continue

            # -----------------------------------------------------
            # Get records
            # -----------------------------------------------------

            records = result.get(
                "records",
                []
            )

            if records is None:

                records = []

            # -----------------------------------------------------
            # Some ImportManager implementations may return
            # imported_data instead of records.
            # -----------------------------------------------------

            if not records:

                records = result.get(
                    "imported_records",
                    []
                )

            # -----------------------------------------------------
            # Add source information if missing
            # -----------------------------------------------------

            cleaned_records = []

            for record in records:

                if not isinstance(
                    record,
                    dict
                ):

                    continue

                record = dict(
                    record
                )

                if not record.get(
                    "source_file"
                ):

                    record[
                        "source_file"
                    ] = filename

                if not record.get(
                    "source_type"
                ):

                    record[
                        "source_type"
                    ] = file_path.suffix.lower().replace(
                        ".",
                        ""
                    )

                cleaned_records.append(
                    record
                )

            # -----------------------------------------------------
            # Add to global list
            # -----------------------------------------------------

            all_records.extend(
                cleaned_records
            )

            successful_files.append(
                filename
            )

            print(
                f"SUCCESS: {len(cleaned_records)}"
            )

        except Exception as exc:

            print(
                "FAILED"
            )

            print(
                "Error:",
                type(exc).__name__,
                str(exc)
            )

        print()


    # =============================================================
    # TOTAL IMPORTED RECORDS
    # =============================================================

    print("=" * 80)

    print(
        f"TOTAL IMPORTED RECORDS: {len(all_records)}"
    )

    print("=" * 80)

    print()


    # =============================================================
    # STOP IF NOTHING IMPORTED
    # =============================================================

    if not all_records:

        print(
            "ERROR: No records were imported."
        )

        print(
            "Check your ImportManager and dataset paths."
        )

        return 1


    # =============================================================
    # CANONICAL MATCHER
    # =============================================================

    matcher = CanonicalEventMatcher(
        all_records
    )


    # =============================================================
    # MATCH
    # =============================================================

    canonical_events = matcher.match()


    # =============================================================
    # SUMMARY
    # =============================================================

    summary = matcher.summary()


    print("=" * 80)

    print(
        "CANONICAL DATA SUMMARY"
    )

    print("=" * 80)

    print()

    print(
        "Canonical timetable events:",
        summary[
            "canonical_events"
        ]
    )

    print(
        "Faculty free slots:",
        summary[
            "faculty_free_slots"
        ]
    )

    print(
        "Class free slots:",
        summary[
            "class_free_slots"
        ]
    )

    print(
        "Room free slots:",
        summary[
            "room_free_slots"
        ]
    )

    print(
        "Contract records:",
        summary[
            "contract_records"
        ]
    )

    print(
        "Unmatched records:",
        summary[
            "unmatched_records"
        ]
    )

    print(
        "Matched groups:",
        summary[
            "matched_groups"
        ]
    )

    print(
        "Multi-source events:",
        summary[
            "multi_source_events"
        ]
    )

    print(
        "Conflict events:",
        summary[
            "conflict_events"
        ]
    )

    print()


    # =============================================================
    # FILE-WISE INFORMATION
    # =============================================================

    print("=" * 80)

    print(
        "SUCCESSFULLY IMPORTED FILES"
    )

    print("=" * 80)

    print()

    for filename in successful_files:

        print(
            f"✓ {filename}"
        )

    print()


    # =============================================================
    # FREE SLOT SAMPLE
    # =============================================================

    free_slots = (
        matcher.get_faculty_free_slots()
    )


    print("=" * 80)

    print(
        "FACULTY FREE SLOT SAMPLE"
    )

    print("=" * 80)

    print()


    for index, record in enumerate(
        free_slots[:10],
        start=1
    ):

        print(
            f"FREE SLOT {index}"
        )

        print(
            "Teacher:",
            record.get(
                "teacher",
                ""
            )
        )

        print(
            "Day:",
            record.get(
                "day",
                ""
            )
        )

        print(
            "Slot:",
            record.get(
                "slot"
            )
        )

        print(
            "Time:",
            record.get(
                "slot_time",
                ""
            )
        )

        print(
            "Source:",
            record.get(
                "source_file",
                ""
            )
        )

        print()


    # =============================================================
    # CANONICAL EVENT SAMPLE
    # =============================================================

    print("=" * 80)

    print(
        "CANONICAL EVENT SAMPLE"
    )

    print("=" * 80)

    print()


    for index, event in enumerate(
        canonical_events[:10],
        start=1
    ):

        print(
            f"EVENT {index}"
        )

        print(
            "Teacher:",
            event.get(
                "teacher",
                ""
            )
        )

        print(
            "Day:",
            event.get(
                "day",
                ""
            )
        )

        print(
            "Slot:",
            event.get(
                "slot"
            )
        )

        print(
            "Time:",
            event.get(
                "slot_time",
                ""
            )
        )

        print(
            "Subject:",
            event.get(
                "subject",
                ""
            )
        )

        print(
            "Room:",
            event.get(
                "room",
                ""
            )
        )

        print(
            "Class:",
            event.get(
                "class_name",
                ""
            )
        )

        print(
            "Sources:",
            event.get(
                "sources",
                []
            )
        )

        print()


    # =============================================================
    # CONTRACT SAMPLE
    # =============================================================

    contracts = (
        matcher.get_contract_records()
    )


    print("=" * 80)

    print(
        "CONTRACT RECORD SAMPLE"
    )

    print("=" * 80)

    print()


    for index, record in enumerate(
        contracts[:5],
        start=1
    ):

        print(
            f"CONTRACT {index}"
        )

        print(
            "Teacher:",
            record.get(
                "teacher",
                ""
            )
        )

        print(
            "Subject:",
            record.get(
                "subject",
                ""
            )
        )

        print(
            "Class:",
            record.get(
                "class_name",
                ""
            )
        )

        print(
            "Group:",
            record.get(
                "group_name",
                ""
            )
        )

        print(
            "Room:",
            record.get(
                "room",
                ""
            )
        )

        print()


    # =============================================================
    # CONFLICT SAMPLE
    # =============================================================

    conflicts = matcher.conflicts


    print("=" * 80)

    print(
        "CONFLICT SAMPLE"
    )

    print("=" * 80)

    print()


    if not conflicts:

        print(
            "No conflicts detected."
        )

    else:

        for index, conflict in enumerate(
            conflicts[:10],
            start=1
        ):

            print(
                f"CONFLICT {index}"
            )

            print(
                "Teacher:",
                conflict.get(
                    "teacher",
                    ""
                )
            )

            print(
                "Day:",
                conflict.get(
                    "day",
                    ""
                )
            )

            print(
                "Slot:",
                conflict.get(
                    "slot"
                )
            )

            print(
                "Subject:",
                conflict.get(
                    "subject",
                    ""
                )
            )

            print(
                "Room:",
                conflict.get(
                    "room",
                    ""
                )
            )

            print(
                "Class:",
                conflict.get(
                    "class_name",
                    ""
                )
            )

            print(
                "Conflicts:",
                conflict.get(
                    "conflicts",
                    []
                )
            )

            print()


    # =============================================================
    # FINAL RESULT
    # =============================================================

    print("=" * 80)

    print(
        "CANONICAL EVENT TEST COMPLETED"
    )

    print("=" * 80)

    print()

    print(
        f"Imported records : {len(all_records)}"
    )

    print(
        f"Canonical events : {len(canonical_events)}"
    )

    print(
        f"Faculty free     : {len(free_slots)}"
    )

    print(
        f"Contracts        : {len(contracts)}"
    )

    print(
        f"Unmatched        : {len(matcher.get_unmatched_records())}"
    )

    print()

    print(
        "UNISCHED AI CANONICAL MATCHER TEST FINISHED."
    )

    print("=" * 80)

    return 0


def test_canonical_events_pipeline():
    """
    Pytest entry point. Runs the exact same real-data
    canonical-event pipeline test as
    `python test_canonical_events.py` (via main()) and asserts
    it completed successfully. main() never raises SystemExit,
    so this behaves as a normal pytest test rather than
    aborting the test process.
    """

    exit_code = main()

    assert exit_code == 0, "canonical-event pipeline test failed "\
        "(see printed output above for details)"


if __name__ == "__main__":
    sys.exit(main())