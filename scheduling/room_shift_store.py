from __future__ import annotations

import json
from pathlib import Path

from utils.file_discovery import state_file
from typing import Any


class RoomShiftStore:
    """
    Small persistent store for CONFIRMED lab/venue room shifts.

    Modeled directly on scheduling/assignment_store.py's existing,
    already-proven pattern (JSON file -> list of dicts, atomic
    write via a temp-file + replace), since room shifts are a
    different KIND of change from a replacement assignment (the
    teacher/class/subject/day/slots stay the same - only the room
    changes) and reusing AssignmentStore's record shape directly
    would be semantically wrong. This is the smallest new overlay
    needed to track confirmed room changes without ever touching
    the original canonical timetable events, which remain
    immutable and fully traceable (each record keeps the original
    "source_room" alongside the new "target_room").

    This store never modifies canonical events. It only persists
    CONFIRMED shift records; planning (LabShiftCoordinator.plan_shift)
    never writes here.
    """

    def __init__(
        self,
        path: str | Path | None = None
    ):
        # no path given -> <FACULTY_STATE_DIR or data>/room_shifts.json
        self.path = Path(path) if path else state_file("room_shifts.json")

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
        shifts: list[dict[str, Any]],
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
                shifts,
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
        Return all persisted (confirmed) room-shift records.
        """
        return self._read()

    def add(
        self,
        shift: dict[str, Any],
    ) -> dict[str, Any]:

        if not isinstance(shift, dict):
            raise TypeError(
                "shift must be a dictionary"
            )

        shifts = self._read()

        shifts.append(
            dict(shift)
        )

        self._write(shifts)

        return dict(shift)

    def clear(self) -> None:
        """
        Remove all room-shift records.
        """
        self._write([])

    # ============================================================
    # SEARCH
    # ============================================================

    def find(
        self,
        shift_id: str,
    ) -> dict[str, Any] | None:

        target = str(shift_id)

        for shift in self._read():

            if str(
                shift.get(
                    "shift_id",
                    "",
                )
            ) == target:

                return dict(shift)

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

    # ============================================================
    # BLOCK-IDENTITY LOOKUP
    #
    # Given the identity of an ORIGINAL canonical block (teacher,
    # class_name, subject, group_name, type, day), find the most
    # recent CONFIRMED shift that moved that exact block, if any.
    # Used to compute "effective" room occupancy: an original
    # event whose block has been confirmed-shifted is no longer
    # considered to occupy its original room - it now occupies
    # the shift's target_room instead.
    # ============================================================

    def find_confirmed_for_block(
        self,
        teacher: str,
        class_name: str,
        subject: str,
        group_name: str,
        event_type: str,
        day: str,
    ) -> dict[str, Any] | None:

        def _key(value: Any) -> str:
            return str(value or "").strip().lower()

        target_key = (
            _key(teacher),
            _key(class_name),
            _key(subject),
            _key(group_name),
            _key(event_type),
            _key(day),
        )

        match = None

        for shift in self._read():

            shift_key = (
                _key(shift.get("teacher")),
                _key(shift.get("class_name")),
                _key(shift.get("subject")),
                _key(shift.get("group_name")),
                _key(shift.get("type")),
                _key(shift.get("day")),
            )

            if shift_key != target_key:
                continue

            # Last matching confirmed record wins (most recent
            # shift for this exact block).
            match = shift

        return dict(match) if match else None