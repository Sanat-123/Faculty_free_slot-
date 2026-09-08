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