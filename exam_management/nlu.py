"""
Natural language -> structured requests.

Principles
  * Faculty names are NEVER taken from the text as-is: they are matched against the
    roster (database).  Unknown / ambiguous names are reported, not guessed.
  * Each person's restriction is parsed from the text around THAT person only.
  * Nothing is invented: a missing value is returned in `missing` so the chatbot asks.
  * No department, person or room is hard-coded; vocabulary here is only language
    (months, number words, verbs).
"""

from __future__ import annotations

import calendar
import difflib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9
WEEKDAYS = {d.lower() for d in list(calendar.day_name) + list(calendar.day_abbr)}
NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
GENERIC_TITLES = {"dr", "prof", "professor", "mr", "mrs", "ms", "miss", "sir", "madam", "shri", "smt",
                  "er", "ar", "doctor"}
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))
_ORD = r"(?:st|nd|rd|th)?"
_SEP = r"(?:-|–|—|to|till|until|through|thru)"


def to_int(tok: str) -> Optional[int]:
    tok = tok.lower().strip()
    if tok.isdigit():
        return int(tok)
    return NUMWORDS.get(tok)


# ===========================================================================
# dates
# ===========================================================================

@dataclass
class DateHit:
    start: int
    end: int
    first: date
    last: date
    is_range: bool = False
    open_ended: bool = False       # "from 16 November" (no end)


class DateParser:

    def __init__(self, today: date, year_hint: Optional[int] = None, window_days: int = 183):
        self.today = today
        self.year_hint = year_hint
        self.window = window_days

    def _make(self, d: int, m: int, y: Optional[int]) -> Optional[date]:
        try:
            if y is not None:
                if y < 100:
                    y += 2000
                return date(y, m, d)
            year = self.year_hint or self.today.year
            cand = date(year, m, d)
            if self.year_hint is None and (self.today - cand).days > self.window:
                cand = date(year + 1, m, d)
            return cand
        except ValueError:
            return None

    def find(self, text: str) -> List[DateHit]:
        low = text.lower()
        singles: List[Tuple[int, int, date]] = []
        taken: List[Tuple[int, int]] = []

        def free(a: int, b: int) -> bool:
            return all(b <= s or a >= e for s, e in taken)

        # 1) "12-15 November", "12 to 15 Nov 2026" (compressed day range)
        out: List[DateHit] = []
        for m in re.finditer(rf"\b(\d{{1,2}}){_ORD}\s*{_SEP}\s*(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MON})\b\.?(?:,?\s*(\d{{4}}))?", low):
            a, b, mo = int(m.group(1)), int(m.group(2)), MONTHS[m.group(3)]
            y = int(m.group(4)) if m.group(4) else None
            d1, d2 = self._make(a, mo, y), self._make(b, mo, y)
            if d1 and d2 and d1 <= d2:
                out.append(DateHit(m.start(), m.end(), d1, d2, True))
                taken.append((m.start(), m.end()))
        # 1b) "10th, 11th and 12th November 2026": several days sharing ONE month written after the last
        for m in re.finditer(rf"\b((?:\d{{1,2}}){_ORD}(?:\s*(?:,|and|&)\s*(?:\d{{1,2}}){_ORD})+)\s+(?:of\s+)?({_MON})\b\.?(?:,?\s*(\d{{4}}))?", low):
            if not free(m.start(), m.end()):
                continue
            y = int(m.group(3)) if m.group(3) else None
            days = [self._make(int(n), MONTHS[m.group(2)], y) for n in re.findall(r"\d{1,2}", m.group(1))]
            if all(days):
                for d in days:
                    singles.append((m.start(), m.end(), d))
                taken.append((m.start(), m.end()))
        # 2) ISO, "12 November 2026", "November 12", dd/mm/yyyy
        pats = [
            (rf"\b(\d{{4}})-(\d{{2}})-(\d{{2}})\b", lambda g: (int(g[2]), int(g[1]), int(g[0]))),
            (rf"\b(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MON})\b\.?(?:,?\s*(\d{{4}}))?",
             lambda g: (int(g[0]), MONTHS[g[1]], int(g[2]) if g[2] else None)),
            (rf"\b({_MON})\b\.?\s+(\d{{1,2}}){_ORD}\b(?:,?\s*(\d{{4}}))?",
             lambda g: (int(g[1]), MONTHS[g[0]], int(g[2]) if g[2] else None)),
            (r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})\b", lambda g: (int(g[0]), int(g[1]), int(g[2]))),
        ]
        for pat, fn in pats:
            for m in re.finditer(pat, low):
                if not free(m.start(), m.end()):
                    continue
                d, mo, y = fn(m.groups())
                dt = self._make(d, mo, y)
                if dt:
                    singles.append((m.start(), m.end(), dt))
                    taken.append((m.start(), m.end()))
        singles.sort()
        # 3) join "X to Y" / "from X to Y" / "between X and Y"
        i = 0
        while i < len(singles):
            a, b, d1 = singles[i]
            if i + 1 < len(singles):
                a2, b2, d2 = singles[i + 1]
                between = low[b:a2]
                lead = low[max(0, a - 14):a]
                if re.fullmatch(rf"\s*{_SEP}\s*", between) or \
                        (re.fullmatch(r"\s*(?:and|&)\s*", between) and re.search(r"between\s*$", lead)):
                    if d1 <= d2:
                        out.append(DateHit(a, b2, d1, d2, True))
                        i += 2
                        continue
            lead = low[max(0, a - 18):a]
            after = low[b:b + 12]
            open_end = bool(re.search(r"(?:from|since|starting|after|beginning)\s*(?:the\s*)?$", lead)) and \
                not re.match(rf"\s*{_SEP}", after)
            out.append(DateHit(a, b, d1, d1, False, open_end))
            i += 1
        return sorted(out, key=lambda h: h.start)


_DAYNUM = r"\d{1,2}(?:st|nd|rd|th)?"
_BARE_LIST = re.compile(
    rf"\b(?:on|dates?|dated|from|for|during|between|the)\s+((?:{_DAYNUM})(?:\s*(?:,|and|&|{_SEP})\s*(?:{_DAYNUM}))+)"
    rf"(?![\d:.]|\s*(?:am|pm|a\.m|p\.m|rooms?|halls?|invigilators?|faculty|faculties|teachers?|staff|days?|dut(?:y|ies)|shifts?|members?|hours?|{_MON})\b)")


def bare_day_hits(parser: "DateParser", text: str, blocked: Sequence[Tuple[int, int]] = ()) -> List[DateHit]:
    """
    Days written WITHOUT a month - "on 11,12,13", "dates 11 to 13" - placed in the current month
    (next month when the day has already passed).  Numbers that belong to a clock range are ignored.
    """

    low = text.lower()
    out: List[DateHit] = []
    for m in _BARE_LIST.finditer(low):
        a, b = m.start(1), m.end(1)
        explicit = low[max(0, a - 12):a].strip().split()[-1:] in (["date"], ["dates"], ["dated"])
        if not explicit and any(not (b <= s0 or a >= e0) for s0, e0 in blocked):
            continue                                  # "9 to 12" is a clock range unless the word "dates" says otherwise
        body = m.group(1)
        nums = [int(x) for x in re.findall(r"\d{1,2}", body)]
        if not nums or any(not 1 <= n <= 31 for n in nums):
            continue
        is_range = bool(re.search(rf"\d\s*(?:{_SEP})\s*\d", body)) and len(nums) == 2 and not re.search(r",|\band\b|&", body)
        month, year, prev, days = parser.today.month, parser.year_hint or parser.today.year, 0, []
        for n in nums:
            d = None
            for _ in range(2):                       # a day that does not exist in the month rolls to the next one
                try:
                    d = date(year, month, n)
                    break
                except ValueError:
                    month, year = (1, year + 1) if month == 12 else (month + 1, year)
            if d is None:
                break
            if not days and (parser.today - d).days > 0 and parser.year_hint is None:
                month, year = (1, year + 1) if month == 12 else (month + 1, year)       # already past -> next month
                d = date(year, month, n)
            elif days and n < prev:
                month, year = (1, year + 1) if month == 12 else (month + 1, year)
                d = date(year, month, n)
            prev = n
            days.append(d)
        if len(days) != len(nums):
            continue
        if is_range and days[0] <= days[1]:
            out.append(DateHit(a, b, days[0], days[1], True))
        else:
            out += [DateHit(a, b, d, d, False) for d in days]
    return out


# ===========================================================================
# faculty names (matched ONLY against the roster)
# ===========================================================================

@dataclass
class Mention:
    start: int
    end: int
    text: str
    candidates: List[int]          # roster ids; >1 means ambiguous

    @property
    def resolved(self) -> Optional[int]:
        return self.candidates[0] if len(self.candidates) == 1 else None


_TOKEN = re.compile(r"[A-Za-z][A-Za-z'’-]*\.?|\d+")


class FacultyResolver:

    def __init__(self, faculty: Iterable[Tuple[int, str]]):
        self.faculty = list(faculty)
        first: Dict[str, int] = {}
        for _, n in self.faculty:
            toks = re.findall(r"[a-z]+", n.lower())
            if len(toks) > 1:
                first[toks[0]] = first.get(toks[0], 0) + 1
        self.titles = GENERIC_TITLES | {t for t, c in first.items() if c >= 3 and len(t) <= 6}
        self.tokens: Dict[int, List[str]] = {
            fid: [t for t in re.findall(r"[a-z]+", n.lower()) if t not in self.titles]
            for fid, n in self.faculty}
        self.name = {fid: n for fid, n in self.faculty}

    def _hit(self, word: str, have: List[str]) -> bool:
        for t in have:
            if word == t or (len(word) == 1 and t.startswith(word)):
                return True
            if len(word) >= 5 and len(t) >= 5 and difflib.SequenceMatcher(None, word, t).ratio() >= 0.86:
                return True
        return False

    def match_phrase(self, words: List[str]) -> List[int]:
        return [fid for fid, have in self.tokens.items()
                if have and all(self._hit(w, have) for w in words)]

    def mentions(self, text: str) -> Tuple[List[Mention], List[str]]:
        toks = [(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(text)]
        norm = [re.sub(r"['’]s$", "", t.rstrip(".").lower()) for t, _, _ in toks]
        skip = {i for i, n in enumerate(norm) if n in MONTHS or n in WEEKDAYS or n.isdigit()}
        found: List[Mention] = []
        unmatched: List[str] = []
        i = 0
        while i < len(toks):
            if i in skip:
                i += 1
                continue
            accepted = None
            for j in range(min(len(toks), i + 4), i, -1):
                if any(k in skip for k in range(i, j)):
                    continue
                window = norm[i:j]
                lead_title = window[0] in self.titles
                words = [w for k, w in enumerate(window) if not (k == 0 and lead_title)]
                if not words or any(w in self.titles for w in words):
                    continue
                if not lead_title and len(words) == 1 and not toks[i][0][0].isupper():
                    continue
                if not lead_title and not all(toks[k][0][0].isupper() for k in range(i, j)):
                    continue
                cands = self.match_phrase(words)
                if cands:
                    accepted = (i, j, cands)
                    break
            if accepted:
                a, b, cands = accepted
                found.append(Mention(toks[a][1], toks[b - 1][2], text[toks[a][1]:toks[b - 1][2]], cands))
                i = b
                continue
            if norm[i] in self.titles and i + 1 < len(toks) and i + 1 not in skip and toks[i + 1][0][0].isupper():
                k = i + 1
                while k < len(toks) and k < i + 4 and k not in skip and toks[k][0][0].isupper():
                    k += 1
                unmatched.append(text[toks[i][1]:toks[k - 1][2]].strip())
                i = k
                continue
            i += 1
        return found, unmatched


# ===========================================================================
# time ranges / shifts
# ===========================================================================

_TIME = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*(?:to|-|–|—|till|until)\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", re.I)


def _clock(h: int, mi: int, mer: Optional[str]) -> Optional[Tuple[int, int]]:
    if not (0 <= mi < 60):
        return None
    if mer:
        mer = mer.lower()[0]
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if mer == "p" else 0)
    elif h > 24:
        return None
    return h * 60 + mi, 0


def parse_times(text: str) -> List[Tuple[int, int, int, int]]:
    """[(start_minute, end_minute, span_start, span_end)] for every clock range."""

    out = []
    for m in _TIME.finditer(text):
        h1, m1, mer1 = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        h2, m2, mer2 = int(m.group(4)), int(m.group(5) or 0), m.group(6)
        has_colon_or_mer = bool(m.group(2) or m.group(5) or mer1 or mer2)
        if not has_colon_or_mer and not (0 < h1 < 24 and 0 < h2 <= 24):
            continue
        if not has_colon_or_mer:
            # plain "9 to 12": reject day-like ranges; treat afternoon hours as 24h only if >12
            pass
        if mer2 and not mer1:
            e = mer2.lower()[0]
            if h2 == 12 or (h1 > h2 and h1 != 12):
                mer1 = "am"
            else:
                mer1 = mer2
        elif mer1 and not mer2:
            mer2 = mer1
        a, b = _clock(h1, m1, mer1), _clock(h2, m2, mer2)
        if a is None or b is None:
            continue
        s, e = a[0], b[0]
        if not mer1 and not mer2 and not (m.group(2) or m.group(5)):
            # bare hours: assume office hours (1-6 -> pm)
            if 1 <= h1 <= 6:
                s += 12 * 60
            if 1 <= h2 <= 7:
                e += 12 * 60
        if e <= s and not mer2 and 1 <= h2 <= 12 and e + 12 * 60 > s:
            e += 12 * 60                         # "12:30 to 2" -> 12:30-14:00
        if e <= s:
            continue
        out.append((s, e, m.start(), m.end()))
    return out


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


_LABEL_STOP = {"from", "at", "the", "and", "is", "are", "shift", "shifts", "session", "sessions", "exam",
               "examination", "on", "during", "between", "of", "a", "an", "have", "we", "time", "timing",
               "timings", "slot", "slots", "in", "each", "day", "days", "with", "will", "be", "for", "two",
               "three", "one", "duty", "duties", "rooms", "room", "daily", "per", "start", "starts", "timed",
               "held", "scheduled", "conducted", "has", "second", "first"} | set(NUMWORDS)


def parse_shifts(text: str) -> List[Dict[str, str]]:
    """Shifts the coordinator described, labelled by the word they used (or Shift N)."""

    times = parse_times(text)
    out: List[Dict[str, str]] = []
    prev_end = 0
    for k, (s, e, a, b) in enumerate(times):
        before = text[max(prev_end, a - 45):a]
        after = text[b:b + 22]
        label = None
        m = re.search(r"([A-Za-z]+)(?:\s+(?:shift|session|slot))?\s*(?:[:(]|\bfrom\b|\bat\b|\bbetween\b)?\s*$", before)
        if m and m.group(1).lower() not in _LABEL_STOP and not m.group(1).lower().endswith("rs"):
            label = m.group(1)
        if label is None:
            m2 = re.match(r"\s*\(\s*([A-Za-z]+)\s*\)", after)
            if m2 and m2.group(1).lower() not in _LABEL_STOP:
                label = m2.group(1)
        out.append({"label": (label or f"Shift {k + 1}").capitalize(), "start": hhmm(s), "end": hhmm(e)})
        prev_end = b
    # unique labels
    seen: Dict[str, int] = {}
    for sh in out:
        key = sh["label"].lower()
        if key in seen:
            seen[key] += 1
            sh["label"] = f"{sh['label']} {seen[key]}"
        else:
            seen[key] = 1
    return out


# ===========================================================================
# examination request
# ===========================================================================

@dataclass
class ExamRequest:
    name: Optional[str] = None
    start: Optional[date] = None
    end: Optional[date] = None
    n_days: Optional[int] = None
    explicit_dates: List[date] = field(default_factory=list)
    skip_dates: List[date] = field(default_factory=list)
    include_weekdays: bool = False
    shifts: List[Dict[str, str]] = field(default_factory=list)
    rooms: Optional[List[str]] = None
    room_count: Optional[int] = None
    per_room: Optional[int] = None
    min_duties: Optional[int] = None
    max_duties: Optional[int] = None
    max_per_day: Optional[int] = None
    classes_suspended: Optional[bool] = None
    prohibit_consecutive: Optional[bool] = None
    dept_include: List[str] = field(default_factory=list)
    dept_exclude: List[str] = field(default_factory=list)
    shifts_per_day_hint: Optional[int] = None
    group_split: Optional[Dict[str, Any]] = None      # {'base': 'cs', 'second': None, 'shares': {...}}

    def merge(self, newer: "ExamRequest") -> "ExamRequest":
        for k, v in newer.__dict__.items():
            if v in (None, [], False) and k != "include_weekdays":
                continue
            if k == "include_weekdays" and not v:
                continue
            setattr(self, k, v)
        return self


_ROOM_NAME = r"[A-Za-z]{0,4}[- ]?\d+[A-Za-z0-9-]*"


def _n(tok: str) -> Optional[int]:
    return to_int(tok)


def parse_exam_request(text: str, today: date, year_hint: Optional[int] = None,
                       window_days: int = 183) -> ExamRequest:
    low = text.lower()
    req = ExamRequest()
    dp = DateParser(today, year_hint, window_days)
    hits = dp.find(text)
    bare_spans: List[Tuple[int, int]] = []
    if not hits:                                      # "on 11,12,13" (no month): this month, or the next one
        hits = bare_day_hits(dp, text, [(a, b) for _, _, a, b in parse_times(text)])
        bare_spans = [(h.start, h.end) for h in hits]

    # skipped / holiday dates: "except 15 November", "excluding 15 Nov (holiday)"
    skip_spans = []
    for h in hits:
        lead = low[max(0, h.start - 40):h.start]
        if re.search(r"(?:except|excluding|exclude|skip|holiday\s+on|holidays?\s+(?:are|is|on)|no\s+exams?\s+on)\s*(?:the\s*)?[\w ,]*$", lead) \
                and not re.search(r"faculty|teacher|dr\.|prof\.", lead):
            req.skip_dates += [h.first + timedelta(days=i) for i in range((h.last - h.first).days + 1)]
            skip_spans.append(h)
    main = [h for h in hits if h not in skip_spans]
    ranges = [h for h in main if h.is_range]
    if ranges:
        req.start, req.end = ranges[0].first, ranges[0].last
    else:
        singles = [h.first for h in main]
        starting = [h for h in main if re.search(r"(?:from|starting|start(?:s|ing)?\s+(?:on|from)?|beginning|commenc\w+)\s*(?:on\s*)?$",
                                                  low[max(0, h.start - 22):h.start])]
        if starting:
            req.start = starting[0].first
        elif len(singles) == 1:
            if main[0].open_ended:                      # "from 16 November": needs an end date or a number of days
                req.start = singles[0]
            else:                                       # "on 12th November": a one-day examination
                req.explicit_dates = [singles[0]]
        elif len(singles) > 1:
            req.explicit_dates = sorted(set(singles))
    dm = re.search(r"\b(\d{1,2}|" + "|".join(NUMWORDS) + r")[\s-]*days?\b", low)
    if dm and not re.search(r"per\s+day|a\s+day", low[dm.start():dm.end() + 8]):
        req.n_days = _n(dm.group(1))
    if re.search(r"including\s+(?:all\s+)?(?:sundays|weekends|holidays)|include\s+sundays|all\s+7\s+days", low):
        req.include_weekdays = True

    # shifts (clock ranges) - the days of a month-less date list are not clock times
    shift_text = text
    for a, b in bare_spans + [(h.start, h.end) for h in hits]:      # text read as a DATE is never a clock time
        shift_text = shift_text[:a] + " " * (b - a) + shift_text[b:]
    req.shifts = parse_shifts(shift_text)

    # rooms
    rm = re.search(r"\b(\d{1,3}|" + "|".join(NUMWORDS) + r")\s+(?:[a-z]+\s+)?(?:rooms?|halls?|venues?|classrooms?)\b", low)
    if rm and not re.search(r"per\s+room", low[rm.start():rm.end() + 9]):
        req.room_count = _n(rm.group(1))
    nm = re.search(r"\b(?:rooms?|halls?)\s*(?:are|:|=|-)?\s*(" + _ROOM_NAME + r"(?:\s*(?:,|and|&)\s*" + _ROOM_NAME + r")+)", text, re.I)
    if nm:
        req.rooms = [r.strip() for r in re.split(r"\s*(?:,|\band\b|&)\s*", nm.group(1)) if r.strip()]
        req.room_count = None

    # invigilators per room
    _who = r"(?:invigilators?|invigilation\s+staff|faculty|faculties|teachers?|staff|supervisors?|members?|persons?|people|examiners?)"
    pm = re.search(r"\b(\d{1,2}|" + "|".join(NUMWORDS) + r")\s+" + _who + r"\s+"
                   r"(?:per|in\s+each|for\s+each|every|in\s+every|a|each|in\s+one|in\s+a)\s+(?:room|hall)", low)
    if not pm:                                   # "2 in each room", "two per room"
        pm = re.search(r"\b(\d{1,2}|" + "|".join(NUMWORDS) + r")\s+(?:in|per|for)\s+(?:each|every|one)\s+(?:room|hall)\b", low)
    if not pm:
        pm = re.search(r"(?:invigilators?|faculty|faculties|teachers?)\s+(?:per|in\s+each)\s+(?:room|hall)\s*(?:is|are|:|=|of)?\s*(\d{1,2}|" +
                       "|".join(NUMWORDS) + r")\b", low)
    if not pm:                                   # "with two invigilators each", "2 invigilators per room"
        pm = re.search(r"\b(\d{1,2}|" + "|".join(NUMWORDS) + r")\s+(?:invigilators?|supervisors?|faculty|faculties|teachers?)\s*(?:each|apiece|per\s+(?:room|hall))\b", low)
    if pm:
        req.per_room = _n(pm.group(1))

    # per-day cap (read before the general max)
    dayc = re.search(r"(?:max(?:imum)?|at\s+most|no\s+more\s+than|only)\s*(?:of\s*)?(\d{1,2}|" + "|".join(NUMWORDS) +
                     r")\s+dut(?:y|ies)\s+(?:per|a|each|in\s+a)\s+day", low)
    if dayc:
        req.max_per_day = _n(dayc.group(1))
    elif re.search(r"\b(?:one|1)\s+dut(?:y|ies)\s+(?:per|a)\s+day", low) or \
            re.search(r"not\s+more\s+than\s+once\s+(?:a|per)\s+day", low):
        req.max_per_day = 1
    scrub = low if not dayc else low[:dayc.start()] + " " * (dayc.end() - dayc.start()) + low[dayc.end():]

    # min / max duties per faculty
    bt = re.search(r"between\s+(\d{1,2})\s+and\s+(\d{1,2})\s+dut", scrub)
    if bt:
        req.min_duties, req.max_duties = int(bt.group(1)), int(bt.group(2))
    mn = re.search(r"(?:min(?:imum)?|at\s+least)\s*(?:of\s*)?(\d{1,2}|" + "|".join(NUMWORDS) + r")\b", scrub)
    mx = re.search(r"(?:max(?:imum)?|at\s+most|no\s+more\s+than|up\s+to|not\s+more\s+than)\s*(?:of\s*)?(\d{1,2}|" +
                   "|".join(NUMWORDS) + r")\b", scrub)
    if mn:
        req.min_duties = _n(mn.group(1))
    if mx:
        req.max_duties = _n(mx.group(1))

    # classes suspended?
    _cls = r"(?:regular\s+)?(?:class(?:es)?|lectures?|teaching)"
    if re.search(rf"(?:no|none\s+of\s+the|not\s+any)\s+{_cls}\s+(?:is\s+|are\s+|will\s+be\s+|has\s+been\s+|have\s+been\s+|gets?\s+)?"
                 r"(?:being\s+)?(?:suspended|cancel\w+|off|stopped|closed)", low) or \
            re.search(rf"{_cls}\s+(?:is\s+|are\s+|will\s+be\s+)?(?:not|never)\s+(?:being\s+)?(?:suspended|cancel\w+|stopped|closed)", low) or \
            re.search(r"(?:classes|lectures|teaching)\s+(?:are\s+|is\s+|will\s+(?:be\s+)?|remain\s+|stay\s+|have\s+been\s+)?"
                 r"(?:not\s+suspended|continu\w+|running|going\s+on|as\s+usual|held|normal)", low) or \
            re.search(r"(?:no|without)\s+(?:class\s+)?suspension", low) or re.search(r"not\s+suspended", low):
        req.classes_suspended = False
    elif re.search(r"(?:class(?:es)?|lectures?|teaching)\s+(?:are\s+|is\s+|will\s+(?:be\s+)?|remain\s+|stay\s+|have\s+been\s+)?"
                   r"(?:suspended|cancel\w+|off|stopped|closed|not\s+held)|"
                   r"(?:suspend\w*|cancel\w*|stop\w*)\s+(?:all\s+|the\s+)?(?:regular\s+)?(?:classes|lectures)|"
                   r"no\s+(?:regular\s+)?(?:classes|lectures)|regular\s+classes\s+(?:are\s+)?(?:off|suspended)", low):
        req.classes_suspended = True

    # consecutive preference (soft by default; hard only when forbidden outright)
    if re.search(r"(?:no|never|prohibit\w*|forbid\w*|strictly\s+no|must\s+not\s+(?:have|get|receive|be\s+given)|"
                 r"do\s+not\s+allow|not\s+allow(?:ed)?)\s+(?:any\s+)?(?:\w+\s+)?consecutive", low):
        req.prohibit_consecutive = True

    # department scope
    for m in re.finditer(r"\bonly\s+(?:from\s+)?(?:the\s+)?([a-z&, ]+?)\s+(?:department|dept|branch)s?\b", low):
        req.dept_include += [p.strip() for p in re.split(r",|\band\b|&", m.group(1)) if p.strip()]
    for m in re.finditer(r"\b(?:except|excluding|exclude|not\s+from|without)\s+(?:the\s+)?([a-z&, ]+?)\s+(?:department|dept|branch)s?\b", low):
        req.dept_exclude += [p.strip() for p in re.split(r",|\band\b|&", m.group(1)) if p.strip()]

    req.group_split = parse_group_split(text)

    # name
    nm2 = re.search(r"(?:for|of)\s+(?:the\s+)?([a-z][a-z -]{2,38}?)\s+exam(?:ination)?s?\b", low)
    if nm2:
        phrase = nm2.group(1).strip()
        if not re.search(r"\bdut|\bdays?\b|\ballocat|\bassign|\binvigil", phrase) and len(phrase.split()) <= 4:
            req.name = phrase.title()
    return req


def _group_label(base: str) -> str:
    base = base.strip()
    return base.upper() if len(base) <= 4 else base.title()


_SPLIT_VERB = r"(?:divid\w*|split\w*|shar\w*|distribut\w*|balanc\w*|mix\w*|ratio|proportion\w*|combination|both|equal\w*)"


def parse_group_split(text: str) -> Optional[Dict[str, Any]]:
    """
    'divide duties between CS and non-CS faculty'  ->  base 'cs', second None (= everyone else)
    'split between Alpha and Beta faculty'  ->  two named departments
    shares: 'equally', '50-50', '60% CS and 40% non-CS', '3:1' (first group : second group)
    """

    low = text.lower()
    if not re.search(_SPLIT_VERB, low) and not re.search(r"\bnon[- ]?[a-z]{2,}\b", low):
        return None
    base = second = None
    m = re.search(r"\bnon[- ]?([a-z]{2,20})\b", low)
    if m and m.group(1) not in ("teaching", "technical", "working", "exam", "examination") and \
            re.search(_SPLIT_VERB + r"|between|each|mix|%|dut(?:y|ies)|faculty", low):
        base = m.group(1)
    else:
        m2 = re.search(r"\bbetween\s+(?:the\s+)?([a-z]+(?: [a-z]+)?)\s+(?:and|&)\s+(?:the\s+)?([a-z]+(?: [a-z]+)?)\s+"
                       r"(?:faculty|teachers?|staff|department|dept|members?)", low)
        if m2:
            base, second = m2.group(1), m2.group(2)
    if not base:
        return None
    shares: Dict[str, float] = {}
    pr = re.search(r"\b(\d{1,3})\s*[:/-]\s*(\d{1,3})\b(?!\s*(?:am|pm|november|december))", low)
    pcts = re.findall(r"(\d{1,3})\s*%\s*(?:of\s+(?:the\s+)?(?:duties|seats)\s+)?(?:to|for|from|are|is)?\s*(non[- ]?)?([a-z]+)", low)
    if len(pcts) >= 2:
        for num, non, who in pcts[:2]:
            key = "second" if non else ("first" if who and who in (base.split()[0], base) else None)
            shares[key or ("first" if not shares else "second")] = float(num)
    elif pr and re.search(r"ratio|split|share|divid|proportion", low):
        shares = {"first": float(pr.group(1)), "second": float(pr.group(2))}
    return {"base": base, "second": second, "shares": shares}


def parse_dept_assignment(text: str, existing: Sequence[str]) -> Optional[Dict[str, Any]]:
    """
    'Dr. A and Dr. B are non-CS faculty'            -> label 'Non-CS' for the named people
    'treat all MTECH faculty as non-CS'             -> label 'Non-CS' for everyone in department MTECH
    'Dr. A belongs to the Mathematics department'   -> label 'Mathematics'
    """

    low = text.lower()
    lab = None
    src = None
    m = re.search(r"\b(?:treat|consider|mark|count|classif\w*|set|make|label)\w*\s+(?:all\s+)?(?:the\s+)?([a-z][a-z ]{1,25}?)\s+"
                  r"(?:faculty|teachers?|staff|department|dept)\s+(?:members\s+)?as\s+(?:the\s+)?(non[- ]?[a-z]+|[a-z][a-z &]{1,30}?)"
                  r"\s*(?:faculty|teachers?|staff|department|dept|group|[.,;]|$)", low)
    if m:
        src, lab = m.group(1).strip(), m.group(2).strip()
    else:
        m = re.search(r"\b(?:is|are|as|belongs?\s+to|belong\s+to|from|in|under|(?:treated|considered|marked|counted)\s+as)\s+"
                      r"(?:a\s+|an\s+|the\s+|part\s+of\s+the\s+)?(non[- ]?[a-z]+|[a-z][a-z &]{1,30}?)\s+"
                      r"(?:faculty|teachers?|staff|department|dept|branch|group)\b", low)
        if m:
            lab = m.group(1).strip()
    if not lab:
        return None
    nm = re.match(r"non[- ]?([a-z]+)$", lab)
    if nm:
        label = "Non-" + _group_label(nm.group(1))
    else:
        match = next((e for e in existing if e.lower() == lab), None)
        label = match or _group_label(lab) if len(lab) <= 4 else (match or lab.title())
    return {"label": label, "source": src}


def expand_dates(req: ExamRequest, non_working: Sequence[str]) -> Tuple[List[date], List[str]]:
    """Exam dates from the request: a range, a start + N days, or explicit dates."""

    notes: List[str] = []
    skip = set(req.skip_dates)
    nonwork = {d.lower() for d in non_working}

    def ok(d: date) -> bool:
        if d in skip:
            return False
        return req.include_weekdays or d.strftime("%A").lower() not in nonwork

    if req.explicit_dates:
        return sorted(set(req.explicit_dates)), notes
    if req.start and req.end:
        days = [req.start + timedelta(days=i) for i in range((req.end - req.start).days + 1)]
        out = [d for d in days if ok(d)]
        dropped = [d for d in days if not ok(d)]
        if dropped:
            notes.append("Skipped (non-working / excluded): " + ", ".join(d.strftime("%d %b (%a)") for d in dropped))
        if req.n_days and req.n_days != len(out):
            notes.append(f"You mentioned {req.n_days} days but {req.start:%d %b} to {req.end:%d %b} holds {len(out)} "
                         f"examination days; I used the date range.")
        return out, notes
    if req.start and req.n_days:
        out, d = [], req.start
        while len(out) < req.n_days and (d - req.start).days < 400:
            if ok(d):
                out.append(d)
            d += timedelta(days=1)
        skipped = [x for x in (req.start + timedelta(days=i) for i in range((out[-1] - req.start).days + 1))
                   if x not in out]
        if skipped:
            notes.append("Skipped (non-working / excluded): " + ", ".join(d.strftime("%d %b (%a)") for d in skipped))
        return out, notes
    if req.start and not req.n_days:
        return [req.start] if req.start else [], notes
    return [], notes


def missing_fields(req: ExamRequest, require_suspension: bool = True) -> List[str]:
    miss = []
    if not (req.explicit_dates or (req.start and (req.end or req.n_days))):
        miss.append("the examination dates (start date with end date or number of days)")
    if not req.shifts:
        miss.append("the examination shift timings (for example: morning 9 am to 12 pm)")
    if not (req.rooms or req.room_count):
        miss.append("the number of examination rooms")
    if not req.per_room:
        miss.append("how many invigilators each room needs")
    if require_suspension and req.classes_suspended is None:
        miss.append("whether regular classes are suspended during this examination")
    return miss


# ===========================================================================
# faculty restriction directives
# ===========================================================================

_RESTORE = re.compile(
    r"\b(?:restor\w*|reinstat\w*|re-?enabl\w*|available\s+again|eligible\s+again|"
    r"make\b.{0,40}\bavailable|remov\w*\b.{0,40}\b(?:exclusion|restriction|unavailab\w*)|"
    r"lift\w*|cancel\w*\b.{0,30}\b(?:exclusion|restriction)|back\s+(?:on|to|in)\s+(?:the\s+)?(?:duty|list|pool)|"
    r"(?:is|are)\s+(?:now\s+)?available\s+(?:from|on|again))", re.I)
_EXCLUDE = re.compile(
    r"\b(?:exclud\w*|unavailab\w*|not\s+available|on\s+leave|leave|remov\w*\s+from|exempt\w*|excus\w*|"
    r"must\s+not|cannot|can't|should\s+not|shall\s+not|do\s+not\s+assign|don't\s+assign|not\s+to\s+be\s+assigned|"
    r"out\s+of\s+station|absent|busy|unable|no\s+dut(?:y|ies)|not\s+be\s+(?:assigned|given))", re.I)
_ENTIRE = re.compile(r"\b(?:entire|complete(?:ly)?|whole|full|throughout|overall|all\s+(?:the\s+)?(?:exam\w*|session|period|days)|"
                     r"duration|exam(?:ination)?\s+(?:period|session))\b", re.I)
_PERM = re.compile(r"\b(?:permanent\w*|forever|indefinite\w*|always|for\s+good)\b", re.I)
_REASON = re.compile(r"\b(?:because(?:\s+of)?|due\s+to|on\s+account\s+of|as\s+(?:he|she|they)\s+(?:is|are|has|have)|"
                     r"since\s+(?:he|she|they)|handling|working\s+on|busy\s+with|engaged\s+in|owing\s+to)\s+(.+)", re.I)


@dataclass
class Scope:
    kind: str                         # session | permanent | date | date_range | shift
    start: Optional[date] = None
    end: Optional[date] = None
    shift: Optional[str] = None


@dataclass
class Directive:
    action: str                       # exclude | restore
    mentions: List[Mention]
    scopes: List[Scope]
    reason: str = ""
    category: str = "general"
    categories: Optional[List[str]] = None      # restore filter
    kinds: Optional[List[str]] = None           # restore filter
    needs: Optional[str] = None
    text: str = ""


def _sentence_bounds(text: str, titles: Set[str]) -> List[Tuple[int, int]]:
    cuts = [0]
    for m in re.finditer(r"[.;!?\n]", text):
        i = m.start()
        if text[i] == ".":
            prev = re.findall(r"[A-Za-z]+", text[max(0, i - 8):i])
            if (prev and prev[-1].lower() in titles) or (i + 1 < len(text) and text[i + 1] not in " \n"):
                continue
        cuts.append(i + 1)
    cuts.append(len(text))
    return [(a, b) for a, b in zip(cuts, cuts[1:]) if text[a:b].strip()]


def _group(mentions: List[Mention], text: str) -> List[List[Mention]]:
    groups: List[List[Mention]] = []
    for m in mentions:
        if groups:
            between = text[groups[-1][-1].end:m.start].strip().lower()
            if re.fullmatch(r"(?:,|and|&|,\s*and|or|as well as|along with|plus)?", between):
                groups[-1].append(m)
                continue
        groups.append([m])
    return groups


def parse_directives(text: str, resolver: FacultyResolver, mentions: List[Mention], shift_labels: Sequence[str],
                     shift_times: Dict[str, Tuple[int, int]], today: date, year_hint: Optional[int],
                     admin_words: Sequence[str], leave_words: Sequence[str], window_days: int = 183
                     ) -> List[Directive]:
    """One Directive per group of people that share the same restriction text."""

    dp = DateParser(today, year_hint, window_days)
    sents = _sentence_bounds(text, resolver.titles)
    groups = _group(mentions, text)
    out: List[Directive] = []
    for gi, g in enumerate(groups):
        gs, ge = g[0].start, g[-1].end
        sent = next(((a, b) for a, b in sents if a <= gs < b), (0, len(text)))
        nxt = groups[gi + 1][0].start if gi + 1 < len(groups) else len(text)
        after_end = min(nxt, sent[1])
        after = text[ge:after_end]
        prevg_end = groups[gi - 1][-1].end if gi else 0
        before = text[max(sent[0], prevg_end):gs]
        body = after
        # a trailing "but ... available ..." clause only informs; the restriction is the first part
        first_part = re.split(r"\b(?:but|however|whereas)\b", body, maxsplit=1, flags=re.I)[0]
        probe = first_part
        has_action = bool(_RESTORE.search(probe) or _EXCLUDE.search(probe))
        if not has_action:
            probe = before + " " + first_part
            has_action = bool(_RESTORE.search(probe) or _EXCLUDE.search(probe))
        if not has_action:
            continue
        action_text = probe
        is_restore = bool(_RESTORE.search(action_text)) and not re.search(r"\bexclud\w*|\bunavailab\w*", first_part, re.I) \
            or bool(re.search(r"\bavailable\s+again\b|\brestor\w*", action_text, re.I))
        # --- scopes
        hits = dp.find(first_part)
        scopes: List[Scope] = []
        shift = None
        low = action_text.lower()
        for lab in shift_labels:
            if re.search(rf"\b{re.escape(lab.lower())}\b", low):
                shift = lab
                break
        if shift is None:
            for s, e, a, b in parse_times(action_text):
                for lab, (ls, le) in shift_times.items():
                    if ls == s and le == e:
                        shift = lab
        if shift is None:
            m = re.search(r"\b(morning|afternoon|evening|forenoon|night)\b(?:\s+(?:shift|session|slot))?", low)
            if m:
                shift = m.group(1).capitalize()      # not a session shift -> reported by validation
        entire = bool(_ENTIRE.search(action_text))
        perm = bool(_PERM.search(action_text))
        needs = None
        if perm:
            scopes.append(Scope("permanent"))
        elif hits and not (entire and not [h for h in hits]):
            for h in hits:
                if h.is_range:
                    scopes.append(Scope("shift" if shift else "date_range", h.first, h.last, shift))
                elif h.open_ended:
                    scopes.append(Scope("shift" if shift else "date_range", h.first, None, shift))
                else:
                    scopes.append(Scope("shift" if shift else "date", h.first, h.first, shift))
        elif entire:
            scopes.append(Scope("session"))
        elif is_restore:
            scopes.append(Scope("session"))               # no window: lift everything asked about
        elif re.search(r"\bexclud\w*|\bexempt\w*|remov\w*\s+from", action_text, re.I):
            scopes.append(Scope("session"))               # "exclude X" = the whole examination
        else:
            needs = "the date(s) or period"
        if shift and not hits and not perm and not entire and not is_restore:
            needs = "the date(s) for that shift"
        # --- reason / category
        reason = ""
        rm = _REASON.search(first_part) or (_REASON.search(before) if not first_part.strip() else None)
        if rm:
            reason = re.sub(r"\s+", " ", rm.group(1)).strip(" ,.;")
            reason = re.split(r",?\s+and\s*$", reason)[0]
        category = "general"
        blob = (reason + " " + action_text).lower()
        if any(w in blob for w in admin_words):
            category = "administrative"
        elif any(w in blob for w in leave_words):
            category = "leave"
        if not reason and category == "leave":
            reason = "On leave"
        d = Directive("restore" if is_restore else "exclude", g, scopes, reason, category, text=action_text.strip(),
                      needs=needs)
        if is_restore:
            if re.search(r"\btemporar\w*", action_text, re.I):
                d.kinds = ["session", "date", "date_range", "shift"]
            if re.search(r"\badministrat\w*|\badmin\b", action_text, re.I):
                d.categories = ["administrative"]
            if perm:
                d.kinds = ["permanent"]
        out.append(d)
    return out


# ===========================================================================
# departments (dynamic, from the roster)
# ===========================================================================

def match_departments(phrases: Iterable[str], departments: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Phrase -> real department label (full name, word subset, or initials)."""

    def toks(s: str) -> List[str]:
        return re.findall(r"[a-z0-9]+", s.lower())

    found, bad = [], []
    for ph in phrases:
        pt = toks(ph)
        hit = None
        for d in departments:
            dt = toks(d)
            initials = "".join(t[0] for t in dt)
            if pt == dt or "".join(pt) == "".join(dt) or (len(pt) == 1 and pt[0] in (initials, dt[0] if dt else "")) \
                    or (pt and set(pt) <= set(dt)) or (pt and all(any(t.startswith(p) for t in dt) for p in pt)):
                hit = d
                break
        (found if hit else bad).append(hit or ph)
    return list(dict.fromkeys(found)), bad


# ===========================================================================
# single-intent helpers
# ===========================================================================

@dataclass
class ReplaceRequest:
    mention: Mention
    on: date
    shift: Optional[str]
    reason: str
    allow_wider: bool


def parse_replace(text: str, mentions: List[Mention], shift_labels: Sequence[str], today: date,
                  year_hint: Optional[int], window_days: int = 183) -> Optional[ReplaceRequest]:
    low = text.lower()
    if not re.search(r"\b(?:remove|replace|take\s+off|relieve|drop|swap)\b", low) or len(mentions) != 1:
        return None
    hits = [h for h in DateParser(today, year_hint, window_days).find(text) if not h.is_range]
    if len(hits) != 1:
        return None
    shift = next((l for l in shift_labels if re.search(rf"\b{re.escape(l.lower())}\b", low)), None)
    rm = _REASON.search(text)
    reason = re.sub(r"\s+", " ", rm.group(1)).strip(" .;,") if rm else ""
    reason = re.split(r"\.\s|\band\s+replace\b", reason)[0].strip(" ,")
    wider = bool(re.search(r"allow\s+wider|wider\s+changes|re-?optimi[sz]e|reshuffle|global", low))
    return ReplaceRequest(mentions[0], hits[0].first, shift, reason, wider)


def parse_roles(text: str) -> List[str]:
    m = re.search(r"\bonly\s+(?:(?:the|for)\s+)?([a-z ,&/]+?)(?:\s+(?:during|for|in|from)\b|[.;]|$)", text.lower())
    if not m:
        return []
    roles = [re.sub(r"\s+", " ", r).strip() for r in re.split(r",|\band\b|&|/", m.group(1))]
    out = []
    for r in roles:
        r = re.sub(r"^(?:the|all)\s+", "", r)
        if r.endswith("s") and len(r) > 3:
            r = r[:-1]
        if r and r not in ("faculty", "teacher", "member"):
            out.append(r)
    return out


def parse_responsibility(text: str, admin_words: Sequence[str]) -> Optional[str]:
    """'... is HOD', '... handling NAAC accreditation work' -> role text (None if not administrative)."""

    low = text.lower()
    m = re.search(r"\b(?:is|are|as|handling|managing|heading|leading|in\s+charge\s+of|incharge\s+of|working\s+(?:as|on)|"
                  r"serving\s+as|appointed\s+(?:as)?)\s+(?:the\s+|a\s+|an\s+)?([a-z0-9&/ \-]+?)"
                  r"(?:\s+(?:during|for\s+the|from|until|till|this)\b|[.,;]|$)", low)
    if not m:
        return None
    role = re.sub(r"\s+(?:work|duties|duty|responsibilit\w+)$", "", m.group(1)).strip()
    if role and any(w in role for w in admin_words):
        return role.upper() if len(role) <= 4 else role.title()
    return None