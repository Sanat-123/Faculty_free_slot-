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
from engine import text_normalizer
from scheduling import duty_history

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

    found = parse_time_ranges(text)

    return found[0] if found else None


def parse_time_ranges(text: str) -> List[Tuple[str, str]]:
    """
    EVERY time range in the text, left to right:
    'from 9 to 10:30 and 1 to 2:30' -> [('09:00','10:30'), ('13:00','14:30')].
    """

    low = text.lower()

    out: List[Tuple[str, str]] = []

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
            if 1 <= a <= 6:
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

            pair = (_clock(start), _clock(min(end, 24 * 60 - 1)))

            if pair not in out:
                out.append(pair)

    return out


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

            # "B1 B2 B3" - several room names separated only by spaces.
            # Each piece must be a room-like token that contains a letter
            # (so a time such as "9" or "12" is never taken for a room), and
            # only the leading run of such pieces counts.
            pieces = part.split()

            if len(pieces) > 1:

                run = []

                for piece in pieces:

                    if re.fullmatch(
                        r"[A-Za-z]{1,4}-?\d{1,4}[A-Za-z]{0,2}|\d{3,4}[A-Za-z]{0,2}",
                        piece,
                    ):
                        run.append(piece)
                    else:
                        break

                if len(run) > 1:
                    rooms.extend(x.upper() for x in run)
                    break

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
        # "12 rooms x 2", "rooms × 2 faculty"
        rf"\b{_ROOM_WORD}\s*[x×*]\s*{_NUM}\b",
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

    # "assign duty to 3 faculty", "need exam duty for 6 invigilators ..." -
    # a plain "<n> <people>" that is not a per-room count
    if total is None and per_room is None:

        m = re.search(
            rf"(?<!\bno\s)(?<!\bnot\s)(?<!\bany\s)(?<!\bevery\s)(?<!\bthe\s)"
            rf"\b(\d{{1,3}}|(?:{'|'.join(w for w in sorted(_NUMBER_WORDS, key=len, reverse=True) if w not in ('a', 'an', 'single', 'pair', 'couple'))}))"
            rf"\s+{_PEOPLE}\b(?!\s+(?:per|for\s+each|in\s+each|to\s+each|each|every))",
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
    excluded: set = field(default_factory=set)            # normalised names
    exclusion_notes: List[str] = field(default_factory=list)
    setup_reused: str = ""
    limit: Optional[Tuple[str, float]] = None      # ("<", 50): duties so far must be < 50
    times: List[Tuple[str, str]] = field(default_factory=list)   # every time window
    distinct_per_day: bool = False                 # nobody on two sessions of one day
    invalid_dates: List[tuple] = field(default_factory=list)     # "31 Nov"


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


def clean_text(text: str, protected=()) -> str:
    """
    Repair typos and odd time notations in a coordinator's message
    ("1: to 2:30", "faculities", "less then 50") before it is read.
    `protected` = words never to touch (faculty-name parts).
    """

    return text_normalizer.normalize(
        text, duty_history.lexicon().get("spell_vocabulary", []), protected
    )


def understand(text: str, today: date, protected=()) -> ParsedRequest:
    """clean_text + parse_request: the full reading of one message."""

    return parse_request(clean_text(text, protected), today)


def parse_request(text: str, today: date) -> ParsedRequest:

    req = ParsedRequest()

    original = str(text or "")

    low = original.lower()

    req.words = len(re.findall(r"\w+", low))

    # ---- duty-count constraint ("fewer than 50 duties"): read first and take
    #      its text out so the number is not reused as a date / time / count ----
    found_limit = duty_history.parse_duty_limit(original)

    if found_limit:
        req.limit, (a, b) = found_limit
        original = original[:a] + " " * (b - a) + original[b:]
        low = original.lower()

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
    req.times = parse_time_ranges(without_dates)

    if req.times:
        req.start, req.end = req.times[0]

    # ---- dates that do not exist ("31 Nov") ----
    if not req.dates:
        req.invalid_dates = date_resolver.find_impossible_dates(original, today)

    # ---- "no two teachers on both timings", "different faculty each time" ----
    for phrase in duty_history.lexicon().get("distinct_per_day_phrases", []):
        if re.search(phrase, low):
            req.distinct_per_day = True
            break

    # ---- rooms and counts ----
    rooms, room_count = parse_rooms(without_dates)

    req.rooms = rooms
    req.room_count = room_count

    req.per_room, req.total = parse_counts(without_dates)

    # "5 faculty each" without any rooms is "5 in every session", not "5 per room"
    if req.per_room is not None and req.total is None and not rooms and not room_count:
        req.total, req.per_room = req.per_room, None

    # "5 faculty each", "5 per session / slot / shift" with no rooms: that many seats
    # in every session
    if req.total is None and req.per_room is None and not rooms:
        m = re.search(
            rf"(?<!\bno\s)\b(\d{{1,3}})\s+(?:{'|'.join(map(re.escape, duty_history.lexicon().get('headcount_nouns', ['faculty', 'teachers', 'teacher', 'invigilators', 'invigilator', 'people', 'members', 'staff'])))})?\s*"
            rf"(?:each|per\s+(?:session|slot|shift|timing|sitting|time)|in\s+each\s+(?:session|slot|shift|timing))\b",
            without_dates.lower(),
        )
        if m:
            req.total = int(m.group(1))

    # ---- intent: whichever instruction verb comes first ----
    first_assign = _ASSIGN.search(low)
    first_ask = _ASK.search(low)

    assign_first = bool(first_assign) and (
        not first_ask or first_assign.start() < first_ask.start()
    )

    # "I have to assign exam duty on 30 Nov ... so find who is free": the verb
    # "assign" comes first but what is being ASKED for is a list of free people
    # (an ask-word + availability words, and no head-count to actually assign)
    asks_for_list = bool(
        first_ask and _ELIGIBLE.search(low)
        and not (req.total or req.per_room or req.rooms or req.room_count)
    )

    if assign_first and not asks_for_list:
        req.intent = "assign"
    elif _ELIGIBLE.search(low):
        req.intent = "eligible"
    elif _TOTALS.search(low):
        req.intent = "totals"
    elif first_assign:
        req.intent = "assign"

    if not req.intent and (
        req.per_room or req.total or req.rooms or req.room_count
        or (req.limit and req.dates)
    ):
        req.intent = "assign"

    if req.limit and not req.intent:
        req.intent = "totals"

    req.fair_first = bool(_FAIR.search(low))
    req.avoid_consecutive = bool(_CONSECUTIVE.search(low))
    req.rich = bool(_RICH.search(low)) or req.words >= 25

    return req




# ======================================================================
# naming people: exclusions / "unavailable" statements
#
# Faculty are found in the question by matching it against the timetable's
# own list of names - there is no list of surnames or departments here.
# Titles are learned from the data (the word most names start with); the
# few generic ones below only cover titles the timetable itself does not use.
# ======================================================================

_GENERIC_TITLES = {
    "dr", "prof", "professor", "mr", "mrs", "ms", "miss", "mx", "sir",
    "madam", "mam", "shri", "smt", "sri", "er",
}

_VERB = (
    r"assign|include|use|select|consider|allocate|pick|choose|give|put|"
    r"schedule|appoint|take|involve|nominate|add|count"
)

_EXCLUDE_TRIGGER = re.compile(
    r"\b(?:exclud(?:e|es|ing|ed)|except(?:ing)?|exempt(?:ing|ed|s)?|"
    r"omit(?:ting|ted|s)?|skip(?:ping|ped|s)?|ignor(?:e|es|ing|ed)|"
    r"without|leave\s+out|leaving\s+out|other\s+than|apart\s+from|"
    r"besides|but\s+not|barring|keep\s+out|remove|drop|"
    rf"not\s+(?:to\s+)?(?:{_VERB})|"
    rf"(?:do|does|did|should|must|shall|can|could|will|would)\s*(?:not|n['’]t)\s+(?:{_VERB})|"
    rf"(?:don|doesn|didn|shouldn|mustn|can|couldn|won|wouldn)['’]?t\s+(?:{_VERB})|"
    rf"never\s+(?:{_VERB})|avoid|no\s+duty\s+(?:for|to)|"
    r"leave\s+.{0,20}\s+out)\b",
    re.I,
)

_STATUS = re.compile(
    r"(?P<who>[^.;\n?!]{2,160}?)\s+"
    r"(?:is|are|will\s+be|remains?|stays?|was|were|has\s+been|have\s+been)\s+"
    r"(?:currently\s+|also\s+|both\s+)?(?:marked\s+(?:as\s+)?)?"
    r"(?:not\s+available|unavailable|on\s+(?:medical\s+|casual\s+|earned\s+)?leave|"
    r"absent|away|out\s+of\s+station|travell?ing|off\s+duty|on\s+tour|sick|ill)\b",
    re.I,
)

_MARK = re.compile(
    r"^\W*(?:please\s+)?(?:can\s+you\s+)?(?:mark|set|record|note|register|flag)"
    r"\s+(?P<who>.+?)\s+(?:as\s+|to\s+be\s+)?"
    r"(?:not\s+available|unavailable|on\s+leave|absent|away)\b(?P<rest>.*)$",
    re.I | re.S,
)

_CLEAR = re.compile(
    r"\b(?:clear|reset|remove|delete|undo|cancel)\b.{0,40}"
    r"\b(?:unavailab\w+|leave|absence)\b"
    r"|\b(?:unavailab\w+|leave)\b.{0,30}\b(?:clear|reset|removed?|cancel\w*)\b",
    re.I,
)

_SHOW_UNAVAILABLE = re.compile(
    r"\b(?:who|which|show|list|display|see|what)\b.{0,50}"
    r"\b(?:unavailab\w+|not\s+available)\b",
    re.I,
)


def _word_tokens(text: str) -> List[str]:

    return re.findall(r"[a-z]+", str(text or "").lower())


def learn_titles(faculty) -> set:
    """Titles used by this timetable: first words shared by several names."""

    first: Dict[str, int] = defaultdict(int)

    for name in faculty:
        tokens = _word_tokens(name)
        if len(tokens) > 1:
            first[tokens[0]] += 1

    learned = {t for t, n in first.items() if n >= 3 and len(t) <= 6}

    return learned | _GENERIC_TITLES


def resolve_faculty_phrase(phrase: str, faculty, titles: set) -> List[str]:
    """
    Every faculty member whose name contains all the (non-title) words of
    `phrase`.  Small typos are tolerated; one-letter words match initials.
    An ambiguous phrase (a surname shared by several people) therefore
    returns all of them - the caller decides what to do with that.
    """

    import difflib

    words = [w for w in _word_tokens(phrase) if w not in titles]

    if not words or len(words) > 4:
        return []

    found = []

    for name in faculty:

        have = [t for t in _word_tokens(name) if t not in titles]

        if not have:
            continue

        def hit(word):
            for t in have:
                if word == t:
                    return True
                if len(word) == 1 and t.startswith(word):
                    return True
                if len(word) >= 5 and len(t) >= 5 and \
                        difflib.SequenceMatcher(None, word, t).ratio() >= 0.86:
                    return True
            return False

        if all(hit(w) for w in words):
            found.append(name)

    return found


def _clause_end(text: str, start: int, titles: set) -> int:
    """End of the sentence that begins at `start` ('Dr.' is not an end)."""

    i = start

    while i < len(text):

        ch = text[i]

        if ch in ";?!\n":
            return i

        if ch == ".":

            before = re.search(r"([A-Za-z]+)$", text[:i])
            word = before.group(1).lower() if before else ""

            if word not in titles and len(word) > 1:
                return i

        i += 1

    return len(text)


def _split_items(chunk: str) -> List[str]:

    chunk = re.sub(
        r"\b(?:as\s+requested|as\s+asked|please|from\s+(?:the\s+)?(?:exam\s+)?"
        r"(?:duty|duties|list|allocation|plan)|for\s+(?:this|the)\s+\w+)\b",
        " ", chunk, flags=re.I,
    )

    parts = re.split(r"\s*(?:,|&|/|\band\b|\bor\b|\bplus\b|\bnor\b)\s*", chunk, flags=re.I)

    return [x.strip(" .:()[]\"'") for x in parts if x.strip(" .:()[]\"'")]


def parse_exclusions(text: str, faculty, titles: set):
    """
    Faculty the request wants left out - by an instruction ("exclude Dr. A and
    B", "except C", "don't use D") or a statement ("E is on leave").

    Returns (excluded names, notes, spans) where notes explain each phrase
    and spans are the text ranges that were used (so those names are not also
    treated as people to *assign*).
    """

    text = str(text or "")

    phrases: List[Tuple[str, int, int]] = []

    for m in _EXCLUDE_TRIGGER.finditer(text):

        end = _clause_end(text, m.end(), titles)

        phrases.append((text[m.end():end], m.end(), end))

    for m in _STATUS.finditer(text):

        phrases.append((m.group("who"), m.start("who"), m.end("who")))

    excluded: Dict[str, str] = {}
    notes: List[str] = []
    spans: List[Tuple[int, int]] = []

    for chunk, start, end in phrases:

        used = False

        for item in _split_items(chunk):

            matches = resolve_faculty_phrase(item, faculty, titles)

            if not matches:

                # a capitalised name (or one with a title) that nobody in the
                # timetable has: say so instead of silently ignoring it
                words = re.findall(r"[A-Za-z][A-Za-z.]*", item)

                looks_like_name = (
                    1 <= len(words) <= 4
                    and all(w[0].isupper() for w in words)
                    and any(
                        w.strip(".").lower() not in titles for w in words
                    )
                )

                if looks_like_name:
                    notes.append(
                        f"“{item}” — no faculty with that name in the "
                        f"timetable, so nobody was excluded for it"
                    )

                continue

            used = True

            for name in matches:
                excluded[_norm(name)] = name

            if len(matches) == 1:
                notes.append(f"“{item}” → {matches[0]}")
            else:
                notes.append(
                    f"“{item}” matches {len(matches)} faculty "
                    f"({', '.join(matches[:6])}{'…' if len(matches) > 6 else ''}) "
                    f"— all were excluded; give the full name to exclude only one"
                )

        if used:
            spans.append((start, end))

    return set(excluded), list(dict.fromkeys(notes)), spans


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
        excluded: Optional[set] = None,
        limit: Optional[Tuple[str, float]] = None,
        distinct_per_day: bool = False,
    ) -> Dict[str, Any]:
        """
        distinct_per_day: nobody is placed in two sessions of the same day
               (not even at different times).
        limit: (operator, number) on a teacher's TOTAL exam duties (uploaded duty
               sheet + confirmed + already planned here), e.g. ("<", 50).  It is
               re-checked seat by seat, so someone on 49 can take one duty and is
               then at 50 and no longer eligible.
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

        order = sorted(sessions, key=lambda s: (s[0], s[1]))

        # nobody may serve twice in a day: fill the session with the FEWEST free
        # people first, so someone who could cover either session is not spent
        # on the roomier one (the results are put back in time order below)
        if distinct_per_day and len(order) > 1:

            def pool_size(sess):
                try:
                    found = self.coordinator.candidates(sess[0].isoformat(), sess[1], sess[2])
                    return len([
                        c for c in found.get("results", [])
                        if _norm(c["teacher"]) not in {_norm(x) for x in (excluded or ())}
                        and duty_history.passes_limit(
                            counts.get(_norm(c["teacher"]), 0), limit)
                    ])
                except Exception:
                    return 10 ** 6

            order = sorted(order, key=lambda sess: (pool_size(sess), sess[0], sess[1]))

        for day, start, end in order:

            cand = self.coordinator.candidates(day.isoformat(), start, end)

            if not cand.get("success"):
                return {
                    "success": False,
                    "reason": cand.get("reason", "candidates_failed"),
                }

            pool = [
                c for c in cand["results"]
                if _norm(c["teacher"]) not in (excluded or set())
            ]

            # the duty-count constraint: who the rule removes from today's pool
            limit_removed = [
                c["teacher"] for c in pool
                if not duty_history.passes_limit(
                    counts.get(_norm(c["teacher"]), 0)
                    + planned_counts[_norm(c["teacher"])], limit)
            ]

            chosen: List[Dict[str, Any]] = []
            shortage: List[str] = []
            used_here = set()

            for room in slots:

                options = [
                    c for c in pool
                    if _norm(c["teacher"]) not in used_here
                    and not overlaps_plan(c["teacher"], day, start, end)
                    and not (
                        distinct_per_day
                        and any(
                            d == day
                            for d, _, _ in planned_duties[_norm(c["teacher"])]
                        )
                    )
                    and duty_history.passes_limit(
                        counts.get(_norm(c["teacher"]), 0)
                        + planned_counts[_norm(c["teacher"])], limit)
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
                "limit_removed": len(limit_removed),
                "eligible_after_limit": cand["count"] - len(limit_removed)
                if limit else cand["count"],
            })

        # ---- per-teacher totals and consecutive-duty report ----
        names = {}

        # filled scarcest-first above: show them in time order again
        out_sessions.sort(key=lambda sess: (sess["exam_date"], sess["start_time"]))

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
            "limit": list(limit) if limit else None,
            "distinct_per_day": distinct_per_day,
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
        unavailable_store=None,
    ):

        self.coordinator = coordinator
        self.unavailable_store = unavailable_store

        # rooms / invigilators of the last request that gave them, so a later
        # "prepare the allocation ... list unfilled rooms" need not repeat them
        self.last_setup: Optional[Dict[str, Any]] = None
        self.allocator = ExamDutyAllocator(coordinator)
        self.model_getter = model_getter
        self.branch_getter = branch_getter
        self.today_getter = today_getter
        self.has_named_teachers = has_named_teachers
        self.unknown_branch = unknown_branch

        self.pending: Optional[Dict[str, Any]] = None

        self._history_synced_with = None

        # a request that only lacked "how many": the next short reply finishes it
        self.awaiting_count: Optional[ParsedRequest] = None

    # ---------------- public ------------------------------------------

    def handle(self, query: str) -> Optional[str]:

        text = str(query or "").strip()

        if not text:
            return None

        # typos / odd clock notations repaired first; names are never touched
        protected = {
            tok for name in self._faculty_names()
            for tok in re.findall(r"[a-z]+", str(name).lower())
        }

        text = clean_text(text, protected)

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

        # ----- the reply to "how many faculty do you need?" -----
        if self.awaiting_count is not None:

            finished = self._finish_awaiting(text)

            if finished is not None:
                return finished

        # ----- "clear confirmed exam duties": forget duties confirmed in the chat -----
        clear_words = "|".join(map(re.escape, duty_history.lexicon()["clear_words"]))

        if re.search(rf"\b(?:{clear_words})\b.{{0,25}}\bconfirmed\b.{{0,15}}\bdut", text.lower()):

            before = len(self.coordinator.store.confirmed_counts())
            self.coordinator.store.clear()
            self.pending = None

            return (
                f"Cleared the exam duties confirmed in the chat ({before} teacher"
                f"{'s' if before != 1 else ''} affected). Counts now come only from "
                f"the uploaded duty sheet."
                if before else "There were no confirmed exam duties to clear."
            )

        # ----- the uploaded duty sheet: "show / clear the duty history" -----
        history_reply = self._history_command(text)

        if history_reply is not None:
            return history_reply

        if (
            not is_exam_duty_text(text)
            and not self._duty_sheet_request(text)
            and not self._multi_window_free_request(text)
        ):
            return None

        self._sync_history()

        # ----- "mark Dr X unavailable", "who is unavailable", "clear ..." -----
        availability_reply = self._availability_command(text)

        if availability_reply is not None:
            return availability_reply

        # "... from the mechanical branch" when no such branch exists:
        # say so instead of silently ignoring it
        if self.unknown_branch is not None:

            message = self.unknown_branch(text)

            if message:
                return message

        today = self.today_getter()

        req = parse_request(text, today)

        # faculty the request leaves out ("exclude Dr. A", "B is on leave")
        faculty = self._faculty_names()

        titles = learn_titles(faculty)

        excluded, notes, spans = parse_exclusions(text, faculty, titles)

        req.excluded = excluded
        req.exclusion_notes = notes

        # a date that does not exist: say so instead of asking for "the date"
        if req.invalid_dates and not req.dates:
            return self._invalid_date_reply(req)

        if not req.intent:
            return None

        # people named only inside an exclusion are not people to ASSIGN
        remaining = list(text)

        for a, b in spans:
            for i in range(a, min(b, len(remaining))):
                remaining[i] = " "

        named = self.has_named_teachers("".join(remaining))

        # ----- which requests belong here -----
        if req.intent == "eligible":

            legacy_ok = bool(
                re.search(r"\bexam duty\b|\bexam duties\b|\binvigilat", low)
            ) and not re.search(
                r"\b(?:show|list|existing|confirmed)\s+exam\s+dut", low
            )

            # the original one-line list is kept only for a plain single-slot
            # question; anything with several slots / dates, a rule, or an
            # uploaded duty sheet gets the full tables
            sheet = self._baseline()
            sheet_loaded = bool(sheet is not None and sheet.exists())

            if (
                not req.rich and legacy_ok and not req.limit
                and len(req.times) <= 1 and len(req.dates) <= 1
                and not sheet_loaded
            ):
                return None

            return self._eligible(req)

        if req.intent == "totals":

            # the original "how many exam duties ..." report is kept for
            # plain questions; this richer table is for ones asking for
            # departments / branches
            if req.limit:
                return self._limit_report(req)

            if not re.search(r"\bdepartments?\b|\bdepts?\b|\bbranch(?:es)?\b", low):
                return None

            return self._totals_report()

        if req.intent == "assign":

            if named:
                return None

            multi = bool(
                req.rooms or req.room_count or req.per_room
                or len(req.dates) > 1
                or len(req.times) > 1
                or req.distinct_per_day
                or req.excluded
                or req.limit
                or (self.last_setup and req.dates and req.start
                    and not req.total)
            )

            # a plain "assign 3 faculty for exam duty on <date> <time>" (or
            # one missing its date/time) is the original flow's job
            if not multi and not req.fair_first:
                return None

            return self._allocate(req)

        return None

    # ---------------- pieces ------------------------------------------

    # ---------------- helpers shared by the replies --------------------

    def _faculty_names(self) -> List[str]:

        model = self.model_getter()

        everyone = set(getattr(model, "faculty_set", None) or [])

        try:
            allowed = self.coordinator._real_faculty()
            if allowed is not None:
                everyone &= set(allowed)
        except Exception:
            pass

        return sorted(everyone)

    def _exclusion_lines(self, req: ParsedRequest) -> List[str]:

        if not req.exclusion_notes:
            return []

        return [
            "*Left out as requested:* " + "; ".join(req.exclusion_notes) + ".",
        ]

    def _availability_command(self, text: str) -> Optional[str]:
        """Mark / clear / list faculty who are unavailable for exam duty."""

        store = self.unavailable_store

        if store is None:
            return None

        faculty = self._faculty_names()
        titles = learn_titles(faculty)

        mark = _MARK.match(text)

        if mark:

            found: Dict[str, str] = {}
            unresolved = []

            for item in _split_items(mark.group("who")):

                matches = resolve_faculty_phrase(item, faculty, titles)

                if not matches:
                    unresolved.append(item)

                elif len(matches) > 1:
                    return (
                        f"“{item}” matches {len(matches)} faculty "
                        f"({', '.join(matches[:6])}). Please use the full name "
                        f"so I mark the right person."
                    )

                else:
                    found[_norm(matches[0])] = matches[0]

            if not found:
                return None

            hits = date_resolver.find_dates(text, self.today_getter())
            when = hits[0].dates[0].isoformat() if hits and hits[0].dates else ""

            reason = re.search(
                r"\b(?:on\s+leave|absent|away|sick|travell?ing)\b",
                text, re.I,
            )

            for name in found.values():
                store.add(name, when, reason.group(0).lower() if reason else "")

            scope = (
                f"on {self._pretty_day(date.fromisoformat(when))}"
                if when else "until cleared"
            )

            return (
                f"Marked unavailable for exam duty {scope}: "
                + ", ".join(found.values()) + "."
                + (f" (Not recognised: {', '.join(unresolved)}.)" if unresolved else "")
                + " They will not be proposed or assigned."
            )

        if _CLEAR.search(text):

            found = {}

            for item in _split_items(re.sub(
                r"(?i)\b(?:clear|reset|remove|delete|undo|cancel|unavailab\w+|"
                r"leave|absence|exam|duty|duties|for|from|the|of|all)\b", " ", text
            )):
                for name in resolve_faculty_phrase(item, faculty, titles):
                    found[_norm(name)] = name

            if found:
                removed = sum(store.remove(n) for n in found.values())
                return (
                    f"Removed {removed} unavailability "
                    f"entr{'y' if removed == 1 else 'ies'} for "
                    + ", ".join(found.values()) + "."
                )

            if re.search(r"\ball\b", text, re.I):
                return f"Cleared {store.clear()} unavailability entries."

            return None

        if _SHOW_UNAVAILABLE.search(text) and not _ASSIGN.search(text.lower()) \
                and not re.search(r"\b(?:free|eligible|available\s+for)\b", text, re.I):

            rows = store.all()

            if not rows:
                return "Nobody is currently marked unavailable for exam duty."

            lines = ["**Marked unavailable for exam duty**", ""]

            for row in rows:
                when = row.get("exam_date") or "until cleared"
                lines.append(f"- {row.get('teacher')} — {when} ({row.get('reason')})")

            return "\n".join(lines)

        return None

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

    # ---------------- the uploaded duty sheet --------------------------

    def _baseline(self):
        return getattr(self.coordinator.store, "baseline", None)

    def _sync_history(self) -> None:
        """Match the sheet's names to the timetable's spelling (once per timetable)."""

        base = self._baseline()

        if base is None or not base.exists():
            return

        faculty = self._faculty_names()

        if self._history_synced_with == len(faculty):
            return

        base.reconcile(faculty, learn_titles(faculty))
        self._history_synced_with = len(faculty)

    def _history_notes(self) -> List[str]:
        """Where the counts came from, and what could not be matched."""

        base = self._baseline()

        if base is None or not base.exists():
            return []

        info = base.info()
        counts = base.counts()
        faculty = self._faculty_names()

        listed = {_norm(n) for n in counts}
        absent = [t for t in faculty if _norm(t) not in listed]

        notes = [
            f"*Duty counts: from “{info.get('source') or 'the uploaded duty sheet'}” "
            f"({info.get('people', len(counts))} names; {info.get('mode') or 'auto-detected'}).*"
        ]

        if info.get("unmatched"):
            names = info["unmatched"]
            notes.append(
                f"*{len(names)} name(s) in the sheet are not in the timetable and "
                f"cannot be assigned: {', '.join(names[:6])}{'…' if len(names) > 6 else ''}.*"
            )

        if info.get("ambiguous"):
            names = info["ambiguous"]
            notes.append(
                f"*{len(names)} name(s) in the sheet match more than one timetable "
                f"teacher and were left unmatched: {', '.join(names[:6])}.*"
            )

        if absent:
            notes.append(
                f"*{len(absent)} timetable faculty are not in the duty sheet and "
                f"were counted as 0 duties.*"
            )

        return ["", *notes]

    def _session_windows(self, req: ParsedRequest):
        windows = req.times or [(req.start, req.end)]
        return [(d, s, e) for d in req.dates for (s, e) in windows]

    def _ask_headcount(self, req: ParsedRequest) -> str:
        """
        Everything but "how many" is known: remember the request so a short
        reply ("6", "6 per session") finishes it, and tell the coordinator how
        many faculty each session can actually draw on under their rule.
        """

        self.awaiting_count = req

        self._sync_history()

        counts = self.allocator.stored_counts()

        lines = []
        known = []

        for day, start, end in self._session_windows(req):

            try:
                cand = self.coordinator.candidates(day.isoformat(), start, end)
            except Exception:
                continue

            pool = [
                c for c in cand["results"]
                if duty_history.passes_limit(
                    counts.get(_norm(c["teacher"]), 0), req.limit)
            ]

            known.append(
                f"- {self._pretty_day(day)}, {start}–{end}: "
                f"**{len(pool)}** faculty are free"
                + (f" and satisfy “exam duties {duty_history.limit_text(*req.limit)}”"
                   if req.limit else "")
            )

        lines.append(
            "I have the "
            + ("date, " if req.dates else "")
            + ("time" + ("s" if len(req.times) > 1 else "") + ", " if req.start else "")
            + ("the duty-count rule" if req.limit else "the schedule")
            + (" and the rule that nobody is on duty at both timings"
               if req.distinct_per_day else "")
            + "."
        )

        if known:
            lines += ["", *known]

        lines += [
            "",
            "**How many faculty do you need per session?** For example *6 per "
            "session*, or *2 invigilators per room for rooms A101–A105*. "
            "Just reply with the number — I’ll keep the rest.",
        ]

        return "\n".join(lines)

    def _finish_awaiting(self, text: str) -> Optional[str]:
        """A short reply to _ask_headcount: '6', '6 per session', 'rooms A1-A3 2 each'."""

        pending = self.awaiting_count
        low = text.lower().strip()

        if re.search(r"\b(cancel|never\s*mind|forget it|stop)\b", low):
            self.awaiting_count = None
            return "Okay, I dropped that request."

        # A complete NEW request (its own date / time / long sentence) is never
        # "the answer": drop the old question and let this one be read on its own.
        fresh = parse_request(text, self.today_getter())

        if fresh.dates or fresh.times or fresh.limit or len(low.split()) > 14:
            self.awaiting_count = None
            return None

        per_room, total = parse_counts(low)
        rooms, room_count = parse_rooms(text)

        if total is None and per_room is None and not rooms and not room_count:
            m = re.fullmatch(
                r"\W*(\d{1,3})\s*(?:faculty|teachers?|invigilators?|per\s+session|each|"
                r"in\s+total|total|people|members)?\W*", low)
            if m:
                total = int(m.group(1))

        if total is None and per_room is None and not rooms and not room_count:
            self.awaiting_count = None       # not an answer: the user moved on
            return None

        if total is not None:
            pending.total = total
        if per_room is not None:
            pending.per_room = per_room
        if rooms or room_count:
            pending.rooms, pending.room_count = rooms, room_count or len(rooms)

        self.awaiting_count = None

        return self._allocate(pending)

    def _invalid_date_reply(self, req: ParsedRequest) -> str:

        import calendar

        text, day, month, year, last = req.invalid_dates[0]

        name = calendar.month_name[month]
        nxt_month = month % 12 + 1
        nxt_year = year + (1 if month == 12 else 0)

        return (
            f"**{text.strip()}** is not a real date — {name} {year} has only "
            f"{last} days.\n\n"
            f"Did you mean **{last} {name} {year}** or "
            f"**{day - last} {calendar.month_name[nxt_month]} {nxt_year}**? "
            f"Send the request again with the correct date and I’ll build the "
            f"allocation."
        )

    def _multi_window_free_request(self, text: str) -> bool:
        """
        "who is free on 30 Nov 9 to 10:30 and 1 to 2:30": the plain free-slot
        engine reads ONE time range and would silently drop the other, so a
        question about several windows is answered here, window by window.
        """

        req = parse_request(text, self.today_getter())

        return bool(
            req.intent == "eligible" and req.dates and len(req.times) >= 2
        )

    def _duty_sheet_request(self, text: str) -> bool:
        """
        With a duty sheet loaded, "assign duty on 12 november ... fewer than 50
        duties" is an exam-duty request even without the word "exam".
        """

        base = self._baseline()

        if base is None or not base.exists():
            return False

        low = text.lower()

        return bool(
            re.search(r"\bdut(?:y|ies)\b|\binvigilat", low)
            and (duty_history.parse_duty_limit(text) or _ASSIGN.search(low))
        )

    def _history_command(self, text: str) -> Optional[str]:
        """"clear the duty history" / "show the duty sheet"."""

        base = self._baseline()

        if base is None:
            return None

        lex = duty_history.lexicon()
        low = text.lower()

        if not any(w in low for w in lex["history_words"]):
            return None

        if any(re.search(rf"\b{re.escape(w)}\b", low) for w in lex["clear_words"]):
            return (
                "Removed the uploaded duty sheet — counts now come only from "
                "duties confirmed in this chat."
                if base.clear() else "No duty sheet is loaded."
            )

        if not base.exists():
            return (
                "No duty sheet is loaded. Upload one (a PDF / Excel / CSV with "
                "teacher names and their exam-duty counts) in the sidebar."
            )

        self._sync_history()

        counts = self.allocator.stored_counts()
        info = base.info()

        rows = sorted(base.counts().items(), key=lambda kv: (kv[1], kv[0].lower()))

        lines = [
            f"**Duty sheet loaded** — {info.get('people')} teachers, read as "
            f"{info.get('mode') or 'auto-detected'}.",
            "",
            "| Faculty | Duties in sheet | Total now |",
            "| --- | --- | --- |",
        ]

        for name, n in rows:
            lines.append(f"| {name} | {int(n)} | {counts.get(_norm(name), int(n))} |")

        return "\n".join(lines + self._history_notes())

    def _limit_report(self, req: ParsedRequest) -> str:
        """Who satisfies a duty-count constraint (no date needed)."""

        self._sync_history()

        counts = self.allocator.stored_counts()

        everyone = self._faculty_names()

        rows = sorted(
            ((counts.get(_norm(t), 0), t) for t in everyone
             if duty_history.passes_limit(counts.get(_norm(t), 0), req.limit)),
            key=lambda r: (r[0], r[1].lower()),
        )

        lines = [
            f"**Faculty with exam duties {duty_history.limit_text(*req.limit)}** "
            f"· {len(rows)} of {len(everyone)}",
            "",
        ]

        if rows:
            lines += ["| # | Faculty | Department | Duties so far |", "| --- | --- | --- | --- |"]
            for n, (c, t) in enumerate(rows, 1):
                lines.append(f"| {n} | {t} | {self._department(t)} | {int(c)} |")
        else:
            lines.append("Nobody satisfies that condition.")

        lines += self._history_notes()

        lines += ["", "*To assign them, add the date, time and how many are needed, "
                      "e.g. “assign 6 faculty on <date> from <time> to <time> …”.*"]

        return "\n".join(lines)

    # ---------------- 1) who is eligible ------------------------------

    def _eligible(self, req: ParsedRequest) -> str:
        """
        Who is free for exam duty - for ONE or SEVERAL sessions (any number of
        dates x time windows).  One clean table per session, then who can cover
        every session and who only fits some.
        """

        ask = self._need_details(req, "list eligible faculty")

        if ask:
            return ask

        self._sync_history()

        counts = self.allocator.stored_counts()
        store = self.coordinator.store
        has_sheet = bool(getattr(store, "baseline", None) and store.baseline.exists())

        sessions = self._session_windows(req)

        per_session = []          # [(label, {norm: row}, cand, dropped)]

        for day, start, end in sessions:

            cand = self.coordinator.candidates(day.isoformat(), start, end)

            if not cand.get("success"):
                return f"I couldn't check availability ({cand.get('reason')})."

            results = [
                c for c in cand["results"]
                if _norm(c["teacher"]) not in req.excluded
            ]

            dropped = 0

            if req.limit:
                kept = [
                    c for c in results
                    if duty_history.passes_limit(
                        counts.get(_norm(c["teacher"]), 0), req.limit)
                ]
                dropped = len(results) - len(kept)
                results = kept

            rows = {}

            for item in results:

                teacher = item["teacher"]
                slots, _ = self._classes_that_day(teacher, cand["day"], start, end)
                total = counts.get(_norm(teacher), 0)

                rows[_norm(teacher)] = {
                    "teacher": teacher,
                    "dept": self._department(teacher),
                    "classes": len(slots),
                    "why": (
                        f"free {cand['availability_start']}–{cand['availability_end']}; "
                        + (f"{len(slots)} class{'es' if len(slots) != 1 else ''} that day"
                           if slots else "no classes that day")
                    ),
                    "total": total,
                    "split": store.duty_breakdown(teacher) if has_sheet else None,
                }

            per_session.append((
                f"{self._pretty_day(day)}, {start}–{end}", rows, cand, dropped
            ))

        def ordered(rows):
            return sorted(
                rows.values(),
                key=lambda r: (r["total"], r["classes"], r["teacher"].lower()),
            )

        many = len(per_session) > 1

        if many:
            title = f"**Faculty eligible for exam duty** · {len(per_session)} sessions"
        else:
            title = (
                f"**Faculty eligible for exam duty — {per_session[0][0]}** · "
                f"{len(per_session[0][1])} found"
            )

        lines = [title, ""]

        first_cand = per_session[0][2]

        lines += [
            f"*A teacher is listed only if free for the whole duty window plus a "
            f"{first_cand['buffer_minutes']}-minute buffer on both sides, with no "
            f"overlapping confirmed exam duty. Fewest exam duties first.*",
            "",
        ]

        if req.limit:
            lines += [
                f"*Duty-count rule applied: exam duties so far "
                f"{duty_history.limit_text(*req.limit)}.*",
                "",
            ]

        any_unknown_dept = False

        for label, rows, cand, dropped in per_session:

            lines += [f"### {label} — {len(rows)} free", ""]

            if not rows:
                lines += ["Nobody is eligible for this window.", ""]
                continue

            if has_sheet:
                lines += [
                    "| # | Faculty | Department | Why eligible | On duty sheet | Confirmed since upload | Total duties |",
                    "| --- | --- | --- | --- | --- | --- | --- |",
                ]
            else:
                lines += [
                    "| # | Faculty | Department | Why eligible | Exam duties |",
                    "| --- | --- | --- | --- | --- |",
                ]

            for n, r in enumerate(ordered(rows), 1):

                any_unknown_dept |= r["dept"] == "—"

                if has_sheet:
                    lines.append(
                        f"| {n} | {r['teacher']} | {r['dept']} | {r['why']} | "
                        f"{r['split'][0]} | {r['split'][1]} | **{r['total']}** |"
                    )
                else:
                    lines.append(
                        f"| {n} | {r['teacher']} | {r['dept']} | {r['why']} | "
                        f"**{r['total']}** |"
                    )

            if req.limit and dropped:
                lines += ["", f"*{dropped} otherwise-free faculty were left out by the duty-count rule.*"]

            lines.append("")

        # ---- who covers every session / only some ----
        if many:

            common = set(per_session[0][1])
            for _, rows, _, _ in per_session[1:]:
                common &= set(rows)

            everyone = {}
            for _, rows, _, _ in per_session:
                everyone.update(rows)

            lines += ["### Across all sessions", ""]

            lines.append(
                f"- Free in **every** session ({len(common)}): "
                + (", ".join(everyone[k]["teacher"] for k in sorted(common, key=lambda k: (everyone[k]["total"], k)))
                   if common else "nobody")
            )

            for label, rows, _, _ in per_session:
                only = [k for k in rows if any(k not in other for _, other, _, _ in per_session)]
                lines.append(f"- Free for **{label}** but not every session: {len(only)}")

            lines += [
                "",
                "*To give each person only one of these sessions, ask e.g. “assign "
                "6 faculty per session, nobody in both”.*",
                "",
            ]

        if any_unknown_dept:
            lines += [
                "*“—” = department unknown (the teacher has no classes in the "
                "timetable to read a branch from).*",
                "",
            ]

        lines += self._exclusion_lines(req)

        lines += self._history_notes()

        saved = store.confirmed_summary()

        if has_sheet:

            lines += [
                "",
                "*Total duties = duties on the uploaded sheet + duties confirmed in "
                "chat after the sheet was uploaded"
                + (f" ({sum(x[3] for x in saved)} so far)." if saved else " (none so far).")
                + " Duties saved before the sheet was uploaded are not added to it.*",
            ]

        elif saved:

            lines += [
                "",
                f"*Exam duties counted: {sum(x[3] for x in saved)} saved in "
                f"`{store.path}`. Say “clear confirmed exam duties” to remove them.*",
            ]

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
        on_duty = {
            c["teacher"] for c in cand.get("conflicts", [])
            if c.get("reason") != "marked_unavailable"
        }
        marked = {
            c["teacher"] for c in cand.get("conflicts", [])
            if c.get("reason") == "marked_unavailable"
        }
        left_out = {
            t for t in everyone
            if _norm(t) in (req.excluded or set())
        }

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

        other = everyone - eligible - on_duty - teaching - marked - left_out

        parts = [
            f"{len(teaching - on_duty - marked - left_out)} teaching in that window or its buffer",
            f"{len(on_duty)} already on a confirmed exam duty then",
        ]

        if marked:
            parts.append(f"{len(marked)} marked unavailable")

        if left_out:
            parts.append(f"{len(left_out)} left out as requested")

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

        # ---- a date and/or a duty-count rule given, the rest missing ----
        if (req.dates or req.limit) and (not req.dates or not req.start):

            self._sync_history()

            have, need = [], []

            if req.dates:
                have.append(f"the date **{self._pretty_day(req.dates[0])}**")
            else:
                need.append("the exam **date(s)**, e.g. *12th November*")

            if req.limit:
                counts = self.allocator.stored_counts()
                ok = sum(
                    1 for t in self._faculty_names()
                    if duty_history.passes_limit(counts.get(_norm(t), 0), req.limit)
                )
                have.append(
                    f"the rule **exam duties so far "
                    f"{duty_history.limit_text(*req.limit)}** "
                    f"({ok} faculty satisfy it right now)"
                )

            if not req.start:
                need.append("the **time**, e.g. *9:00 AM to 12:00 PM*")

            if not (req.per_room or req.total or req.rooms or req.room_count):
                need.append(
                    "**how many** faculty are needed (e.g. *6 faculty*, or "
                    "*5 rooms A101–A105 with 2 invigilators each*)"
                )

            lines = [
                "I have " + " and ".join(have) + "." if have else "",
                "To build the proposal I still need:",
                "",
            ] + [f"- {item}" for item in need]

            return "\n".join(x for x in lines if x is not None)

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

        # ---- nothing about rooms said: reuse the last setup, if any ----
        said_setup = bool(
            req.rooms or req.room_count or req.per_room or req.total
        )

        if not said_setup and self.last_setup:

            req.rooms = list(self.last_setup["rooms"])
            req.room_count = self.last_setup["room_count"]
            req.per_room = self.last_setup["per_room"]
            req.total = self.last_setup["total"]

            req.setup_reused = self.last_setup["text"]

        # ---- how many seats per session ----
        rooms = list(req.rooms)

        if rooms and req.room_count and len(rooms) < req.room_count:

            return (
                f"You mentioned {req.room_count} rooms but I could only read "
                f"{len(rooms)} room name{'s' if len(rooms) != 1 else ''} "
                f"({', '.join(rooms)}). Please list every room, separated by "
                f"commas — for example *A101, A102, A103* — or give a range "
                f"such as *A101–A105*."
            )

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

            return self._ask_headcount(req)

        windows = req.times or [(req.start, req.end)]

        sessions = [(d, s, e) for d in req.dates for (s, e) in windows]

        plan = self.allocator.plan(
            sessions,
            slots,
            fair_first=req.fair_first,
            avoid_consecutive=True,
            excluded=req.excluded,
            limit=req.limit,
            distinct_per_day=req.distinct_per_day,
        )

        if not plan.get("success"):
            return f"I couldn't build the allocation ({plan.get('reason')})."

        plan["rooms"] = rooms
        plan["per_room"] = per_room

        if rooms or req.total:

            if rooms and per_room:
                text = f"{len(rooms)} rooms × {per_room} invigilators"
            elif rooms:
                text = f"{len(rooms)} rooms, {req.total} invigilators"
            else:
                text = f"{req.total} invigilators"

            self.last_setup = {
                "rooms": list(rooms),
                "room_count": req.room_count,
                "per_room": per_room,
                "total": req.total,
                "text": text,
            }

        assigned = sum(len(s["assignments"]) for s in plan["sessions"])

        if not assigned:

            why = ""

            if req.limit:
                gone = sum(x.get("limit_removed", 0) for x in plan["sessions"])
                why = (
                    f" The rule “exam duties so far "
                    f"{duty_history.limit_text(*req.limit)}” alone removed "
                    f"{gone} otherwise-free faculty - try a different number."
                )

            return (
                "Nobody is eligible for that time, so no duties could be "
                "allocated. Try another time or date." + why
            )

        self.pending = plan

        return self._render_plan(plan, req)

    # ---------------- rendering ---------------------------------------

    def _render_plan(self, plan: Dict[str, Any], req: ParsedRequest) -> str:

        sessions = plan["sessions"]

        multi = len(sessions) > 1

        head_dates = ", ".join(dict.fromkeys(
            date_resolver.pretty(date.fromisoformat(s["exam_date"]))
            for s in sessions
        ))

        windows = list(dict.fromkeys(
            f"{s['start_time']}–{s['end_time']}" for s in sessions
        ))

        if len(windows) == 1:
            free_text = (
                f"Everyone listed is free from {sessions[0]['availability_start']} "
                f"to {sessions[0]['availability_end']} "
                f"({sessions[0]['buffer_minutes']}-minute buffer before and after "
                f"the duty)"
            )
        else:
            free_text = (
                f"Everyone listed is free for their whole session "
                f"({sessions[0]['buffer_minutes']}-minute buffer before and after "
                f"each duty)"
            )

        lines = [
            f"**Proposed exam-duty allocation — {head_dates}, "
            f"{' and '.join(windows)}**",
            "",
            f"*{free_text}, is not in two rooms at once, and has no clashing "
            f"confirmed exam duty."
            + (" Faculty with fewer exam duties were preferred."
               if plan["fair_first"] else "")
            + "*",
            "",
        ]

        if plan.get("limit"):

            op, value = plan["limit"]
            removed = max(s.get("limit_removed", 0) for s in sessions)
            kept = min(s.get("eligible_after_limit", 0) for s in sessions)

            lines += [
                f"**Duty-count rule:** only faculty with total exam duties "
                f"**{duty_history.limit_text(op, value)}** were considered "
                f"(uploaded duty sheet + confirmed duties + this plan). "
                f"It removed {removed} otherwise-free faculty and left "
                f"{kept} eligible.",
                "",
            ]

        if plan.get("distinct_per_day") and len(windows) > 1:
            lines += [
                "**Each faculty member appears in only one session per day** "
                "(nobody is on duty at both timings).",
                "",
            ]

        rooms = plan.get("rooms") or []

        if req.setup_reused:
            lines += [
                f"*Using the same room setup as before: {req.setup_reused} "
                f"(rooms: {', '.join(rooms) if rooms else 'not named'}).*",
                "",
            ]

        for sess in sessions:

            day = date.fromisoformat(sess["exam_date"])

            if multi:
                lines += [
                    f"**{self._pretty_day(day)}, "
                    f"{sess['start_time']}–{sess['end_time']}**",
                    "",
                ]

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

            if sess["shortage"] and rooms:

                short = defaultdict(int)

                for room in sess["shortage"]:
                    short[room] += 1

                lines += [
                    "",
                    "**Unfilled rooms:** " + "; ".join(
                        f"{room} (needs {n} more)" for room, n in short.items()
                    ),
                ]

            elif rooms:

                lines += ["", "**Unfilled rooms:** none — every room is fully staffed."]

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

        lines += self._exclusion_lines(req)

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