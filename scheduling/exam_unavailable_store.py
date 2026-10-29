"""
exam_unavailable_store.py - faculty "marked unavailable" for exam duty
=====================================================================

A tiny JSON overlay (<state folder>/exam_unavailable.json), modelled on
exam_duty_store.py.  Each entry is

    {"teacher": "<name as in the timetable>",
     "exam_date": "YYYY-MM-DD" | "",      # "" = unavailable until cleared
     "reason": "on leave" | "...",
     "created": "<iso timestamp>"}

It never touches the timetable events or the confirmed duties.  The
exam-duty planner reads it when it builds the list of eligible faculty, so
a teacher marked unavailable is never proposed for that date.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.file_discovery import state_file


def _norm(name: str) -> str:
    return " ".join(str(name or "").lower().split())


class ExamUnavailableStore:

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else state_file("exam_unavailable.json")

    # ---------------- internals ----------------

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(self.path)

    # ---------------- public ----------------

    def add(self, teacher: str, exam_date: str = "", reason: str = "") -> dict:
        rows = self._read()

        for row in rows:
            if _norm(row.get("teacher")) == _norm(teacher) \
                    and (row.get("exam_date") or "") == (exam_date or ""):
                if reason:
                    row["reason"] = reason
                self._write(rows)
                return row

        row = {
            "teacher": teacher,
            "exam_date": exam_date or "",
            "reason": reason or "marked unavailable",
            "created": datetime.now(timezone.utc).isoformat(),
        }
        rows.append(row)
        self._write(rows)
        return row

    def remove(self, teacher: str, exam_date: str | None = None) -> int:
        """Remove entries for a teacher (every date, or only `exam_date`)."""
        rows = self._read()
        keep = [
            r for r in rows
            if not (
                _norm(r.get("teacher")) == _norm(teacher)
                and (exam_date is None or (r.get("exam_date") or "") == exam_date)
            )
        ]
        if len(keep) != len(rows):
            self._write(keep)
        return len(rows) - len(keep)

    def clear(self) -> int:
        rows = self._read()
        if rows:
            self._write([])
        return len(rows)

    def all(self) -> list[dict[str, Any]]:
        return self._read()

    def for_date(self, exam_date: str) -> dict[str, str]:
        """{normalised teacher: reason} of everyone unavailable that day."""
        out: dict[str, str] = {}
        for row in self._read():
            if (row.get("exam_date") or "") in ("", exam_date):
                out[_norm(row.get("teacher"))] = row.get("reason") or "marked unavailable"
        return out