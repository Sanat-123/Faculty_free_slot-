"""
semester_exam.py - "5th sem has exams on 2 November 9 to 10:30": what that means for the timetable
=================================================================================================

While a semester sits its exam, that semester's normal classes DO NOT RUN.  So in
the exam window

  * the teachers who would have been teaching those classes are free,
  * the rooms those classes would have used are free (they become exam halls),
  * and "faculty from that semester" can mean the teachers of those classes.

Nothing about semesters, class names or rooms is hard-coded:

  * which class belongs to which semester is read from the class NAMES in the
    uploaded timetable by the patterns in config/duty_lexicon.json
    ("class_semester_patterns": "5CSA" -> 5, "3CS-AI-B" -> 3, "Sem-5 CSE" -> 5 ...);
  * the way a coordinator says it ("5th sem", "fifth semester", "sem V", "semester 5",
    or a class name like "5CSA") is read by the patterns in the same file;
  * the days, slots and rooms all come from the timetable model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from scheduling import duty_history


# ----------------------------------------------------------------------
# reading numbers the way people write them
# ----------------------------------------------------------------------

_ROMAN = {"i": 1, "v": 5, "x": 10}


def roman_to_int(token: str) -> Optional[int]:

    t = str(token).strip().lower()

    if not t or any(ch not in _ROMAN for ch in t):
        return None

    total = 0

    for i, ch in enumerate(t):
        v = _ROMAN[ch]
        total += -v if i + 1 < len(t) and _ROMAN[t[i + 1]] > v else v

    return total if 0 < total < 40 else None


def to_number(token: str, ordinals: Dict[str, int]) -> Optional[int]:
    """'5' -> 5, 'fifth' -> 5, 'V' -> 5."""

    t = str(token).strip().lower()

    if t.isdigit():
        return int(t)

    if t in ordinals:
        return ordinals[t]

    return roman_to_int(t)


def class_semester(name: str) -> Optional[int]:
    """Semester number encoded in a class name, or None ("MBA-I", "M.Tech-I")."""

    for pattern in duty_history.lexicon().get("class_semester_patterns", []):
        m = re.search(pattern, str(name or ""), re.I)
        if m:
            try:
                return int(m.group(1))
            except (TypeError, ValueError):
                continue

    return None


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


# ----------------------------------------------------------------------
# what the coordinator said
# ----------------------------------------------------------------------

@dataclass
class ScopeSpec:
    """The semester / classes named in a request."""

    semester: Optional[int] = None
    class_names: List[str] = field(default_factory=list)   # explicit classes ("5CSA")
    only_teachers: bool = False                              # "teachers of 5th sem only"
    mention: str = ""                                        # the words used


def find_scope(text: str, model_classes: Iterable[str]) -> Optional[ScopeSpec]:
    """Read '5th sem', 'semester V', 'fifth semester', or class names like 5CSA / 3 CS-AI-B."""

    lex = duty_history.lexicon()
    ordinals = {str(k).lower(): int(v) for k, v in lex.get("ordinal_words", {}).items()}
    classes = sorted({str(c) for c in model_classes if str(c).strip()})

    low = str(text or "").lower()

    spec = ScopeSpec()

    # ---- "5th sem" ----
    for pattern in lex.get("semester_patterns", []):

        m = re.search(pattern, low, re.I)

        if not m:
            continue

        number = to_number(m.group(1), ordinals)

        if number:
            spec.semester, spec.mention = number, m.group(0)
            break

    # ---- explicit class names ("5CSA", "3 CS-AI-B") ----
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]*", text or "")
    by_key = {_compact(c): c for c in classes if len(_compact(c)) >= 3}

    for size in (3, 2, 1):
        for i in range(len(tokens) - size + 1):

            key = _compact("".join(tokens[i:i + size]))

            if key in by_key and by_key[key] not in spec.class_names:
                spec.class_names.append(by_key[key])

    if spec.semester is None and not spec.class_names:
        return None

    # ---- "teachers of / belonging to / teaching that semester" ----
    # the semester may be named more than once ("5th sem has exams ... teachers
    # belonging to the 5th sem"), so look around EVERY mention
    if spec.mention:
        sem_re = re.escape(spec.mention)
        places = [m.start() for m in re.finditer(sem_re, low)]
    else:
        sem_re = r"\b(?:" + "|".join(re.escape(c.lower()) for c in spec.class_names) + r")\b"
        places = [m.start() for m in re.finditer(sem_re, low)]

    around = " ... ".join(
        low[max(0, i - 90): i + len(spec.mention or "") + 90] for i in places
    ) or low

    for phrase in lex.get("semester_only_phrases", []):

        pattern = phrase.replace("{SEM}", sem_re)

        if re.search(pattern, low if "{SEM}" in phrase else around, re.I):
            spec.only_teachers = True
            break

    return spec


# ----------------------------------------------------------------------
# what it means in the timetable
# ----------------------------------------------------------------------

@dataclass
class ExamScope:
    """A resolved scope: which classes are in exam, and who may invigilate."""

    label: str
    semester: Optional[int]
    classes: List[str]
    teachers_of_scope: Set[str]            # everyone who teaches any of these classes
    only_teachers: bool = False


def resolve(spec: ScopeSpec, model) -> Optional[ExamScope]:
    """Turn a ScopeSpec into the actual class list of the loaded timetable."""

    all_classes = sorted({str(e.get("class_name", "")).strip() for e in model.events} - {""})

    chosen: List[str] = list(spec.class_names)

    if spec.semester is not None:
        chosen += [c for c in all_classes if class_semester(c) == spec.semester and c not in chosen]

    if not chosen:
        return None

    wanted = set(chosen)

    teachers = {
        str(e.get("teacher", "")).strip()
        for e in model.events
        if str(e.get("class_name", "")).strip() in wanted and str(e.get("teacher", "")).strip()
    }

    if spec.semester is not None and not spec.class_names:
        label = f"semester {spec.semester}"
    else:
        label = ", ".join(spec.class_names[:3]) + ("…" if len(spec.class_names) > 3 else "")

    return ExamScope(
        label=label,
        semester=spec.semester,
        classes=chosen,
        teachers_of_scope=teachers,
        only_teachers=spec.only_teachers,
    )


def _overlapping(model, day: str, start_min: int, end_min: int, classes: Optional[Set[str]] = None):
    """Timetable events on `day` that overlap [start_min, end_min)."""

    out = []

    for e in model.events:

        if str(e.get("day", "")).lower() != day:
            continue

        info = model.slot_info.get(e.get("slot"))

        if not info or info.get("start") is None:
            continue

        if min(end_min, info["end"]) - max(start_min, info["start"]) <= 0:
            continue

        if classes is not None and str(e.get("class_name", "")).strip() not in classes:
            continue

        out.append(e)

    return out


def suspended_events(model, scope: ExamScope, day: str, start_min: int, end_min: int):
    """Classes of the exam scope that would have run in the exam window, but won't."""

    return _overlapping(model, day, start_min, end_min, set(scope.classes))


def freed_teachers(
    model, scope: ExamScope, day: str,
    start_min: int, end_min: int, from_min: int, to_min: int,
) -> Dict[str, int]:
    """
    {teacher: remaining periods that day} for teachers who are free for
    [from_min, to_min] ONLY because the scope's classes are suspended in the exam
    window [start_min, end_min]: each has at least one suspended class, and no
    other class overlapping the buffered window.
    """

    suspended = suspended_events(model, scope, day, start_min, end_min)
    suspended_ids = {id(e) for e in suspended}

    candidates = {str(e.get("teacher", "")).strip() for e in suspended} - {""}

    busy_buffer = _overlapping(model, day, from_min, to_min)

    still_busy = {
        str(e.get("teacher", "")).strip()
        for e in busy_buffer if id(e) not in suspended_ids
    }

    day_events = [e for e in model.events if str(e.get("day", "")).lower() == day]

    freed = {}

    for t in candidates - still_busy:

        slots = {e.get("slot") for e in day_events
                 if str(e.get("teacher", "")).strip() == t and id(e) not in suspended_ids}

        freed[t] = len(slots)

    return freed


def exam_rooms(model, scope: ExamScope, day: str, start_min: int, end_min: int) -> List[str]:
    """
    Rooms the scope's suspended classes would have used (theory rooms first, then
    labs/seminar rooms) - they are empty during the exam and make natural exam halls.
    """

    theory, other = [], []

    for e in suspended_events(model, scope, day, start_min, end_min):

        room = str(e.get("room", "")).strip()

        if not room:
            continue

        bucket = theory if str(e.get("type", "")).lower() == "theory" else other

        if room not in bucket:
            bucket.append(room)

    def natural(v):
        return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", v)]

    return sorted(theory, key=natural) + [r for r in sorted(other, key=natural) if r not in theory]


def room_classes(model, scope: ExamScope, day: str, start_min: int, end_min: int) -> Dict[str, List[str]]:
    """{room: [classes of the scope that would have used it in the window]}."""

    out: Dict[str, List[str]] = {}

    for e in suspended_events(model, scope, day, start_min, end_min):

        room = str(e.get("room", "")).strip()
        cls = str(e.get("class_name", "")).strip()

        if room and cls and cls not in out.setdefault(room, []):
            out[room].append(cls)

    return out


# ----------------------------------------------------------------------
# rooms that nobody uses in the window (extra exam halls)
# ----------------------------------------------------------------------

def _natural(value: str):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", value)]


def classroom_kinds(model) -> Dict[str, str]:
    """
    {room: "classroom" | "lab"} - a room that hosts at least one THEORY class
    anywhere in the timetable is a classroom, a room that only ever hosts labs
    is a lab.  Read from the timetable, nothing is listed by hand.
    """

    kinds: Dict[str, str] = {}

    for e in model.events:

        room = str(e.get("room", "")).strip()

        if not room:
            continue

        theory = str(e.get("type", "")).lower() == "theory"

        if theory:
            kinds[room] = "classroom"
        else:
            kinds.setdefault(room, "lab")

    return kinds


def vacant_rooms(
    model,
    day: str,
    start_min: int,
    end_min: int,
    skip: Optional[Iterable[str]] = None,
    ignore_classes: Optional[Iterable[str]] = None,
) -> List[Tuple[str, str]]:
    """
    [(room, kind)] of rooms with NO class in [start_min, end_min] on `day`,
    classrooms first, then labs; each group in natural order.

    skip           - rooms to leave out (already chosen / booked for another exam)
    ignore_classes - classes that do not run (a semester sitting its exam)
    """

    ignore = set(ignore_classes or ())
    skipped = {str(r) for r in (skip or ())}

    kinds = classroom_kinds(model)

    busy = {
        str(e.get("room", "")).strip()
        for e in _overlapping(model, day, start_min, end_min)
        if str(e.get("class_name", "")).strip() not in ignore
    }

    free = [(r, k) for r, k in kinds.items() if r not in busy and r not in skipped]

    return sorted(free, key=lambda rk: (rk[1] != "classroom", _natural(rk[0])))


def rooms_for_exam(
    model,
    scope: Optional[ExamScope],
    day: str,
    start_min: int,
    end_min: int,
    needed: Optional[int],
    booked: Optional[Iterable[str]] = None,
) -> Tuple[List[str], Dict[str, str], int]:
    """
    The exam halls for one window.

    1. a semester exam: the rooms its suspended classes would have used;
    2. then other rooms nobody uses at that time (classrooms before labs), until
       `needed` rooms are found - `needed=None` keeps just the first group
       (or, with no semester, every vacant classroom);
    3. rooms already booked for another exam are never offered.

    Returns (rooms, {room: note shown in the table}, how many are still missing).
    """

    booked_set = {str(b) for b in (booked or ())}
    notes: Dict[str, str] = {}
    rooms: List[str] = []

    if scope is not None:

        by_class = room_classes(model, scope, day, start_min, end_min)

        for room in exam_rooms(model, scope, day, start_min, end_min):

            if room in booked_set:
                continue

            rooms.append(room)
            notes[room] = ", ".join(by_class.get(room, [])) or "—"

    if needed is not None and len(rooms) >= needed:
        rooms = rooms[:needed]
        return rooms, {r: notes[r] for r in rooms}, 0

    if needed is None and scope is not None:
        return rooms, notes, 0

    extra = vacant_rooms(
        model, day, start_min, end_min,
        skip=set(rooms) | booked_set,
        ignore_classes=set(scope.classes) if scope else None,
    )

    for room, kind in extra:

        if needed is not None and len(rooms) >= needed:
            break

        rooms.append(room)
        notes[room] = "free " + kind + " (no class then)"

    missing = max(0, (needed or 0) - len(rooms))

    return rooms, notes, missing