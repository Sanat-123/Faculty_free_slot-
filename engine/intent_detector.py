import re


class IntentDetector:
    """
    Rule-based Intent Detector.

    Uses:
    - keywords
    - extracted entities
    - extracted day/slot

    No fuzzy sentence matching.
    """

    INTENT_RULES = {

        # ==================================================
        # FACULTY STATUS
        # ==================================================

        "FACULTY_STATUS": {
            "keywords": {
                "free",
                "available",
                "vacant",
                "busy",
                "occupied"
            }
        },

        # ==================================================
        # FIND TEACHER
        # ==================================================

        "FIND_TEACHER": {
            "keywords": {
                "teach",
                "teaches",
                "teacher",
                "teachers",
                "teaching",
                "faculty",
                "handle",
                "handles",
                "handling"
            }
        },

        # ==================================================
        # SHOW TIMETABLE
        # ==================================================

        "SHOW_TIMETABLE": {
            "keywords": {
                "timetable",
                "schedule",
                "routine"
            }
        },

        # ==================================================
        # FIND ROOM
        # ==================================================

        "FIND_ROOM": {
            "keywords": {
                "room",
                "where",
                "location"
            }
        },

        # ==================================================
        # FIND FREE FACULTY
        # ==================================================

        "FIND_FREE_FACULTY": {
            "keywords": {
                "free",
                "available",
                "vacant"
            }
        },

        # ==================================================
        # FIND SUBJECT
        # ==================================================

        "FIND_SUBJECT": {
            "keywords": {
                "subject",
                "subjects",
                "course",
                "courses"
            }
        }
    }

    # ======================================================
    # HELPERS
    # ======================================================

    @staticmethod
    def _safe_dict(value):
        """
        Make sure entities/day_slot are dictionaries.
        """

        if isinstance(value, dict):
            return value

        return {}

    @staticmethod
    def _has_keywords(words, keywords):
        """
        Returns True when at least one keyword is present.
        """

        return bool(words.intersection(keywords))

    # ======================================================
    # MAIN DETECTOR
    # ======================================================

    @staticmethod
    def detect(tokens, entities, day_slot):

        # --------------------------------------------------
        # SAFETY
        # --------------------------------------------------

        if tokens is None:
            tokens = []

        if entities is None:
            entities = {}

        if day_slot is None:
            day_slot = {}

        entities = IntentDetector._safe_dict(
            entities
        )

        day_slot = IntentDetector._safe_dict(
            day_slot
        )

        # --------------------------------------------------
        # NORMALIZE TOKENS
        # --------------------------------------------------

        words = {
            str(token).strip().casefold()
            for token in tokens
            if str(token).strip()
        }

        # --------------------------------------------------
        # EXTRACT ENTITIES SAFELY
        # --------------------------------------------------

        teachers = entities.get(
            "teachers",
            []
        )

        subjects = entities.get(
            "subjects",
            []
        )

        rooms = entities.get(
            "rooms",
            []
        )

        classes = entities.get(
            "classes",
            []
        )

        groups = entities.get(
            "groups",
            []
        )

        # Make sure entity values are lists.
        if not isinstance(teachers, list):
            teachers = []

        if not isinstance(subjects, list):
            subjects = []

        if not isinstance(rooms, list):
            rooms = []

        if not isinstance(classes, list):
            classes = []

        if not isinstance(groups, list):
            groups = []

        # --------------------------------------------------
        # DAY / SLOT
        # --------------------------------------------------

        day = day_slot.get(
            "day"
        )

        slot = day_slot.get(
            "slot"
        )

        has_day = bool(day)

        # IMPORTANT:
        # slot 0 should still be considered a supplied slot.
        has_slot = slot is not None

        # ==================================================
        # 1. FACULTY STATUS
        # ==================================================
        #
        # Highest priority.
        #
        # Example:
        #
        # "Is Mr. Nitin Goyal free on Monday slot 2?"
        #
        # "Is Mr. Nitin Goyal busy on Monday slot 2?"
        #
        # "Is Mr. Nitin Goyal available Monday slot 2?"
        #
        # These MUST NOT become FIND_FREE_FACULTY.
        #
        # They refer to ONE specific faculty member.
        # ==================================================

        faculty_status_words = (
            words.intersection(
                IntentDetector.INTENT_RULES[
                    "FACULTY_STATUS"
                ]["keywords"]
            )
        )

        if faculty_status_words:

            if (
                teachers
                and has_day
                and has_slot
            ):

                return "FACULTY_STATUS"

        # ==================================================
        # 2. FIND FREE FACULTY
        # ==================================================
        #
        # Examples:
        #
        # "Which faculty is free on Monday?"
        #
        # "Which faculty is available on Monday slot 3?"
        #
        # "Give me free faculty Monday slot 2"
        #
        # A day is required by QueryPlanner.
        # ==================================================

        free_faculty_words = (
            words.intersection(
                IntentDetector.INTENT_RULES[
                    "FIND_FREE_FACULTY"
                ]["keywords"]
            )
        )

        if free_faculty_words:

            # ------------------------------------------------
            # Do NOT classify a named faculty as
            # FIND_FREE_FACULTY when all three pieces are
            # present. That case was already handled above.
            # ------------------------------------------------

            if teachers and has_day and has_slot:
                return "FACULTY_STATUS"

            # ------------------------------------------------
            # General free-faculty query requires a day.
            #
            # QueryPlanner also requires a day.
            # ------------------------------------------------

            if has_day:

                return "FIND_FREE_FACULTY"

        # ==================================================
        # 3. FIND TEACHER
        # ==================================================
        #
        # Example:
        #
        # "Who teaches DBMS?"
        #
        # "Who is the teacher of Operating System?"
        #
        # "Which faculty teaches DBMS?"
        # ==================================================

        teacher_keywords = (
            words.intersection(
                IntentDetector.INTENT_RULES[
                    "FIND_TEACHER"
                ]["keywords"]
            )
        )

        if teacher_keywords:

            # ==============================================
            # 3a. DISAMBIGUATE CLASS vs SUBJECT
            # ==============================================
            #
            # A raw query token that looks like a class code
            # (digits followed by letters, e.g. "7cs", "5cs",
            # "3ece", optionally with a "-section" suffix)
            # is treated as referring to a CLASS even when the
            # fuzzy subject matcher also produced a coincidental
            # subject match from unrelated text containing that
            # same token (e.g. a subject whose raw description
            # happens to mention "5CS").
            #
            # This is a generic, shape-based check - it never
            # references any specific class name, so it applies
            # equally to any class present in the timetable.
            # ==============================================

            class_like_token = any(
                re.fullmatch(
                    r"\d+[a-z]+(-[a-z0-9]+)*",
                    word
                )
                for word in words
            )

            # ==============================================
            # 3b. GENUINE SUBJECT vs COINCIDENTAL SUBJECT
            # ==============================================
            #
            # The guard above exists for the coincidental
            # case: a class-shaped token (e.g. "7cs") is
            # ALSO what the fuzzy subject matcher matched on
            # (entities["subjects"][i]["text"] is itself
            # class-shaped) - there the class interpretation
            # should win.
            #
            # It must NOT fire when a real, separate subject
            # is present (e.g. "Who teaches AOA in 7CSA?" -
            # the subject entity's own matched text is "aoa",
            # not class-shaped) just because the query ALSO
            # contains a class-like token. That case has BOTH
            # a genuine subject and a genuine class, and
            # QueryPlanner's FIND_TEACHER handler filters by
            # both, so it should win instead.
            # ==============================================

            subject_matches_are_class_shaped = bool(
                subjects
            ) and all(
                re.fullmatch(
                    r"\d+[a-z]+(-[a-z0-9]+)*",
                    str(
                        entry.get("text", "")
                        if isinstance(entry, dict)
                        else entry
                    ).strip().casefold()
                )
                for entry in subjects
            )

            if classes and (
                not subjects
                or (
                    class_like_token
                    and subject_matches_are_class_shaped
                )
            ):

                return "FIND_CLASS_TEACHER"

            if subjects:

                return "FIND_TEACHER"

        # ==================================================
        # 4. FIND SUBJECT
        # ==================================================
        #
        # Example:
        #
        # "What subjects does Mr. Sharma teach?"
        #
        # "Subjects of Mr. Sharma"
        # ==================================================

        subject_keywords = (
            words.intersection(
                IntentDetector.INTENT_RULES[
                    "FIND_SUBJECT"
                ]["keywords"]
            )
        )

        if subject_keywords:

            if teachers:

                return "FIND_SUBJECT"

        # ==================================================
        # 5. FIND ROOM
        # ==================================================
        #
        # Example:
        #
        # "Where is DBMS?"
        #
        # "What room is DBMS in?"
        #
        # "DBMS classroom?"
        # ==================================================

        room_keywords = (
            words.intersection(
                IntentDetector.INTENT_RULES[
                    "FIND_ROOM"
                ]["keywords"]
            )
        )

        if room_keywords:

            if subjects:

                return "FIND_ROOM"

        # ==================================================
        # 6. SHOW TIMETABLE
        # ==================================================
        #
        # Example:
        #
        # "Show Mr. Sharma timetable"
        #
        # "Give timetable of 3CSA"
        #
        # "Show faculty schedule"
        # ==================================================

        timetable_keywords = (
            words.intersection(
                IntentDetector.INTENT_RULES[
                    "SHOW_TIMETABLE"
                ]["keywords"]
            )
        )

        if timetable_keywords:

            if (
                teachers
                or classes
                or groups
                or rooms
                or subjects
            ):

                return "SHOW_TIMETABLE"

        # ==================================================
        # 7. UNKNOWN
        # ==================================================

        return "UNKNOWN"


__all__ = [
    "IntentDetector"
]