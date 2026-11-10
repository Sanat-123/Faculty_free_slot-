"""Plain data structures shared by the layers (no database, no chatbot)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass(frozen=True)
class FacultyInfo:
    id: int
    name: str
    department: str
    history: float = 0.0        # historical duties (baseline + completed)
    committed: float = 0.0      # approved duties of OTHER sessions not yet completed
    tier: int = 1               # allocation priority tier (1 = first choice)


@dataclass(frozen=True)
class SlotInfo:
    index: int                  # position in Problem.slots
    exam_date: date
    shift_id: int
    shift_label: str
    start: str                  # "HH:MM"
    end: str
    day_pos: int                # position of the date in the exam-date list
    rooms: Tuple[str, ...]      # room labels needed in this slot
    per_room: int

    @property
    def required(self) -> int:
        return len(self.rooms) * self.per_room


@dataclass
class Problem:
    """Everything the optimiser needs - nothing about databases or chat."""

    faculty: List[FacultyInfo]
    slots: List[SlotInfo]
    eligible: Dict[int, Set[int]]            # faculty id -> slot indexes allowed
    min_duties: Optional[int] = None
    max_duties: Optional[int] = None
    prohibit_consecutive: bool = False
    max_per_day: Optional[int] = None
    fixed: Set[Tuple[int, int]] = field(default_factory=set)       # (fid, slot) must stay
    forbidden: Set[Tuple[int, int]] = field(default_factory=set)   # (fid, slot) never
    keep_preferred: Set[Tuple[int, int]] = field(default_factory=set)  # soft keep
    previous: Set[Tuple[int, int]] = field(default_factory=set)    # avoid repeating
    seed: int = 0
    # optional division of every slot between groups (e.g. CS / non-CS)
    groups: Dict[int, str] = field(default_factory=dict)              # faculty id -> group
    group_quota: Dict[int, Dict[str, int]] = field(default_factory=dict)  # slot -> group -> seats
    # overlapping slot pairs (same date, overlapping clock time) - derived
    overlaps: List[Tuple[int, int]] = field(default_factory=list)

    def fixed_slots(self) -> Set[int]:
        return {sl for _, sl in self.fixed}

    @property
    def total_required(self) -> int:
        return sum(s.required for s in self.slots)


@dataclass
class SolveResult:
    status: str                              # complete | partial | infeasible
    assignments: Dict[int, List[int]]        # slot index -> faculty ids
    seats: Dict[int, List[Tuple[str, int, int]]]  # slot -> [(room, seat, fid)]
    unfilled: List[Dict[str, Any]]
    quality: Dict[str, Any]
    seed: int
    solver_status: str = ""
    notes: List[str] = field(default_factory=list)