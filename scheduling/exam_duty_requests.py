"""
exam_duty_requests.py - plain-English exam-duty requests, rooms and fairness
============================================================================

Understands requests such as

  "Find all faculty members who are free on 10 November 2026, from 9:00 AM
   to 12:00 PM, for examination duty. Check their teaching timetable and
   existing exam duties. Show their names, departments and reasons."

  "Assign examination duties for 10 November 2026, from 9:00 AM to 12:00 PM.
   We have 5 examination rooms: A101, A102, A103, A104 and A105. Assign 2
   invigilators to each room. Select only available faculty and make sure
   nobody is in two rooms at the same time."

  "Allocate exam duties for the upcoming examinations. Prioritize faculty with
   fewer exam duties, avoid consecutive duties, show everyone's total duties."

and answers them from the uploaded timetable.  Nothing is hard-coded: dates,
times, room names, counts, faculty and departments all come from the question
or from the data.  The eligibility rules are the planner's own
(ExamDutyCoordinator.candidates): free in the regular timetable for the whole
duty window plus the buffer, no overlapping confirmed exam duty, real faculty
only, optional branch filter.

Allocation rules
  * every room gets the requested number of invigilators;
  * nobody is placed in two rooms (or two overlapping sessions) at once;
  * faculty with fewer exam duties are preferred (stored + this plan);
  * duties on the day before/after, or back-to-back on the same day, are
    avoided whenever somebody else is available;
  * if there are not enough eligible faculty, the plan says exactly which
    rooms are short instead of silently double-booking.

The plan is only a PROPOSAL; "confirm" saves it, "cancel" drops it.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from engine import date_resolver

# ======================================================================
# small language helpers
# ======================================================================

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "pair": 2,
    "couple": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20,
}

_NUM = r"(\d{1,3}|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"

_PEOPLE = (
    r"(?:invigilators?|faculty(?:\s+members?)?|faculties|teachers?|staff|"
    r"supervisors?|members?|persons?|people|examiners?|professors?)"
)

_ROOM_WORD = r"(?:rooms?|halls?|venues?|classrooms?|centres?|centers?|labs?)"


def _to_int(token: str) -> Optional[int]:

    token = str(token).strip().lower()

    if token.isdigit():
        return int(token)

    return _NUMBER_WORDS.get(token)


def _clock(minutes: int) -> str:

    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ======================================================================
# times
# ======================================================================

_TIME = r"(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?"

_RANGE_RE = re.compile(
    rf"(?P<lead>\bfrom\b|\bbetween\b|\bduring\b|\bat\b|\btime\b|\bslot\b)?"
    rf"\s*(?<![\w:.]){_TIME}\s*(?P<sep>to|till|until|through|and|-|–|—)\s*{_TIME}"
    rf"(?!\s*(?:invigilat|faculty|teacher|rooms?\b|halls?\b|staff|members?|"
    rf"people|persons?|per\b|each\b))",
)


def _hour24(hour: int, meridiem: Optional[str]) -> Optional[int]:

    if meridiem:

        pm = meridiem.lower().startswith("p")

        if not 1 <= hour <= 12:
            return hour if hour <= 23 else None

        if hour == 12:
            return 12 if pm else 0

        return hour + 12 if pm else hour

    return hour if hour <= 24 else None


def parse_time_range(text: str) -> Optional[Tuple[str, str]]:
    """'from 9:00 AM to 12:00 PM', '9 to 12', '14:00-17:00' -> ('09:00','12:00')."""

    low = text.lower()

    # "12 noon" / "noon" -> 12 pm,  "midnight" -> 12 am
    low = re.sub(r"\b(?:12\s*)?noon\b", "12 pm", low)
    low = re.sub(r"\b(?:12\s*)?midnight\b", "12 am", low)

    for m in _RANGE_RE.finditer(low):

        h1, m1, mer1 = m.group(2), m.group(3), m.group(4)
        h2, m2, mer2 = m.group(6), m.group(7), m.group(8)

        lead = m.group("lead")
        sep = m.group("sep")

        explicit = bool(m1 or m2 or mer1 or mer2)

        # a bare "5 to 7" needs a word like "from"/"between" or a dash,
        # otherwise it is probably "5 rooms to 7 ..." and not a time
        if not explicit and not lead and sep == "and":
            continue

        a, b = int(h1), int(h2)

        if a > 24 or b > 24:
            continue

        if int(m1 or 0) > 59 or int(m2 or 0) > 59:
            continue

        start_h, end_h = _hour24(a, mer1), _hour24(b, mer2)

        if start_h is None or end_h is None:
            continue

        # one side has am/pm, the other doesn't: borrow it, then fix order
        if mer2 and not mer1:

            start_h = _hour24(a, mer2)

            if start_h is not None and start_h * 60 + int(m1 or 0) \
                    >= end_h * 60 + int(m2 or 0):
                start_h = _hour24(a, "am" if mer2.lower().startswith("p") else "pm")

        elif mer1 and not mer2:

            end_h = _hour24(b, mer1)

            if end_h is not None and end_h * 60 + int(m2 or 0) \
                    <= start_h * 60 + int(m1 or 0):
                end_h = _hour24(b, "pm" if mer1.lower().startswith("a") else "am")

        elif not mer1 and not mer2:

            # exams are daytime events: a bare 1-6 means afternoon
            if 1 <= a <= 6 and not m1 and a < 7:
                start_h = a + 12

            if 1 <= b <= 6 and b < 12:
                end_h = b + 12

            if end_h * 60 + int(m2 or 0) <= start_h * 60 + int(m1 or 0):
                end_h += 12

        if start_h is None or end_h is None:
            continue

        start = start_h * 60 + int(m1 or 0)
        end = end_h * 60 + int(m2 or 0)

        if 0 <= start < end <= 24 * 60:
            return _clock(start), _clock(min(end, 24 * 60 - 1))

    return None


# ======================================================================
# rooms and counts
# ======================================================================

_ROOM_TOKEN = re.compile(
    r"^(?:(?:room|hall|lab|venue|class(?:room)?)\s*)?"
    r"[a-z]{0,4}[-\s]?\d{1,4}[a-z]{0,2}$|^(?:room|hall|lab)\s+[a-z0-9]+$",
    re.I,
)

_ROOM_RANGE = re.compile(
    r"^([a-z]{0,4}[-\s]?)(\d{1,4})\s*(?:to|-|–|—|through|till)\s*"
    r"(?:\1)?(\d{1,4})$",
    re.I,
)


_ROOM_RANGE_START = re.compile(
    r"^([a-z]{0,4}[-\s]?)(\d{1,4})\s*(?:to|-|–|—|through|till)\s*"
    r"(?:\1)?(\d{1,4})\b",
    re.I,
)


def parse_rooms(text: str) -> Tuple[List[str], Optional[int]]:
    """Room names listed in the text and/or a stated number of rooms."""

    rooms: List[str] = []

    for m in re.finditer(
        rf"\b{_ROOM_WORD}\b\s*(?:are|is|namely|being|named|called|:|-|–)?\s*"
        rf"([^.;\n?!]*)",
        text,
        re.I,
    ):

        chunk = m.group(1)

        # "... for halls H1, H2" - the list starts after the room word
        first_item = re.split(r"\s*,\s*", chunk.strip(), maxsplit=1)[0]

        starts_with_room = bool(
            _ROOM_TOKEN.match(first_item)
            or _ROOM_RANGE.match(first_item)
            or _ROOM_RANGE_START.match(first_item)
        )

        again = None if starts_with_room else re.match(
            rf"^[^,]*?\b{_ROOM_WORD}\b\s*(?:are|is|:|-|–)?\s*", chunk, re.I
        )

        if again:
            chunk = chunk[again.end():]

        # only the leading run of room-like items counts
        for part in re.split(r"\s*(?:,|&|/|\band\b|\bor\b)\s*", chunk):

            part = part.strip(" :()[]\"'")

            if not part:
                continue

            ranged = _ROOM_RANGE.match(part) or _ROOM_RANGE_START.match(part)

            if ranged and abs(int(ranged.group(3)) - int(ranged.group(2))) < 60:

                prefix = ranged.group(1)
                low, high = int(ranged.group(2)), int(ranged.group(3))

                if low <= high:
                    width = len(ranged.group(2))
                    rooms.extend(
                        f"{prefix}{str(n).zfill(width)}".strip().upper()
                        for n in range(low, high + 1)
                    )
                    if ranged.end() < len(part):
                        break
                    continue

            if _ROOM_TOKEN.match(part) and len(part) <= 20:
                rooms.append(re.sub(r"\s+", " ", part).upper())

            else:

                lead_token = re.match(
                    r"^((?:room|hall|lab)\s*)?([a-z]{0,4}[-]?\d{1,4}[a-z]{0,2})\b",
                    part, re.I,
                )

                # "rooms, 2 invigilators each": that "2" is a head-count,
                # not a room called 2
                rest = part[lead_token.end():] if lead_token else ""

                if lead_token and not re.match(
                    rf"\s*(?:{_PEOPLE}|per\b|each\b|every\b|apiece\b)",
                    rest, re.I,
                ):
                    rooms.append(lead_token.group(0).strip().upper())

                if rooms:
                    break

    # "room A101" / "in hall B" (single, preceded by a preposition)
    if not rooms:

        m = re.search(
            rf"\b(?:in|at|for)\s+(?:the\s+)?{_ROOM_WORD}\s+"
            rf"([a-z]{{0,4}}[-]?\d{{1,4}}[a-z]{{0,2}}|[a-z])\b",
            text,
            re.I,
        )

        if m:
            rooms.append(m.group(1).upper())

    seen, unique = set(), []

    for room in rooms:
        if room not in seen:
            seen.add(room)
            unique.append(room)

    count = None

    m = re.search(
        rf"\b{_NUM}\s+(?:(?:examination|exam|test)\s+)?{_ROOM_WORD}\b", text, re.I
    )

    if m:
        count = _to_int(m.group(1))

    return unique, count


def parse_counts(text: str) -> Tuple[Optional[int], Optional[int]]:
    """(invigilators per room, total invigilators)."""

    low = text.lower()

    per_room = None

    patterns = [
        rf"\b{_NUM}\s+{_PEOPLE}\s+(?:per|for\s+each|for\s+every|in\s+each|"
        rf"in\s+every|to\s+each|to\s+every|at\s+each|at\s+every|each|every)"
        rf"(?:\s+{_ROOM_WORD})?",
        rf"\b{_NUM}\s+(?:per|for\s+each|in\s+each|to\s+each)\s+{_ROOM_WORD}\b",
        rf"\b(?:each|every)\s+{_ROOM_WORD}\s+(?:should\s+|must\s+|will\s+|to\s+)?"
        rf"(?:have|get|gets|needs?|requires?|with|has)\s+{_NUM}\b",
        rf"\b{_ROOM_WORD}\s+(?:each\s+)?(?:with|needs?|requires?|gets?)\s+{_NUM}\s+{_PEOPLE}",
        rf"\b{_NUM}\s+(?:each|apiece)\b",
    ]

    for pattern in patterns:

        m = re.search(pattern, low)

        if m:
            per_room = _to_int(m.group(1))
            break

    total = None

    m = re.search(
        rf"\b(?:assign|allocate|allot|need|require|appoint|get|want|select|"
        rf"find|give|nominate|arrange)\s+(?:me\s+|us\s+)?{_NUM}\s+{_PEOPLE}\b"
        rf"(?!\s+(?:per|for\s+each|in\s+each|to\s+each|each|every))",
        low,
    )

    if m:
        total = _to_int(m.group(1))

    return per_room, total


# ======================================================================
# request understanding
# ======================================================================

_ASSIGN = re.compile(
    r"\b(?:assign|allocate|allot|appoint|designate|deploy|roster|arrange|"
    r"nominate|schedule|depute|distribute|organi[sz]e|finali[sz]e|"
    r"set\s*up|plan|prepare|generate|create|make|fix)\b"
)

_ASK = re.compile(
    r"\b(?:find|who|which|list|show|display|tell|what|check|identify|get|"
    r"see|are\s+there|how\s+many|give\s+me|name)\b"
)

_ELIGIBLE = re.compile(
    r"\b(?:free|available|availability|eligible|eligibility|"
    r"can\s+(?:take|do|handle|perform|serve|cover|manage|be\s+assigned|"
    r"be\s+given|be\s+put|be\s+used)|"
    r"who\s+can|who\s+could|suitable|candidates?|not\s+busy|without\s+class)\b"
)

_TOTALS = re.compile(
    r"\b(?:total|totals|how\s+many|count|counts|tally|number\s+of|summary|"
    r"each\s+faculty|per\s+faculty|every\s+faculty)\b"
)

_FAIR = re.compile(
    r"\b(?:fewer|least|lowest|minimum|less)\b.{0,40}\b(?:dut(?:y|ies)|assign\w*|"
    r"allocat\w*)\b|\bfair(?:ly|ness)?\b|\bequal(?:ly)?\b|\bbalanc\w+\b|"
    r"\bevenly\b|\brotat\w+\b|\bprioriti[sz]e\b|\bwho\s+(?:has|have)\s+not\b"
)

_CONSECUTIVE = re.compile(
    r"\bconsecutive\b|\bback[\s-]*to[\s-]*back\b|\bsuccessive\b|\bin\s+a\s+row\b"
)

_RICH = re.compile(
    r"\bdepartments?\b|\bdepts?\b|\bbranch(?:es)?\b|\breasons?\b|\bwhy\b|"
    r"\beligib\w+\b|\bdetails?\b|\bexplain\b|\bjustif\w+\b|\bexisting\b|"
    r"\bcheck\b"
)


@dataclass
class ParsedRequest:

    intent: str = ""                 # eligible | assign | totals | ""
    dates: List[date] = field(default_factory=list)
    start: Optional[str] = None
    end: Optional[str] = None
    rooms: List[str] = field(default_factory=list)
    room_count: Optional[int] = None
    per_room: Optional[int] = None
    total: Optional[int] = None
    fair_first: bool = False
    avoid_consecutive: bool = False
    rich: bool = False
    words: int = 0


def is_exam_duty_text(text: str) -> bool:

    low = text.lower()

    return bool(
        re.search(r"\b(?:exam\w*|invigilat\w*)\b", low)
        and (
            re.search(
                r"\b(?:dut(?:y|ies)|invigilat\w*|supervis\w*|proctor\w*)\b",
                low,
            )
            or (
                re.search(rf"\b{_PEOPLE}\b", low)
                and re.search(rf"\b{_ROOM_WORD}\b", low)
            )
        )
    )


def parse_request(text: str, today: date) -> ParsedRequest:

    req = ParsedRequest()

    original = str(text or "")

    low = original.lower()

    req.words = len(re.findall(r"\w+", low))

    # ---- dates (and take their text out so digits aren't reused) ----
    hits = date_resolver.find_dates(original, today)

    masked = list(original)

    for hit in hits:

        for item in hit.dates:
            if item not in req.dates:
                req.dates.append(item)

        for i in range(hit.start, hit.end):
            masked[i] = " "

    without_dates = "".join(masked)

    # ---- times ----
    times = parse_time_range(without_dates)

    if times:
        req.start, req.end = times

    # ---- rooms and counts ----
    rooms, room_count = parse_rooms(without_dates)

    req.rooms = rooms
    req.room_count = room_count

    req.per_room, req.total = parse_counts(without_dates)

    # ---- intent: whichever instruction verb comes first ----
    first_assign = _ASSIGN.search(low)
    first_ask = _ASK.search(low)

    assign_first = bool(first_assign) and (
        not first_ask or first_assign.start() < first_ask.start()
    )

    if assign_first:
        req.intent = "assign"
    elif _ELIGIBLE.search(low):
        req.intent = "eligible"
    elif _TOTALS.search(low):
        req.intent = "totals"
    elif first_assign:
        req.intent = "assign"

    if not req.intent and (
        req.per_room or req.total or req.rooms or req.room_count
    ):
        req.intent = "assign"

    req.fair_first = bool(_FAIR.search(low))
    req.avoid_consecutive = bool(_CONSECUTIVE.search(low))
    req.rich = bool(_RICH.search(low)) or req.words >= 25

    return req


# ======================================================================
# allocation
# ======================================================================

def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip().lower())


class ExamDutyAllocator:

    def __init__(self, coordinator):

        self.coordinator = coordinator
        self.store = coordinator.store

    # -------- stored duties ------------------------------------------

    def stored_counts(self) -> Dict[str, int]:

        counts: Dict[str, int] = defaultdict(int)

        for name, n in self.store.duty_counts().items():
            counts[_norm(name)] += n

        return counts

    def stored_duties(self, teacher: str):

        out = []

        for duty in self.store.for_teacher(teacher):

            try:
                day = date.fromisoformat(str(duty.get("exam_date")))
            except ValueError:
                continue

            out.append((day, duty.get("start_time"), duty.get("end_time")))

        return out

    # -------- planning -----------------------------------------------

    def plan(
        self,
        sessions: List[Tuple[date, str, str]],
        slots: List[str],
        fair_first: bool = False,
        avoid_consecutive: bool = True,
    ) -> Dict[str, Any]:
        """
        sessions: [(date, 'HH:MM', 'HH:MM'), ...]
        slots:    room label for every invigilator seat of ONE session,
                  position-major so every room gets one before any gets two.
        """

        counts = self.stored_counts()

        planned_counts: Dict[str, int] = defaultdict(int)
        planned_duties: Dict[str, List[tuple]] = defaultdict(list)

        out_sessions = []

        stored_cache: Dict[str, list] = {}

        def stored_for(teacher):
            key = _norm(teacher)
            if key not in stored_cache:
                stored_cache[key] = self.stored_duties(teacher)
            return stored_cache[key]

        def minutes(value):
            hours, mins = str(value).split(":")
            return int(hours) * 60 + int(mins)

        def penalty(teacher, day, start, end):
            """number of neighbouring duties: same day (non-overlapping) or
            the previous / next calendar day"""

            score = 0

            for d, s, e in stored_for(teacher) + planned_duties[_norm(teacher)]:

                if d == day:
                    score += 1
                elif abs((d - day).days) == 1:
                    score += 1

            return score

        def overlaps_plan(teacher, day, start, end):

            for d, s, e in planned_duties[_norm(teacher)]:

                if d == day and minutes(s) < minutes(end) \
                        and minutes(start) < minutes(e):
                    return True

            return False

        for day, start, end in sorted(sessions, key=lambda s: (s[0], s[1])):

            cand = self.coordinator.candidates(day.isoformat(), start, end)

            if not cand.get("success"):
                return {
                    "success": False,
                    "reason": cand.get("reason", "candidates_failed"),
                }

            pool = list(cand["results"])

            chosen: List[Dict[str, Any]] = []
            shortage: List[str] = []
            used_here = set()

            for room in slots:

                options = [
                    c for c in pool
                    if _norm(c["teacher"]) not in used_here
                    and not overlaps_plan(c["teacher"], day, start, end)
                ]

                if not options:
                    shortage.append(room)
                    continue

                def key(c):

                    teacher = c["teacher"]
                    n = _norm(teacher)

                    total_duties = counts.get(n, 0) + planned_counts[n]

                    pen = penalty(teacher, day, start, end) \
                        if avoid_consecutive else 0

                    if fair_first:
                        return (pen, total_duties, c.get("daily_periods", 0),
                                teacher.lower())

                    return (pen, planned_counts[n], c.get("daily_periods", 0),
                            counts.get(n, 0), teacher.lower())

                best = min(options, key=key)

                n = _norm(best["teacher"])

                used_here.add(n)
                planned_counts[n] += 1
                planned_duties[n].append((day, start, end))

                chosen.append({
                    "hall": room,
                    "teacher": best["teacher"],
                    "daily_periods": best.get("daily_periods", 0),
                    "stored_before": counts.get(n, 0),
                })

            out_sessions.append({
                "exam_date": day.isoformat(),
                "day": cand["day"],
                "start_time": start,
                "end_time": end,
                "buffer_minutes": cand["buffer_minutes"],
                "availability_start": cand["availability_start"],
                "availability_end": cand["availability_end"],
                "eligible_count": cand["count"],
                "conflicts": cand.get("conflicts", []),
                "assignments": chosen,
                "shortage": shortage,
            })

        # ---- per-teacher totals and consecutive-duty report ----
        names = {}

        for sess in out_sessions:
            for a in sess["assignments"]:
                names[_norm(a["teacher"])] = a["teacher"]

        totals = []

        for n, teacher in names.items():
            totals.append({
                "teacher": teacher,
                "before": counts.get(n, 0),
                "new": planned_counts[n],
                "after": counts.get(n, 0) + planned_counts[n],
            })

        totals.sort(key=lambda t: (t["after"], t["teacher"].lower()))

        consecutive = []

        for n, teacher in names.items():

            days = sorted({d for d, _, _ in planned_duties[n]})

            near = [
                (a, b) for a in days for b in days
                if a < b and (b - a).days <= 1
            ]

            if near:
                consecutive.append(teacher)

        return {
            "success": True,
            "kind": "allocation",
            "sessions": out_sessions,
            "totals": totals,
            "consecutive": sorted(consecutive),
            "fair_first": fair_first,
            "seats_per_session": len(slots),
            "created": datetime.now(timezone.utc).isoformat(),
        }

    # -------- confirming ----------------------------------------------

    def confirm(self, plan: Dict[str, Any]) -> Dict[str, Any]:

        if not plan or not plan.get("success"):
            return {"success": False, "reason": "invalid_plan"}

        # re-check every teacher is still eligible (state may have changed)
        for sess in plan["sessions"]:

            fresh = self.coordinator.candidates(
                sess["exam_date"], sess["start_time"], sess["end_time"]
            )

            if not fresh.get("success"):
                return {"success": False, "reason": "stale_plan"}

            allowed = {_norm(c["teacher"]) for c in fresh["results"]}

            for a in sess["assignments"]:
                if _norm(a["teacher"]) not in allowed:
                    return {
                        "success": False,
                        "reason": "stale_plan",
                        "teacher": a["teacher"],
                    }

        saved = []

        stamp = datetime.now(timezone.utc).timestamp()

        for s_index, sess in enumerate(plan["sessions"]):

            group = f"examdutygrp-{stamp:.6f}-{s_index}"

            for a_index, a in enumerate(sess["assignments"]):

                record = {
                    "duty_id": f"{group}-{a_index}",
                    "duty_group_id": group,
                    "session_key": self.store.session_key(
                        sess["exam_date"], sess["start_time"],
                        sess["end_time"], a["hall"],
                    ),
                    "exam_date": sess["exam_date"],
                    "day": sess["day"],
                    "start_time": sess["start_time"],
                    "end_time": sess["end_time"],
                    "hall": a["hall"],
                    "session_id": "",
                    "teacher": a["teacher"],
                    "status": "confirmed",
                    "confirmed_at": datetime.now(timezone.utc).isoformat(),
                }

                self.store.add(record)
                saved.append(record)

        return {"success": True, "duties": saved}


# ======================================================================
# glue: understand -> answer
# ======================================================================

class ExamDutyRequestHandler:
    """
    handle(query) returns the reply text, or None when the question is a
    simple one that the existing exam-duty flow already answers.
    """

    def __init__(
        self,
        coordinator,
        model_getter: Callable[[], Any],
        branch_getter: Callable[[], Any],
        today_getter: Callable[[], date],
        has_named_teachers: Callable[[str], bool],
        unknown_branch: Optional[Callable[[str], Optional[str]]] = None,
    ):

        self.coordinator = coordinator
        self.allocator = ExamDutyAllocator(coordinator)
        self.model_getter = model_getter
        self.branch_getter = branch_getter
        self.today_getter = today_getter
        self.has_named_teachers = has_named_teachers
        self.unknown_branch = unknown_branch

        self.pending: Optional[Dict[str, Any]] = None

    # ---------------- public ------------------------------------------

    def handle(self, query: str) -> Optional[str]:

        text = str(query or "").strip()

        if not text:
            return None

        low = text.lower()

        # ----- answering a waiting proposal -----
        if self.pending is not None:

            if re.fullmatch(
                r"\W*(?:yes[\s,]*)?(?:please\s+)?(?:confirm(?:ed)?|approve|apply|"
                r"save|go\s+ahead)"
                r"(?:\s+(?:it|this|the|plan|assignment|allocation|duties|"
                r"all))*\W*",
                low,
            ):
                return self._confirm()

            if re.fullmatch(
                r"\W*(?:no[\s,]*)?(?:please\s+)?(?:cancel|discard|drop|"
                r"never\s*mind|forget\s+it|reject)(?:\s+(?:it|this|the|plan|"
                r"assignment|allocation))*\W*",
                low,
            ):
                self.pending = None
                return "Okay - the proposed exam-duty allocation was discarded."

        if not is_exam_duty_text(text):
            return None

        # "... from the mechanical branch" when no such branch exists:
        # say so instead of silently ignoring it
        if self.unknown_branch is not None:

            message = self.unknown_branch(text)

            if message:
                return message

        today = self.today_getter()

        req = parse_request(text, today)

        if not req.intent:
            return None

        named = self.has_named_teachers(text)

        # ----- which requests belong here -----
        if req.intent == "eligible":

            legacy_ok = bool(
                re.search(r"\bexam duty\b|\bexam duties\b|\binvigilat", low)
            ) and not re.search(
                r"\b(?:show|list|existing|confirmed)\s+exam\s+dut", low
            )

            if not req.rich and legacy_ok:
                return None

            if len(req.dates) > 1:
                return self._multi_date_eligible_hint()

            return self._eligible(req)

        if req.intent == "totals":

            # the original "how many exam duties ..." report is kept for
            # plain questions; this richer table is for ones asking for
            # departments / branches
            if not re.search(r"\bdepartments?\b|\bdepts?\b|\bbranch(?:es)?\b", low):
                return None

            return self._totals_report()

        if req.intent == "assign":

            if named:
                return None

            multi = bool(
                req.rooms or req.room_count or req.per_room
                or len(req.dates) > 1
            )

            # a plain "assign 3 faculty for exam duty on <date> <time>" (or
            # one missing its date/time) is the original flow's job
            if not multi and not req.fair_first:
                return None

            return self._allocate(req)

        return None

    # ---------------- pieces ------------------------------------------

    def _pretty_day(self, day: date) -> str:

        return (
            f"{date_resolver.weekday_name(day).title()}, "
            f"{date_resolver.pretty(day)}"
        )

    def _department(self, teacher: str) -> str:

        try:
            index = self.branch_getter()
            if index is not None:
                label = index.primary.get(teacher)
                if label:
                    return label
        except Exception:
            pass

        return "—"

    def _classes_that_day(self, teacher: str, weekday: str, start: str, end: str):
        """(slot numbers, text) of the teacher's regular classes that day."""

        model = self.model_getter()

        slots = sorted({
            e["slot"] for e in model.events
            if e.get("teacher") == teacher and e.get("day") == weekday
            and e.get("slot") in model.slot_info
        })

        if not slots:
            return [], "no classes that day"

        first = model.slot_info[slots[0]]
        last = model.slot_info[slots[-1]]

        def clock(value):
            return _clock(int(value)) if value is not None else "?"

        return slots, (
            f"{len(slots)} class{'es' if len(slots) != 1 else ''} that day "
            f"(slot{'s' if len(slots) != 1 else ''} "
            f"{', '.join(str(s) for s in slots)}; "
            f"{clock(first.get('start'))}–{clock(last.get('end'))})"
        )

    def _need_details(self, req: ParsedRequest, what: str) -> Optional[str]:

        missing = []

        if not req.dates:
            missing.append("the exam **date** (e.g. 10th November 2026)")

        if not req.start:
            missing.append("the **time** (e.g. 9:00 AM to 12:00 PM)")

        if not missing:
            return None

        return (
            f"To {what} I still need " + " and ".join(missing) + "."
        )

    # ---------------- 1) who is eligible ------------------------------

    def _eligible(self, req: ParsedRequest) -> str:

        ask = self._need_details(req, "list eligible faculty")

        if ask:
            return ask

        day = req.dates[0]

        cand = self.coordinator.candidates(day.isoformat(), req.start, req.end)

        if not cand.get("success"):
            return f"I couldn't check availability ({cand.get('reason')})."

        rows = []

        counts = self.allocator.stored_counts()

        for item in cand["results"]:

            teacher = item["teacher"]

            slots, class_text = self._classes_that_day(
                teacher, cand["day"], req.start, req.end
            )

            duties = counts.get(_norm(teacher), 0)

            reason = (
                f"free {cand['availability_start']}–{cand['availability_end']}"
                f"; {class_text}"
                f"; {duties} exam dut{'y' if duties == 1 else 'ies'} so far"
                f"{', none overlapping' if duties else ''}"
            )

            rows.append((duties, item.get("daily_periods", 0), teacher,
                         self._department(teacher), reason))

        rows.sort(key=lambda r: (r[0], r[1], r[2].lower()))

        title = (
            f"**Faculty eligible for exam duty — {self._pretty_day(day)}, "
            f"{req.start}–{req.end}** · {len(rows)} found"
        )

        lines = [
            title,
            "",
            f"*Rule: a teacher is eligible only if free for the whole duty "
            f"window plus a {cand['buffer_minutes']}-minute buffer on both "
            f"sides ({cand['availability_start']}–{cand['availability_end']}), "
            f"has no overlapping confirmed exam duty, and is listed in the "
            f"faculty timetable. Fewest exam duties first.*",
            "",
        ]

        if rows:

            lines += [
                "| # | Faculty | Department | Why eligible |",
                "| --- | --- | --- | --- |",
            ]

            for number, (_, _, teacher, dept, reason) in enumerate(rows, 1):
                lines.append(f"| {number} | {teacher} | {dept} | {reason} |")

        else:
            lines.append("Nobody is eligible for that window.")

        if any(r[3] == "—" for r in rows):
            lines += [
                "",
                "*“—” = department unknown (the teacher has no classes in "
                "the timetable to read a branch from).*",
            ]

        lines += ["", self._exclusion_summary(cand, req)]

        return "\n".join(lines)

    def _multi_date_eligible_hint(self) -> str:

        return (
            "I can check one exam date at a time. Ask again with a single "
            "date, or ask me to **assign** duties for several dates at once."
        )

    def _exclusion_summary(self, cand, req) -> str:

        model = self.model_getter()

        everyone = set(model.faculty_set)

        try:
            allowed = self.coordinator._real_faculty()
            if allowed is not None:
                everyone &= set(allowed)
        except Exception:
            pass

        eligible = {c["teacher"] for c in cand["results"]}
        on_duty = {c["teacher"] for c in cand.get("conflicts", [])}

        lo = int(cand["availability_start"][:2]) * 60 \
            + int(cand["availability_start"][3:])
        hi = int(cand["availability_end"][:2]) * 60 \
            + int(cand["availability_end"][3:])

        teaching = set()

        for e in model.events:

            t = e.get("teacher")

            if t in everyone and e.get("day") == cand["day"] \
                    and e.get("slot") in model.slot_info:

                info = model.slot_info[e["slot"]]

                if info.get("start") is not None and \
                        min(hi, info["end"]) - max(lo, info["start"]) > 0:
                    teaching.add(t)

        other = everyone - eligible - on_duty - teaching

        parts = [
            f"{len(teaching - on_duty)} teaching in that window or its buffer",
            f"{len(on_duty)} already on a confirmed exam duty then",
        ]

        if other:
            parts.append(
                f"{len(other)} without a complete timetable page "
                f"(can't be confirmed free)"
            )

        return (
            f"*Not eligible — {len(everyone) - len(eligible)} of "
            f"{len(everyone)} faculty: " + "; ".join(parts) + ".*"
        )

    # ---------------- 2/3) allocate -----------------------------------

    def _totals_report(self) -> str:

        counts = self.allocator.stored_counts()

        names = {}

        for duty in self.coordinator.all_duties():
            names[_norm(duty.get("teacher"))] = duty.get("teacher")

        everyone = sorted(self.model_getter().faculty_set, key=str.lower)

        have = sorted(
            [(counts.get(_norm(t), 0), t) for t in everyone
             if counts.get(_norm(t), 0)],
            key=lambda x: (-x[0], x[1].lower()),
        )

        if not have:
            return (
                "**Exam-duty totals:** no exam duties have been confirmed "
                f"yet, so all {len(everyone)} faculty are at 0."
            )

        lines = [
            f"**Exam-duty totals** — {len(have)} of {len(everyone)} faculty "
            f"have duties",
            "",
            "| Faculty | Department | Total duties |",
            "| --- | --- | --- |",
        ]

        for n, teacher in have:
            lines.append(f"| {teacher} | {self._department(teacher)} | {n} |")

        lines.append("")
        lines.append(f"*The other {len(everyone) - len(have)} faculty have 0.*")

        return "\n".join(lines)

    def _allocate(self, req: ParsedRequest) -> str:

        # ---- no schedule given: explain what is needed ----
        if not req.dates or not req.start:

            lines = [
                "I can allocate the exam duties as soon as I know the "
                "exam schedule. Please tell me, in one message:",
                "",
                "- the **date(s)**, e.g. *10th, 11th and 12th November 2026*",
                "- the **time**, e.g. *9:00 AM to 12:00 PM*",
                "- the **rooms and invigilators**, e.g. *5 rooms A101–A105, "
                "2 invigilators per room* (or just a total, e.g. *10 "
                "invigilators*)",
                "",
                "I will then use only faculty who are free (with the "
                f"{getattr(self.coordinator, '_buffered_times')(0, 0)[0]}-minute "
                "buffer), prefer those with **fewer exam duties**, avoid "
                "**consecutive-day or back-to-back** duties where possible, "
                "never place anyone in two rooms at the same time, and "
                "show every assigned faculty member's **total duties**.",
                "",
                self._totals_report(),
            ]

            return "\n".join(lines)

        # ---- how many seats per session ----
        rooms = list(req.rooms)

        if not rooms and req.room_count and req.room_count > 1:
            rooms = [f"Room {n}" for n in range(1, req.room_count + 1)]

        per_room = req.per_room

        if rooms and not per_room and req.total:

            slots = [rooms[i % len(rooms)] for i in range(req.total)]

        elif rooms and per_room:

            slots = [room for _ in range(per_room) for room in rooms]

        elif rooms and not per_room:

            return (
                f"I have {len(rooms)} room(s) — "
                f"{', '.join(rooms)}. How many invigilators should each "
                f"room get (for example *2 invigilators per room*)?"
            )

        elif req.total:

            label = ""
            slots = [label] * req.total

        else:

            return (
                "How many invigilators do you need? For example *2 "
                "invigilators per room for rooms A101–A105*, or *10 "
                "invigilators in total*."
            )

        sessions = [(d, req.start, req.end) for d in req.dates]

        plan = self.allocator.plan(
            sessions,
            slots,
            fair_first=req.fair_first,
            avoid_consecutive=True,
        )

        if not plan.get("success"):
            return f"I couldn't build the allocation ({plan.get('reason')})."

        plan["rooms"] = rooms
        plan["per_room"] = per_room

        assigned = sum(len(s["assignments"]) for s in plan["sessions"])

        if not assigned:
            return (
                "Nobody is eligible for that time, so no duties could be "
                "allocated. Try another time or date."
            )

        self.pending = plan

        return self._render_plan(plan, req)

    # ---------------- rendering ---------------------------------------

    def _render_plan(self, plan: Dict[str, Any], req: ParsedRequest) -> str:

        sessions = plan["sessions"]

        multi = len(sessions) > 1

        head_dates = ", ".join(
            date_resolver.pretty(date.fromisoformat(s["exam_date"]))
            for s in sessions
        )

        lines = [
            f"**Proposed exam-duty allocation — {head_dates}, "
            f"{sessions[0]['start_time']}–{sessions[0]['end_time']}**",
            "",
            f"*Everyone listed is free from {sessions[0]['availability_start']} "
            f"to {sessions[0]['availability_end']} "
            f"({sessions[0]['buffer_minutes']}-minute buffer before and after "
            f"the duty), is not in two rooms at once, and has no clashing "
            f"confirmed exam duty."
            + (" Faculty with fewer exam duties were preferred."
               if plan["fair_first"] else "")
            + "*",
            "",
        ]

        rooms = plan.get("rooms") or []

        for sess in sessions:

            day = date.fromisoformat(sess["exam_date"])

            if multi:
                lines += [f"**{self._pretty_day(day)}**", ""]

            if rooms:

                by_room: Dict[str, List[str]] = defaultdict(list)

                for a in sess["assignments"]:
                    by_room[a["hall"]].append(
                        f"{a['teacher']} ({self._department(a['teacher'])})"
                    )

                width = max(
                    [len(v) for v in by_room.values()] + [plan["per_room"] or 1]
                )

                header = "| Room | " + " | ".join(
                    f"Invigilator {n}" for n in range(1, width + 1)
                ) + " |"

                lines += [header, "| --- |" + " --- |" * width]

                for room in rooms:

                    people = by_room.get(room, [])

                    cells = people + ["⚠ not filled"] * (width - len(people))

                    lines.append(f"| {room} | " + " | ".join(cells) + " |")

            else:

                lines += ["| # | Faculty | Department |", "| --- | --- | --- |"]

                for n, a in enumerate(sess["assignments"], 1):
                    lines.append(
                        f"| {n} | {a['teacher']} | "
                        f"{self._department(a['teacher'])} |"
                    )

            if sess["shortage"]:
                lines += [
                    "",
                    f"⚠ Only {len(sess['assignments'])} of "
                    f"{len(sess['assignments']) + len(sess['shortage'])} "
                    f"places could be filled — {sess['eligible_count']} "
                    f"faculty were eligible.",
                ]

            lines.append("")

        # ---- fairness table ----
        lines += [
            "**Exam duties per assigned faculty (after this plan)**",
            "",
            "| Faculty | Before | New | Total |",
            "| --- | --- | --- | --- |",
        ]

        for t in plan["totals"]:
            lines.append(
                f"| {t['teacher']} | {t['before']} | {t['new']} | {t['after']} |"
            )

        if plan["consecutive"]:
            lines += [
                "",
                "*Consecutive-day or back-to-back duties could not be "
                "avoided for: " + ", ".join(plan["consecutive"]) + ".*",
            ]
        elif multi or req.avoid_consecutive:
            lines += ["", "*No consecutive-day or back-to-back duties.*"]

        total_seats = sum(
            len(s["assignments"]) + len(s["shortage"]) for s in sessions
        )

        filled = sum(len(s["assignments"]) for s in sessions)

        lines += [
            "",
            f"{filled} of {total_seats} places filled. "
            f"Reply **confirm** to save these duties, or **cancel** to "
            f"discard them.",
        ]

        return "\n".join(lines)

    # ---------------- confirm -----------------------------------------

    def _confirm(self) -> str:

        plan = self.pending

        self.pending = None

        result = self.allocator.confirm(plan)

        if not result.get("success"):

            return (
                "I couldn't save the allocation "
                f"({result.get('reason', 'unknown')}). Availability may have "
                "changed since the proposal — please ask again for a fresh "
                "proposal."
            )

        duties = result["duties"]

        lines = [f"**Confirmed {len(duties)} exam duties.**", ""]

        for duty in duties:

            room = f" · {duty['hall']}" if duty.get("hall") else ""

            lines.append(
                f"- {duty['teacher']} — {duty['exam_date']} "
                f"{duty['start_time']}–{duty['end_time']}{room}"
            )

        lines += ["", "Ask *show exam duties* any time to see them again."]

        return "\n".join(lines)