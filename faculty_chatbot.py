import re
from datetime import datetime, date

from engine.query_tokenizer import QueryTokenizer
from engine.stopword_filter import StopWordFilter
from engine.day_slot_extractor import DaySlotExtractor
from engine.entity_extractor import EntityExtractor
from engine.intent_detector import IntentDetector
from engine.query_planner import QueryPlanner
from engine.response_generator import ResponseGenerator
from engine.response_formatter import format_workload_response

from import_engine.import_manager import ImportManager
from data_engine.canonical_event_matcher import CanonicalEventMatcher
from scheduling.workload_engine import FacultyWorkloadEngine
from scheduling.absence_engine import FacultyAbsenceEngine
from scheduling.assignment_engine import FacultyAssignmentEngine
from scheduling.room_shift_store import RoomShiftStore
from scheduling.lab_shift_planner import LabShiftCoordinator
from scheduling.multi_absence_planner import MultiAbsenceCoordinator
from scheduling.what_if_coordinator import WhatIfCoordinator
from scheduling.exam_duty_store import ExamDutyStore
from scheduling.exam_duty_planner import ExamDutyCoordinator
from query_engine import QueryEngine
from query_engine.natural_language_query import NaturalLanguageQuery



class FacultyAIChatbot:

    def __init__(self, files=None, matcher=None):

        print("\n" + "=" * 70)
        print("LOADING FACULTY AI KNOWLEDGE BASE")
        print("=" * 70)

        # --------------------------------------------------
        # PRE-BUILT CANONICAL MATCHER (OPTIONAL)
        #
        # Lets a caller that has ALREADY imported and
        # canonicalized timetable data itself (e.g. the web
        # app, which lets a user upload files through its own
        # ImportManager-based pipeline) hand that finished
        # CanonicalEventMatcher straight to FacultyAIChatbot,
        # instead of FacultyAIChatbot importing the same files
        # a second time through a second, independent pipeline.
        # `matcher.match()` is assumed to have already been
        # called by the caller. `files` is ignored (kept only
        # as informational metadata) whenever `matcher` is
        # supplied. When `matcher` is omitted, behavior is
        # completely unchanged from before: FacultyAIChatbot
        # imports `files` (or its own default file list) itself.
        # --------------------------------------------------

        if matcher is not None:

            self.files = files

            self.matcher = matcher

            print(
                "\nUsing a pre-built canonical timetable "
                "(skipping file import)."
            )

            summary = self.matcher.summary()

            print(
                f"Canonical events: "
                f"{summary.get('canonical_events', len(self.matcher.events))}"
            )

        else:

            # --------------------------------------------------
            # FILES
            # --------------------------------------------------

            if files is None:
                files = [
                    "data/Facultywise TT 20 sep.pdf",
                    "data/classwise TT 27 sep.pdf",
                    "data/Location wise TT 27 sep 2025.pdf",
                    "data/timetable.xlsx",
                    "data/test_timetable.csv"
                ]

            self.files = files

            # --------------------------------------------------
            # IMPORT DATA
            # --------------------------------------------------

            manager = ImportManager()

            all_records = []

            print("\nImporting timetable files...")

            for file_path in files:

                try:

                    print(f"\nLoading: {file_path}")

                    result = manager.import_file(file_path)

                    if isinstance(result, dict):
                        records = result.get("records", [])
                    else:
                        records = result

                    print(
                        f"Imported: {len(records)} records"
                    )

                    all_records.extend(records)

                except Exception as e:

                    print(
                        f"Warning: Could not import {file_path}"
                    )

                    print(e)

            print("\n" + "-" * 70)

            print(
                f"Total imported records: {len(all_records)}"
            )

            # --------------------------------------------------
            # CANONICAL MATCHING
            # --------------------------------------------------

            print("\nBuilding canonical timetable...")

            self.matcher = CanonicalEventMatcher(
                all_records
            )

            self.matcher.match()

            summary = self.matcher.summary()

            print(
                f"Canonical events: "
                f"{summary.get('canonical_events', len(self.matcher.events))}"
            )

        # --------------------------------------------------
        # QUERY ENGINE
        # --------------------------------------------------

        print("\nCreating Query Engine...")

        self.query_engine = QueryEngine(
            self.matcher
        )

        print("Query Engine ready.")

        # --------------------------------------------------
        # ENTITY EXTRACTOR
        #
        # Built AFTER the QueryEngine, from
        # query_engine.entity_knowledge() - the faculty/
        # subject/room/class/group names actually present in
        # the CURRENTLY loaded canonical timetable. This
        # replaces the previous dependency on a separate,
        # independently-built database/faculty.db snapshot,
        # which could silently drift out of sync with the
        # real data (confirmed: it was already missing real
        # entries such as "Dr. Nilam", "3CS", and "5CS").
        # If the timetable source files are replaced, entity
        # recognition now automatically reflects the new
        # data on the next FacultyAIChatbot() construction -
        # no separate database rebuild step required.
        # --------------------------------------------------

        self.extractor = EntityExtractor(
            knowledge=self.query_engine.entity_knowledge()
        )

        print("Entity Extractor ready.")

                # --------------------------------------------------
        # SCHEDULING ENGINES
        # --------------------------------------------------

        print("Creating Workload Engine...")

        self.workload_engine = FacultyWorkloadEngine(
            self.query_engine
        )

        print("Workload Engine ready.")

        print("Creating Absence Engine...")

        self.absence_engine = FacultyAbsenceEngine(
            self.query_engine
        )

        print("Absence Engine ready.")

        print("Creating Assignment Engine...")

        self.assignment_engine = FacultyAssignmentEngine(
            self.query_engine,
            self.absence_engine
        )

        print("Assignment Engine ready.")
        self.workload_engine = FacultyWorkloadEngine(
            self.query_engine
        )

        print("Workload Engine ready.")

        self.absence_engine = FacultyAbsenceEngine(
            self.query_engine
        )

        print("Absence Engine ready.")

        # --------------------------------------------------
        # LAB SHIFT COORDINATOR
        #
        # Reuses self.query_engine (room/class lookups) and
        # self.absence_engine (the SAME multi-period block
        # detection already used for absence/replacement
        # planning). Confirmed room shifts are persisted to a
        # SEPARATE small overlay store
        # (data/room_shifts.json), never by mutating canonical
        # events, so the original source timetable stays fully
        # traceable.
        # --------------------------------------------------

        self.room_shift_store = RoomShiftStore()

        self.lab_shift_planner = LabShiftCoordinator(
            self.query_engine,
            self.absence_engine,
            store=self.room_shift_store
        )

        # Holds the most recently PROPOSED (not yet confirmed)
        # room-shift plan for this chatbot session, so a
        # follow-up "confirm the shift" message can reference
        # it. This is conversational state only - the planner
        # itself (LabShiftCoordinator) remains stateless and
        # side-effect free; plan_shift()/confirm_shift() take
        # explicit arguments and never read this attribute.
        self._pending_lab_shift_plan = None

        print("Lab Shift Coordinator ready.")

        # --------------------------------------------------
        # MULTI-ABSENCE COORDINATOR
        #
        # Previously verified independently (test_multi_
        # absence_planner.py, 26/26 checks passed) but not
        # wired into the live chatbot. Constructed here so the
        # What-If Coordinator (below) can reuse its
        # side-effect-free plan() for absence what-if
        # simulation, without duplicating any block-detection
        # or replacement-ranking logic.
        # --------------------------------------------------

        self.multi_absence_coordinator = MultiAbsenceCoordinator(
            self.absence_engine,
            self.assignment_engine,
            self.workload_engine
        )

        print("Multi-Absence Coordinator ready.")

        # --------------------------------------------------
        # WHAT-IF COORDINATOR
        #
        # A thin layer that only delegates to the three
        # existing side-effect-free planners/checkers above
        # (multi_absence_coordinator.plan(),
        # lab_shift_planner.plan_shift(),
        # assignment_engine.check_conflict()) and normalizes
        # their results into one consistent, explanatory
        # shape. Never confirms/persists anything itself.
        # --------------------------------------------------

        self.what_if_coordinator = WhatIfCoordinator(
            self.query_engine,
            self.absence_engine,
            self.assignment_engine,
            self.lab_shift_planner,
            self.multi_absence_coordinator
        )

        print("What-If Coordinator ready.")

        # --------------------------------------------------
        # EXAM DUTY COORDINATOR
        #
        # Reuses self.query_engine (HH:MM validation) and
        # self.workload_engine (exam_duty_candidates() - the
        # existing free/busy + workload ranking). Confirmed exam
        # duties are persisted to a SEPARATE small overlay store
        # (data/exam_duties.json), never by mutating canonical
        # events, AssignmentStore, or RoomShiftStore.
        # --------------------------------------------------

        self.exam_duty_store = ExamDutyStore()

        self.exam_duty_coordinator = ExamDutyCoordinator(
            self.query_engine,
            self.workload_engine,
            store=self.exam_duty_store
        )

        # Holds the most recently PROPOSED (not yet confirmed)
        # exam-duty plan for this chatbot session, so a follow-up
        # "confirm" message can reference it. Conversational state
        # only - ExamDutyCoordinator itself remains stateless and
        # side-effect free; plan_duty()/confirm_duty() take
        # explicit arguments and never read this attribute.
        self._pending_exam_duty_plan = None

        print("Exam Duty Coordinator ready.")

        self.nl_query = NaturalLanguageQuery(
            self.query_engine
        )

        print("Natural Language Query Engine ready.")

        print("\n" + "=" * 70)
        print("KNOWLEDGE BASE LOADED SUCCESSFULLY")
        print("=" * 70)

    # ======================================================
    # TIME UTILITIES
    # ======================================================

    @staticmethod
    def _normalize_time(value):

        """
        Convert different time formats into HH:MM.

        Supported examples:

            9:15
            09:15
            9.15
            09.15
            9:15 AM
            11:15 PM
            9 AM
            11 PM

        No faculty names or timetable values are hard-coded.
        """

        if not value:
            return None

        value = str(value).strip().lower()

        value = value.replace(".", ":")

        # Remove unnecessary spaces
        value = re.sub(r"\s+", " ", value)

        # --------------------------------------------------
        # HH:MM AM/PM
        # --------------------------------------------------

        match = re.fullmatch(
            r"(\d{1,2}):(\d{2})\s*(am|pm)?",
            value
        )

        if not match:
            # ----------------------------------------------
            # HH AM/PM
            # ----------------------------------------------

            match = re.fullmatch(
                r"(\d{1,2})\s*(am|pm)",
                value
            )

            if not match:
                # ------------------------------------------
                # Plain hour: HH
                # Example: 9 -> 09:00
                #          11 -> 11:00
                # ------------------------------------------

                match = re.fullmatch(
                    r"(\d{1,2})",
                    value
                )

                if not match:
                    return None

                hour = int(match.group(1))
                minute = 0
                meridiem = None
            else:
                hour = int(match.group(1))
                minute = 0
                meridiem = match.group(2)
        else:
            hour = int(match.group(1))
            minute = int(match.group(2))
            meridiem = match.group(3)

        if hour > 23 or minute > 59:
            return None

        if meridiem:

            if meridiem == "am":

                if hour == 12:
                    hour = 0

            elif meridiem == "pm":

                if hour != 12:
                    hour += 12

        return f"{hour:02d}:{minute:02d}"

    # ======================================================
    # EXTRACT TIME RANGE
    # ======================================================

    @classmethod
    def _extract_time_range(cls, query):

        """
        Extract a start/end time from natural language.

        Examples:

            between 9:15 and 11:15
            from 9:15 to 11:15
            9:15 - 11:15
            09:15–11:15
            between 9.15 and 11.15

        Returns:

            (start_time, end_time)

        or:

            (None, None)
        """

        if not query:
            return None, None

        text = str(query).lower()

        # Normalize dash characters
        text = text.replace("–", "-")
        text = text.replace("—", "-")
        text = text.replace("−", "-")

        # Normalize decimal time
        # Example: 9.15 -> 9:15
        text = re.sub(
            r"(?<!\d)(\d{1,2})\.(\d{2})(?!\d)",
            r"\1:\2",
            text
        )

        # --------------------------------------------------
        # Pattern 1
        #
        # between 9:15 and 11:15
        # --------------------------------------------------

        match = re.search(
            r"\bbetween\s+"
            r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
            r"\s+and\s+"
            r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
            text
        )

        if match:

            start = cls._normalize_time(
                match.group(1)
            )

            end = cls._normalize_time(
                match.group(2)
            )

            if start and end:
                return start, end

        # --------------------------------------------------
        # Pattern 2
        #
        # from 9:15 to 11:15
        # --------------------------------------------------

        match = re.search(
            r"\bfrom\s+"
            r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
            r"\s+to\s+"
            r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
            text
        )

        if match:

            start = cls._normalize_time(
                match.group(1)
            )

            end = cls._normalize_time(
                match.group(2)
            )

            if start and end:
                return start, end

        # --------------------------------------------------
        # Pattern 3
        #
        # 9:15 - 11:15
        # --------------------------------------------------

        match = re.search(
            r"\b"
            r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
            r"\s*-\s*"
            r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
            r"\b",
            text
        )

        if match:

            start = cls._normalize_time(
                match.group(1)
            )

            end = cls._normalize_time(
                match.group(2)
            )

            if start and end:
                return start, end
                    # --------------------------------------------------
        # Pattern 4
        #
        # 13:30 to 15:30
        # --------------------------------------------------

        match = re.search(
            r"\b"
            r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
            r"\s+to\s+"
            r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)"
            r"\b",
            text
        )

        if match:

            start = cls._normalize_time(
                match.group(1)
            )

            end = cls._normalize_time(
                match.group(2)
            )

            if start and end:
                return start, end

        return None, None
    

    # ======================================================
    # FORMAT SLOT RANGE
    #
    # Turns a list of slot numbers like [6, 7] into a readable
    # "6-7", a single slot [6] into "6", and a non-consecutive
    # list like [2, 5] into "2, 5" - purely a display helper,
    # never used for any scheduling decision.
    # ======================================================

    @staticmethod
    def _format_slot_range(slots):

        if not slots:
            return "-"

        ordered = sorted(slots)

        if len(ordered) == 1:
            return str(ordered[0])

        is_consecutive = all(
            ordered[i + 1] == ordered[i] + 1
            for i in range(len(ordered) - 1)
        )

        if is_consecutive:
            return f"{ordered[0]}-{ordered[-1]}"

        return ", ".join(str(slot) for slot in ordered)


    # ======================================================
    # WORD-BOUNDARY KEYWORD MATCH
    #
    # A plain `word in text` substring check is wrong for short
    # trigger words that can appear INSIDE unrelated words - e.g.
    # "lab" is a literal substring of "available" ("avai-LAB-le"),
    # so a naive substring check on "which rooms are available"
    # would incorrectly "see" a lab reference that was never
    # there. This checks for the word as a whole word instead.
    # ======================================================

    @staticmethod
    def _contains_any_word(text, words):

        for word in words:

            if re.search(
                r"\b" + re.escape(word) + r"\b",
                text
            ):

                return True

        return False


    # ======================================================
    # EXTRACT DAY
    # ======================================================

    @staticmethod
    def _extract_day(query):

        """
        Universal weekday extraction.

        Returns the canonical day name.
        """

        text = str(query).lower()

        days = {
            "monday": "Monday",
            "mon": "Monday",

            "tuesday": "Tuesday",
            "tue": "Tuesday",
            "tues": "Tuesday",

            "wednesday": "Wednesday",
            "wed": "Wednesday",

            "thursday": "Thursday",
            "thu": "Thursday",
            "thur": "Thursday",
            "thurs": "Thursday",

            "friday": "Friday",
            "fri": "Friday",

            "saturday": "Saturday",
            "sat": "Saturday",

            "sunday": "Sunday",
            "sun": "Sunday",
        }

        # Longest first prevents partial matching
        for word in sorted(
            days,
            key=len,
            reverse=True
        ):

            if re.search(
                rf"\b{re.escape(word)}\b",
                text
            ):

                return days[word]

        return None

    # ======================================================
    # EXTRACT REAL CALENDAR DATE
    #
    # Unlike _extract_day() above (which only ever recognizes a
    # recurring WEEKDAY name), this recognizes an actual calendar
    # date - required for exam duty, which must be tied to a real
    # date rather than a recurring weekday/slot so that different
    # exam weeks are never confused with one another.
    #
    # Supported examples:
    #
    #     2026-09-10          (ISO)
    #     10-09-2026
    #     10/09/2026
    #     10 September 2026
    #     10th Sept 2026
    #     September 10 2026
    #     Sept 10, 2026
    #
    # Returns an ISO "YYYY-MM-DD" string, or None if no real,
    # valid calendar date is present. No specific institutional
    # date is hard-coded here - only generic calendar-month-name
    # vocabulary used purely to parse whatever date the user
    # actually typed.
    # ======================================================

    _MONTH_NAMES = {
        "january": 1, "jan": 1,
        "february": 2, "feb": 2,
        "march": 3, "mar": 3,
        "april": 4, "apr": 4,
        "may": 5,
        "june": 6, "jun": 6,
        "july": 7, "jul": 7,
        "august": 8, "aug": 8,
        "september": 9, "sep": 9, "sept": 9,
        "october": 10, "oct": 10,
        "november": 11, "nov": 11,
        "december": 12, "dec": 12,
    }

    @classmethod
    def _extract_exam_date(cls, query):

        text = str(query).lower()

        # ISO: 2026-09-10
        match = re.search(
            r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b",
            text
        )

        if match:
            try:
                year, month, day = (
                    int(part) for part in match.groups()
                )
                return date(year, month, day).isoformat()
            except ValueError:
                return None

        # DD-MM-YYYY or DD/MM/YYYY
        match = re.search(
            r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b",
            text
        )

        if match:
            try:
                day, month, year = (
                    int(part) for part in match.groups()
                )
                return date(year, month, day).isoformat()
            except ValueError:
                return None

        # 10 September 2026 / 10th Sept 2026
        match = re.search(
            r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
            r"([a-z]+)\s+(\d{4})\b",
            text
        )

        if match:
            day = int(match.group(1))
            month = cls._MONTH_NAMES.get(match.group(2))
            year = int(match.group(3))

            if month:
                try:
                    return date(year, month, day).isoformat()
                except ValueError:
                    return None

        # September 10 2026 / September 10, 2026 / Sept 10th 2026
        match = re.search(
            r"\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+"
            r"(\d{4})\b",
            text
        )

        if match:
            month = cls._MONTH_NAMES.get(match.group(1))
            day = int(match.group(2))
            year = int(match.group(3))

            if month:
                try:
                    return date(year, month, day).isoformat()
                except ValueError:
                    return None

        return None

    # ======================================================
    # EXTRACT SEMESTER NUMBER
    # ======================================================

    @staticmethod
    def _extract_semester_number(query):
        """
        Extract a semester number (1-10) from phrasing such as
        "7th semester", "seventh semester", "semester 7", or
        "sem 7".

        This is a purely linguistic ordinal/number parser - it
        does not reference any specific semester, class, or
        faculty member from the timetable data, and returns
        None when no semester phrase is present.
        """

        text = str(query).lower()

        ordinal_words = {
            "first": 1, "1st": 1,
            "second": 2, "2nd": 2,
            "third": 3, "3rd": 3,
            "fourth": 4, "4th": 4,
            "fifth": 5, "5th": 5,
            "sixth": 6, "6th": 6,
            "seventh": 7, "7th": 7,
            "eighth": 8, "8th": 8,
            "ninth": 9, "9th": 9,
            "tenth": 10, "10th": 10,
        }

        for word, value in ordinal_words.items():

            if re.search(
                rf"\b{re.escape(word)}\s+sem",
                text
            ):
                return value

        match = re.search(
            r"\bsem(?:ester)?\s*(\d{1,2})\b",
            text
        )

        if match:
            value = int(match.group(1))
            if 1 <= value <= 10:
                return value

        match = re.search(
            r"\b(\d{1,2})\s*(?:th|st|nd|rd)?\s+sem",
            text
        )

        if match:
            value = int(match.group(1))
            if 1 <= value <= 10:
                return value

        return None

    # ======================================================
    # ORDINAL FORMATTING (generic linguistic helper - e.g.
    # 1 -> "1st", 7 -> "7th". Not tied to any specific
    # semester, class, or faculty data.)
    # ======================================================

    @staticmethod
    def _ordinal(number):

        try:
            number = int(number)
        except (TypeError, ValueError):
            return str(number)

        if 10 <= (number % 100) <= 20:
            suffix = "th"
        else:
            suffix = {
                1: "st",
                2: "nd",
                3: "rd"
            }.get(number % 10, "th")

        return f"{number}{suffix}"

    # ======================================================
    # EXTRACT SLOT NUMBER (for room-shift queries)
    # ======================================================

    @staticmethod
    def _extract_slot(query):

        text = str(query).lower()

        match = re.search(r"\bslot\s+(\d{1,2})\b", text)

        if match:
            return int(match.group(1))

        match = re.search(r"\bperiod\s+(\d{1,2})\b", text)

        if match:
            return int(match.group(1))

        return None

    # ======================================================
    # EXTRACT ROOM (for room-shift queries)
    #
    # Resolved purely against the rooms actually present in
    # the currently loaded canonical timetable
    # (query_engine.entity_knowledge()) - never a hard-coded
    # room list. Prefers an explicit "room <X>" mention, then
    # falls back to scanning the query text for any known room
    # name (longest names checked first, so a short numeric
    # room name never incorrectly matches inside a longer one).
    # ======================================================

    def _extract_room(self, query):

        text = str(query)

        known_rooms = (
            self.query_engine.entity_knowledge().get(
                "rooms",
                []
            )
        )

        if not known_rooms:
            return None

        match = re.search(
            r"room\s+([A-Za-z0-9:\-]+)",
            text,
            re.IGNORECASE
        )

        if match:

            resolution = (
                self.lab_shift_planner.resolve_room(
                    match.group(1)
                )
            )

            if resolution["mode"] == "exact":
                return resolution["room"]

        for room in sorted(
            known_rooms,
            key=len,
            reverse=True
        ):

            pattern = (
                r"(?<![A-Za-z0-9])"
                + re.escape(room)
                + r"(?![A-Za-z0-9])"
            )

            if re.search(pattern, text, re.IGNORECASE):
                return room

        return None

    def _extract_period_teacher(self, query):
        """Resolve a specific faculty name from a period query."""

        text = str(query).lower()

        # Get canonical faculty names from the existing
        # NaturalLanguageQuery object.
        try:
            names = self.nl_query._known_teachers()
        except Exception:
            names = []

        # Remove common faculty titles from the query.
        query_key = re.sub(
            r"\b(?:dr|mr|mrs|ms|prof|professor)\.?\s*",
            "",
            text,
            flags=re.IGNORECASE
        )

        query_key = re.sub(
            r"\s+",
            " ",
            query_key
        ).strip()

        # Check longest names first.
        # This prevents "Dinesh Kumar" from matching
        # before "Dinesh Kumar Sharma".
        for name in sorted(
            names,
            key=lambda x: len(str(x)),
            reverse=True
        ):

            name_key = re.sub(
                r"\b(?:dr|mr|mrs|ms|prof|professor)\.?\s*",
                "",
                str(name),
                flags=re.IGNORECASE
            )

            name_key = re.sub(
                r"\s+",
                " ",
                name_key
            ).strip()

            if not name_key:
                continue

            # Match the complete faculty name.
            if re.search(
                r"(?<![a-z])"
                + re.escape(name_key)
                + r"(?![a-z])",
                query_key,
                re.IGNORECASE
            ):
                return name

        return None

    def _extract_all_period_teachers(self, query):
        """
        Like _extract_period_teacher, but returns EVERY known
        faculty name mentioned in the query (in the order they
        appear), instead of just one. Used for queries naming
        multiple faculty at once, e.g. "What if Mr. X and
        Dr. Y are absent on Monday?".
        """

        text = str(query).lower()

        try:
            names = self.nl_query._known_teachers()
        except Exception:
            names = []

        query_key = re.sub(
            r"\b(?:dr|mr|mrs|ms|prof|professor)\.?\s*",
            "",
            text,
            flags=re.IGNORECASE
        )

        query_key = re.sub(
            r"\s+",
            " ",
            query_key
        ).strip()

        matches = []

        # Longest names first, so a longer name is claimed
        # before a shorter name that happens to be a prefix
        # of it (e.g. "Dinesh Kumar" vs "Dinesh Kumar Sharma").
        for name in sorted(
            names,
            key=lambda x: len(str(x)),
            reverse=True
        ):

            name_key = re.sub(
                r"\b(?:dr|mr|mrs|ms|prof|professor)\.?\s*",
                "",
                str(name),
                flags=re.IGNORECASE
            )

            name_key = re.sub(
                r"\s+",
                " ",
                name_key
            ).strip()

            if not name_key:
                continue

            pattern = (
                r"(?<![a-z])"
                + re.escape(name_key)
                + r"(?![a-z])"
            )

            match = re.search(
                pattern,
                query_key,
                re.IGNORECASE
            )

            if not match:
                continue

            matches.append((match.start(), name))

            # Remove the matched span so a shorter name
            # contained inside an already-claimed longer name
            # cannot also match.
            query_key = (
                query_key[:match.start()]
                + " " * (match.end() - match.start())
                + query_key[match.end():]
            )

        matches.sort(key=lambda pair: pair[0])

        # De-duplicate while preserving order.
        seen = set()
        ordered = []

        for _, name in matches:
            if name not in seen:
                seen.add(name)
                ordered.append(name)

        return ordered

    # ======================================================
    # PERIOD QUERY DETECTION
    # ======================================================

    def _is_faculty_period_query(self, query):
        """
        Detect whether the user is asking about faculty
        availability/status over a time interval.
        """

        text = str(query).lower()

        faculty_words = (
    "faculty",
    "faculties",
    "teacher",
    "teachers",
    "professor",
    "professors",
    "staff",
    "sir",
    "ma'am",
    "madam",

    "who is free",
    "who is available",
    "who should be assigned",
    "who should take",
    "who can take",

    "available faculty",
    "free faculty",
)

        status_words = (
            "free",
            "available",
            "availability",
            "busy",
            "occupied",
            "unavailable",
            "take duty",
            "exam duty",
            "can take",
            "can handle",
        )

        has_faculty_reference = any(
            word in text
            for word in faculty_words
        )

        has_status_reference = any(
            word in text
            for word in status_words
        )

        has_time_range = bool(
            self._extract_time_range(query)[0]
            and self._extract_time_range(query)[1]
        )

        has_specific_teacher = (
            self._extract_period_teacher(query) is not None
        )

        return (
            has_status_reference
            and has_time_range
            and (
                has_faculty_reference
                or has_specific_teacher
            )
        )

    # ======================================================
    # PERIOD QUERY
    # ======================================================

    def _process_faculty_period_query(self, query):

        """
        Handle natural-language faculty availability
        queries containing a start and end time.
        """

        day = self._extract_day(query)

        start_time, end_time = (
            self._extract_time_range(query)
        )

    # --------------------------------------------------
        # Extract specific faculty, if mentioned
        # --------------------------------------------------

        teacher = self._extract_period_teacher(query)

        # --------------------------------------------------
        # Missing day
        # --------------------------------------------------

        if not day:

            return (
                "Please specify a day, for example "
                "Monday, Tuesday, Wednesday, etc."
            )

        # --------------------------------------------------
        # Missing time range
        # --------------------------------------------------

        if not start_time or not end_time:

            return (
                "Please specify a complete time range, "
                "for example 09:15 to 11:15."
            )

        # --------------------------------------------------
        # Validate time ordering
        # --------------------------------------------------

        try:

            start = datetime.strptime(
                start_time,
                "%H:%M"
            )

            end = datetime.strptime(
                end_time,
                "%H:%M"
            )

            if end <= start:

                return (
                    "The ending time must be later than "
                    "the starting time."
                )

        except ValueError:

            return (
                "I could not understand the requested "
                "time range."
            )

        # --------------------------------------------------
        # SPECIFIC FACULTY PERIOD STATUS
        # --------------------------------------------------
        if teacher:
            result = self.query_engine.faculty_status_for_period(
                teacher, day, start_time, end_time
            )

            status = result.get("status")
            if status == "free":
                slots = result.get("slots", [])
                slot_text = ", ".join(str(x) for x in slots)
                return (
                    f"{teacher} is FREE on {day} from "
                    f"{start_time} to {end_time}."
                    + (f"\nOverlapping slots: {slot_text}" if slot_text else "")
                )

            if status == "busy":
                busy_slots = [
                    self.query_engine._slot(x.get("slot"))
                    for x in result.get("events", [])
                    if self.query_engine._slot(x.get("slot")) is not None
                ]
                slot_text = ", ".join(str(x) for x in busy_slots)
                return (
                    f"{teacher} is BUSY on {day} from "
                    f"{start_time} to {end_time}."
                    + (f"\nBusy slots: {slot_text}" if slot_text else "")
                )

            return (
                f"I could not determine the availability of {teacher} "
                f"on {day} from {start_time} to {end_time}."
            )
                  # --------------------------------------------------
        # BUSY/FREE FACULTY LOGIC
        # --------------------------------------------------

        


        # --------------------------------------------------
        # EXAM DUTY RECOMMENDATION
        # --------------------------------------------------

        text = str(query).lower()

        exam_duty_words = (
            "exam duty",
            "exam duties",
            "exam invigilation",
            "invigilation",
            "assign duty",
            "assigned duty",
            "take duty",
            "can take duty",
            "who should be assigned",
            "who should take",
        )

        is_exam_duty_intent = any(
            phrase in text
            for phrase in exam_duty_words
        )

        if is_exam_duty_intent:

            try:

                duty_result = (
                    self.workload_engine.exam_duty_candidates(
                        day,
                        start_time,
                        end_time
                    )
                )

            except Exception as e:

                return (
                    "Unable to calculate exam-duty candidates.\n"
                    f"Error: {e}"
                )

            if not isinstance(duty_result, dict):

                return (
                    "Unable to retrieve exam-duty candidates."
                )

            candidates = duty_result.get(
                "results",
                []
            )

            if not candidates:

                return (
                    f"No suitable faculty members were found "
                    f"for exam duty on {day} from "
                    f"{start_time} to {end_time}."
                )

            lines = []

            lines.append(
                f"Recommended faculty for exam duty on "
                f"{day} from {start_time} to {end_time}:"
            )

            lines.append("")

            for index, candidate in enumerate(
                candidates,
                start=1
            ):

                teacher = str(
                    candidate.get(
                        "teacher",
                        ""
                    )
                ).strip()

                daily_periods = candidate.get(
                    "daily_periods",
                    0
                )

                priority = candidate.get(
                    "priority",
                    ""
                )

                if not teacher:
                    continue

                lines.append(
                    f"{index}. {teacher} — "
                    f"{daily_periods} periods — "
                    f"{priority} priority"
                )

            lines.append("")
            lines.append(
                f"Total candidates: {len(candidates)}"
            )

            return "\n".join(lines)


        # --------------------------------------------------
        # USE EXISTING VALIDATED QUERY ENGINE
        # --------------------------------------------------

        # --------------------------------------------------
        # DETECT BUSY / OCCUPIED / UNAVAILABLE INTENT
        # --------------------------------------------------

        text = str(query).lower()

        busy_words = (
            "busy",
            "occupied",
            "unavailable",
            "not available",
        )

        is_busy_intent = any(
            word in text
            for word in busy_words
        )

        if is_busy_intent:

            # Get the complete faculty roster from the existing faculty records.
            all_records = self.query_engine._faculty_records()
            all_names = []

            for item in all_records:
                if not isinstance(item, dict):
                    continue

                name = str(item.get("teacher", "")).strip()
                if name:
                    all_names.append(name)

            all_names = list(dict.fromkeys(all_names))
            busy_faculty = []

            for name in all_names:
                status_result = self.query_engine.faculty_status_for_period(
                    name,
                    day,
                    start_time,
                    end_time
                )

                if (
                    isinstance(status_result, dict)
                    and status_result.get("status") == "busy"
                ):
                    busy_faculty.append(name)

            busy_faculty = list(dict.fromkeys(busy_faculty))
            busy_faculty.sort(key=lambda x: x.lower())

            if not busy_faculty:
                return (
                    "No faculty members are busy on "
                    f"{day} from {start_time} to {end_time}."
                )

            lines = [
                f"Faculty members busy on {day} "
                f"from {start_time} to {end_time}:",
                ""
            ]

            for index, teacher in enumerate(busy_faculty, start=1):
                lines.append(f"{index}. {teacher}")

            lines.extend(("", f"Total faculty: {len(busy_faculty)}"))
            return "\n".join(lines)

        # --------------------------------------------------
        # EXISTING FREE-FACULTY LOGIC
        # --------------------------------------------------

        

        result = (
            self.query_engine.faculty_free_for_period(
                day,
                start_time,
                end_time
            )
        )

        if not isinstance(result, dict):

            return (
                "Unable to retrieve faculty availability "
                "for the requested period."
            )

        results = result.get(
            "results",
            []
        )

        # --------------------------------------------------
        # Extract valid faculty names
        # --------------------------------------------------

        teachers = []

        for item in results:

            if not isinstance(item, dict):
                continue

            teacher = str(
                item.get("teacher", "")
            ).strip()

            if not teacher:
                continue

            teachers.append(teacher)

        # --------------------------------------------------
        # Remove duplicate names
        # --------------------------------------------------

        teachers = list(
            dict.fromkeys(teachers)
        )

        teachers.sort(
            key=lambda x: x.lower()
        )

        # --------------------------------------------------
        # No faculty
        # --------------------------------------------------

        if not teachers:

            return (
                "No faculty members are free for "
                f"{day} from {start_time} to {end_time}."
            )

        # --------------------------------------------------
        # Response
        #
        # Each free faculty member's semester(s)/subject(s)
        # are looked up from the SAME teacher_search() records
        # FIND_SUBJECT already uses - nothing is hard-coded,
        # this is purely derived from whatever timetable data
        # is currently loaded. Semester numbers reuse
        # QueryEngine._semester_from_class_name(), the same
        # class-name-driven derivation semester_schedule()
        # already relies on.
        # --------------------------------------------------

        lines = []

        lines.append(
            f"Faculty members free on {day} "
            f"from {start_time} to {end_time}:"
        )

        lines.append("")

        lines.append(
            "| # | Faculty | Semester(s) | Subject(s) |"
        )

        lines.append(
            "|---|---|---|---|"
        )

        for index, teacher in enumerate(
            teachers,
            start=1
        ):

            teacher_records = (
                self.query_engine.teacher_search(
                    teacher
                ).get(
                    "results",
                    []
                )
            )

            semesters = set()
            subjects = set()

            for record in teacher_records:

                if not isinstance(record, dict):
                    continue

                record_class = record.get(
                    "class_name"
                ) or record.get(
                    "class"
                )

                semester = (
                    self.query_engine.
                    _semester_from_class_name(
                        record_class
                    )
                )

                if semester is not None:
                    semesters.add(semester)

                record_subject = str(
                    record.get(
                        "subject",
                        ""
                    )
                ).strip()

                if record_subject:
                    subjects.add(record_subject)

            semester_text = ", ".join(
                str(s)
                for s in sorted(semesters)
            ) or "—"

            subject_text = ", ".join(
                sorted(subjects)
            ) or "—"

            lines.append(
                f"| {index} | {teacher} | "
                f"{semester_text} | {subject_text} |"
            )

        lines.append("")

        lines.append(
            f"Total faculty: {len(teachers)}"
        )

        return "\n".join(lines)

    # ======================================================
    # PROCESS QUERY
    # ======================================================

        # ======================================================
    # WORKLOAD RANK EXTRACTION
    # ======================================================

    def _extract_workload_rank(self, query):

        text = str(query).lower().strip()

        # Explicit ordinal words
        rank_words = {
            "first": 1,
            "1st": 1,
            "second": 2,
            "2nd": 2,
            "third": 3,
            "3rd": 3,
            "fourth": 4,
            "4th": 4,
            "fifth": 5,
            "5th": 5,
            "sixth": 6,
            "6th": 6,
            "seventh": 7,
            "7th": 7,
            "eighth": 8,
            "8th": 8,
            "ninth": 9,
            "9th": 9,
            "tenth": 10,
            "10th": 10,
        }

        for word, rank in rank_words.items():

            if re.search(
                rf"\b{re.escape(word)}\b",
                text
            ):
                return rank

        # If no explicit rank is mentioned,
        # treat the query as first/highest/lowest.
        return 1


    # ======================================================
    # PROCESS QUERY
    # ======================================================

    

    def process_query(self, query):

        query = str(query).strip()

        if not query:
            return "Please enter a query."
       

        # --------------------------------------------------
        # EXAM DUTY / INVIGILATION
        #
        # Confirmation-capable and duty-conflict-aware actions
        # delegate to self.exam_duty_coordinator
        # (scheduling/exam_duty_planner.py, backed by
        # scheduling/exam_duty_store.py). Regular timetable data
        # only has a recurring weekday/slot concept, which cannot
        # distinguish different exam weeks, so any action that
        # needs to account for already-CONFIRMED exam duties (or
        # that PERSISTS one) always requires a real calendar date
        # (extracted by self._extract_exam_date()). When no real
        # date is present, this falls back to the existing,
        # previously-verified FacultyWorkloadEngine.
        # exam_duty_candidates()/assign_exam_duty() helpers
        # directly and unchanged, so already-working weekday-only
        # phrasing never regresses. Neither of those helpers has
        # ever persisted anything - CONFIRMATION THROUGH
        # exam_duty_coordinator.confirm_duty() IS THE ONLY
        # OPERATION THAT PERSISTS AN EXAM-DUTY RECORD.
        #
        # CONFIRM is checked first: if the previous turn already
        # proposed an exam-duty plan (self._pending_exam_duty_plan)
        # and this message says "confirm", that plan is confirmed
        # rather than re-parsed as a new request - the same
        # propose-then-confirm pattern already used for lab shifts
        # (self._pending_lab_shift_plan / LabShiftCoordinator.
        # confirm_shift() above).
        # --------------------------------------------------

        text = str(query).lower()

        exam_duty_confirm_words = (
            "confirm",
            "yes confirm",
            "go ahead",
        )

        is_exam_duty_confirm = (
            any(
                word in text
                for word in exam_duty_confirm_words
            )
            and self._pending_exam_duty_plan is not None
        )

        if is_exam_duty_confirm:

            plan = self._pending_exam_duty_plan
            self._pending_exam_duty_plan = None

            result = self.exam_duty_coordinator.confirm_duty(
                plan
            )

            if result.get("success"):

                duties = result.get("duties", [])

                if duties:
                    lines = [
                        f"Confirmed exam duty for "
                        f"{duties[0]['exam_date']} from "
                        f"{duties[0]['start_time']} to "
                        f"{duties[0]['end_time']}:"
                    ]
                else:
                    lines = ["Confirmed exam duty."]

                lines.append("")

                for index, duty in enumerate(duties, start=1):
                    lines.append(
                        f"{index}. {duty.get('teacher')}"
                    )

                return "\n".join(lines)

            reason = result.get("reason", "unknown")

            return (
                "Could not confirm the exam duty "
                f"({reason}). The availability state may have "
                "changed since the plan was proposed - please "
                "ask again to get a fresh proposal."
            )

        exam_duty_words = (
            "exam duty",
            "exam duties",
            "exam invigilation",
            "invigilation",
            "invigilator",
            "invigilators",
            "assign duty",
            "assigned duty",
            "take duty",
            "can take duty",
            "who should be assigned",
            "who should take",
        )

        is_exam_duty_intent = any(
            phrase in text
            for phrase in exam_duty_words
        )

        if is_exam_duty_intent:

            # --------------------------------------------------
            # DUTY COUNT QUERY
            #
            # "How many exam duties does each faculty member
            # have?" - date independent, delegates entirely to
            # ExamDutyStore.duty_counts() through the coordinator.
            # --------------------------------------------------

            duty_count_words = (
                "how many exam duties",
                "how many exam duty",
                "exam duty count",
                "exam duties count",
                "duty counts",
            )

            if any(word in text for word in duty_count_words):

                counts = self.exam_duty_coordinator.duty_counts()

                if not counts:
                    return (
                        "No exam duties have been confirmed yet."
                    )

                lines = ["Confirmed exam-duty counts:", ""]

                for teacher in sorted(
                    counts,
                    key=lambda name: (
                        -counts[name],
                        name.lower()
                    )
                ):
                    lines.append(
                        f"{teacher}: {counts[teacher]}"
                    )

                return "\n".join(lines)

            # --------------------------------------------------
            # SHOW CONFIRMED EXAM DUTIES
            #
            # Date independent unless a real calendar date is
            # also present in the query, in which case the list
            # is filtered to that date.
            # --------------------------------------------------

            show_duty_words = (
                "show exam dut",
                "list exam dut",
                "existing exam dut",
                "confirmed exam dut",
            )

            if any(word in text for word in show_duty_words):

                shown_date = self._extract_exam_date(query)

                if shown_date:
                    duties = (
                        self.exam_duty_coordinator
                        .duties_for_date(shown_date)
                    )
                else:
                    duties = (
                        self.exam_duty_coordinator.all_duties()
                    )

                if not duties:
                    return (
                        "No confirmed exam duties found"
                        + (
                            f" for {shown_date}."
                            if shown_date else "."
                        )
                    )

                lines = ["Confirmed exam duties:", ""]

                for index, duty in enumerate(duties, start=1):

                    hall_text = (
                        f" in {duty.get('hall')}"
                        if duty.get("hall") else ""
                    )

                    lines.append(
                        f"{index}. {duty.get('teacher')} — "
                        f"{duty.get('exam_date')} "
                        f"{duty.get('start_time')}-"
                        f"{duty.get('end_time')}{hall_text}"
                    )

                return "\n".join(lines)

            # --------------------------------------------------
            # SHARED EXTRACTION
            # --------------------------------------------------

            exam_date_text = self._extract_exam_date(query)

            start_time, end_time = (
                self._extract_time_range(query)
            )

            hall_text = self._extract_room(query)

            count_patterns = (
                r"\bassign\s+(\d+)\s+"
                r"(?:faculty|faculties|teachers|professors|"
                r"staff|invigilators?)\b",

                r"\bneed\s+(\d+)\s+"
                r"(?:faculty|faculties|teachers|professors|"
                r"staff|invigilators?)\b",

                r"\b(\d+)\s+"
                r"(?:faculty|faculties|teachers|professors|"
                r"staff|invigilators?)\b",

                r"\bassign\s+(\d+)\b",

                r"\bneed\s+(\d+)\b",
            )

            required_count = None

            for pattern in count_patterns:

                match = re.search(pattern, text)

                if match:
                    try:
                        required_count = int(match.group(1))
                    except ValueError:
                        required_count = None
                    break

            # --------------------------------------------------
            # REAL-CALENDAR-DATE PATH
            #
            # Required for anything that must account for
            # already-CONFIRMED exam duties, or that will PERSIST
            # one.
            # --------------------------------------------------

            if exam_date_text:

                if not start_time or not end_time:
                    return (
                        "Please specify a complete time range "
                        "for the exam duty, for example 09:00 "
                        "to 11:00."
                    )

                if required_count is not None:

                    if required_count <= 0:
                        return (
                            "The number of faculty to assign "
                            "must be greater than zero."
                        )

                    plan = self.exam_duty_coordinator.plan_duty(
                        exam_date=exam_date_text,
                        start_time=start_time,
                        end_time=end_time,
                        required_faculty=required_count,
                        hall=hall_text,
                    )

                    if plan.get("success"):

                        self._pending_exam_duty_plan = plan

                        lines = [
                            "Proposed exam duty for "
                            f"{plan['exam_date']} from "
                            f"{plan['start_time']} to "
                            f"{plan['end_time']}"
                            + (
                                f" in {plan['hall']}"
                                if plan.get("hall") else ""
                            )
                            + ":"
                        ]

                        lines.append("")

                        for index, candidate in enumerate(
                            plan["recommended"],
                            start=1
                        ):
                            lines.append(
                                f"{index}. {candidate['teacher']} "
                                f"— {candidate['daily_periods']} "
                                "periods — existing exam duties: "
                                f"{candidate['existing_exam_duty_count']}"
                            )

                        lines.append("")
                        lines.append(
                            "Reply \"confirm\" to apply this "
                            "exam-duty assignment."
                        )

                        return "\n".join(lines)

                    reason = plan.get("reason", "unknown")

                    if reason == "insufficient_available_faculty":
                        return (
                            "Only "
                            f"{plan.get('available_count', 0)} "
                            "faculty members are available, but "
                            f"{plan.get('required_count')} were "
                            "requested."
                        )

                    if reason == "no_eligible_faculty":
                        return (
                            "No suitable faculty members were "
                            "found for exam duty on "
                            f"{plan.get('exam_date', exam_date_text)} "
                            f"from {start_time} to {end_time}."
                        )

                    if reason == "invalid_date":
                        return (
                            "I could not understand that exam "
                            "date. Please provide a real "
                            "calendar date, for example "
                            "2026-09-10."
                        )

                    if reason == "invalid_time_range":
                        return (
                            "I could not understand the "
                            "requested time range."
                        )

                    return (
                        "Unable to propose an exam-duty "
                        f"assignment ({reason})."
                    )

                # ----------------------------------------------
                # No explicit count - date-aware, side-effect
                # free candidate discovery for this exact date/
                # time, which (unlike the plain-weekday fallback
                # below) also accounts for already-CONFIRMED exam
                # duties.
                # ----------------------------------------------

                candidate_result = (
                    self.exam_duty_coordinator.candidates(
                        exam_date_text,
                        start_time,
                        end_time
                    )
                )

                if not candidate_result.get("success"):

                    reason = candidate_result.get(
                        "reason",
                        "unknown"
                    )

                    if reason == "invalid_date":
                        return (
                            "I could not understand that exam "
                            "date. Please provide a real "
                            "calendar date, for example "
                            "2026-09-10."
                        )

                    return (
                        "Unable to calculate exam-duty "
                        f"candidates ({reason})."
                    )

                candidates = candidate_result.get("results", [])

                if not candidates:
                    return (
                        "No suitable faculty members were found "
                        "for exam duty on "
                        f"{candidate_result['exam_date']} from "
                        f"{start_time} to {end_time}."
                    )

                lines = [
                    "Recommended faculty for exam duty on "
                    f"{candidate_result['exam_date']} from "
                    f"{start_time} to {end_time}:"
                ]

                lines.append("")

                for index, candidate in enumerate(
                    candidates,
                    start=1
                ):
                    lines.append(
                        f"{index}. {candidate['teacher']} — "
                        f"{candidate['daily_periods']} periods "
                        "— existing exam duties: "
                        f"{candidate['existing_exam_duty_count']}"
                    )

                lines.append("")
                lines.append(
                    f"Total candidates: {len(candidates)}"
                )

                return "\n".join(lines)

            # --------------------------------------------------
            # NO CALENDAR DATE GIVEN
            #
            # Falls back to the existing, previously-verified
            # weekday-only candidate discovery/assignment helpers,
            # unchanged, so already-working phrasing (e.g. "Who is
            # available for exam duty on Monday from 9 to 11?")
            # keeps working exactly as before. Neither of these
            # helpers has ever persisted anything.
            # --------------------------------------------------

            day = self._extract_day(query)

            if not day:
                return (
                    "Please specify a day for the exam duty, "
                    "for example Monday, or a real calendar "
                    "date such as 2026-09-10."
                )

            if not start_time or not end_time:
                return (
                    "Please specify a complete time range for "
                    "the exam duty, for example 09:00 to 11:00."
                )

            # --------------------------------------------------
            # ACTUAL ASSIGNMENT (READ-ONLY - see module docstring
            # in scheduling/workload_engine.py: assign_exam_duty()
            # is a candidate-selection helper, not persistence)
            # --------------------------------------------------

            if required_count is not None:

                if required_count <= 0:
                    return (
                        "The number of faculty to assign "
                        "must be greater than zero."
                    )

                try:
                    duty_result = (
                        self.workload_engine.assign_exam_duty(
                            day,
                            start_time,
                            end_time,
                            required_count
                        )
                    )
                except Exception as e:
                    return (
                        "Unable to assign exam duty.\n"
                        f"Error: {e}"
                    )

                if not isinstance(duty_result, dict):
                    return (
                        "Unable to retrieve the exam-duty "
                        "assignment result."
                    )

                assigned = duty_result.get("results", [])
                success = duty_result.get("success", False)

                if not assigned:
                    return (
                        f"No suitable faculty members were found "
                        f"for exam duty on {day} from "
                        f"{start_time} to {end_time}."
                    )

                lines = []

                if success:
                    lines.append(
                        f"Exam duty assigned successfully on "
                        f"{day} from {start_time} to {end_time}."
                    )
                else:
                    lines.append(
                        f"Exam-duty assignment result for "
                        f"{day} from {start_time} to {end_time}:"
                    )

                lines.append("")

                for index, candidate in enumerate(assigned, start=1):

                    teacher = str(
                        candidate.get("teacher", "")
                    ).strip()

                    daily_periods = candidate.get(
                        "daily_periods", 0
                    )

                    priority = candidate.get(
                        "priority", ""
                    )

                    if not teacher:
                        continue

                    lines.append(
                        f"{index}. {teacher} — "
                        f"{daily_periods} periods — "
                        f"{priority} priority"
                    )

                lines.append("")
                lines.append(
                    f"Assigned: {len(assigned)} / {required_count}"
                )

                return "\n".join(lines)

            # --------------------------------------------------
            # RECOMMENDATION ONLY
            #
            # NOTE: the "return" below was previously indented
            # one level too deep (inside the "for" loop), so the
            # function returned after formatting only the FIRST
            # candidate instead of the complete ranked list. It
            # is now aligned with the loop itself, so it only
            # runs once the loop has finished.
            # --------------------------------------------------

            try:
                duty_result = (
                    self.workload_engine.exam_duty_candidates(
                        day,
                        start_time,
                        end_time
                    )
                )
            except Exception as e:
                return (
                    "Unable to calculate exam-duty candidates.\n"
                    f"Error: {e}"
                )

            if not isinstance(duty_result, dict):
                return (
                    "Unable to retrieve exam-duty candidates."
                )

            candidates = duty_result.get("results", [])

            if not candidates:
                return (
                    f"No suitable faculty members were found "
                    f"for exam duty on {day} from "
                    f"{start_time} to {end_time}."
                )

            lines = []

            lines.append(
                f"Recommended faculty for exam duty on "
                f"{day} from {start_time} to {end_time}:"
            )

            lines.append("")

            for index, candidate in enumerate(candidates, start=1):

                teacher = str(
                    candidate.get("teacher", "")
                ).strip()

                daily_periods = candidate.get(
                    "daily_periods", 0
                )

                priority = candidate.get(
                    "priority", ""
                )

                if not teacher:
                    continue

                lines.append(
                    f"{index}. {teacher} — "
                    f"{daily_periods} periods — "
                    f"{priority} priority"
                )

            lines.append("")
            lines.append(
                f"Total candidates: {len(candidates)}"
            )

            return "\n".join(lines)

        # --------------------------------------------------
        # WHAT-IF SIMULATION
        #
        # Example:
        # What if Dr. X is absent on Monday?
        # What happens if Dr. X is absent Tuesday?
        # Would moving this lab to <room> cause a conflict?
        #
        # Checked BEFORE the semester/absence/lab-shift
        # branches below, since a hypothetical phrasing (e.g.
        # "what if I move this lab to another room") would
        # otherwise be caught by the real lab-shift branch and
        # treated as an actual shift request instead of a
        # simulation.
        #
        # Delegates entirely to self.what_if_coordinator
        # (scheduling/what_if_coordinator.py), which itself
        # only calls the existing side-effect-free planners -
        # no scheduling decision is made here, and nothing is
        # ever confirmed/persisted from this branch.
        # --------------------------------------------------

        whatif_words = ("what if", "what happens if")

        is_whatif_intent = (
            any(word in text for word in whatif_words)
            or ("would" in text and "conflict" in text)
        )

        if is_whatif_intent:

            whatif_teacher = self._extract_period_teacher(
                query
            )
            whatif_day = self._extract_day(query)
            whatif_slot = self._extract_slot(query)
            whatif_room = self._extract_room(query)

            room_shift_words = (
                "shift", "shifting", "shifted",
                "move", "moving", "moved",
            )
            room_context_words = ("room", "lab", "venue")

            has_lab_shift_whatif = (
                any(word in text for word in room_shift_words)
                and any(
                    word in text for word in room_context_words
                )
            )

            if has_lab_shift_whatif:

                if (
                    not whatif_teacher
                    or not whatif_day
                    or whatif_slot is None
                    or not whatif_room
                ):

                    return (
                        "Please specify the faculty, day, "
                        "slot, and target room for the "
                        "what-if lab shift - for example "
                        "\"Would moving Dr. X's lab on "
                        "Monday slot 6 to room 103 cause a "
                        "conflict?\""
                    )

                result = (
                    self.what_if_coordinator.simulate_lab_shift(
                        teacher=whatif_teacher,
                        day=whatif_day,
                        slot=whatif_slot,
                        target_room=whatif_room,
                    )
                )

                header = (
                    f"What if {result['current_state'].get('teacher', whatif_teacher)}'s "
                    f"lab on {str(whatif_day).capitalize()} "
                    f"slots {result['current_state'].get('slots')} "
                    f"were moved to room {whatif_room}?"
                )

                if result["outcome"] == "feasible":

                    return (
                        f"{header}\n\nNo conflict - room "
                        f"{whatif_room} is free for the "
                        "complete block. (This is only a "
                        "simulation - nothing has been "
                        "changed.)"
                    )

                if result["outcome"] == "conflict":

                    conflict_lines = "; ".join(
                        f"slot {c['slot']} "
                        f"({c['teacher']} - {c['class_name']})"
                        for c in result["conflicts"]
                    )

                    return (
                        f"{header}\n\nYes - room "
                        f"{whatif_room} is occupied: "
                        f"{conflict_lines}. (This is only a "
                        "simulation - nothing has been "
                        "changed.)"
                    )

                return (
                    f"{header}\n\nCould not simulate this "
                    f"shift ({result.get('reason', 'unknown')})."
                )

            whatif_all_teachers = (
                self._extract_all_period_teachers(query)
            )

            if len(whatif_all_teachers) >= 2 and whatif_day:

                plan = self.multi_absence_coordinator.plan(
                    [
                        {"teacher": t, "day": whatif_day}
                        for t in whatif_all_teachers
                    ]
                )

                names_text = ", ".join(whatif_all_teachers)

                lines = [
                    f"What if {names_text} are all absent on "
                    f"{str(whatif_day).capitalize()}?",
                    "",
                    f"{plan['covered_count']} block(s) covered, "
                    f"{plan['uncovered_count']} block(s) "
                    "uncovered.",
                ]

                if plan["covered"]:
                    lines.append("")
                    lines.append("Suggested replacements:")
                    for item in plan["covered"]:
                        lines.append(
                            f"• {item.get('absent_teacher', '?')} \u2192 "
                            f"{item.get('replacement_teacher', '?')} "
                            f"for {item.get('class_name', '?')} "
                            f"({item.get('subject', '?')}) "
                            f"slots {item.get('slots', '?')}"
                        )

                if plan["uncovered"]:
                    lines.append("")
                    lines.append("Not covered:")
                    for item in plan["uncovered"]:
                        lines.append(
                            f"• {item.get('absent_teacher', '?')} - "
                            f"{item.get('class_name', '?')} "
                            f"slots {item.get('slots', '?')} "
                            f"({item.get('reason', 'no candidate found')})"
                        )

                lines.append("")
                lines.append(
                    "(This is only a simulation - nothing has "
                    "been changed.)"
                )

                return "\n".join(lines)

            if whatif_teacher and whatif_day:

                result = self.what_if_coordinator.simulate_absence(
                    whatif_teacher,
                    whatif_day,
                )

                header = (
                    f"What if {whatif_teacher} is absent on "
                    f"{str(whatif_day).capitalize()}?"
                )

                if result["outcome"] == "no_scheduled_classes":

                    return (
                        f"{header}\n\n{whatif_teacher} has no "
                        f"scheduled classes on "
                        f"{str(whatif_day).capitalize()} - no "
                        "impact."
                    )

                summary = (
                    f"{len(result['affected_entities'])} "
                    f"block(s) affected ({result['outcome']})."
                )

                lines = [header, "", summary]

                covered_entities = [
                    entity
                    for entity in result["affected_entities"]
                    if entity.get("replacement_teacher")
                ]

                uncovered_entities = [
                    entity
                    for entity in result["affected_entities"]
                    if not entity.get("replacement_teacher")
                ]

                if covered_entities:

                    lines.append("")
                    lines.append("Suggested replacements:")

                    for index, item in enumerate(
                        covered_entities,
                        start=1
                    ):
                        lines.append(
                            f"{index}. {item['replacement_teacher']} "
                            f"for {item.get('class_name', '?')} "
                            f"({item.get('subject', '?')}) "
                            f"slots {item.get('slots', '?')}"
                        )

                if uncovered_entities:

                    lines.append("")
                    lines.append("Not covered:")

                    for index, item in enumerate(
                        uncovered_entities,
                        start=1
                    ):
                        lines.append(
                            f"{index}. {item.get('class_name', '?')} "
                            f"({item.get('subject', '?')}) "
                            f"slots {item.get('slots', '?')} "
                            f"- {item.get('reason', 'no candidate found')}"
                        )

                lines.append("")
                lines.append(
                    "(This is only a simulation - nothing has "
                    "been changed.)"
                )

                return "\n".join(lines)

            return (
                "Please specify the faculty and day for the "
                "what-if scenario - for example \"What if "
                "Dr. X is absent on Monday?\""
            )

        # --------------------------------------------------
        # SEMESTER-WIDE FACULTY LOAD
        #
        # Example:
        # Who is taking 7th semester workload?
        # Who is taking 7th semester load?
        # Who teaches 7th semester?
        # Who is teaching 7th semester?
        # Which faculty are teaching 7th semester?
        # Which faculty have workload in 7th semester?
        # Show 7th semester faculty workload
        # Who has load in 7th semester?
        #
        # This is checked BEFORE the absence/workload keyword
        # checks below, because words like "classes" and "load"
        # in these phrasings would otherwise be misread as an
        # absence or per-teacher workload query.
        #
        # The semester number is parsed purely from the query
        # text (see _extract_semester_number - a linguistic
        # ordinal/number parser). Which classes belong to that
        # semester, which faculty teach them, and their period
        # counts are derived entirely from
        # workload_engine.semester_workload(), which reuses the
        # EXISTING workload engine's "one canonical event = one
        # period" definition (the same definition used by
        # daily_workload()/weekly_workload() for a single
        # teacher) and query_engine's own leading-digit semester
        # derivation (_semester_from_class_name), so multi-slot
        # labs are counted the same way everywhere and nothing
        # here hard-codes a semester number, class name, faculty
        # name, or period count.
        # --------------------------------------------------

        has_semester_word = bool(
            re.search(r"\bsem(?:ester)?\b", text)
        )

        if has_semester_word:

            semester_number = self._extract_semester_number(
                query
            )

            if semester_number is not None:

                day = self._extract_day(query)

                workload_result = (
                    self.workload_engine.semester_workload(
                        semester_number,
                        day=day
                    )
                )

                results = workload_result.get(
                    "results",
                    []
                )

                if not results:

                    return (
                        f"No faculty found teaching "
                        f"semester {semester_number}"
                        + (f" on {day}" if day else "")
                        + "."
                    )

                header = (
                    f"{self._ordinal(semester_number)} "
                    f"Semester Faculty Workload"
                )

                if day:
                    header += f" on {day}"

                return (
                    ResponseGenerator
                    .format_teacher_workload_list(
                        header,
                        results
                    )
                )

        # --------------------------------------------------
        # ROOM AVAILABILITY
        #
        # Example:
        # Which rooms are free on Monday slot 3?
        # Which rooms are available on Monday slot 3?
        # What rooms are free Monday at slot 3?
        # Find free rooms on Tuesday from 10:15 to 12:15.
        #
        # Checked BEFORE lab shifting. "room"/"rooms" plus an
        # availability word ("free"/"available"/"vacant") is a
        # general room-availability question UNLESS it also
        # references a specific lab/venue (see LAB SHIFTING
        # below) or a specific faculty member's class - those
        # are left to fall through to that branch instead. The
        # absence of any faculty/lab reference is what makes
        # this a plain room-availability query rather than a
        # request to move something's venue.
        #
        # Delegates entirely to the existing
        # QueryEngine.room_free_slots() (single slot) /
        # QueryEngine.room_free_for_period() (time range) -
        # both operate on the SAME canonical dataset every other
        # query in this method uses; no new/parallel room engine
        # is introduced.
        # --------------------------------------------------

        room_words = ("room", "rooms")

        room_availability_words = (
            "free", "available", "vacant"
        )

        lab_reference_words = (
            "lab", "labs", "venue"
        )

        is_room_availability_intent = (
            any(word in text for word in room_words)
            and any(
                word in text
                for word in room_availability_words
            )
            and not self._contains_any_word(
                text, lab_reference_words
            )
            and not self._extract_period_teacher(query)
        )

        if is_room_availability_intent:

            room_day = self._extract_day(query)

            room_start, room_end = (
                self._extract_time_range(query)
            )

            room_slot = self._extract_slot(query)

            if not room_day:

                return (
                    "Please specify a day for the room "
                    "availability query, for example \"Which "
                    "rooms are free on Monday slot 3?\""
                )

            if room_slot is not None:

                result = self.query_engine.room_free_slots(
                    day=room_day,
                    slot=room_slot
                )

                rooms = sorted(
                    {
                        str(
                            self.query_engine._get(
                                record, "room", "classroom"
                            )
                        ).strip()
                        for record in result.get("results", [])
                        if str(
                            self.query_engine._get(
                                record, "room", "classroom"
                            )
                        ).strip()
                    },
                    key=lambda name: name.casefold()
                )

                day_label = str(
                    result.get("day") or room_day
                ).capitalize()

                if not rooms:

                    return (
                        "Room availability cannot be "
                        "determined for "
                        f"{day_label} slot {room_slot} from "
                        "the current upload."
                    )

                lines = [
                    f"Rooms free on {day_label} slot "
                    f"{room_slot}:"
                ]

                lines.append("")

                for index, room in enumerate(rooms, start=1):

                    lines.append(f"{index}. {room}")

                return "\n".join(lines)

            if room_start and room_end:

                result = self.query_engine.room_free_for_period(
                    day=room_day,
                    start_time=room_start,
                    end_time=room_end
                )

                rooms = [
                    record["room"]
                    for record in result.get("results", [])
                ]

                day_label = str(
                    result.get("day") or room_day
                ).capitalize()

                if not rooms:

                    return (
                        "Room availability cannot be "
                        f"determined for {day_label} from "
                        f"{room_start} to {room_end} from the "
                        "current upload."
                    )

                lines = [
                    f"Rooms free on {day_label} from "
                    f"{room_start} to {room_end}:"
                ]

                lines.append("")

                for index, room in enumerate(rooms, start=1):

                    lines.append(f"{index}. {room}")

                return "\n".join(lines)

            return (
                "Please specify a slot number or a complete "
                "time range for the room availability query, "
                "for example \"Which rooms are free on Monday "
                "slot 3?\" or \"Which rooms are free from "
                "10:15 to 12:15 on Wednesday?\""
            )

        # --------------------------------------------------
        # LAB SHIFTING / VENUE CHANGE
        #
        # Example:
        # Which rooms are available for this lab?
        # Find another room for this lab.
        # Can this lab be shifted to room 103?
        # Shift the lab to room 103.
        # Confirm the shift.
        #
        # Delegates entirely to self.lab_shift_planner
        # (scheduling/lab_shift_planner.py) - the multi-period
        # block, room resolution, and availability logic all
        # live there, reusing the existing absence_engine block
        # detection and query_engine room/class lookups. This
        # branch only extracts the query's teacher/day/slot/
        # room and formats the result - no scheduling decision
        # is made here.
        #
        # CONFIRM is checked first: if the previous turn already
        # proposed a plan (self._pending_lab_shift_plan) and this
        # message says "confirm", that plan is confirmed rather
        # than re-parsed as a new request.
        # --------------------------------------------------

        confirm_words = ("confirm", "yes confirm", "go ahead")

        is_confirm_intent = (
            any(word in text for word in confirm_words)
            and self._pending_lab_shift_plan is not None
        )

        if is_confirm_intent:

            plan = self._pending_lab_shift_plan
            self._pending_lab_shift_plan = None

            result = self.lab_shift_planner.confirm_shift(
                plan
            )

            if result.get("success"):

                shift = result["shift"]

                return (
                    f"Confirmed: {shift['teacher']}'s "
                    f"{shift['class_name']} "
                    f"({shift['subject']}) on "
                    f"{shift['day'].capitalize()} "
                    f"slots {shift['slots']} has been moved "
                    f"from room {shift['source_room']} to "
                    f"room {shift['target_room']}."
                )

            reason = result.get("reason", "unknown")

            return (
                "Could not confirm the room shift "
                f"({reason}). The timetable state may have "
                "changed since the plan was proposed - please "
                "ask again to get a fresh proposal."
            )

        room_shift_words = ("shift", "move")

        room_availability_words_lab = (
            "available", "free", "another"
        )

        room_context_words = ("lab", "labs", "venue")

        has_room_shift_intent = (
            (
                any(word in text for word in room_shift_words)
                or any(
                    word in text
                    for word in room_availability_words_lab
                )
            )
            and self._contains_any_word(
                text, room_context_words
            )
        )

        if has_room_shift_intent:

            shift_teacher = self._extract_period_teacher(query)
            shift_day = self._extract_day(query)
            shift_slot = self._extract_slot(query)
            shift_room = self._extract_room(query)

            # --------------------------------------------------
            # NO SLOT GIVEN - AUTO-RESOLVE FROM THE TEACHER'S
            # OWN LAB BLOCKS ON THAT DAY
            #
            # e.g. "Can Dr. X's lab be moved to another room on
            # Wednesday?" names no slot. If the teacher has
            # exactly one lab block that day, that block is
            # unambiguous and is used directly (its data comes
            # straight from absence_engine._affected_blocks(),
            # the same block lookup find_lab_block()/plan_shift()
            # already use - nothing here is hardcoded). If there
            # is more than one, the user is asked to disambiguate
            # rather than guessing.
            # --------------------------------------------------

            if (
                shift_teacher
                and shift_day
                and shift_slot is None
            ):

                day_blocks = (
                    self.absence_engine._affected_blocks(
                        shift_teacher,
                        shift_day,
                    )
                )

                lab_blocks = [
                    block
                    for block in day_blocks
                    if str(
                        block.get("type", "")
                    ).strip().lower() == "lab"
                ]

                if len(lab_blocks) == 1:

                    block_slots = lab_blocks[0].get("slots", [])

                    if block_slots:
                        shift_slot = block_slots[0]

                elif len(lab_blocks) > 1:

                    options = "; ".join(
                        f"{b.get('class_name', '?')} "
                        f"({b.get('subject', '?')}) "
                        f"slots {b.get('slots', '?')}"
                        for b in lab_blocks
                    )

                    return (
                        f"{shift_teacher} has more than one lab "
                        f"on {str(shift_day).capitalize()}: "
                        f"{options}. Please specify which one, "
                        "e.g. by slot or subject."
                    )

                else:

                    return (
                        f"{shift_teacher} has no lab session on "
                        f"{str(shift_day).capitalize()}."
                    )

            if not shift_teacher or not shift_day or (
                shift_slot is None
            ):

                return (
                    "Please specify the faculty, day, and slot "
                    "for the lab you want to move - for example "
                    "\"Which rooms are available for Dr. X's "
                    "lab on Monday slot 6?\""
                )

            if shift_room:

                plan = self.lab_shift_planner.plan_shift(
                    teacher=shift_teacher,
                    day=shift_day,
                    slot=shift_slot,
                    target_room=shift_room
                )

                if plan.get("success"):

                    self._pending_lab_shift_plan = plan

                    return (
                        f"Proposed: move {plan['teacher']}'s "
                        f"{plan['class_name']} "
                        f"({plan['subject']}) on "
                        f"{plan['day'].capitalize()} slots "
                        f"{plan['slots']} from room "
                        f"{plan['source_room']} to room "
                        f"{plan['target_room']}. Reply "
                        "\"confirm\" to apply this change."
                    )

                reason = plan.get("reason", "unknown")

                if reason == "target_room_occupied":

                    conflicts = plan.get("conflicts", [])

                    conflict_lines = "; ".join(
                        f"slot {c['slot']} ({c['teacher']} - "
                        f"{c['class_name']})"
                        for c in conflicts
                    )

                    return (
                        f"Room {plan.get('target_room')} is "
                        "not available for the complete lab "
                        f"block: {conflict_lines}."
                    )

                return (
                    "Could not propose that room shift "
                    f"({reason})."
                )

            availability = (
                self.lab_shift_planner
                .find_available_rooms(
                    teacher=shift_teacher,
                    day=shift_day,
                    slot=shift_slot
                )
            )

            if not availability.get("success"):

                reason = availability.get(
                    "reason",
                    "unknown"
                )

                return (
                    "Could not find that lab "
                    f"({reason})."
                )

            available_rooms = availability.get(
                "available_rooms",
                []
            )

            if not available_rooms:

                return (
                    f"No alternative room is free for "
                    f"{availability['teacher']}'s "
                    f"{availability['class_name']} on "
                    f"{availability['day'].capitalize()} "
                    f"slots {availability['slots']} "
                    f"(currently room "
                    f"{availability['source_room']})."
                )

            slots_label = self._format_slot_range(
                availability["slots"]
            )

            lines = [
                f"Available rooms for "
                f"{availability['teacher']}'s "
                f"{availability['class_name']} "
                f"({availability['subject']})"
            ]

            lines.append("")

            lines.append(
                f"Day: {availability['day'].capitalize()}"
            )

            lines.append(
                f"Slots: {slots_label}"
            )

            lines.append(
                f"Current room: "
                f"{availability['source_room']}"
            )

            lines.append("")

            lines.append(
                f"Available rooms ({len(available_rooms)}):"
            )

            lines.append("")

            for index, room in enumerate(
                available_rooms,
                start=1
            ):

                lines.append(
                    f"{index}. {room}"
                )

            return "\n".join(lines)

        # ==================================================
        # ABSENT FACULTY / REPLACEMENT QUERY
        # ==================================================

        absence_words = (
            "absent",
            "absence",
            "substitute",
            "substitution",
            "replacement",
            "replace",
            "cover the class",
            "cover class",
            "cover for",
        )

        is_absence_intent = any(
            phrase in text
            for phrase in absence_words
        )

        if is_absence_intent:

            day = self._extract_day(query)

            if not day:
                return (
                    "Please specify a day, for example Monday."
                )

            teacher = self._extract_period_teacher(query)

            if not teacher:
                return (
                    "Please specify the absent faculty member, "
                    "for example Mr. Rajesh Rajaan."
                )

            # --------------------------------------------------
            # FIND CLASSES OF ABSENT FACULTY
            # --------------------------------------------------

            if any(
                phrase in text
                for phrase in (
                    "what classes",
                    "which classes",
                    "classes affected",
                    "affected classes",
                    "classes will be affected",
                    "classes are affected",
                    "what periods",
                    "which periods",
                    "periods affected",
                )
            ):

                try:

                    result = (
                        self.absence_engine.absent_faculty_classes(
                            teacher,
                            day
                        )
                    )

                except Exception as e:

                    return (
                        "Unable to retrieve the classes of the "
                        "absent faculty.\n"
                        f"Error: {e}"
                    )

                classes = result.get(
                    "classes",
                    []
                )

                if not classes:

                    return (
                        f"{teacher} has no scheduled classes "
                        f"on {day}."
                    )

                lines = [
                    f"Classes affected by the absence of "
                    f"{teacher} on {day}:",
                    ""
                ]

                for index, cls in enumerate(
                    classes,
                    start=1
                ):

                    slot = cls.get(
                        "slot",
                        ""
                    )

                    slot_time = str(
                        cls.get(
                            "slot_time",
                            ""
                        )
                    ).strip()

                    subject = str(
                        cls.get(
                            "subject",
                            ""
                        )
                    ).strip()

                    class_name = str(
                        cls.get(
                            "class_name",
                            ""
                        )
                    ).strip()

                    group_name = str(
                        cls.get(
                            "group_name",
                            ""
                        )
                    ).strip()

                    room = str(
                        cls.get(
                            "room",
                            ""
                        )
                    ).strip()

                    details = []

                    if class_name:
                        details.append(
                            class_name
                        )

                    if group_name:
                        details.append(
                            group_name
                        )

                    if subject:
                        details.append(
                            subject
                        )

                    class_details = " | ".join(
                        details
                    )

                    if slot:
                        slot_text = (
                            f"Slot {slot}"
                        )
                    else:
                        slot_text = "Slot"

                    if slot_time:
                        slot_text += (
                            f" ({slot_time})"
                        )

                    line = (
                        f"{index}. {slot_text}"
                    )

                    if class_details:
                        line += (
                            f" — {class_details}"
                        )

                    if room:
                        line += (
                            f" — Room: {room}"
                        )

                    lines.append(line)

                lines.append("")

                lines.append(
                    f"Total affected classes: "
                    f"{len(classes)}"
                )

                return "\n".join(lines)

            # --------------------------------------------------
            # FIND REPLACEMENT FACULTY
            # --------------------------------------------------

            if any(
                phrase in text
                for phrase in (
                    "substitute",
                    "substitution",
                    "replacement",
                    "replace",
                    "who can cover",
                    "who can take",
                    "who should take",
                    "cover the class",
                    "cover class",
                )
            ):

                try:

                    result = (
                        self.absence_engine.replacement_candidates(
                            teacher,
                            day
                        )
                    )

                except Exception as e:

                    return (
                        "Unable to calculate replacement "
                        "faculty.\n"
                        f"Error: {e}"
                    )

                candidates = result.get(
                    "results",
                    []
                )

                if not candidates:

                    return (
                        f"No suitable replacement faculty "
                        f"were found for {teacher} on {day}."
                    )

                lines = [
                    f"Replacement faculty for {teacher} "
                    f"on {day}:",
                    ""
                ]

                for index, candidate in enumerate(
                    candidates,
                    start=1
                ):

                    replacement = str(
                        candidate.get(
                            "replacement_teacher",
                            ""
                        )
                    ).strip()

                    slot = candidate.get(
                        "slot",
                        ""
                    )

                    slot_time = str(
                        candidate.get(
                            "slot_time",
                            ""
                        )
                    ).strip()

                    subject = str(
                        candidate.get(
                            "subject",
                            ""
                        )
                    ).strip()

                    class_name = str(
                        candidate.get(
                            "class_name",
                            ""
                        )
                    ).strip()

                    group_name = str(
                        candidate.get(
                            "group_name",
                            ""
                        )
                    ).strip()

                    if not replacement:
                        continue

                    details = []

                    if class_name:
                        details.append(
                            class_name
                        )

                    if group_name:
                        details.append(
                            group_name
                        )

                    if subject:
                        details.append(
                            subject
                        )

                    class_details = " | ".join(
                        details
                    )

                    line = (
                        f"{index}. {replacement}"
                    )

                    if slot:
                        line += (
                            f" — Slot {slot}"
                        )

                    if slot_time:
                        line += (
                            f" ({slot_time})"
                        )

                    if class_details:
                        line += (
                            f" — {class_details}"
                        )

                    lines.append(line)

                lines.append("")

                lines.append(
                    f"Total replacement options: "
                    f"{len(candidates)}"
                )

                return "\n".join(lines)

            # --------------------------------------------------
            # GENERAL ABSENCE QUERY
            # --------------------------------------------------

            try:

                result = (
                    self.absence_engine.absent_faculty_classes(
                        teacher,
                        day
                    )
                )

            except Exception as e:

                return (
                    "Unable to process the absence query.\n"
                    f"Error: {e}"
                )

            classes = result.get(
                "classes",
                []
            )

            if not classes:

                return (
                    f"{teacher} has no scheduled classes "
                    f"on {day}."
                )

            lines = [
                f"{teacher} has {len(classes)} scheduled "
                f"class(es) on {day}.",
                "",
                "Affected classes:"
            ]

            for index, cls in enumerate(
                classes,
                start=1
            ):

                subject = str(
                    cls.get(
                        "subject",
                        ""
                    )
                ).strip()

                class_name = str(
                    cls.get(
                        "class_name",
                        ""
                    )
                ).strip()

                slot = cls.get(
                    "slot",
                    ""
                )

                details = []

                if class_name:
                    details.append(
                        class_name
                    )

                if subject:
                    details.append(
                        subject
                    )

                details_text = " | ".join(
                    details
                )

                if details_text:
                    lines.append(
                        f"{index}. Slot {slot} — "
                        f"{details_text}"
                    )
                else:
                    lines.append(
                        f"{index}. Slot {slot}"
                    )

            return "\n".join(lines)
                    # ==================================================
        # FACULTY WORKLOAD QUERY
        # ==================================================

        text = str(query).lower()

        workload_words = (
            "workload",
            "work load",
            "periods",
            "teaching load",
            "classes",
            "class",
        )

        is_workload_intent = any(
            phrase in text
            for phrase in workload_words
        )

        if is_workload_intent:

            day = self._extract_day(query)
                                   # --------------------------------------------------
            # WEEKLY WORKLOAD
            # --------------------------------------------------

            weekly_words = (
                "weekly",
                "this week",
                "for the week",
                "week's",
                "week workload",
                "weekly workload",
            )

            is_weekly_workload = any(
                phrase in text
                for phrase in weekly_words
            )

            if is_weekly_workload:

                teacher = self._extract_period_teacher(query)

                if not teacher:
                    return (
                        "Please specify a faculty member, "
                        "for example Mr. Rajesh Rajaan."
                    )

                result = (
                    self.workload_engine.weekly_workload(
                        teacher
                    )
                )

                total_periods = result.get(
                    "total_periods",
                    0
                )

                by_day = result.get(
                    "by_day",
                    {}
                )

                lines = [
                    f"Weekly workload of {teacher}:",
                    ""
                ]

                day_order = (
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                    "saturday",
                    "sunday",
                )

                for current_day in day_order:

                    if current_day in by_day:

                        periods = by_day[current_day]

                        lines.append(
                            f"{current_day.capitalize()}: "
                            f"{periods} periods"
                        )

                lines.append("")
                lines.append(
                    f"Total weekly periods: {total_periods}"
                )

                return "\n".join(lines)

                        # --------------------------------------------------
            # LOWEST WORKLOAD / RANKED LOWEST WORKLOAD
            # --------------------------------------------------

            if any(
                phrase in text
                for phrase in (
                    "lowest workload",
                    "least workload",
                    "minimum workload",
                    "lowest load",
                    "least load",
                    "fewest periods",
                    "minimum periods",
                    "fewest classes",
                    "least classes",
                )
            ):

                if not day:
                    return (
                        "Please specify a day, for example Monday."
                    )

                results = (
                    self.workload_engine.lowest_workload(
                        day,
                        limit=1000
                    )
                )

                if not results:
                    return (
                        f"No workload data found for {day}."
                    )

                rank = self._extract_workload_rank(query)

                results = sorted(
                    results,
                    key=lambda x: (
                        x["periods"],
                        x["teacher"].lower()
                    )
                )

                ranked_periods = sorted(
                    set(
                        item["periods"]
                        for item in results
                    )
                )

                if rank > len(ranked_periods):
                    return (
                        f"There are only "
                        f"{len(ranked_periods)} workload levels "
                        f"on {day}."
                    )

                target_periods = ranked_periods[rank - 1]

                ranked_faculty = [
                    item
                    for item in results
                    if item["periods"] == target_periods
                ]

                rank_names = {
                    1: "lowest",
                    2: "second lowest",
                    3: "third lowest",
                    4: "fourth lowest",
                    5: "fifth lowest",
                }

                rank_text = rank_names.get(
                    rank,
                    f"{rank}th lowest"
                )

                lines = [
                    f"Faculty with the {rank_text} "
                    f"workload on {day}:",
                    ""
                ]

                for index, item in enumerate(
                    ranked_faculty,
                    start=1
                ):
                    lines.append(
                        f"{index}. {item['teacher']} — "
                        f"{item['periods']} periods"
                    )

                return "\n".join(lines)

                        # --------------------------------------------------
            # HIGHEST WORKLOAD / RANKED HIGHEST WORKLOAD
            # --------------------------------------------------

            if any(
                phrase in text
                for phrase in (
                    "highest workload",
                    "maximum workload",
                    "highest load",
                    "maximum load",
                    "most periods",
                    "maximum periods",
                    "most classes",
                    "maximum classes",
                )
            ):

                if not day:
                    return (
                        "Please specify a day, for example Monday."
                    )

                results = (
                    self.workload_engine.highest_workload(
                        day,
                        limit=1000
                    )
                )

                if not results:
                    return (
                        f"No workload data found for {day}."
                    )

                rank = self._extract_workload_rank(query)

                # Sort by workload descending
                results = sorted(
                    results,
                    key=lambda x: (
                        -x["periods"],
                        x["teacher"].lower()
                    )
                )

                if rank > len(results):
                    return (
                        f"There is no {rank}th highest "
                        f"workload faculty on {day}."
                    )

                ranked_periods = sorted(
                    set(
                        item["periods"]
                        for item in results
                    ),
                    reverse=True
                )

                if rank > len(ranked_periods):
                    return (
                        f"There are only "
                        f"{len(ranked_periods)} workload levels "
                        f"on {day}."
                    )

                target_periods = ranked_periods[rank - 1]

                ranked_faculty = [
                    item
                    for item in results
                    if item["periods"] == target_periods
                ]

                rank_names = {
                    1: "highest",
                    2: "second highest",
                    3: "third highest",
                    4: "fourth highest",
                    5: "fifth highest",
                }

                rank_text = rank_names.get(
                    rank,
                    f"{rank}th highest"
                )

                lines = [
                    f"Faculty with the {rank_text} "
                    f"workload on {day}:",
                    ""
                ]

                for index, item in enumerate(
                    ranked_faculty,
                    start=1
                ):
                    lines.append(
                        f"{index}. {item['teacher']} — "
                        f"{item['periods']} periods"
                    )

                return "\n".join(lines)

            # --------------------------------------------------
            # SPECIFIC FACULTY WORKLOAD
            # --------------------------------------------------

            teacher = self._extract_period_teacher(query)

            if teacher and day:

                result = (
                    self.workload_engine.faculty_daily_workload(
                        teacher,
                        day
                    )
                )

                periods = result.get("periods", 0)

                return (
                    f"{teacher} has {periods} periods on {day}."
                )

            # --------------------------------------------------
            # SPECIFIC FACULTY WORKLOAD - NO DAY GIVEN
            #
            # e.g. "What is the workload of Mr. Nitin Goyal?"
            # Falls back to the full weekly breakdown, plus a
            # theory/lab split and a per-semester count, all
            # derived from the event data itself (event "type"
            # /subject text for theory-vs-lab, and the leading
            # digit of class_name for semester) - nothing here
            # is hardcoded to any specific teacher or class.
            # --------------------------------------------------

            if teacher and not day:

                result = (
                    self.workload_engine.weekly_workload(
                        teacher
                    )
                )

                total_periods = result.get("total_periods", 0)
                by_day = result.get("by_day", {})
                events = result.get("events", [])

                if total_periods == 0:
                    return (
                        f"No timetable entries found for "
                        f"{teacher}."
                    )

                lab_count = 0
                theory_count = 0
                semester_counts = {}

                for event in events:

                    subject = str(
                        event.get("subject", "")
                    ).lower()
                    event_type = str(
                        event.get("type", "")
                    ).lower()

                    if "lab" in subject or "lab" in event_type:
                        lab_count += 1
                    else:
                        theory_count += 1

                    class_name = str(
                        event.get("class_name", "")
                    ).strip()

                    sem_match = re.match(
                        r"(\d+)",
                        class_name
                    )

                    if sem_match:
                        sem = sem_match.group(1)
                        semester_counts[sem] = (
                            semester_counts.get(sem, 0) + 1
                        )

                # ------------------------------------------
                # Hand the structured result to the
                # presentation layer (engine/response_
                # formatter.py) - this function only computes
                # WHAT the workload is; formatting HOW it looks
                # is entirely response_formatter's job.
                # ------------------------------------------

                workload_data = {
                    "faculty": teacher,
                    "total": total_periods,
                    "theory": theory_count,
                    "lab": lab_count,
                    "by_day": by_day,
                    "by_semester": semester_counts,
                }

                return format_workload_response(
                    workload_data
                )

            # --------------------------------------------------
            # WORKLOAD SUMMARY
            # --------------------------------------------------

            if day:

                result = (
                    self.workload_engine.workload_summary(day)
                )

                return (
                    f"Workload summary for {day}:\n"
                    f"Faculty count: {result['faculty_count']}\n"
                    f"Total periods: {result['total_periods']}\n"
                    f"Average periods: {result['average_periods']}\n"
                    f"Minimum periods: {result['minimum_periods']}\n"
                    f"Maximum periods: {result['maximum_periods']}"
                )

            return (
                "Please specify a day for the workload query, "
                "for example Monday."
            )


        


        # ==================================================
        # FACULTY PERIOD QUERY
        # ==================================================

        if self._is_faculty_period_query(query):

            start_time, end_time = (
                self._extract_time_range(query)
            )

            day = self._extract_day(query)

            if day and start_time and end_time:

                return self._process_faculty_period_query(
                    query
                )


        # --------------------------------------------------
        # EXISTING QUERY PIPELINE
        # --------------------------------------------------
        


        # --------------------------------------------------
        # EXISTING QUERY PIPELINE
        # --------------------------------------------------

        # STEP 1 - TOKENIZATION

        tokens = QueryTokenizer.tokenize(
            query
        )

        # STEP 2 - STOPWORD REMOVAL

        filtered_tokens = StopWordFilter.filter(
            tokens
        )

        # STEP 3 - DAY & SLOT EXTRACTION

        day_slot = DaySlotExtractor.extract(
            filtered_tokens
        )

        # STEP 4 - ENTITY EXTRACTION

        entities = self.extractor.extract(
            day_slot["remaining_tokens"]
        )

        # STEP 5 - INTENT DETECTION

        intent = IntentDetector.detect(
            tokens,
            entities,
            day_slot
        )

        # STEP 6 - QUERY PLANNING

        result = QueryPlanner.plan(
            intent,
            entities,
            day_slot,
            self.query_engine
        )

        # STEP 7 - RESPONSE GENERATION

        response = ResponseGenerator.generate(
            intent,
            result
        )

        return response


# ==========================================================
# COMMAND LINE CHATBOT
# ==========================================================

def main():

    print("=" * 70)
    print("        FACULTY FREE SLOT AI ASSISTANT")
    print("=" * 70)

    print("\nExamples:")

    print("• Who teaches Python?")
    print("• Show timetable of 3CS-D")
    print("• Where is OS III?")
    print("• Available faculty Monday Slot 3")
    print("• Who is free on Monday between 09:15 and 11:15?")
    print("• Which faculty are available Wednesday from 09:15 to 11:15?")
    print("• Subject of Dr Mehul Mahrishi")

    print("\nType 'exit' to quit.")

    chatbot = FacultyAIChatbot()

    while True:

        try:

            query = input(
                "\nYou : "
            ).strip()

            if not query:
                continue

            if query.lower() in {
                "exit",
                "quit",
                "bye"
            }:

                print(
                    "\nAssistant : Goodbye! Have a nice day."
                )

                break

            response = chatbot.process_query(
                query
            )

            print("\nAssistant:\n")
            print(response)

        except KeyboardInterrupt:

            print(
                "\n\nAssistant : Session terminated."
            )

            break

        except Exception as e:

            print(
                "\nAssistant : Something went wrong."
            )

            print(
                f"Error: {e}"
            )


if __name__ == "__main__":
    main()