"""
date_resolver.py - understands REAL CALENDAR DATES in chat questions
====================================================================

The timetable only knows recurring weekdays (Monday ... Saturday).  People
ask about real dates:  "who is free on 15th October?",  "assign exam duty
on 12/10/26",  "is Dr. X free next Monday?".  This module finds such dates
in a question and works out - from the real calendar and TODAY'S date -
which weekday they fall on, so every feature of the chatbot can answer.

Nothing is hard-coded: no year, no date, no weekday of any date.  The year
of a date written without one comes from today's date, and the weekday
comes from Python's own calendar.

Understood (examples):

    2026-10-15   2026/10/15   15-10-2026   15/10/2026   15.10.26   12/10/26
    15 October   15th October   15th of October   October 15   Oct 15th
    15 oct 2026  15-oct-2026   Oct 15, 2026   15th Oct '26
    15th and 16th October      15th to 17th October      15, 16 & 17 Oct
    the 15th of this month     15th of next month        on the 15th
    tomorrow   day after tomorrow   in 3 days   3 days later
    next Monday   this Friday   coming Tuesday   last Wednesday

Rules for dates written without a year:  the year (and month, for "the
15th") is taken from the system clock - "15th October" typed in 2026 means
15 October 2026, typed in 2027 it means 2027.  Add a year ("15th October
2025") to ask about another one.  Numeric dates are read day-first
(12/10/26 = 12 October 2026) unless the first number is impossible as a
day (10/25/26 = 25 October) or the environment variable FACULTY_DATE_ORDER
is set to "mdy".

Public functions:

    find_dates(text, today=None)  -> [DateHit, ...]
    rewrite(text, hits, mode="smart" | "legacy", with_iso=False) -> str
    note(hits, today=None)        -> one-line explanation for the answer
    first_iso(text, today=None)   -> "YYYY-MM-DD" of the first date, or None
"""

from __future__ import annotations

import calendar
import datetime
import difflib
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

DAY_NAMES = [
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
]

_WEEKDAY_WORDS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tues": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thurs": 3, "thur": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAY_RE = "|".join(sorted(_WEEKDAY_WORDS, key=len, reverse=True))
_ORD = r"(?:\s{0,2}(?:st|nd|rd|th)(?![a-z]))"

# words that show a number+ordinal is NOT a day of the month
_NOT_A_DATE_AFTER = (
    r"(?!\s*(?:slots?|periods?|lectures?|lecs?|hours?|class(?:es)?|floor|"
    r"sem(?:ester)?s?|years?|half|week|batch|section|row|rank|position))"
)

_RANGE_WORDS = {"-", "–", "to", "till", "until", "through"}



# long month names that may be mistyped ("octobar", "novmber", "septmber")
_FUZZY_MONTHS = {
    "january": 1, "february": 2, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}


def _month_of(token: str) -> Optional[int]:
    """Month number for a word - exact, abbreviated or slightly misspelt."""

    token = token.strip(".").lower()

    if token in _MONTHS:
        return _MONTHS[token]

    if len(token) >= 6:

        close = difflib.get_close_matches(
            token, list(_FUZZY_MONTHS), n=1, cutoff=0.85
        )

        # same first three letters keeps ordinary words ("members",
        # "decide", "marking") from being mistaken for months
        if close and token[:3] == close[0][:3]:
            return _FUZZY_MONTHS[close[0]]

    return None


# ======================================================================
# result type
# ======================================================================

@dataclass
class DateHit:
    start: int
    end: int
    dates: List[datetime.date]
    kind: str = "absolute"          # absolute | relative | basic
    year_assumed: bool = False
    text: str = ""
    is_list: bool = field(default=False)


# ======================================================================
# helpers
# ======================================================================

def _as_date(value) -> datetime.date:

    if value is None:
        return datetime.date.today()

    if isinstance(value, datetime.datetime):
        return value.date()

    return value


def _valid(year: int, month: int, day: int) -> Optional[datetime.date]:

    try:
        return datetime.date(year, month, day)
    except (ValueError, OverflowError):
        return None


def _upcoming(month: int, day: int, today: datetime.date):
    """
    The date with this month/day in the CURRENT year (the year comes
    from the system clock, never from a fixed number).  Only when that
    date does not exist - 29 February in a non-leap year - the next year
    that has it is used.
    """

    for year in range(today.year, today.year + 9):

        found = _valid(year, month, day)

        if found:
            return found

    return None


def _upcoming_day_of_month(day: int, today: datetime.date):
    """
    "the 15th" -> the 15th of the CURRENT month (from the system clock);
    if this month has no such day (31st in a 30-day month) the next
    month that has it.
    """

    year, month = today.year, today.month

    for _ in range(24):

        found = _valid(year, month, day)

        if found:
            return found

        month += 1

        if month > 12:
            month, year = 1, year + 1

    return None


def _shift_month(today: datetime.date, delta: int):

    month_index = today.year * 12 + (today.month - 1) + delta

    return month_index // 12, month_index % 12 + 1


def _two_digit_year(value: int) -> int:

    return 2000 + value if value < 100 else value


def _mask(text: str) -> str:
    """
    Blank out things that look like dates but are slot lists or clock
    times ("slots 2 to 4", "3rd and 4th lecture", "09:15"), keeping the
    string length so match positions stay valid.
    """

    def blank(match):
        return " " * (match.end() - match.start())

    patterns = [
        r"\b(?:slots?|periods?|lectures?|lecs?|hours?)\s*(?:no\.?|number|#)?"
        r"\s*\d{1,2}(?:\s*(?:,|&|and|or|to|-|–|till|until|through)\s*"
        r"(?:(?:slots?|periods?|lectures?)\s*)?\d{1,2})*",

        rf"\b\d{{1,2}}(?:{_ORD})?(?:\s*(?:,|&|and|or|to|-|–|till|until|"
        rf"through)\s*\d{{1,2}}(?:{_ORD})?)*\s*"
        rf"(?:slots?|periods?|lectures?|lecs?|hours?)\b",

        r"(?<![\d.])\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?",
    ]

    for pattern in patterns:
        text = re.sub(pattern, blank, text)

    return text


def _numeric_order() -> str:

    return "mdy" if os.environ.get(
        "FACULTY_DATE_ORDER", "dmy"
    ).strip().lower() == "mdy" else "dmy"


# ======================================================================
# finders (each returns a list of DateHit)
# ======================================================================

def _find_iso(text, today):

    hits = []

    for m in re.finditer(
        r"(?<![\d/.\-])(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)", text
    ):
        found = _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))

        if found:
            hits.append(DateHit(m.start(), m.end(), [found]))

    return hits


def _find_numeric(text, today):

    hits = []

    for m in re.finditer(
        r"(?<![\d/.:\-])(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4}|\d{2})(?![\d:/])",
        text,
    ):
        first, second, year = (int(g) for g in m.groups())

        year = _two_digit_year(year)

        if first > 12 and second <= 12:
            day, month = first, second
        elif second > 12 and first <= 12:
            day, month = second, first
        elif _numeric_order() == "mdy":
            day, month = second, first
        else:
            day, month = first, second

        found = _valid(year, month, day)

        if found:
            hits.append(DateHit(m.start(), m.end(), [found]))

    return hits


def _may_is_a_month(text, match, has_year, has_ordinal) -> bool:
    """'may' is also a verb ("may I know ...") - accept only clear dates."""

    if has_year:
        return True

    after = text[match.end():].lstrip().split(" ")[:1]

    if after and after[0].strip("?,.!") in {
        "i", "we", "you", "he", "she", "they", "it", "be", "not", "have",
        "also", "please", "know", "need", "get", "ask", "take", "see",
        "just", "help", "like", "want", "kindly", "tell", "check", "give",
        "show", "find", "assign", "do", "is", "are", "come", "go", "can",
    }:
        return False

    if has_ordinal:
        return True

    chunk = match.group(0)

    if re.search(r"\bof\b", chunk):
        return True

    before = text[:match.start()].rstrip().split(" ")[-1:]

    return bool(before) and before[0] in {
        "on", "for", "by", "from", "till", "until", "to", "dated",
        "between", "and",
    }


def _find_text_dmy(text, today):
    """15 October [2026] / 15th of October / 12 th oct / 12thoct / 15-oct-26"""

    hits = []

    pattern = (
        rf"(?<![\d:.])(\d{{1,2}})(\s{{0,2}}(?:st|nd|rd|th))?(?:\s+of\s+|[\s\-/.]*)"
        rf"([a-z]{{3,10}})(?![a-z])\.?"
        rf"(?:[\s,\-/.]*(?:(\d{{4}})\b|'(\d{{2}})\b))?"
    )

    for m in re.finditer(pattern, text):

        month = _month_of(m.group(3))

        if not month:
            continue

        day = int(m.group(1))

        year = m.group(4) or m.group(5)

        if month == 5 and m.group(3) == "may" and not _may_is_a_month(
            text, m, bool(year), bool(m.group(2))
        ):
            continue

        if year:
            found = _valid(_two_digit_year(int(year)), month, day)
            assumed = False
        else:
            found = _upcoming(month, day, today)
            assumed = True

        if found:
            hits.append(DateHit(m.start(), m.end(), [found],
                                year_assumed=assumed))

    return hits


def _find_text_mdy(text, today):
    """October 15 [2026] / Oct 15th, 2026 / oct12 / oct 12 th"""

    hits = []

    pattern = (
        rf"(?<![a-z\d])([a-z]{{3,10}})(?![a-z])\.?[\s\-/.]*(\d{{1,2}})"
        rf"({_ORD})?(?![\w:])(?:[\s,]+(\d{{4}})\b)?"
    )

    for m in re.finditer(pattern, text):

        month = _month_of(m.group(1))

        if not month:
            continue

        day = int(m.group(2))
        year = m.group(4)

        if m.group(1) == "may" and not _may_is_a_month(
            text, m, bool(year), bool(m.group(3))
        ):
            continue

        if year:
            found = _valid(int(year), month, day)
            assumed = False
        else:
            found = _upcoming(month, day, today)
            assumed = True

        if found:
            hits.append(DateHit(m.start(), m.end(), [found],
                                year_assumed=assumed))

    return hits


def _find_day_lists(text, today):
    """15th to 17th October / 15th and 16th Oct / 15, 16 & 17 October"""

    hits = []

    pattern = (
        rf"(?<![\d:.])(\d{{1,2}}(?:{_ORD})?"
        rf"(?:\s*(?:-|–|to|till|until|through|,|and|&)\s*"
        rf"\d{{1,2}}(?:{_ORD})?)+)"
        rf"\s*(?:of\s+)?([a-z]{{3,10}})(?![a-z])\.?(?:[\s,]+(\d{{4}})\b)?"
    )

    for m in re.finditer(pattern, text):

        month = _month_of(m.group(2))

        if not month:
            continue

        if m.group(2) == "may" and not _may_is_a_month(
            text, m, bool(m.group(3)), bool(re.search(_ORD, m.group(1)))
        ):
            continue

        year = m.group(3)

        tokens = re.findall(r"(\d{1,2})|(-|–|to|till|until|through|,|and|&)",
                            m.group(1))

        days, pending_range = [], False

        for number, connector in tokens:

            if number:

                value = int(number)

                if pending_range and days and value >= days[-1]:
                    days.extend(range(days[-1] + 1, value + 1))
                else:
                    days.append(value)

                pending_range = False

            elif connector in _RANGE_WORDS:
                pending_range = True

        if not days or len(days) > 31:
            continue

        base_year = int(year) if year else None
        dates = []

        for day in days:

            if base_year:
                found = _valid(base_year, month, day)
            else:
                found = _upcoming(month, day, today)

            if found:
                dates.append(found)

        if len(dates) >= 2:
            hits.append(DateHit(
                m.start(), m.end(), dates,
                year_assumed=not year, is_list=True,
            ))

    return hits


def _find_date_ranges(text, today):
    """10 Nov to 12 Nov / 28 Oct - 2 Nov 2026 -> every day in between"""

    hits = []

    pattern = (
        rf"(?<![\d:.])(\d{{1,2}})(?:{_ORD})?[\s\-/.]*([a-z]{{3,10}})(?![a-z])\.?"
        rf"(?:[\s,]+(\d{{4}})\b)?\s*(?:-|–|—|to|till|until|through)\s*"
        rf"(\d{{1,2}})(?:{_ORD})?[\s\-/.]*([a-z]{{3,10}})(?![a-z])\.?"
        rf"(?:[\s,]+(\d{{4}})\b)?"
    )

    for m in re.finditer(pattern, text):

        month_a, month_b = _month_of(m.group(2)), _month_of(m.group(5))

        if not month_a or not month_b:
            continue

        year_b = int(m.group(6)) if m.group(6) else None
        year_a = int(m.group(3)) if m.group(3) else year_b

        start = (
            _valid(year_a, month_a, int(m.group(1))) if year_a
            else _upcoming(month_a, int(m.group(1)), today)
        )

        if year_b:
            end = _valid(year_b, month_b, int(m.group(4)))
        elif start:
            end = _valid(start.year, month_b, int(m.group(4)))
            if end and end < start:
                end = _valid(start.year + 1, month_b, int(m.group(4)))
        else:
            end = None

        if not start or not end or end < start or (end - start).days > 60:
            continue

        days = [
            start + datetime.timedelta(days=n)
            for n in range((end - start).days + 1)
        ]

        hits.append(DateHit(
            m.start(), m.end(), days,
            year_assumed=not (m.group(3) or m.group(6)), is_list=True,
        ))

    return hits


def _find_ordinal_only(text, today):
    """'on the 15th' / 'for 15th' -> the next 15th of a month"""

    hits = []

    patterns = [
        rf"(?<!\w)(?:on|for|by|dated)\s+((?:the\s+)?(\d{{1,2}}){_ORD})"
        rf"(?!\w){_NOT_A_DATE_AFTER}",
        rf"(?<!\w)(the\s+(\d{{1,2}}){_ORD})(?!\w){_NOT_A_DATE_AFTER}",
    ]

    for pattern in patterns:

        for m in re.finditer(pattern, text):

            day = int(m.group(2))

            if not 1 <= day <= 31:
                continue

            tail = text[m.end():].lstrip()

            # "the 3rd of October" is handled by the dmy finder
            if re.match(r"of\s+(?:" + _MONTH_RE + r"|this|next|last)\b", tail):
                continue

            found = _upcoming_day_of_month(day, today)

            if found:
                hits.append(DateHit(m.start(1), m.end(1), [found],
                                    year_assumed=True))

    return hits


def _find_month_relative(text, today):
    """15th of this month / next month / last month"""

    hits = []

    pattern = (
        rf"(?<![\d:.])(\d{{1,2}})(?:{_ORD})?\s+of\s+"
        r"(this|current|next|coming|last|previous)\s+month\b"
    )

    delta = {"this": 0, "current": 0, "next": 1, "coming": 1,
             "last": -1, "previous": -1}

    for m in re.finditer(pattern, text):

        year, month = _shift_month(today, delta[m.group(2)])

        found = _valid(year, month, int(m.group(1)))

        if found:
            hits.append(DateHit(m.start(), m.end(), [found]))

    return hits


def _find_relative(text, today):

    hits = []

    def add(m, days, kind="relative"):
        hits.append(DateHit(
            m.start(), m.end(),
            [today + datetime.timedelta(days=days)], kind=kind,
        ))

    for m in re.finditer(r"\bday\s+after\s+(?:tomorrow|tmrw|tomorow)\b", text):
        add(m, 2)

    for m in re.finditer(r"\bday\s+before\s+yesterday\b", text):
        add(m, -2)

    for m in re.finditer(
        r"\b(?:in|after)\s+(\d{1,3})\s+days?(?:\s+(?:time|from\s+now))?\b",
        text,
    ):
        add(m, int(m.group(1)))

    for m in re.finditer(
        r"\b(\d{1,3})\s+days?\s+(?:later|from\s+now|ahead)\b", text
    ):
        add(m, int(m.group(1)))

    for m in re.finditer(
        rf"\b(next|this|coming|upcoming|last|previous)\s+({_WEEKDAY_RE})\b",
        text,
    ):
        target = _WEEKDAY_WORDS[m.group(2)]
        ahead = (target - today.weekday()) % 7

        word = m.group(1)

        if word == "this":
            offset = ahead
        elif word in ("next", "coming", "upcoming"):
            offset = ahead or 7
        else:
            offset = -((today.weekday() - target) % 7 or 7)

        add(m, offset)

    for m in re.finditer(r"\b(today|tomorrow|tmrw|tomorow|yesterday)\b", text):

        offset = {"today": 0, "yesterday": -1}.get(m.group(1), 1)

        add(m, offset, kind="basic")

    return hits


# ======================================================================
# public API
# ======================================================================

def find_dates(text, today=None) -> List[DateHit]:
    """All dates mentioned in `text`, left to right, without overlaps."""

    today = _as_date(today)

    original = str(text or "")
    work = _mask(original.lower())

    hits: List[DateHit] = []

    for finder in (
        _find_iso, _find_numeric, _find_date_ranges, _find_day_lists,
        _find_text_dmy,
        _find_text_mdy, _find_month_relative, _find_ordinal_only,
        _find_relative,
    ):
        hits.extend(finder(work, today))

    # earliest first; at the same start prefer the longest match
    hits.sort(key=lambda h: (h.start, -(h.end - h.start)))

    chosen, last_end = [], -1

    for hit in hits:

        if hit.start >= last_end:
            hit.text = original[hit.start:hit.end]
            chosen.append(hit)
            last_end = hit.end

    return chosen


def weekday_name(date_value: datetime.date) -> str:

    return DAY_NAMES[date_value.weekday()]


def _day_words(hit: DateHit) -> str:

    names = []

    for item in hit.dates:

        name = weekday_name(item)

        if name not in names:
            names.append(name)

    if len(names) == 1:
        return names[0]

    return ", ".join(names[:-1]) + " and " + names[-1]


def rewrite(text, hits, mode="smart", with_iso=False) -> str:
    """
    Replace every date by its weekday name.

      mode="smart":   plain weekday words ("on thursday"); bare
                      today/tomorrow/yesterday are left alone because the
                      smart engine already understands them.
      mode="legacy":  also rewrites today/tomorrow/yesterday.
      with_iso=True:  a single date becomes "thursday 2026-10-15" so the
                      exam-duty planner (which needs the real date) still
                      receives it.
    """

    pieces, position = [], 0

    for hit in hits:

        if hit.kind == "basic" and mode == "smart":
            continue

        pieces.append(text[position:hit.start])

        words = _day_words(hit)

        if with_iso and len(hit.dates) == 1:
            words = f"{words} {hit.dates[0].isoformat()}"

        pieces.append(words)

        position = hit.end

    pieces.append(text[position:])

    return "".join(pieces)


def pretty(date_value: datetime.date) -> str:

    return (
        f"{date_value.day} {calendar.month_name[date_value.month]} "
        f"{date_value.year}"
    )


def note(hits, today=None) -> str:
    """One markdown line telling the user how the date(s) were read."""

    today = _as_date(today)

    shown = [h for h in hits if h.kind != "basic"]

    if not shown:
        return ""

    parts, assumed_years = [], []

    for hit in shown:

        for item in hit.dates:
            parts.append(
                f"{pretty(item)} is a {weekday_name(item).title()}"
                + (" (already past)" if item < today else "")
            )

        if hit.year_assumed and hit.kind == "absolute":
            assumed_years.extend(d.year for d in hit.dates)

    text = "; ".join(parts)

    if assumed_years:
        text += (
            f" — year taken as {sorted(set(assumed_years))[0]}; "
            f"add a year if you meant another"
        )

    return f"*{text}.*"


def first_iso(text, today=None) -> Optional[str]:
    """ISO date of the first date in `text`, or None."""

    hits = find_dates(text, today)

    return hits[0].dates[0].isoformat() if hits else None

def find_impossible_dates(text, today=None):
    """
    Dates that were written but do not exist ("31 Nov", "30 February 2026").

    find_dates() silently skips them, so a request containing one would look as
    if it had no date at all.  Returns [(written_text, day, month, year, last_day)].
    """

    import calendar

    today = _as_date(today)
    low = str(text or "").lower()

    found = []

    patterns = (
        # 31 nov [2026] / 31st of november
        rf"(?<![\d/.-])(\d{{1,2}})(?:\s{{0,2}}(?:st|nd|rd|th))?\s*(?:of\s+)?({_MONTH_RE})\b\.?(?:\s*,?\s*(\d{{4}}))?",
        # nov 31 [2026]
        rf"\b({_MONTH_RE})\b\.?\s*(\d{{1,2}})(?:\s{{0,2}}(?:st|nd|rd|th))?(?!\d)(?:\s*,?\s*(\d{{4}}))?",
    )

    for i, pattern in enumerate(patterns):

        for m in re.finditer(pattern, low):

            if i == 0:
                day, month_word, year = m.group(1), m.group(2), m.group(3)
            else:
                month_word, day, year = m.group(1), m.group(2), m.group(3)

            month = _MONTHS.get(month_word)
            day = int(day)

            if not month or day < 1:
                continue

            yr = int(year) if year else today.year

            last = calendar.monthrange(yr, month)[1]

            if day > last:
                found.append((original_slice(text, m), day, month, yr, last))

    return found


def original_slice(text, match):
    return str(text)[match.start():match.end()].strip()