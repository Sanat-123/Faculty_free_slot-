from __future__ import annotations

import json
from pathlib import Path

from utils.file_discovery import state_file
from typing import Any


class AssignmentStore:
    """
    Small persistent store for faculty replacement assignments.

    The store intentionally keeps the data format simple:
        JSON file -> list of assignment dictionaries

    This makes the assignment state:
        - easy to inspect
        - easy to back up
        - independent of the timetable parser
        - usable by CLI, chatbot, Streamlit, or future API code
    """

    def __init__(self, path: str | Path | None = None):
        # no path given -> <FACULTY_STATE_DIR or data>/assignments.json
        self.path = Path(path) if path else state_file("assignments.json")

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
        assignments: list[dict[str, Any]],
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
                assignments,
                file,
                indent=2,
                ensure_ascii=False,
            )

        temporary_path.replace(self.path)

    # ============================================================
    # PUBLIC API
    # ============================================================

    def all(self) -> list[dict[str, Any]]:
        """
        Return all persisted assignments.
        """
        return self._read()

    def add(
        self,
        assignment: dict[str, Any],
    ) -> dict[str, Any]:

        if not isinstance(assignment, dict):
            raise TypeError(
                "assignment must be a dictionary"
            )

        assignments = self._read()

        assignments.append(
            dict(assignment)
        )

        self._write(assignments)

        return dict(assignment)

    def remove(
        self,
        assignment_id: str,
    ) -> bool:

        assignments = self._read()

        remaining = [
            item
            for item in assignments
            if str(
                item.get("assignment_id", "")
            ) != str(assignment_id)
        ]

        removed = (
            len(remaining)
            != len(assignments)
        )

        if removed:
            self._write(remaining)

        return removed

    def clear(self) -> None:
        """
        Remove all assignments.
        """
        self._write([])

    # ============================================================
    # SEARCH
    # ============================================================

    def find(
        self,
        assignment_id: str,
    ) -> dict[str, Any] | None:

        target = str(assignment_id)

        for assignment in self._read():

            if str(
                assignment.get(
                    "assignment_id",
                    "",
                )
            ) == target:

                return dict(assignment)

        return None

    def for_day(
        self,
        day: str,
    ) -> list[dict[str, Any]]:

        day_key = str(
            day or ""
        ).strip().lower()

        return [
            dict(item)
            for item in self._read()
            if str(
                item.get("day", "")
            ).strip().lower()
            == day_key
        ]

    def for_teacher(
        self,
        teacher: str,
    ) -> list[dict[str, Any]]:

        teacher_key = str(
            teacher or ""
        ).strip().lower()

        return [
            dict(item)
            for item in self._read()
            if str(
                item.get(
                    "replacement_teacher",
                    "",
                )
            ).strip().lower()
            == teacher_key
        ]

    # ============================================================
    # CONFLICT SUPPORT
    # ============================================================

    def teacher_has_assignment(
        self,
        teacher: str,
        day: str,
        slot: Any,
    ) -> bool:

        teacher_key = str(
            teacher or ""
        ).strip().lower()

        day_key = str(
            day or ""
        ).strip().lower()

        slot_key = str(slot)

        for assignment in self._read():

            if (
                str(
                    assignment.get(
                        "replacement_teacher",
                        "",
                    )
                ).strip().lower()
                != teacher_key
            ):
                continue

            if (
                str(
                    assignment.get(
                        "day",
                        "",
                    )
                ).strip().lower()
                != day_key
            ):
                continue

            assigned_slots = assignment.get(
                "slots",
                [],
            )

            if not isinstance(
                assigned_slots,
                list,
            ):
                assigned_slots = [
                    assigned_slots
                ]

            if any(
                str(existing_slot)
                == slot_key
                for existing_slot
                in assigned_slots
            ):
                return True

        return False

    def teacher_free_for_slots(
        self,
        teacher: str,
        day: str,
        slots: list[Any],
    ) -> bool:

        for slot in slots:

            if self.teacher_has_assignment(
                teacher,
                day,
                slot,
            ):
                return False

        return True