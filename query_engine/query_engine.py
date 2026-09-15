"""
UNISCHED AI - QUERY ENGINE

Purpose:
    Provides a query layer on top of CanonicalEventMatcher.

The Query Engine does NOT:
    - read PDF files
    - read Excel files
    - read CSV files
    - perform data fusion
    - perform canonical event matching

Those responsibilities are handled by the existing project modules.

This module only queries the canonical data.
"""

from __future__ import annotations

import re

from typing import Any, Dict, List, Optional


class QueryEngine:
    """
    Query layer for UNISCHED AI.
    """

    def __init__(self, matcher: Any):

        if matcher is None:
            raise ValueError(
                "QueryEngine requires a CanonicalEventMatcher."
            )

        self.matcher = matcher

    # =========================================================
    # GENERIC HELPERS
    # =========================================================

    @staticmethod
    def _clean(value: Any) -> str:

        if value is None:
            return ""

        return " ".join(
            str(value)
            .replace("\xa0", " ")
            .strip()
            .split()
        )

    @staticmethod
    def _normalize(value: Any) -> str:

        return QueryEngine._clean(value).lower()

    @staticmethod
    def _get(
        record: Dict[str, Any],
        *keys: str
    ) -> Any:

        if not isinstance(record, dict):
            return ""

        for key in keys:

            if key in record:

                value = record[key]

                if value is not None:
                    return value

        return ""

    @classmethod
    def _day(cls, value: Any) -> str:

        if not value:
            return ""

        text = cls._normalize(value)

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

        return mapping.get(text, text)

    @classmethod
    def _slot(cls, value: Any) -> Optional[Any]:

        if value is None:
            return None

        text = cls._clean(value)

        if not text:
            return None

        try:

            number = float(text)

            if number.is_integer():
                return int(number)

            return number

        except (ValueError, TypeError):

            return text.lower()

    @classmethod
    def _same_day(
        cls,
        a: Any,
        b: Any
    ) -> bool:

        return cls._day(a) == cls._day(b)

    @classmethod
    def _same_slot(
        cls,
        a: Any,
        b: Any
    ) -> bool:

        return cls._slot(a) == cls._slot(b)

    @classmethod
    def _contains(
        cls,
        value: Any,
        query: Any
    ) -> bool:

        value_text = cls._normalize(value)
        query_text = cls._normalize(query)

        if not value_text or not query_text:
            return False

        return query_text in value_text

    # =========================================================
    # MATCHER ACCESS
    # =========================================================

    def _events(self) -> List[Dict[str, Any]]:
        """Get canonical scheduled events."""

        try:

            return list(
                self.matcher.get_events()
            )

        except Exception:

            return list(
                getattr(
                    self.matcher,
                    "events",
                    []
                )
            )

    def _faculty_free(self) -> List[Dict[str, Any]]:
        """Get faculty free slots."""

        try:

            return list(
                self.matcher.get_faculty_free_slots()
            )

        except Exception:

            return list(
                getattr(
                    self.matcher,
                    "faculty_free_slots",
                    []
                )
            )

    def _class_free(self) -> List[Dict[str, Any]]:
        """Get class free slots."""

        try:

            return list(
                self.matcher.get_class_free_slots()
            )

        except Exception:

            return list(
                getattr(
                    self.matcher,
                    "class_free_slots",
                    []
                )
            )

    def _room_free(self) -> List[Dict[str, Any]]:
        """Get room free slots."""

        try:

            return list(
                self.matcher.get_room_free_slots()
            )

        except Exception:

            return list(
                getattr(
                    self.matcher,
                    "room_free_slots",
                    []
                )
            )

    def _contracts(self) -> List[Dict[str, Any]]:
        """Get contract records."""

        try:

            return list(
                self.matcher.get_contract_records()
            )

        except Exception:

            return list(
                getattr(
                    self.matcher,
                    "contract_records",
                    []
                )
            )

    # =========================================================
    # FACULTY FREE SLOTS
    # =========================================================

    def faculty_free_slots(
        self,
        teacher: Optional[str] = None,
        day: Optional[str] = None,
        slot: Optional[Any] = None
    ) -> Dict[str, Any]:

        results = []

        for record in self._faculty_free():

            record_teacher = self._get(
                record,
                "teacher",
                "faculty"
            )

            record_day = self._get(
                record,
                "day"
            )

            record_slot = self._get(
                record,
                "slot"
            )

            if teacher:

                if not self._contains(
                    record_teacher,
                    teacher
                ):
                    continue

            if day:

                if not self._same_day(
                    record_day,
                    day
                ):
                    continue

            if slot is not None:

                if not self._same_slot(
                    record_slot,
                    slot
                ):
                    continue

            results.append(record)

        return {
            "query_type": "faculty_free",
            "teacher": teacher,
            "day": self._day(day) if day else None,
            "slot": self._slot(slot),
            "count": len(results),
            "results": results,
        }

    # =========================================================
    # FACULTY STATUS FOR SINGLE SLOT
    # =========================================================

    def faculty_status(
        self,
        teacher: str,
        day: str,
        slot: Any
    ) -> Dict[str, Any]:
        """
        Determine whether a faculty member is busy or free.
        """

        schedule = self.teacher_schedule(
            teacher=teacher,
            day=day,
            slot=slot
        )

        if schedule["count"] > 0:

            return {
                "query_type": "faculty_status",
                "teacher": teacher,
                "day": self._day(day),
                "slot": self._slot(slot),
                "status": "busy",
                "is_free": False,
                "events": schedule["results"],
            }

        free = self.faculty_free_slots(
            teacher=teacher,
            day=day,
            slot=slot
        )

        if free["count"] > 0:

            return {
                "query_type": "faculty_status",
                "teacher": teacher,
                "day": self._day(day),
                "slot": self._slot(slot),
                "status": "free",
                "is_free": True,
                "free_slots": free["results"],
            }

        return {
            "query_type": "faculty_status",
            "teacher": teacher,
            "day": self._day(day),
            "slot": self._slot(slot),
            "status": "unknown",
            "is_free": None,
            "events": [],
            "free_slots": [],
            "message": (
                "No matching scheduled event or explicit "
                "free-slot record was found."
            ),
        }

    # =========================================================
    # TEACHER SCHEDULE
    # =========================================================

    def teacher_schedule(
        self,
        teacher: str,
        day: Optional[str] = None,
        slot: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Return scheduled/busy classes for a teacher.

        IMPORTANT:
        We separately track whether ANY timetable record exists,
        including explicit free-slot records.

        This allows the response layer to distinguish:

            1. No timetable data exists
            2. Timetable data exists but teacher has no classes
        """

        results = []

        has_any_records = False

        for record in self._faculty_records():

            record_teacher = self._get(
                record,
                "teacher",
                "faculty"
            )

            if not self._contains(
                record_teacher,
                teacher
            ):
                continue

            record_day = self._get(
                record,
                "day"
            )

            if day and not self._same_day(
                record_day,
                day
            ):
                continue

            record_slot = self._get(
                record,
                "slot"
            )

            if (
                slot is not None
                and not self._same_slot(
                    record_slot,
                    slot
                )
            ):
                continue

            # -------------------------------------------------
            # IMPORTANT:
            # Record exists, whether busy OR free.
            # -------------------------------------------------

            has_any_records = True

            # -------------------------------------------------
            # Only busy/scheduled records go into results.
            # -------------------------------------------------

            if self._cell_is_busy(record):

                results.append(record)

        return {
            "query_type": "teacher_schedule",
            "teacher": teacher,
            "day": self._day(day) if day else None,
            "slot": self._slot(slot),
            "count": len(results),
            "results": results,
            "has_any_records": has_any_records,
        }

    # =========================================================
    # CLASS SCHEDULE
    # =========================================================

    def class_schedule(
        self,
        class_name: str,
        day: Optional[str] = None,
        slot: Optional[Any] = None
    ) -> Dict[str, Any]:

        results = []

        # Resolve ONCE, exact-match-first: if class_name is
        # itself a real, standalone class (e.g. "3CS-D"), match
        # ONLY that class - never also pull in classes that
        # merely happen to start with the same text (e.g.
        # "3CS-DS", "3CS-DS-A"). Only when there's no such exact
        # class does this fall back to the broader substring
        # behaviour, e.g. for an intentionally partial query
        # like "7CS".
        resolution = self.resolve_class_reference(
            class_name
        )

        matching_classes = set(
            resolution.get("matching_classes", [])
        )

        for event in self._events():

            record_class = self._get(
                event,
                "class_name",
                "class"
            )

            if record_class not in matching_classes:
                continue

            record_day = self._get(
                event,
                "day"
            )

            record_slot = self._get(
                event,
                "slot"
            )

            if day:

                if not self._same_day(
                    record_day,
                    day
                ):
                    continue

            if slot is not None:

                if not self._same_slot(
                    record_slot,
                    slot
                ):
                    continue

            results.append(event)

        return {
            "query_type": "class_schedule",
            "class_name": class_name,
            "day": self._day(day) if day else None,
            "slot": self._slot(slot),
            "count": len(results),
            "results": results,
        }

    # =========================================================
    # SEMESTER SCHEDULE
    #
    # A "semester" is not a field stored anywhere in the
    # timetable data. It is derived dynamically from the
    # LEADING digits of each event's class_name (e.g.
    # "7CS-DS" -> semester 7, "3CSA" -> semester 3, "5CS-D"
    # -> semester 5). No semester-to-class mapping, class
    # name, or faculty name is hard-coded here - the mapping
    # is computed on the fly from whatever class names are
    # actually present in the loaded timetable data, so it
    # stays correct even if the timetable data changes.
    # =========================================================

    @staticmethod
    def _semester_from_class_name(
        class_name: Any
    ) -> Optional[int]:

        if not class_name:
            return None

        match = re.match(
            r"\s*(\d+)",
            str(class_name)
        )

        if not match:
            return None

        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None

    def semester_schedule(
        self,
        semester: Any,
        day: Optional[str] = None,
        slot: Optional[Any] = None
    ) -> Dict[str, Any]:

        try:
            target_semester = int(semester)
        except (TypeError, ValueError):

            return {
                "query_type": "semester_schedule",
                "semester": semester,
                "day": self._day(day) if day else None,
                "slot": self._slot(slot),
                "count": 0,
                "results": [],
            }

        results = []

        for event in self._events():

            record_class = self._get(
                event,
                "class_name",
                "class"
            )

            record_semester = self._semester_from_class_name(
                record_class
            )

            if record_semester != target_semester:
                continue

            record_day = self._get(
                event,
                "day"
            )

            record_slot = self._get(
                event,
                "slot"
            )

            if day:

                if not self._same_day(
                    record_day,
                    day
                ):
                    continue

            if slot is not None:

                if not self._same_slot(
                    record_slot,
                    slot
                ):
                    continue

            results.append(event)

        return {
            "query_type": "semester_schedule",
            "semester": target_semester,
            "day": self._day(day) if day else None,
            "slot": self._slot(slot),
            "count": len(results),
            "results": results,
        }

    # =========================================================
    # CLASS FREE SLOTS
    # =========================================================

    def class_free_slots(
        self,
        class_name: Optional[str] = None,
        day: Optional[str] = None,
        slot: Optional[Any] = None
    ) -> Dict[str, Any]:

        results = []

        matching_classes = None

        if class_name:

            matching_classes = set(
                self.resolve_class_reference(
                    class_name
                ).get("matching_classes", [])
            )

        for record in self._class_free():

            record_class = self._get(
                record,
                "class_name",
                "class"
            )

            record_day = self._get(
                record,
                "day"
            )

            record_slot = self._get(
                record,
                "slot"
            )

            if matching_classes is not None:

                if record_class not in matching_classes:
                    continue

            if day:

                if not self._same_day(
                    record_day,
                    day
                ):
                    continue

            if slot is not None:

                if not self._same_slot(
                    record_slot,
                    slot
                ):
                    continue

            results.append(record)

        return {
            "query_type": "class_free",
            "class_name": class_name,
            "day": self._day(day) if day else None,
            "slot": self._slot(slot),
            "count": len(results),
            "results": results,
        }

    # =========================================================
    # ROOM SCHEDULE
    # =========================================================

    def room_schedule(
        self,
        room: str,
        day: Optional[str] = None,
        slot: Optional[Any] = None
    ) -> Dict[str, Any]:

        results = []

        # Same exact-match-first principle as class_schedule():
        # a real, standalone room like "CL-1" must never also
        # pull in "CL-10", "CL-15", etc. just because they share
        # that text as a prefix.
        matching_rooms = set(
            self.resolve_room_reference(
                room
            ).get("matching_rooms", [])
        )

        for event in self._events():

            record_room = self._get(
                event,
                "room",
                "classroom"
            )

            if record_room not in matching_rooms:
                continue

            record_day = self._get(
                event,
                "day"
            )

            record_slot = self._get(
                event,
                "slot"
            )

            if day:

                if not self._same_day(
                    record_day,
                    day
                ):
                    continue

            if slot is not None:

                if not self._same_slot(
                    record_slot,
                    slot
                ):
                    continue

            results.append(event)

        return {
            "query_type": "room_schedule",
            "room": room,
            "day": self._day(day) if day else None,
            "slot": self._slot(slot),
            "count": len(results),
            "results": results,
        }

    # =========================================================
    # ROOM FREE SLOTS
    # =========================================================

    def room_free_slots(
        self,
        room: Optional[str] = None,
        day: Optional[str] = None,
        slot: Optional[Any] = None
    ) -> Dict[str, Any]:

        results = []

        matching_rooms = None

        if room:

            matching_rooms = set(
                self.resolve_room_reference(
                    room
                ).get("matching_rooms", [])
            )

        for record in self._room_free():

            record_room = self._get(
                record,
                "room",
                "classroom"
            )

            record_day = self._get(
                record,
                "day"
            )

            record_slot = self._get(
                record,
                "slot"
            )

            if matching_rooms is not None:

                if record_room not in matching_rooms:
                    continue

            if day:

                if not self._same_day(
                    record_day,
                    day
                ):
                    continue

            if slot is not None:

                if not self._same_slot(
                    record_slot,
                    slot
                ):
                    continue

            results.append(record)

        return {
            "query_type": "room_free",
            "room": room,
            "day": self._day(day) if day else None,
            "slot": self._slot(slot),
            "count": len(results),
            "results": results,
        }

    # =========================================================
    # SUBJECT SEARCH
    # =========================================================

    def subject_search(
        self,
        subject: str
    ) -> Dict[str, Any]:

        results = []

        for event in self._events():

            record_subject = self._get(
                event,
                "subject"
            )

            if self._contains(
                record_subject,
                subject
            ):

                results.append(event)

        for record in self._contracts():

            record_subject = self._get(
                record,
                "subject"
            )

            if self._contains(
                record_subject,
                subject
            ):

                results.append(record)

        return {
            "query_type": "subject_search",
            "subject": subject,
            "count": len(results),
            "results": results,
        }

    # =========================================================
    # TEACHER SEARCH
    # =========================================================

    def teacher_search(
        self,
        teacher: str
    ) -> Dict[str, Any]:

        results = []

        for event in self._events():

            record_teacher = self._get(
                event,
                "teacher",
                "faculty"
            )

            if self._contains(
                record_teacher,
                teacher
            ):

                results.append(event)

        for record in self._contracts():

            record_teacher = self._get(
                record,
                "teacher",
                "faculty"
            )

            if self._contains(
                record_teacher,
                teacher
            ):

                results.append(record)

        return {
            "query_type": "teacher_search",
            "teacher": teacher,
            "count": len(results),
            "results": results,
        }

    # =========================================================
    # GENERAL SEARCH
    # =========================================================

    def search(
        self,
        text: str
    ) -> Dict[str, Any]:

        query = self._normalize(text)

        results = []

        for event in self._events():

            fields = [

                self._get(
                    event,
                    "teacher"
                ),

                self._get(
                    event,
                    "subject"
                ),

                self._get(
                    event,
                    "room"
                ),

                self._get(
                    event,
                    "class_name",
                    "class"
                ),

                self._get(
                    event,
                    "day"
                ),

                self._get(
                    event,
                    "slot"
                ),
            ]

            combined = " ".join(
                self._normalize(field)
                for field in fields
            )

            if query in combined:

                results.append(event)

        return {
            "query_type": "search",
            "query": text,
            "count": len(results),
            "results": results,
        }

    # =========================================================
    # DATASET SUMMARY
    # =========================================================

    def summary(self) -> Dict[str, Any]:

        events = self._events()

        faculty_free = self._faculty_free()

        class_free = self._class_free()

        room_free = self._room_free()

        contracts = self._contracts()

        return {
            "canonical_events": len(events),
            "faculty_free_slots": len(faculty_free),
            "class_free_slots": len(class_free),
            "room_free_slots": len(room_free),
            "contract_records": len(contracts),
        }

    # =========================================================
    # ENTITY KNOWLEDGE
    #
    # Returns the distinct faculty/subject/room/class/group
    # names actually present in the CURRENTLY LOADED canonical
    # timetable data - the same shape previously produced by
    # database/knowledge_loader.py's KnowledgeLoader.load()
    # (teachers/subjects/rooms/classes/groups), but always
    # computed fresh from self._events() instead of a separate,
    # independently-built snapshot.
    #
    # This is the single source of truth EntityExtractor uses
    # for fuzzy-matching candidate lists, so entity recognition
    # can never drift out of sync with what QueryEngine can
    # actually answer questions about: if the timetable source
    # files are replaced with different data, the very next
    # FacultyAIChatbot() construction sees the new names
    # automatically, with no separate database rebuild step.
    #
    # No faculty name, class name, subject, or room is
    # hard-coded here - every value comes from whatever records
    # are currently loaded.
    # =========================================================

    def entity_knowledge(self) -> Dict[str, List[str]]:

        events = self._events()

        teachers = set()
        subjects = set()
        rooms = set()
        classes = set()
        groups = set()

        for event in events:

            teacher = self._get(event, "teacher")
            if teacher:
                teachers.add(str(teacher))

            subject = self._get(event, "subject")
            if subject:
                subjects.add(str(subject))

            room = self._get(event, "room")
            if room:
                rooms.add(str(room))

            class_name = self._get(
                event,
                "class_name",
                "class"
            )
            if class_name:
                classes.add(str(class_name))

            group_name = self._get(
                event,
                "group_name",
                "group"
            )
            if group_name:
                groups.add(str(group_name))

        return {
            "teachers": sorted(teachers),
            "subjects": sorted(subjects),
            "rooms": sorted(rooms),
            "classes": sorted(classes),
            "groups": sorted(groups),
        }

    # =========================================================
    # CLASS REFERENCE RESOLUTION
    #
    # ONE generic resolver, reused by every class-scoped query
    # (class-teacher search, class timetable, and any future
    # class-scoped feature) so "7CS" is interpreted the same way
    # everywhere, instead of each caller deciding independently
    # whether to treat it as an exact class or a broad prefix.
    #
    # The distinction is derived entirely from the canonical
    # classes actually present in the currently loaded timetable
    # (via entity_knowledge()) - never from a fixed list:
    #
    #   EXACT: the raw text the user typed is itself (after the
    #          same normalization _contains() already uses) one
    #          of the real canonical class names.
    #
    #   BROAD: the raw text is not itself a canonical class, but
    #          is contained in one or more canonical class names
    #          (the same substring rule class_schedule() already
    #          uses internally) - e.g. "7cs" is a prefix shared
    #          by "7CSA", "7CS-DS", "7CS-IOT", etc.
    #
    #   NONE:  the raw text matches no canonical class at all.
    #
    # No class name, semester, or department code is hard-coded
    # here - if tomorrow's timetable has entirely different
    # classes, this resolves against whatever entity_knowledge()
    # reports for that data.
    # =========================================================

    def _resolve_entity_reference(
        self,
        raw_text: Any,
        known_values: List[str],
        value_key: str,
        matches_key: str
    ) -> Dict[str, Any]:

        """
        Generic exact-match-first, broad-substring-fallback
        resolver shared by class and room reference resolution
        (see resolve_class_reference / resolve_room_reference).

        EXACT: the raw text, once normalized, IS one of the
               known values outright (e.g. "3CS-D" is itself a
               real class name in this dataset).

        BROAD: the raw text isn't itself a known value, but is
               a substring of one or more of them (e.g. "7cs" is
               a prefix shared by "7CSA", "7CS-DS", "7CS-IOT").
               Only reached when NO exact match exists, so a
               real, standalone value like "3CS-D" is never
               silently absorbed into "3CS-DS"/"3CS-DS-A" just
               because it happens to be a text-prefix of them.

        NONE:  the raw text matches nothing at all.
        """

        query_text = self._normalize(raw_text)

        if not query_text:
            return {
                "query_text": raw_text,
                "mode": "none",
                value_key: None,
                matches_key: [],
            }

        exact_matches = [
            value
            for value in known_values
            if self._normalize(value) == query_text
        ]

        if exact_matches:
            return {
                "query_text": raw_text,
                "mode": "exact",
                value_key: exact_matches[0],
                matches_key: exact_matches,
            }

        broad_matches = sorted(
            value
            for value in known_values
            if self._contains(value, raw_text)
        )

        if broad_matches:
            return {
                "query_text": raw_text,
                "mode": "broad",
                value_key: None,
                matches_key: broad_matches,
            }

        return {
            "query_text": raw_text,
            "mode": "none",
            value_key: None,
            matches_key: [],
        }

    def resolve_class_reference(
        self,
        raw_text: Any
    ) -> Dict[str, Any]:

        return self._resolve_entity_reference(
            raw_text,
            self.entity_knowledge()["classes"],
            "class_name",
            "matching_classes"
        )

    def resolve_room_reference(
        self,
        raw_text: Any
    ) -> Dict[str, Any]:

        return self._resolve_entity_reference(
            raw_text,
            self.entity_knowledge()["rooms"],
            "room",
            "matching_rooms"
        )


# =============================================================
# IMPORTANT HELPER METHODS
# =============================================================

def _faculty_records(self) -> List[Dict[str, Any]]:
    """
    Return all faculty timetable records.

    This combines:
        - scheduled events
        - explicit faculty free-slot records

    The faculty free-slot records are required for questions such as:

        Is Mr. Nitin Goyal free on Monday slot 2?

    and:

        What is Mr. Nitin Goyal's schedule on Monday?
    """

    records = []

    # Scheduled/busy events
    for event in self._events():

        if isinstance(event, dict):

            teacher = self._get(
                event,
                "teacher",
                "faculty"
            )

            if teacher:
                records.append(event)

    # Explicit free-slot records
    for record in self._faculty_free():

        if isinstance(record, dict):

            teacher = self._get(
                record,
                "teacher",
                "faculty"
            )

            if teacher:
                records.append(record)

    return records


def _cell_is_busy(self, record: Dict[str, Any]) -> bool:
    """
    Determine whether a timetable record represents a busy class.

    Explicit faculty-free records are never busy.
    """

    if not isinstance(record, dict):
        return False

    record_type = self._normalize(
        record.get("record_type", "")
    )

    # Explicit free-slot records of ANY kind (faculty, class, or
    # room) are never busy. Previously this only recognized
    # "faculty_free_slot" - harmless while this helper's only
    # caller was faculty_free_for_period() (which only ever sees
    # faculty-related records), but wrong for a room- or class-
    # free record, where the record's own "room"/"class_name"
    # field is populated with the WHICH-ONE-IS-FREE identity
    # rather than indicating an occupying class - without this,
    # such a record would incorrectly fall through to "busy".
    if any(
        free_type in record_type
        for free_type in (
            "faculty_free_slot",
            "class_free_slot",
            "room_free_slot",
        )
    ):
        return False

    # Explicit free indicators
    for key in (
        "is_free",
        "free",
        "available"
    ):

        if key in record:

            value = record.get(key)

            if isinstance(value, bool):

                if value:
                    return False

    # Check common fields
    subject = self._clean(
        record.get("subject", "")
    )

    room = self._clean(
        record.get("room", "")
    )

    class_name = self._clean(
        record.get("class_name", "")
    )

    group_name = self._clean(
        record.get("group_name", "")
    )

    lecture_type = self._clean(
        record.get("type", "")
    )

    # If the record is explicitly marked free
    combined = " ".join(
        [
            subject,
            room,
            class_name,
            group_name,
            lecture_type,
            record_type,
        ]
    ).lower()

    free_words = (
        "free",
        "available",
        "faculty_free_slot",
    )

    if any(word in combined for word in free_words):

        if "faculty_free_slot" in record_type:
            return False

        if subject == "" and room == "" and class_name == "":
            return False

    # A normal canonical event is considered busy.
    return True


def _time_to_minutes(
    self,
    value: Any
) -> Optional[int]:
    """
    Convert HH:MM time to minutes.
    """

    if value is None:
        return None

    text = self._clean(value)

    if not text:
        return None

    try:

        parts = text.split(":")

        if len(parts) != 2:
            return None

        hour = int(parts[0])

        minute = int(parts[1])

        if not (
            0 <= hour <= 23
            and 0 <= minute <= 59
        ):
            return None

        return hour * 60 + minute

    except (
        ValueError,
        TypeError
    ):

        return None


def _slot_overlaps_period(
    self,
    slot_time: Any,
    start: int,
    end: int
) -> bool:
    """
    Determine whether a timetable slot overlaps
    the requested time range.
    """

    if not slot_time:
        return False

    text = self._clean(slot_time)

    # Expected:
    # 09:15 - 10:15
    parts = text.split("-")

    if len(parts) != 2:
        return False

    slot_start = self._time_to_minutes(
        parts[0].strip()
    )

    slot_end = self._time_to_minutes(
        parts[1].strip()
    )

    if (
        slot_start is None
        or slot_end is None
    ):
        return False

    return (
        slot_start < end
        and slot_end > start
    )


def faculty_status_for_period(
    self,
    teacher: str,
    day: str,
    start_time: str,
    end_time: str
) -> Dict[str, Any]:
    """
    Return the status of one faculty member
    for a complete time range.

    Example:

        Mr. Nitin Goyal
        Monday
        09:15
        11:15

    returns slots 2 and 3 if both overlap
    the requested period.
    """

    start = self._time_to_minutes(
        start_time
    )

    end = self._time_to_minutes(
        end_time
    )

    if (
        start is None
        or end is None
        or end <= start
    ):

        return {
            "query_type": "faculty_status_period",
            "teacher": teacher,
            "day": self._day(day),
            "start_time": start_time,
            "end_time": end_time,
            "status": "unknown",
            "is_free": None,
            "slots": [],
            "events": [],
            "message": "Invalid time range."
        }

    day_key = self._day(day)

    matching = []

    for record in self._faculty_records():

        if self._normalize(
            self._get(
                record,
                "teacher",
                "faculty"
            )
        ) != self._normalize(
            teacher
        ):

            continue

        if self._day(
            record.get("day")
        ) != day_key:

            continue

        if not self._slot_overlaps_period(
            self._clean(
                record.get("slot_time")
            ),
            start,
            end
        ):

            continue

        matching.append(record)

    matching.sort(
        key=lambda r: (
            self._slot(
                r.get("slot")
            ) or 999
        )
    )

    if not matching:

        return {
            "query_type": "faculty_status_period",
            "teacher": teacher,
            "day": day_key,
            "start_time": start_time,
            "end_time": end_time,
            "status": "unknown",
            "is_free": None,
            "slots": [],
            "events": [],
            "message": (
                "No faculty timetable records overlap "
                "the requested period."
            )
        }

    busy = [
        r
        for r in matching
        if self._cell_is_busy(r)
    ]

    return {
        "query_type": "faculty_status_period",
        "teacher": teacher,
        "day": day_key,
        "start_time": start_time,
        "end_time": end_time,

        "status": (
            "busy"
            if busy
            else "free"
        ),

        "is_free": not busy,

        "slots": [
            self._slot(
                r.get("slot")
            )
            for r in matching
            if self._slot(
                r.get("slot")
            ) is not None
        ],

        "events": busy,

        "records": matching,
    }


def faculty_free_for_period(
    self,
    day: str,
    start_time: str,
    end_time: str
) -> Dict[str, Any]:
    """
    Return faculty members who are completely free during
    the requested period.

    IMPORTANT:
    If both FACULTY_FREE_SLOT and SCHEDULED_EVENT exist
    for the same faculty/day/slot, SCHEDULED_EVENT wins.
    """

    start = self._time_to_minutes(start_time)
    end = self._time_to_minutes(end_time)

    day_key = self._day(day)

    if (
        start is None
        or end is None
        or end <= start
    ):
        return {
            "query_type": "faculty_free_period",
            "day": day_key,
            "start_time": start_time,
            "end_time": end_time,
            "slots": [],
            "count": 0,
            "results": []
        }

    all_records = self._faculty_records()

    # ---------------------------------------------------------
    # Get all faculty names
    # ---------------------------------------------------------

    teachers = sorted(
        {
            self._clean(
                self._get(
                    record,
                    "teacher",
                    "faculty"
                )
            )
            for record in all_records
            if self._clean(
                self._get(
                    record,
                    "teacher",
                    "faculty"
                )
            )
        },
        key=lambda x: x.casefold()
    )

    # ---------------------------------------------------------
    # Records overlapping requested period
    # ---------------------------------------------------------

    requested_records = [
        record
        for record in all_records
        if self._day(
            record.get("day")
        ) == day_key
        and self._slot_overlaps_period(
            self._clean(
                record.get("slot_time")
            ),
            start,
            end
        )
    ]

    # ---------------------------------------------------------
    # Requested slots
    # ---------------------------------------------------------

    requested_slots = sorted(
        {
            self._slot(
                record.get("slot")
            )
            for record in requested_records
            if self._slot(
                record.get("slot")
            ) is not None
        }
    )

    free_faculty = []

    # ---------------------------------------------------------
    # Check every faculty member
    # ---------------------------------------------------------

    for teacher in teachers:

        teacher_records = [
            record
            for record in requested_records
            if self._normalize(
                self._get(
                    record,
                    "teacher",
                    "faculty"
                )
            ) == self._normalize(teacher)
        ]

        if not teacher_records:
            continue

        # -----------------------------------------------------
        # Group records by slot
        #
        # This is the important correction.
        # -----------------------------------------------------

        records_by_slot = {}

        for record in teacher_records:

            slot = self._slot(
                record.get("slot")
            )

            if slot is None:
                continue

            records_by_slot.setdefault(
                slot,
                []
            ).append(record)

        is_free = True

        # -----------------------------------------------------
        # Check every requested slot
        # -----------------------------------------------------

        for slot in requested_slots:

            slot_records = records_by_slot.get(
                slot,
                []
            )

            # No record for this slot
            #
            # We cannot automatically call this FREE because
            # there may be incomplete timetable information.
            if not slot_records:
                is_free = False
                break

            # -------------------------------------------------
            # BUSY HAS PRIORITY
            #
            # If ANY scheduled/busy event exists for this
            # faculty + day + slot, faculty is BUSY.
            # -------------------------------------------------

            slot_is_busy = any(
                self._cell_is_busy(record)
                for record in slot_records
            )

            if slot_is_busy:
                is_free = False
                break

        # -----------------------------------------------------
        # Faculty is completely free
        # -----------------------------------------------------

        if is_free:

            free_faculty.append(
                {
                    "teacher": teacher,
                    "day": day_key,
                    "start_time": start_time,
                    "end_time": end_time,
                    "slots": requested_slots,
                }
            )

    free_faculty.sort(
        key=lambda x: x["teacher"].casefold()
    )

    return {
        "query_type": "faculty_free_period",
        "day": day_key,
        "start_time": start_time,
        "end_time": end_time,
        "slots": requested_slots,
        "count": len(free_faculty),
        "results": free_faculty,
    }


def _room_records(self) -> List[Dict[str, Any]]:
    """
    Return all room timetable records.

    This combines:
        - scheduled events that occupy a room
        - explicit room free-slot records

    Mirrors _faculty_records() above, but for rooms - needed
    for questions such as:

        Which rooms are free from 10:15 to 12:15 on Wednesday?
    """

    records = []

    # Scheduled/busy events
    for event in self._events():

        if isinstance(event, dict):

            room = self._get(
                event,
                "room",
                "classroom"
            )

            if room:
                records.append(event)

    # Explicit free-slot records
    for record in self._room_free():

        if isinstance(record, dict):

            room = self._get(
                record,
                "room",
                "classroom"
            )

            if room:
                records.append(record)

    return records


def room_free_for_period(
    self,
    day: str,
    start_time: str,
    end_time: str
) -> Dict[str, Any]:
    """
    Return rooms that are completely free during the requested
    period.

    Mirrors faculty_free_for_period() above, but groups by
    ROOM instead of by teacher, and reuses the same
    _slot_overlaps_period()/_cell_is_busy() primitives so a
    room's free/busy status is judged with exactly the same
    logic as a faculty member's.

    IMPORTANT:
    If both ROOM_FREE_SLOT and a SCHEDULED_EVENT exist for the
    same room/day/slot, the SCHEDULED_EVENT wins (room is busy).
    """

    start = self._time_to_minutes(start_time)
    end = self._time_to_minutes(end_time)

    day_key = self._day(day)

    if (
        start is None
        or end is None
        or end <= start
    ):
        return {
            "query_type": "room_free_period",
            "day": day_key,
            "start_time": start_time,
            "end_time": end_time,
            "slots": [],
            "count": 0,
            "results": []
        }

    all_records = self._room_records()

    # ---------------------------------------------------------
    # Get all room names
    # ---------------------------------------------------------

    rooms = sorted(
        {
            self._clean(
                self._get(
                    record,
                    "room",
                    "classroom"
                )
            )
            for record in all_records
            if self._clean(
                self._get(
                    record,
                    "room",
                    "classroom"
                )
            )
        },
        key=lambda x: x.casefold()
    )

    # ---------------------------------------------------------
    # Records overlapping requested period
    # ---------------------------------------------------------

    requested_records = [
        record
        for record in all_records
        if self._day(
            record.get("day")
        ) == day_key
        and self._slot_overlaps_period(
            self._clean(
                record.get("slot_time")
            ),
            start,
            end
        )
    ]

    # ---------------------------------------------------------
    # Requested slots
    # ---------------------------------------------------------

    requested_slots = sorted(
        {
            self._slot(
                record.get("slot")
            )
            for record in requested_records
            if self._slot(
                record.get("slot")
            ) is not None
        }
    )

    free_rooms = []

    # ---------------------------------------------------------
    # Check every room
    # ---------------------------------------------------------

    for room in rooms:

        room_records = [
            record
            for record in requested_records
            if self._normalize(
                self._get(
                    record,
                    "room",
                    "classroom"
                )
            ) == self._normalize(room)
        ]

        if not room_records:
            continue

        records_by_slot = {}

        for record in room_records:

            slot = self._slot(
                record.get("slot")
            )

            if slot is None:
                continue

            records_by_slot.setdefault(
                slot,
                []
            ).append(record)

        is_free = True

        # -----------------------------------------------------
        # Check every requested slot
        # -----------------------------------------------------

        for slot in requested_slots:

            slot_records = records_by_slot.get(
                slot,
                []
            )

            # No record for this slot - cannot automatically
            # call this FREE because there may be incomplete
            # timetable information.
            if not slot_records:
                is_free = False
                break

            slot_is_busy = any(
                self._cell_is_busy(record)
                for record in slot_records
            )

            if slot_is_busy:
                is_free = False
                break

        # -----------------------------------------------------
        # Room is completely free
        # -----------------------------------------------------

        if is_free:

            free_rooms.append(
                {
                    "room": room,
                    "day": day_key,
                    "start_time": start_time,
                    "end_time": end_time,
                    "slots": requested_slots,
                }
            )

    free_rooms.sort(
        key=lambda x: x["room"].casefold()
    )

    return {
        "query_type": "room_free_period",
        "day": day_key,
        "start_time": start_time,
        "end_time": end_time,
        "slots": requested_slots,
        "count": len(free_rooms),
        "results": free_rooms,
    }


# =============================================================
# ATTACH METHODS TO QUERY ENGINE
#
# These assignments guarantee that the methods above become
# actual QueryEngine methods.
# =============================================================

QueryEngine._faculty_records = _faculty_records
QueryEngine._room_records = _room_records
QueryEngine._cell_is_busy = _cell_is_busy
QueryEngine._time_to_minutes = _time_to_minutes
QueryEngine._slot_overlaps_period = _slot_overlaps_period
QueryEngine.faculty_status_for_period = faculty_status_for_period
QueryEngine.faculty_free_for_period = faculty_free_for_period
QueryEngine.room_free_for_period = room_free_for_period


__all__ = [
    "QueryEngine"
]