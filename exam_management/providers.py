"""
Adapters between the exam engine and the rest of the project.

Everything the engine needs from outside is a tiny interface here, so the
engine can serve any college and can be tested without the chatbot:

    FacultyDirectory     -> [(name, department), ...]
    HistoryProvider      -> {name: historical duties}
    TimetableConflicts   -> {faculty names busy teaching in a date/time window}
"""

from __future__ import annotations

from datetime import date
from typing import Callable, Dict, Iterable, List, Optional, Protocol, Set, Tuple


class FacultyDirectory(Protocol):
    def faculty(self) -> List[Tuple[str, str]]: ...


class HistoryProvider(Protocol):
    def counts(self) -> Dict[str, float]: ...


class TimetableConflicts(Protocol):
    def busy(self, on: date, start: str, end: str) -> Set[str]: ...


class StaticDirectory:
    def __init__(self, rows: Iterable[Tuple[str, str]]):
        self._rows = list(rows)

    def faculty(self) -> List[Tuple[str, str]]:
        return list(self._rows)


class NoTimetable:
    """Used when classes are always suspended / no teaching timetable is loaded."""

    def busy(self, on: date, start: str, end: str) -> Set[str]:
        return set()


class StaticTimetable:
    """Weekday -> [(name, start, end)] busy table (used by tests and other colleges)."""

    def __init__(self, busy_by_weekday: Dict[str, List[Tuple[str, str, str]]]):
        self.table = {k.lower(): v for k, v in busy_by_weekday.items()}

    def busy(self, on: date, start: str, end: str) -> Set[str]:
        out = set()
        for name, s, e in self.table.get(on.strftime("%A").lower(), []):
            if s < end and start < e:
                out.add(name)
        return out


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")[:2]
    return int(h) * 60 + int(m)


# ----------------------------------------------------------------------------
# adapters for THIS project (timetable model, branch index, duty sheet)
# ----------------------------------------------------------------------------

class ProjectDirectory:
    """Every faculty member of the loaded timetable + the department learned for them."""

    def __init__(self, model_getter: Callable, branch_getter: Callable,
                 real_faculty_getter: Optional[Callable] = None):
        self.model_getter = model_getter
        self.branch_getter = branch_getter
        self.real_faculty_getter = real_faculty_getter

    def faculty(self) -> List[Tuple[str, str]]:
        model = self.model_getter()
        names = set(getattr(model, "faculty_set", None) or [])
        if self.real_faculty_getter:
            try:
                allowed = self.real_faculty_getter()
                if allowed:
                    names &= set(allowed)
            except Exception:
                pass
        index = None
        try:
            index = self.branch_getter()
        except Exception:
            index = None
        rows = []
        for n in sorted(names):
            dept = ""
            if index is not None:
                try:
                    dept = index.primary.get(n) or ""
                except Exception:
                    dept = ""
            rows.append((n, dept))
        return rows


class ProjectTimetable:
    """Teaching conflicts straight from the project's timetable model (with its buffer)."""

    def __init__(self, model_getter: Callable, buffer_minutes: int = 0):
        self.model_getter = model_getter
        self.buffer = buffer_minutes

    def busy(self, on: date, start: str, end: str) -> Set[str]:
        model = self.model_getter()
        day = on.strftime("%A")
        s, e = _minutes(start) - self.buffer, _minutes(end) + self.buffer
        busy: Set[str] = set()
        for slot in model.slots_overlapping(s, e):
            busy |= set(model.busy_faculty(day, slot))
        return busy


class ProjectHistory:
    """Duties recorded before this chat (uploaded duty sheet) + duties confirmed by the legacy flow."""

    def __init__(self, store_getter: Callable):
        self.store_getter = store_getter

    def counts(self) -> Dict[str, float]:
        store = self.store_getter()
        out: Dict[str, float] = {}
        try:
            for k, v in store.baseline.counts().items():
                out[k] = out.get(k, 0.0) + float(v)
        except Exception:
            pass
        return out