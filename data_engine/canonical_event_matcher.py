"""
=============================================================
UNISCHED AI - CANONICAL EVENT MATCHER
=============================================================

Purpose
-------
Convert records coming from different dataset sources into
one common canonical representation.

Supported source types
----------------------
1. Facultywise timetable PDF
2. Classwise timetable PDF
3. Location-wise timetable PDF
4. Excel timetable
5. CSV timetable

The engine separates:

    SCHEDULED EVENT
    FACULTY FREE SLOT
    CLASS FREE SLOT
    ROOM FREE SLOT
    CONTRACT RECORD
    UNMATCHED RECORD

Important
---------
This matcher does NOT assume that every uploaded dataset has
day/slot information.

Excel/CSV contract datasets can therefore be imported even
when they contain no day or slot.

=============================================================
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple


class CanonicalEventMatcher:

    # =========================================================
    # INITIALIZATION
    # =========================================================

    def __init__(
        self,
        records: Optional[Iterable[Dict[str, Any]]] = None
    ):

        # Raw input
        self.records: List[Dict[str, Any]] = []

        # Canonical scheduled events
        self.events: List[Dict[str, Any]] = []

        # Free-slot categories
        self.faculty_free_slots: List[Dict[str, Any]] = []
        self.class_free_slots: List[Dict[str, Any]] = []
        self.room_free_slots: List[Dict[str, Any]] = []

        # Contract datasets such as Excel / CSV
        self.contract_records: List[Dict[str, Any]] = []

        # Records that cannot be classified/matched
        self.unmatched_records: List[Dict[str, Any]] = []

        # Groups generated during matching
        self.matched_groups: Dict[
            Tuple,
            List[Dict[str, Any]]
        ] = {}

        # Conflicts
        self.conflicts: List[Dict[str, Any]] = []

        # Statistics
        self.raw_record_count = 0

        if records is not None:
            self.records = list(records)

    # =========================================================
    # GENERIC FIELD ACCESS
    # =========================================================

    @staticmethod
    def get_field(
        record: Dict[str, Any],
        field: str,
        default: Any = ""
    ) -> Any:

        if not isinstance(record, dict):
            return default

        value = record.get(field, default)

        if value is None:
            return default

        return value

    # =========================================================
    # TEXT NORMALIZATION
    # =========================================================

    @staticmethod
    def normalize_text(
        value: Any
    ) -> str:

        if value is None:
            return ""

        text = str(value)

        text = text.replace(
            "\xa0",
            " "
        )

        text = " ".join(
            text.strip().split()
        )

        return text.lower()

    # =========================================================
    # DISPLAY TEXT NORMALIZATION
    # =========================================================

    @staticmethod
    def clean_display_text(
        value: Any
    ) -> str:

        if value is None:
            return ""

        return " ".join(
            str(value)
            .replace("\xa0", " ")
            .strip()
            .split()
        )

    # =========================================================
    # DAY NORMALIZATION
    # =========================================================

    @classmethod
    def normalize_day(
        cls,
        day: Any
    ) -> str:

        value = cls.normalize_text(day)

        mapping = {

            "mo": "monday",
            "mon": "monday",
            "monday": "monday",

            "tu": "tuesday",
            "tue": "tuesday",
            "tues": "tuesday",
            "tuesday": "tuesday",

            "we": "wednesday",
            "wed": "wednesday",
            "wednesday": "wednesday",

            "th": "thursday",
            "thu": "thursday",
            "thur": "thursday",
            "thurs": "thursday",
            "thursday": "thursday",

            "fr": "friday",
            "fri": "friday",
            "friday": "friday",

            "sa": "saturday",
            "sat": "saturday",
            "saturday": "saturday",

            "su": "sunday",
            "sun": "sunday",
            "sunday": "sunday",

        }

        return mapping.get(
            value,
            value
        )

    # =========================================================
    # SLOT NORMALIZATION
    # =========================================================

    @classmethod
    def normalize_slot(
        cls,
        slot: Any
    ) -> Any:

        if slot is None:
            return None

        text = str(slot).strip()

        if not text:
            return None

        try:

            number = float(text)

            if number.is_integer():
                return int(number)

            return number

        except (
            ValueError,
            TypeError
        ):

            return text.lower()

    # =========================================================
    # SOURCE TYPE
    # =========================================================

    @classmethod
    def source_type(
        cls,
        record: Dict[str, Any]
    ) -> str:

        return cls.normalize_text(
            cls.get_field(
                record,
                "source_type"
            )
        )

    # =========================================================
    # SOURCE FILE
    # =========================================================

    @classmethod
    def source_file(
        cls,
        record: Dict[str, Any]
    ) -> str:

        return cls.clean_display_text(
            cls.get_field(
                record,
                "source_file"
            )
        )

    # =========================================================
    # IDENTIFY SOURCE
    # =========================================================

    @classmethod
    def identify_source(
        cls,
        record: Dict[str, Any]
    ) -> str:

        # -----------------------------------------------------
        # CONTENT-BASED (preferred)
        #
        # Set directly by PDFImporter.create_record() from
        # detect_page_identity() - i.e. from what the PAGE
        # ITSELF says it is (a "Teacher ..." line, or a bare
        # class/room code immediately above the slot-header
        # row), never from the uploaded file's name. This is
        # what makes classification independent of what the
        # user happens to name their file.
        # -----------------------------------------------------

        page_identity_type = cls.normalize_text(
            cls.get_field(
                record,
                "page_identity_type"
            )
        )

        if page_identity_type == "teacher":
            return "FACULTYWISE"

        if page_identity_type == "class":
            return "CLASSWISE"

        if page_identity_type == "room":
            return "LOCATIONWISE"

        # -----------------------------------------------------
        # FILENAME-BASED (fallback)
        #
        # Only reached for records that don't carry a
        # page_identity_type at all (e.g. Excel/CSV-sourced
        # records, or a PDF page whose content-based identity
        # could not be determined - see detect_page_identity()).
        # -----------------------------------------------------

        source = cls.normalize_text(
            cls.source_file(record)
        )

        source_type = cls.source_type(
            record
        )

        if "facultywise" in source:
            return "FACULTYWISE"

        if "classwise" in source:
            return "CLASSWISE"

        if (
            "location wise" in source
            or "location-wise" in source
            or "locationwise" in source
        ):
            return "LOCATIONWISE"

        if source_type == "excel":
            return "EXCEL"

        if source_type == "csv":
            return "CSV"

        if source_type == "pdf":
            return "PDF"

        return source_type.upper()

    # =========================================================
    # HAS DAY
    # =========================================================

    @classmethod
    def has_day(
        cls,
        record: Dict[str, Any]
    ) -> bool:

        return bool(
            cls.normalize_day(
                cls.get_field(
                    record,
                    "day"
                )
            )
        )

    # =========================================================
    # HAS SLOT
    # =========================================================

    @classmethod
    def has_slot(
        cls,
        record: Dict[str, Any]
    ) -> bool:

        return (
            cls.normalize_slot(
                cls.get_field(
                    record,
                    "slot"
                )
            )
            is not None
        )

    # =========================================================
    # HAS TEACHER
    # =========================================================

    @classmethod
    def has_teacher(
        cls,
        record: Dict[str, Any]
    ) -> bool:

        return bool(
            cls.clean_display_text(
                cls.get_field(
                    record,
                    "teacher"
                )
            )
        )

    # =========================================================
    # HAS SUBJECT
    # =========================================================

    @classmethod
    def has_subject(
        cls,
        record: Dict[str, Any]
    ) -> bool:

        return bool(
            cls.clean_display_text(
                cls.get_field(
                    record,
                    "subject"
                )
            )
        )

    # =========================================================
    # HAS CLASS
    # =========================================================

    @classmethod
    def has_class(
        cls,
        record: Dict[str, Any]
    ) -> bool:

        return bool(
            cls.clean_display_text(
                cls.get_field(
                    record,
                    "class_name"
                )
            )
        )

    # =========================================================
    # HAS ROOM
    # =========================================================

    @classmethod
    def has_room(
        cls,
        record: Dict[str, Any]
    ) -> bool:

        return bool(
            cls.clean_display_text(
                cls.get_field(
                    record,
                    "room"
                )
            )
        )

    # =========================================================
    # CONTRACT RECORD DETECTION
    # =========================================================

    @classmethod
    def is_contract_record(
        cls,
        record: Dict[str, Any]
    ) -> bool:

        source_type = cls.source_type(
            record
        )

        # -----------------------------------------------------
        # Excel and CSV are contract datasets in the current
        # UNISCHED architecture.
        # -----------------------------------------------------

        if source_type in {
            "excel",
            "csv"
        }:

            return True

        # -----------------------------------------------------
        # Generic fallback:
        # no day + no slot, but useful timetable information
        # exists.
        # -----------------------------------------------------

        if not cls.has_day(record) and not cls.has_slot(record):

            useful_fields = [

                "teacher",
                "subject",
                "class_name",
                "group_name",
                "room",
                "length",
                "lessons_per_week",
                "available_classrooms",
                "cycle",

            ]

            for field in useful_fields:

                value = cls.get_field(
                    record,
                    field
                )

                if cls.clean_display_text(value):

                    return True

        return False

    # =========================================================
    # DETERMINE EMPTY CELL
    # =========================================================

    @classmethod
    def is_empty_cell(
        cls,
        record: Dict[str, Any]
    ) -> bool:

        # A record without day/slot cannot represent a
        # timetable cell.
        if not cls.has_day(record):
            return False

        if not cls.has_slot(record):
            return False

        # -----------------------------------------------------
        # The field that represents THIS PAGE's own identity
        # (e.g. class_name for a classwise page, room for a
        # location-wise page) is always populated, even for a
        # genuinely free slot -- it identifies the page, not
        # whether the slot is occupied. That field must be
        # excluded from the busy-signal check, or a
        # classwise/location-wise page would never register a
        # free slot at all.
        # -----------------------------------------------------

        source = cls.identify_source(
            record
        )

        if source == "CLASSWISE":

            fields = [
                "subject",
                "room",
                "group_name",
            ]

        elif source == "LOCATIONWISE":

            fields = [
                "subject",
                "class_name",
                "group_name",
            ]

        else:

            fields = [
                "subject",
                "room",
                "class_name",
                "group_name",
            ]

        for field in fields:

            if cls.clean_display_text(
                cls.get_field(
                    record,
                    field
                )
            ):

                return False

        return True

    # =========================================================
    # DETECT EMPTY SLOT TYPE
    # =========================================================

    @classmethod
    def detect_empty_slot_type(
        cls,
        record: Dict[str, Any]
    ) -> str:

        source = cls.identify_source(
            record
        )

        if source == "FACULTYWISE":

            return "FACULTY_FREE_SLOT"

        if source == "CLASSWISE":

            return "CLASS_FREE_SLOT"

        if source == "LOCATIONWISE":

            return "ROOM_FREE_SLOT"

        return "UNKNOWN_EMPTY_SLOT"

    # =========================================================
    # FACULTY FREE SLOT
    # =========================================================

    @classmethod
    def is_free_slot(
        cls,
        record: Dict[str, Any]
    ) -> bool:

        source = cls.identify_source(
            record
        )

        # Facultywise timetable is the authoritative source
        # for faculty availability.
        if source != "FACULTYWISE":
            return False

        if not cls.has_teacher(record):
            return False

        if not cls.has_day(record):
            return False

        if not cls.has_slot(record):
            return False

        # Empty subject/class/room means teacher is free.
        return (

            not cls.has_subject(record)
            and not cls.has_room(record)
            and not cls.has_class(record)

        )

    # =========================================================
    # SCHEDULED EVENT CHECK
    # =========================================================

    @classmethod
    def is_scheduled_event(
        cls,
        record: Dict[str, Any]
    ) -> bool:

        if not cls.has_day(record):
            return False

        if not cls.has_slot(record):
            return False

        # At least one meaningful schedule field.
        return (

            cls.has_subject(record)
            or cls.has_room(record)
            or cls.has_class(record)

        )

    # =========================================================
    # MATCH KEY
    # =========================================================

    @classmethod
    def match_key(
        cls,
        record: Dict[str, Any]
    ) -> Optional[Tuple]:

        if not cls.is_scheduled_event(record):
            return None

        teacher = cls.normalize_text(
            cls.get_field(
                record,
                "teacher"
            )
        )

        day = cls.normalize_day(
            cls.get_field(
                record,
                "day"
            )
        )

        slot = cls.normalize_slot(
            cls.get_field(
                record,
                "slot"
            )
        )

        subject = cls.normalize_text(
            cls.get_field(
                record,
                "subject"
            )
        )

        room = cls.normalize_text(
            cls.get_field(
                record,
                "room"
            )
        )

        class_name = cls.normalize_text(
            cls.get_field(
                record,
                "class_name"
            )
        )

        # -----------------------------------------------------
        # Primary identity:
        #
        # teacher + day + slot
        #
        # This works especially well for Facultywise data.
        # -----------------------------------------------------

        if teacher:

            return (
                "SCHEDULED",
                teacher,
                day,
                slot,
                subject,
                room,
                class_name,
            )

        # -----------------------------------------------------
        # Classwise / location-wise fallback
        # -----------------------------------------------------

        if class_name:

            return (
                "SCHEDULED_CLASS",
                class_name,
                day,
                slot,
                subject,
                room,
            )

        if room:

            return (
                "SCHEDULED_ROOM",
                room,
                day,
                slot,
                subject,
                class_name,
            )

        return None

    # =========================================================
    # CANONICAL EVENT CREATION
    # =========================================================

    @classmethod
    def create_canonical_event(
        cls,
        records: List[Dict[str, Any]]
    ) -> Dict[str, Any]:

        if not records:
            return {}

        # Prefer Facultywise information
        ordered = sorted(
            records,
            key=lambda r: (
                0
                if cls.identify_source(r)
                == "FACULTYWISE"
                else
                1
            )
        )

        primary = ordered[0]

        event = {

            "record_type":
                "SCHEDULED_EVENT",

            "teacher":
                cls.clean_display_text(
                    primary.get(
                        "teacher"
                    )
                ),

            "day":
                cls.normalize_day(
                    primary.get(
                        "day"
                    )
                ),

            "slot":
                cls.normalize_slot(
                    primary.get(
                        "slot"
                    )
                ),

            "slot_time":
                cls.clean_display_text(
                    primary.get(
                        "slot_time"
                    )
                ),

            "subject":
                cls.clean_display_text(
                    primary.get(
                        "subject"
                    )
                ),

            "room":
                cls.clean_display_text(
                    primary.get(
                        "room"
                    )
                ),

            "class_name":
                cls.clean_display_text(
                    primary.get(
                        "class_name"
                    )
                ),

            "group_name":
                cls.clean_display_text(
                    primary.get(
                        "group_name"
                    )
                ),

            "type":
                cls.clean_display_text(
                    primary.get(
                        "type"
                    )
                ),

            "source_file":
                cls.source_file(
                    primary
                ),

            "source_type":
                cls.source_type(
                    primary
                ),

            "source_page":
                primary.get(
                    "source_page"
                ),

            "sources": [],

            "source_records": [],

            "multi_source": False,

            "conflicts": [],

        }

        # -----------------------------------------------------
        # Enrich from all records
        # -----------------------------------------------------

        source_names = set()

        for record in records:

            source = cls.source_file(
                record
            )

            if source:
                source_names.add(
                    source
                )

            event[
                "source_records"
            ].append(
                dict(record)
            )

            # Fill missing fields
            for field in [

                "teacher",
                "subject",
                "room",
                "class_name",
                "group_name",
                "slot_time",
                "type",

            ]:

                if not event.get(field):

                    value = cls.clean_display_text(
                        record.get(
                            field
                        )
                    )

                    if value:
                        event[field] = value

        event[
            "sources"
        ] = sorted(
            source_names
        )

        event[
            "multi_source"
        ] = len(
            source_names
        ) > 1

        return event

    # =========================================================
    # CONFLICT DETECTION
    # =========================================================

    @classmethod
    def detect_conflicts(
        cls,
        records: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:

        conflicts = []

        fields = [

            "teacher",
            "subject",
            "room",
            "class_name",

        ]

        for field in fields:

            values = set()

            for record in records:

                value = cls.normalize_text(
                    record.get(
                        field
                    )
                )

                if value:

                    values.add(
                        value
                    )

            if len(values) > 1:

                conflicts.append({

                    "field": field,

                    "values":
                        sorted(values),

                })

        return conflicts

    # =========================================================
    # MAIN MATCH METHOD
    # =========================================================

    @staticmethod
    def _teacher_initials(teacher: str) -> str:

        """
        Derive simple initials from a full teacher name, e.g.
        "Dr. Mehul Mahrishi" -> "MM", "Mr. Ashish Pant" -> "AP".
        Purely mechanical (title stripped, first letter of each
        remaining word) - not a lookup against any fixed list,
        so it generalizes to whatever names the uploaded
        timetable actually contains.
        """

        text = str(teacher or "").strip()

        text = re.sub(
            r"^(?:dr|mr|mrs|ms|prof|professor)\.?\s*",
            "",
            text,
            flags=re.IGNORECASE
        )

        parts = [
            p for p in re.split(
                r"[\s.]+",
                text
            )
            if p
        ]

        if not parts:
            return ""

        return "".join(p[0] for p in parts).upper()

    def _normalize_subjects_for_fusion(
        self,
        records: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:

        """
        Strip two data-derived kinds of noise from `subject`
        before matching, so the same real class session
        described slightly differently across source files
        (a room fragment or instructor short-code glued onto
        the subject text in one file but not another) still
        normalizes to the same text and can fuse together:

          1. A trailing token equal to a ROOM value seen
             elsewhere in THIS dataset -> moved into `room` if
             blank, always stripped from `subject` (stripping is
             safe regardless of ambiguity - it never invents an
             attribution, it only removes noise).
          2. A trailing token equal to a teacher's initials
             (title dropped, first letter of each name part) ->
             always stripped from `subject`; only used to FILL a
             blank `teacher` field when the initials are
             unambiguous (belong to exactly one teacher actually
             present in this dataset).

        Both reference sets (known_rooms, known_initials) are
        built FROM the uploaded data itself - nothing here is a
        fixed list of abbreviations.
        """

        known_rooms = set()
        known_full_names = set()
        trusted_full_names = set()
        initials_to_teachers: Dict[str, set] = defaultdict(set)

        for record in records:

            if not isinstance(record, dict):
                continue

            room = str(record.get("room", "")).strip()

            if room:
                known_rooms.add(room)

            teacher = str(record.get("teacher", "")).strip()

            if teacher:

                known_full_names.add(teacher)

                # A Facultywise page's teacher identity comes
                # straight from that page's own header line
                # (detect_teacher()), not from cell-level legend
                # text or abbreviation resolution - it is the
                # most reliable name source available, and is
                # used below to repair a teacher value that a
                # DIFFERENT source's own text-extraction has
                # corrupted (e.g. two overlapping text runs on a
                # page rendering as one scrambled string).
                if record.get("page_identity_type") == "teacher":
                    trusted_full_names.add(teacher)

                initials = self._teacher_initials(teacher)

                if initials:
                    initials_to_teachers[initials].add(teacher)

        known_initials = set(initials_to_teachers.keys())

        unambiguous_initials = {
            initials: next(iter(teachers))
            for initials, teachers in initials_to_teachers.items()
            if len(teachers) == 1
        }

        # ------------------------------------------------------
        # ROOM PREFIX ALIASES
        #
        # The same physical room can appear under a shorter,
        # bare-word reference in one source and its full
        # multi-word name in another (e.g. a room-wise page's own
        # title is "IAI Lab", but a Facultywise cell's text ends
        # in just "IAI"). For every multi-word room in this
        # dataset, its FIRST word is added as a recognized alias
        # for the full name - but ONLY when that first word is
        # not shared by any OTHER multi-word room here (no
        # collision), so this never guesses between two different
        # rooms that happen to start with the same word.
        # ------------------------------------------------------

        room_first_word_owners: Dict[str, set] = defaultdict(set)

        for room in known_rooms:

            room_words = room.split()

            if len(room_words) > 1:
                room_first_word_owners[room_words[0]].add(room)

        room_prefix_aliases = {
            first_word: next(iter(owners))
            for first_word, owners in (
                room_first_word_owners.items()
            )
            if len(owners) == 1
            and first_word not in known_rooms
        }

        # ------------------------------------------------------
        # REPAIR A CORRUPTED TEACHER FIELD USING A TRUSTED NAME
        #
        # Some source pages garble adjacent text during PDF
        # extraction (two overlapping text runs interleaved into
        # one string) - e.g. "Trivendra Kumar Sharma Dr.
        # AnjanAaS" where the real value should just be
        # "Trivendra Kumar Sharma". Rather than trying to parse
        # the garbage, if a teacher value STARTS WITH an already
        # trusted, clean name and has extra trailing text, it is
        # truncated back to that trusted name - the same
        # correction a person would make by cross-checking
        # against the Facultywise page for that same teacher.
        # ------------------------------------------------------

        if trusted_full_names:

            sorted_trusted = sorted(
                trusted_full_names,
                key=len,
                reverse=True
            )

            for record in records:

                if not isinstance(record, dict):
                    continue

                teacher = str(record.get("teacher", "")).strip()

                if not teacher or teacher in trusted_full_names:
                    continue

                for trusted in sorted_trusted:

                    if (
                        teacher.startswith(trusted)
                        and len(teacher) > len(trusted)
                        and teacher[len(trusted)] in (
                            " ", "\t"
                        )
                    ):
                        record["teacher"] = trusted
                        break

        cleaned = []

        for record in records:

            if not isinstance(record, dict):
                cleaned.append(record)
                continue

            subject = str(record.get("subject", "")).strip()

            if not subject:
                cleaned.append(record)
                continue

            tokens = subject.split()

            if not tokens:
                cleaned.append(record)
                continue

            record = dict(record)
            changed_any = False

            while len(tokens) > 1:

                stripped_one = False

                # Try multi-word room names first (longest
                # match wins), since some real rooms in this
                # dataset are 2-3 words (e.g. "IAI Lab",
                # "Central Lib") - checking only the single
                # trailing token would miss these. A bare-word
                # alias (e.g. "IAI" for "IAI Lab" - see
                # room_prefix_aliases above) is checked right
                # alongside the exact room set, and resolves to
                # the FULL room name, not the bare word.
                max_span = min(3, len(tokens) - 1)

                for span in range(max_span, 0, -1):

                    candidate = " ".join(tokens[-span:])

                    resolved_room = (
                        candidate
                        if candidate in known_rooms
                        else room_prefix_aliases.get(candidate)
                    )

                    if resolved_room:

                        if not str(
                            record.get("room", "")
                        ).strip():
                            record["room"] = resolved_room

                        tokens = tokens[:-span]
                        stripped_one = True
                        break

                if stripped_one:
                    changed_any = True
                    continue

                # Try a trailing run of tokens that exactly
                # matches an ALREADY-CONFIRMED full teacher name
                # from elsewhere in this dataset (e.g. a page
                # names someone by their bare first name, like
                # "Sumita", rather than a code) - this only ever
                # matches a name this dataset has independently
                # established belongs to a real teacher, so it
                # never invents an identity.
                max_name_span = min(4, len(tokens) - 1)

                for span in range(max_name_span, 0, -1):

                    candidate = " ".join(tokens[-span:])

                    if candidate in known_full_names:

                        if not str(
                            record.get("teacher", "")
                        ).strip():
                            record["teacher"] = candidate

                        tokens = tokens[:-span]
                        stripped_one = True
                        break

                if stripped_one:
                    changed_any = True
                    continue

                trailing = tokens[-1]

                if trailing in known_initials:

                    if (
                        trailing in unambiguous_initials
                        and not str(
                            record.get("teacher", "")
                        ).strip()
                    ):
                        record["teacher"] = (
                            unambiguous_initials[trailing]
                        )

                    tokens = tokens[:-1]
                    stripped_one = True

                if not stripped_one:
                    break

                changed_any = True

            if changed_any:
                record["subject"] = " ".join(tokens).strip()

            cleaned.append(record)

        return cleaned

    # =========================================================
    # CLASS-GRANULARITY RECONCILIATION
    #
    # Two source files can legitimately describe the same real
    # class period at different levels of detail - e.g. a
    # classwise page that only tracks the parent class ("5CS")
    # next to a facultywise page that also captures the division
    # ("5CS-AI-A"). match_key() treats class_name as an exact
    # field, so these land in different buckets and show up as
    # two rows for what is really one session - ESPECIALLY once
    # _normalize_subjects_for_fusion() above successfully
    # recovers a teacher name that was previously blank on the
    # coarser record.
    #
    # This pass merges canonical events ONLY when teacher, day,
    # slot, subject AND room are all identical AND the class
    # names form a genuine parent/child pair (one equals the
    # other, or one is exactly "<other>-<something>"). Anything
    # else - including two events that merely share day+slot,
    # or share day+slot+class, or share day+slot+subject, but
    # differ in teacher/room/class-in-a-way-that-isn't-a-clean
    # hierarchy - is left alone, per the project's rule that
    # legitimate parallel events must never be blindly collapsed.
    # =========================================================

    @staticmethod
    def _class_names_compatible(
        class_a: str,
        class_b: str
    ) -> bool:

        a = str(class_a or "").strip().lower()
        b = str(class_b or "").strip().lower()

        if a == b:
            return True

        if not a or not b:
            # One side has no class information at all - not
            # enough to safely claim a hierarchy relationship.
            return False

        if b.startswith(a + "-"):
            return True

        if a.startswith(b + "-"):
            return True

        return False

    @staticmethod
    def _rooms_compatible(room_a: str, room_b: str) -> bool:

        a = str(room_a or "").strip().lower()
        b = str(room_b or "").strip().lower()

        # A blank room on one side just means that source page
        # didn't repeat the room for this cell - it is not a
        # claim of "no room" that could conflict with a real
        # one. Two different REAL rooms are never merged.
        if not a or not b:
            return True

        return a == b

    def _reconcile_class_granularity(
        self,
        events: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:

        groups: Dict[Tuple, List[int]] = defaultdict(list)

        for index, event in enumerate(events):

            identity = (
                self.normalize_text(
                    event.get("teacher", "")
                ),
                self.normalize_day(
                    event.get("day", "")
                ),
                self.normalize_slot(
                    event.get("slot", "")
                ),
                self.normalize_text(
                    event.get("subject", "")
                ),
            )

            groups[identity].append(index)

        to_drop = set()

        for identity, indices in groups.items():

            if len(indices) < 2:
                continue

            # A blank teacher/subject identity is too weak to
            # safely merge on - only reconcile groups with a
            # real teacher AND subject in common. Room is
            # checked for compatibility separately below, since
            # a blank room on one side shouldn't block a merge.
            if not all(identity):
                continue

            group_events = [events[i] for i in indices]

            class_names = [
                e.get("class_name", "") for e in group_events
            ]

            rooms = [
                e.get("room", "") for e in group_events
            ]

            all_compatible = all(
                self._class_names_compatible(
                    class_names[i],
                    class_names[j]
                )
                and self._rooms_compatible(
                    rooms[i],
                    rooms[j]
                )
                for i in range(len(class_names))
                for j in range(i + 1, len(class_names))
            )

            if not all_compatible:
                continue

            # Keep the event with the most specific (longest)
            # class name as the primary record, and fold every
            # other event's sources/source_records into it.
            primary_local_index = max(
                range(len(group_events)),
                key=lambda i: len(
                    str(group_events[i].get("class_name", ""))
                )
            )

            primary = group_events[primary_local_index]
            primary_global_index = indices[primary_local_index]

            merged_sources = list(primary.get("sources", []))
            merged_records = list(
                primary.get("source_records", [])
            )

            for local_i, event in enumerate(group_events):

                if local_i == primary_local_index:
                    continue

                for source in event.get("sources", []):
                    if source not in merged_sources:
                        merged_sources.append(source)

                merged_records.extend(
                    event.get("source_records", [])
                )

                if not primary.get("room") and event.get(
                    "room"
                ):
                    primary["room"] = event["room"]

                to_drop.add(indices[local_i])

            primary["sources"] = merged_sources
            primary["source_records"] = merged_records
            primary["multi_source"] = len(merged_sources) > 1

        if not to_drop:
            events = events
        else:
            events = [
                event
                for index, event in enumerate(events)
                if index not in to_drop
            ]

        return self._reconcile_blank_teacher_via_context(
            events
        )

    def _reconcile_blank_teacher_via_context(
        self,
        events: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:

        """
        A record with NO teacher attached (its own source page
        never named one, and _normalize_subjects_for_fusion
        couldn't resolve a trailing short code because it was
        ambiguous - e.g. "MM" matches more than one teacher)
        can still be identified safely by relational context:
        if its (day, slot, subject, room, class) combination is
        IDENTICAL to an already-known, directly-sourced event
        that DOES have a teacher, that is the same real session,
        and the known teacher is adopted.

        This is intentionally a SEPARATE, stricter pass from
        _reconcile_class_granularity: it requires day, slot,
        subject, room AND class (not just teacher/day/slot/
        subject/room with a class hierarchy) to match exactly,
        specifically because it is filling in the teacher field
        itself rather than merging two already-attributed
        records. It never resolves a conflict between two
        different named teachers, and it never merges two
        blank-teacher records with each other.
        """

        by_context: Dict[Tuple, List[str]] = defaultdict(list)

        for event in events:

            teacher = str(event.get("teacher", "")).strip()

            if not teacher:
                continue

            # Room is intentionally EXCLUDED from the grouping
            # key here (day, slot, subject, class only) because
            # a blank room on one record just means that source
            # page didn't repeat the room for this cell - not
            # that it's a different room. Room agreement is
            # still checked below before any actual merge, so a
            # record with a real but DIFFERENT room is never
            # merged.
            context = (
                self.normalize_day(event.get("day", "")),
                self.normalize_slot(event.get("slot", "")),
                self.normalize_text(event.get("subject", "")),
                self.normalize_text(event.get("class_name", "")),
            )

            if not all(context):
                continue

            if teacher not in by_context[context]:
                by_context[context].append(teacher)

        kept = []

        for index, event in enumerate(events):

            teacher = str(event.get("teacher", "")).strip()

            if teacher:
                kept.append(event)
                continue

            context = (
                self.normalize_day(event.get("day", "")),
                self.normalize_slot(event.get("slot", "")),
                self.normalize_text(event.get("subject", "")),
                self.normalize_text(event.get("class_name", "")),
            )

            candidates = by_context.get(context, [])

            # Only resolve when the context points to exactly
            # ONE known teacher - a context matching two
            # different named teachers is left alone rather
            # than guessing between them.
            if len(candidates) == 1:

                target_teacher = candidates[0]
                own_room = self.normalize_text(
                    event.get("room", "")
                )

                merged = False

                for other in events:

                    if (
                        str(
                            other.get("teacher", "")
                        ).strip()
                        != target_teacher
                    ):
                        continue

                    if (
                        self.normalize_day(
                            other.get("day", "")
                        ) != context[0]
                        or self.normalize_slot(
                            other.get("slot", "")
                        ) != context[1]
                        or self.normalize_text(
                            other.get("subject", "")
                        ) != context[2]
                        or self.normalize_text(
                            other.get("class_name", "")
                        ) != context[3]
                    ):
                        continue

                    other_room = self.normalize_text(
                        other.get("room", "")
                    )

                    # Never merge two records that both name a
                    # real room but DISAGREE on which one.
                    if own_room and other_room and (
                        own_room != other_room
                    ):
                        continue

                    for source in event.get("sources", []):
                        if source not in other["sources"]:
                            other["sources"].append(source)

                    other["source_records"].extend(
                        event.get("source_records", [])
                    )

                    other["multi_source"] = (
                        len(other["sources"]) > 1
                    )

                    if not other.get("room") and event.get(
                        "room"
                    ):
                        other["room"] = event["room"]

                    merged = True
                    break

                if merged:
                    continue

            kept.append(event)

        return kept

    def match(
        self,
        records: Optional[
            Iterable[Dict[str, Any]]
        ] = None
    ) -> List[Dict[str, Any]]:

        if records is not None:

            self.records = list(
                records
            )

        records = self.records

        self.raw_record_count = len(
            records
        )

        # =====================================================
        # CROSS-FILE SUBJECT NORMALIZATION (see
        # _normalize_subjects_for_fusion for full rationale)
        # =====================================================

        records = self._normalize_subjects_for_fusion(
            records
        )

        # Reset state
        self.events = []

        self.faculty_free_slots = []

        self.class_free_slots = []

        self.room_free_slots = []

        self.contract_records = []

        self.unmatched_records = []

        self.matched_groups = {}

        self.conflicts = []

        # =====================================================
        # BUCKET SCHEDULED RECORDS
        # =====================================================

        buckets = defaultdict(list)

        # =====================================================
        # PROCESS RECORDS
        # =====================================================

        for record in records:

            if not isinstance(
                record,
                dict
            ):

                continue

            # -------------------------------------------------
            # CONTRACT
            # -------------------------------------------------

            if self.is_contract_record(
                record
            ):

                contract = dict(
                    record
                )

                contract[
                    "record_type"
                ] = "CONTRACT_RECORD"

                self.contract_records.append(
                    contract
                )

                continue

            # -------------------------------------------------
            # FACULTY FREE SLOT
            # -------------------------------------------------

            if self.is_free_slot(
                record
            ):

                free_record = dict(
                    record
                )

                free_record[
                    "day"
                ] = self.normalize_day(
                    record.get(
                        "day"
                    )
                )

                free_record[
                    "slot"
                ] = self.normalize_slot(
                    record.get(
                        "slot"
                    )
                )

                free_record[
                    "record_type"
                ] = "FACULTY_FREE_SLOT"

                self.faculty_free_slots.append(
                    free_record
                )

                continue

            # -------------------------------------------------
            # EMPTY CLASS / ROOM CELL
            # -------------------------------------------------

            if self.is_empty_cell(
                record
            ):

                empty_record = dict(
                    record
                )

                empty_record[
                    "day"
                ] = self.normalize_day(
                    record.get(
                        "day"
                    )
                )

                empty_record[
                    "slot"
                ] = self.normalize_slot(
                    record.get(
                        "slot"
                    )
                )

                empty_record[
                    "record_type"
                ] = self.detect_empty_slot_type(
                    empty_record
                )

                record_type = (
                    empty_record[
                        "record_type"
                    ]
                )

                if record_type == (
                    "FACULTY_FREE_SLOT"
                ):

                    self.faculty_free_slots.append(
                        empty_record
                    )

                elif record_type == (
                    "CLASS_FREE_SLOT"
                ):

                    self.class_free_slots.append(
                        empty_record
                    )

                elif record_type == (
                    "ROOM_FREE_SLOT"
                ):

                    self.room_free_slots.append(
                        empty_record
                    )

                continue

            # -------------------------------------------------
            # SCHEDULED EVENT
            # -------------------------------------------------

            key = self.match_key(
                record
            )

            if key is None:

                self.unmatched_records.append(
                    record
                )

                continue

            buckets[key].append(
                record
            )

        # =====================================================
        # BUILD CANONICAL EVENTS
        # =====================================================

        for key, grouped_records in buckets.items():

            event = (
                self.create_canonical_event(
                    grouped_records
                )
            )

            event[
                "match_key"
            ] = key

            conflict_list = (
                self.detect_conflicts(
                    grouped_records
                )
            )

            event[
                "conflicts"
            ] = conflict_list

            if conflict_list:

                self.conflicts.append(
                    event
                )

            self.events.append(
                event
            )

            self.matched_groups[
                key
            ] = grouped_records

        self.events = self._reconcile_class_granularity(
            self.events
        )

        return self.events

    # =========================================================
    # ALIAS
    # =========================================================

    def process(
        self,
        records: Optional[
            Iterable[Dict[str, Any]]
        ] = None
    ):

        return self.match(
            records
        )

    # =========================================================
    # GETTERS
    # =========================================================

    def get_events(
        self
    ) -> List[Dict[str, Any]]:

        return self.events

    def get_faculty_free_slots(
        self
    ) -> List[Dict[str, Any]]:

        return self.faculty_free_slots

    def get_class_free_slots(
        self
    ) -> List[Dict[str, Any]]:

        return self.class_free_slots

    def get_room_free_slots(
        self
    ) -> List[Dict[str, Any]]:

        return self.room_free_slots

    def get_contract_records(
        self
    ) -> List[Dict[str, Any]]:

        return self.contract_records

    def get_unmatched_records(
        self
    ) -> List[Dict[str, Any]]:

        return self.unmatched_records

    # =========================================================
    # SUMMARY
    # =========================================================

    def summary(
        self
    ) -> Dict[str, Any]:

        return {

            "raw_records":
                self.raw_record_count,

            "canonical_events":
                len(
                    self.events
                ),

            "faculty_free_slots":
                len(
                    self.faculty_free_slots
                ),

            "class_free_slots":
                len(
                    self.class_free_slots
                ),

            "room_free_slots":
                len(
                    self.room_free_slots
                ),

            "contract_records":
                len(
                    self.contract_records
                ),

            "unmatched_records":
                len(
                    self.unmatched_records
                ),

            "matched_groups":
                len(
                    self.matched_groups
                ),

            "multi_source_events":
                sum(
                    1
                    for event in self.events
                    if event.get(
                        "multi_source"
                    )
                ),

            "conflict_events":
                len(
                    self.conflicts
                ),

        }

    # =========================================================
    # PRINT SUMMARY
    # =========================================================

    def print_summary(
        self
    ) -> None:

        summary = self.summary()

        print()
        print(
            "# UNISCHED AI - CANONICAL EVENT MATCHER"
        )

        print()

        print(
            "Raw records:",
            summary[
                "raw_records"
            ]
        )

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

    # =========================================================
    # FIND FACULTY FREE SLOTS
    # =========================================================

    def find_faculty_free_slots(
        self,
        teacher: Optional[str] = None,
        day: Optional[str] = None,
        slot: Optional[Any] = None
    ) -> List[Dict[str, Any]]:

        teacher_normalized = (
            self.normalize_text(
                teacher
            )
            if teacher
            else None
        )

        day_normalized = (
            self.normalize_day(
                day
            )
            if day
            else None
        )

        slot_normalized = (
            self.normalize_slot(
                slot
            )
            if slot is not None
            else None
        )

        results = []

        for record in (
            self.faculty_free_slots
        ):

            record_teacher = (
                self.normalize_text(
                    record.get(
                        "teacher"
                    )
                )
            )

            record_day = (
                self.normalize_day(
                    record.get(
                        "day"
                    )
                )
            )

            record_slot = (
                self.normalize_slot(
                    record.get(
                        "slot"
                    )
                )
            )

            if (
                teacher_normalized
                and
                record_teacher
                != teacher_normalized
            ):

                continue

            if (
                day_normalized
                and
                record_day
                != day_normalized
            ):

                continue

            if (
                slot_normalized
                is not None
                and
                record_slot
                != slot_normalized
            ):

                continue

            results.append(
                record
            )

        return results

    # =========================================================
    # FIND CLASS FREE SLOTS
    # =========================================================

    def find_class_free_slots(
        self,
        class_name: Optional[str] = None,
        day: Optional[str] = None,
        slot: Optional[Any] = None
    ) -> List[Dict[str, Any]]:

        class_normalized = (
            self.normalize_text(
                class_name
            )
            if class_name
            else None
        )

        day_normalized = (
            self.normalize_day(
                day
            )
            if day
            else None
        )

        slot_normalized = (
            self.normalize_slot(
                slot
            )
            if slot is not None
            else None
        )

        results = []

        for record in (
            self.class_free_slots
        ):

            record_class = (
                self.normalize_text(
                    record.get(
                        "class_name"
                    )
                )
            )

            record_day = (
                self.normalize_day(
                    record.get(
                        "day"
                    )
                )
            )

            record_slot = (
                self.normalize_slot(
                    record.get(
                        "slot"
                    )
                )
            )

            if (
                class_normalized
                and
                record_class
                != class_normalized
            ):

                continue

            if (
                day_normalized
                and
                record_day
                != day_normalized
            ):

                continue

            if (
                slot_normalized
                is not None
                and
                record_slot
                != slot_normalized
            ):

                continue

            results.append(
                record
            )

        return results

    # =========================================================
    # FIND ROOM FREE SLOTS
    # =========================================================

    def find_room_free_slots(
        self,
        room: Optional[str] = None,
        day: Optional[str] = None,
        slot: Optional[Any] = None
    ) -> List[Dict[str, Any]]:

        room_normalized = (
            self.normalize_text(
                room
            )
            if room
            else None
        )

        day_normalized = (
            self.normalize_day(
                day
            )
            if day
            else None
        )

        slot_normalized = (
            self.normalize_slot(
                slot
            )
            if slot is not None
            else None
        )

        results = []

        for record in (
            self.room_free_slots
        ):

            record_room = (
                self.normalize_text(
                    record.get(
                        "room"
                    )
                )
            )

            record_day = (
                self.normalize_day(
                    record.get(
                        "day"
                    )
                )
            )

            record_slot = (
                self.normalize_slot(
                    record.get(
                        "slot"
                    )
                )
            )

            if (
                room_normalized
                and
                record_room
                != room_normalized
            ):

                continue

            if (
                day_normalized
                and
                record_day
                != day_normalized
            ):

                continue

            if (
                slot_normalized
                is not None
                and
                record_slot
                != slot_normalized
            ):

                continue

            results.append(
                record
            )

        return results

    # =========================================================
    # FIND TEACHER SCHEDULE
    # =========================================================

    def find_teacher_schedule(
        self,
        teacher: str,
        day: Optional[str] = None,
        slot: Optional[Any] = None
    ) -> List[Dict[str, Any]]:

        teacher_normalized = (
            self.normalize_text(
                teacher
            )
        )

        results = []

        for event in self.events:

            event_teacher = (
                self.normalize_text(
                    event.get(
                        "teacher"
                    )
                )
            )

            if (
                event_teacher
                != teacher_normalized
            ):

                continue

            if day is not None:

                if (
                    self.normalize_day(
                        event.get(
                            "day"
                        )
                    )
                    !=
                    self.normalize_day(
                        day
                    )
                ):

                    continue

            if slot is not None:

                if (
                    self.normalize_slot(
                        event.get(
                            "slot"
                        )
                    )
                    !=
                    self.normalize_slot(
                        slot
                    )
                ):

                    continue

            results.append(
                event
            )

        return results

    # =========================================================
    # FIND CLASS SCHEDULE
    # =========================================================

    def find_class_schedule(
        self,
        class_name: str,
        day: Optional[str] = None,
        slot: Optional[Any] = None
    ) -> List[Dict[str, Any]]:

        class_normalized = (
            self.normalize_text(
                class_name
            )
        )

        results = []

        for event in self.events:

            event_class = (
                self.normalize_text(
                    event.get(
                        "class_name"
                    )
                )
            )

            if (
                event_class
                != class_normalized
            ):

                continue

            if day is not None:

                if (
                    self.normalize_day(
                        event.get(
                            "day"
                        )
                    )
                    !=
                    self.normalize_day(
                        day
                    )
                ):

                    continue

            if slot is not None:

                if (
                    self.normalize_slot(
                        event.get(
                            "slot"
                        )
                    )
                    !=
                    self.normalize_slot(
                        slot
                    )
                ):

                    continue

            results.append(
                event
            )

        return results

    # =========================================================
    # FIND ROOM SCHEDULE
    # =========================================================

    def find_room_schedule(
        self,
        room: str,
        day: Optional[str] = None,
        slot: Optional[Any] = None
    ) -> List[Dict[str, Any]]:

        room_normalized = (
            self.normalize_text(
                room
            )
        )

        results = []

        for event in self.events:

            event_room = (
                self.normalize_text(
                    event.get(
                        "room"
                    )
                )
            )

            if (
                event_room
                != room_normalized
            ):

                continue

            if day is not None:

                if (
                    self.normalize_day(
                        event.get(
                            "day"
                        )
                    )
                    !=
                    self.normalize_day(
                        day
                    )
                ):

                    continue

            if slot is not None:

                if (
                    self.normalize_slot(
                        event.get(
                            "slot"
                        )
                    )
                    !=
                    self.normalize_slot(
                        slot
                    )
                ):

                    continue

            results.append(
                event
            )

        return results

    # =========================================================
    # FACULTY STATUS
    # =========================================================

    def faculty_status(
        self,
        teacher: str,
        day: str,
        slot: Any
    ) -> str:

        teacher_normalized = (
            self.normalize_text(
                teacher
            )
        )

        day_normalized = (
            self.normalize_day(
                day
            )
        )

        slot_normalized = (
            self.normalize_slot(
                slot
            )
        )

        # -----------------------------------------------------
        # First check scheduled events.
        # -----------------------------------------------------

        for event in self.events:

            event_teacher = (
                self.normalize_text(
                    event.get(
                        "teacher"
                    )
                )
            )

            event_day = (
                self.normalize_day(
                    event.get(
                        "day"
                    )
                )

            )

            event_slot = (
                self.normalize_slot(
                    event.get(
                        "slot"
                    )
                )
            )

            if (

                event_teacher
                == teacher_normalized

                and

                event_day
                == day_normalized

                and

                event_slot
                == slot_normalized

            ):

                return "BUSY"

        # -----------------------------------------------------
        # Check explicit Facultywise free slots.
        # -----------------------------------------------------

        for record in (
            self.faculty_free_slots
        ):

            record_teacher = (
                self.normalize_text(
                    record.get(
                        "teacher"
                    )
                )
            )

            record_day = (
                self.normalize_day(
                    record.get(
                        "day"
                    )
                )

            )

            record_slot = (
                self.normalize_slot(
                    record.get(
                        "slot"
                    )
                )
            )

            if (

                record_teacher
                == teacher_normalized

                and

                record_day
                == day_normalized

                and

                record_slot
                == slot_normalized

            ):

                return "FREE"

        return "UNKNOWN"


# =============================================================
# STANDALONE TEST
# =============================================================

if __name__ == "__main__":

    print()
    print(
        "=" * 60
    )

    print(
        "UNISCHED AI - CANONICAL EVENT MATCHER"
    )

    print(
        "=" * 60
    )

    print()

    print(
        "CanonicalEventMatcher loaded successfully."
    )

    print()

    print(
        "This module expects imported universal records."
    )

    print()

    print(
        "Supported record categories:"
    )

    print(
        "  ✓ Scheduled events"
    )

    print(
        "  ✓ Faculty free slots"
    )

    print(
        "  ✓ Class free slots"
    )

    print(
        "  ✓ Room free slots"
    )

    print(
        "  ✓ Contract records"
    )

    print(
        "  ✓ Unmatched records"
    )

    print()

    print(
        "=" * 60
    )