from __future__ import annotations

import json
from pathlib import Path

from utils.file_discovery import state_file
from typing import Any


class ExamDutyStore:
    """
    Small persistent store for CONFIRMED exam-duty records.

    Modeled directly on the existing, already-proven pattern used
    by scheduling/assignment_store.py and
    scheduling/room_shift_store.py (JSON file -> list of dicts,
    atomic write via a temp-file + replace). Exam duty is a
    different KIND of record from a replacement assignment or a
    room shift - it has no "absent_teacher"/"replacement_teacher"
    pair and no "slots" concept, and it is keyed by a REAL
    CALENDAR DATE plus clock time rather than a recurring
    weekday/slot - so reusing either existing store's record
    shape directly would be semantically wrong. This is the
    smallest new persistence overlay needed to track confirmed
    exam-duty invigilation without touching any other store or
    the canonical timetable events, which remain untouched.

    This store never modifies canonical events, AssignmentStore,
    or RoomShiftStore. It only persists CONFIRMED exam-duty
    records; planning (ExamDutyCoordinator.plan_duty) never
    writes here - only ExamDutyCoordinator.confirm_duty does.

    A fresh project starts with no records at all (no seed/sample
    data is ever written by this module).
    """

    def __init__(
        self,
        path: str | Path | None = None
    ):
        # no path given -> <FACULTY_STATE_DIR or data>/exam_duties.json
        self.path = Path(path) if path else state_file("exam_duties.json")

    # ============================================================
    # INTERNAL HELPERS
    # ============================================================

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []

        try:
            with self.path.open(
                "r",
                encoding="utf-8",
            ) as file:
                data = json.load(file)

            if not isinstance(data, list):
                return []

            return [
                item
                for item in data
                if isinstance(item, dict)
            ]

        except (
            OSError,
            json.JSONDecodeError,
        ):
            return []

    def _write(
        self,
        duties: list[dict[str, Any]],
    ) -> None:

        self._ensure_parent()

        temporary_path = self.path.with_suffix(
            self.path.suffix + ".tmp"
        )

        with temporary_path.open(
            "w",
            encoding="utf-8",
        ) as file:

            json.dump(
                duties,
                file,
                indent=2,
                ensure_ascii=False,
            )

        temporary_path.replace(self.path)

    @staticmethod
    def _key(value: Any) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def _minutes(value: Any) -> int | None:

        text = str(value or "").strip()

        if not text:
            return None

        parts = text.split(":")

        if len(parts) != 2:
            return None

        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except ValueError:
            return None

        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None

        return hour * 60 + minute

    @classmethod
    def _ranges_overlap(
        cls,
        start_a: Any,
        end_a: Any,
        start_b: Any,
        end_b: Any,
    ) -> bool:

        a_start = cls._minutes(start_a)
        a_end = cls._minutes(end_a)
        b_start = cls._minutes(start_b)
        b_end = cls._minutes(end_b)

        if None in (a_start, a_end, b_start, b_end):
            return False

        return a_start < b_end and b_start < a_end

    # ============================================================
    # SESSION IDENTITY
    #
    # An exam SESSION is identified either by an explicit
    # session_id supplied by the caller, or - when none was
    # supplied - by the (exam_date, start_time, end_time, hall)
    # tuple. This is used to detect duplicate assignment of the
    # SAME faculty member to the SAME exam session, as distinct
    # from two merely time-overlapping (but different) sessions.
    # ============================================================

    @classmethod
    def session_key(
        cls,
        exam_date: Any,
        start_time: Any,
        end_time: Any,
        hall: Any = None,
        session_id: Any = None,
    ) -> str:

        if session_id:
            return f"session:{cls._key(session_id)}"

        return (
            f"{cls._key(exam_date)}|"
            f"{cls._key(start_time)}|"
            f"{cls._key(end_time)}|"
            f"{cls._key(hall)}"
        )

    # ============================================================
    # PUBLIC API
    # ============================================================

    def all(self) -> list[dict[str, Any]]:
        """
        Return all persisted (confirmed) exam-duty records.
        """
        return self._read()

    def add(
        self,
        duty: dict[str, Any],
    ) -> dict[str, Any]:

        if not isinstance(duty, dict):
            raise TypeError(
                "duty must be a dictionary"
            )

        duties = self._read()

        duties.append(
            dict(duty)
        )

        self._write(duties)

        return dict(duty)

    def clear(self) -> None:
        """
        Remove all exam-duty records.
        """
        self._write([])

    # ============================================================
    # SEARCH
    # ============================================================

    def find(
        self,
        duty_id: str,
    ) -> dict[str, Any] | None:

        target = str(duty_id)

        for duty in self._read():

            if str(
                duty.get("duty_id", "")
            ) == target:

                return dict(duty)

        return None

    def for_date(
        self,
        exam_date: str,
    ) -> list[dict[str, Any]]:

        date_key = self._key(exam_date)

        return [
            dict(item)
            for item in self._read()
            if self._key(item.get("exam_date")) == date_key
        ]

    def for_teacher(
        self,
        teacher: str,
    ) -> list[dict[str, Any]]:

        teacher_key = self._key(teacher)

        return [
            dict(item)
            for item in self._read()
            if self._key(item.get("teacher")) == teacher_key
        ]

    def for_session(
        self,
        session_key: str,
    ) -> list[dict[str, Any]]:

        return [
            dict(item)
            for item in self._read()
            if item.get("session_key") == session_key
        ]

    # ============================================================
    # CONFLICT / DUPLICATE SUPPORT
    # ============================================================

    def teacher_has_overlap(
        self,
        teacher: str,
        exam_date: str,
        start_time: str,
        end_time: str,
        exclude_duty_id: str | None = None,
    ) -> bool:
        """
        True if `teacher` already has a CONFIRMED exam-duty
        record on `exam_date` whose time range overlaps
        [start_time, end_time). A faculty member can be free from
        the regular timetable but still unavailable because of an
        already-confirmed exam duty - this is what makes that
        distinction.
        """

        teacher_key = self._key(teacher)
        date_key = self._key(exam_date)

        for duty in self._read():

            if exclude_duty_id and str(
                duty.get("duty_id", "")
            ) == str(exclude_duty_id):
                continue

            if self._key(duty.get("teacher")) != teacher_key:
                continue

            if self._key(duty.get("exam_date")) != date_key:
                continue

            if self._ranges_overlap(
                duty.get("start_time"),
                duty.get("end_time"),
                start_time,
                end_time,
            ):
                return True

        return False

    def teacher_already_in_session(
        self,
        teacher: str,
        session_key: str,
    ) -> bool:
        """
        True if `teacher` is already confirmed for the exact same
        exam session (same session_key) - prevents assigning the
        same faculty member twice to one session.
        """

        teacher_key = self._key(teacher)

        for duty in self._read():

            if duty.get("session_key") != session_key:
                continue

            if self._key(duty.get("teacher")) == teacher_key:
                return True

        return False

    # ============================================================
    # FAIRNESS / COUNTS
    # ============================================================

    def duty_count(
        self,
        teacher: str,
    ) -> int:

        teacher_key = self._key(teacher)

        return sum(
            1
            for duty in self._read()
            if self._key(duty.get("teacher")) == teacher_key
        )

    def duty_counts(self) -> dict[str, int]:
        """
        Return {teacher_display_name: confirmed_duty_count} for
        every teacher who has at least one confirmed exam-duty
        record. The display name used is whatever casing first
        appeared in the store; counting itself is
        case-insensitive.
        """

        counts: dict[str, int] = {}
        display_names: dict[str, str] = {}

        for duty in self._read():

            teacher = str(duty.get("teacher", "")).strip()

            if not teacher:
                continue

            key = self._key(teacher)

            display_names.setdefault(key, teacher)
            counts[key] = counts.get(key, 0) + 1

        return {
            display_names[key]: count
            for key, count in counts.items()
        }