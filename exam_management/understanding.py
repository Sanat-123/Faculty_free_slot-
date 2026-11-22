"""
Request understanding: turns a coordinator's message (or several messages) into ONE validated ``ExamRequest``.

    message -> rule reader (nlu.parse_exam_request)          deterministic, always available
            -> semesters / years / classes                    read against the lexicon
            -> department ROLES (students vs invigilators)    resolved against the database's departments
            -> optional language-model extraction             validated, grounded in the message, never trusted blindly
            -> contradictions and genuinely missing facts    -> ``Understanding``

Nothing here knows a department, semester, date, room or person: they come from the message or the database.
A requirement that no reader understood is KEPT in ``req.unmapped`` and reported - never silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from . import duty_rules
from . import lexicon as lx
from . import nlu
from .nlu import SRC_DB, SRC_DEFAULT, SRC_INFERRED, SRC_LLM, SRC_USER, ExamRequest


# ---------------------------------------------------------------------------------------------- catalog
class ClassCatalog(Protocol):
    """What the timetable knows about classes (optional: a college without class data simply has none)."""

    def classes(self) -> List[str]: ...
    def semester_of(self, class_name: str) -> Optional[int]: ...
    def department_of(self, class_name: str) -> Optional[str]: ...


class NoClasses:
    def classes(self) -> List[str]:
        return []

    def semester_of(self, class_name: str) -> Optional[int]:
        return None

    def department_of(self, class_name: str) -> Optional[str]:
        return None


@dataclass
class Scope:
    """Which timetable classes sit this examination (and therefore do not teach during it)."""

    classes: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    resolved: bool = False                      # the request named a scope the catalog could look at


def semesters_per_year() -> int:
    try:
        from scheduling import duty_history
        return int(duty_history.lexicon().get("semesters_per_year", 2) or 2)
    except Exception:
        return 2


def resolve_scope(req: ExamRequest, catalog: ClassCatalog, dept_label: Callable[[str], Optional[str]] = lambda x: x) -> Scope:
    """The timetable classes taking this examination, from semester / year / class names / student departments."""

    sc = Scope()
    all_classes = catalog.classes()
    wants = bool(req.semesters or req.years or req.class_names or req.student_departments or req.college_wide)
    sc.resolved = wants
    if not wants:
        return sc
    if req.college_wide:
        sc.classes = list(all_classes)
        sc.notes.append("whole college: every class in the timetable is treated as suspended")
        return sc
    sems = set(req.semesters)
    present = {catalog.semester_of(c) for c in all_classes} - {None}
    per = semesters_per_year()
    for y in req.years:
        if y == "final":
            if present:
                top = max((s + per - 1) // per for s in present)
                sems |= {s for s in present if (s + per - 1) // per == top}
        else:
            try:
                yi = int(y)
                sems |= set(range((yi - 1) * per + 1, yi * per + 1))
            except (TypeError, ValueError):
                pass
    depts = set(req.student_departments)
    chosen = []
    for c in all_classes:
        if req.class_names and c in req.class_names:
            chosen.append(c)
            continue
        if sems or depts:
            sem_ok = (not sems) or catalog.semester_of(c) in sems
            dep = catalog.department_of(c)
            dep_ok = (not depts) or (dep is not None and dep in depts)
            if sem_ok and dep_ok and not req.class_names:
                chosen.append(c)
    sc.classes = sorted(set(chosen))
    if not sc.classes:
        what = []
        if sems:
            what.append("semester " + ", ".join(map(str, sorted(sems))))
        if depts:
            what.append(", ".join(sorted(depts)))
        if req.class_names:
            what.append(", ".join(req.class_names))
        sc.notes.append("no class of " + " / ".join(what) + " is in the loaded timetable, so no timetable class was treated as "
                        "suspended (teaching is still checked for everyone the timetable covers)")
    else:
        missing = [s for s in sorted(sems) if not any(catalog.semester_of(c) == s for c in all_classes)]
        if missing:
            sc.notes.append("semester " + ", ".join(map(str, missing)) + " has no class in the loaded timetable")
    return sc


# ---------------------------------------------------------------------------------------------- semesters
def _ordinals() -> Dict[str, int]:
    try:
        from scheduling import duty_history
        return {str(k).lower(): int(v) for k, v in duty_history.lexicon().get("ordinal_words", {}).items()}
    except Exception:
        return {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8}


def _roman(tok: str) -> Optional[int]:
    vals = {"i": 1, "v": 5, "x": 10}
    t = tok.lower()
    if not t or any(c not in vals for c in t):
        return None
    tot = 0
    for i, ch in enumerate(t):
        tot += -vals[ch] if i + 1 < len(t) and vals[t[i + 1]] > vals[ch] else vals[ch]
    return tot if 0 < tot < 20 else None


def _ordinal_value(tok: str) -> Optional[int]:
    t = re.sub(r"(?:st|nd|rd|th)$", "", tok.lower().strip())
    if t.isdigit():
        return int(t)
    return _ordinals().get(tok.lower().strip()) or _ordinals().get(t) or _roman(t)


def parse_semesters(text: str) -> Tuple[List[int], List[Any]]:
    """
    Every semester and year named: "3rd semester", "first and third semester", "semesters 1, 3 and 5",
    "sem 1 to 4", "second-year", "final year".  Returns (semesters, years).
    """

    ords = "|".join(sorted(_ordinals(), key=len, reverse=True))
    one = rf"(?:\d{{1,2}}(?:st|nd|rd|th)?|{ords}|[ivx]{{1,4}})"
    joiner = r"(?:\s*(?:,|and|&|to|-|–|or)\s*)"
    sem = r"sem(?:ester|ister)?s?"
    sems: List[int] = []

    def take(seq: str) -> None:
        toks = re.findall(one, seq, re.I)
        vals = [_ordinal_value(t) for t in toks]
        vals = [v for v in vals if v and 0 < v <= 20]
        if re.search(r"\b(?:to|-|–)\b|[-–]", seq) and len(vals) == 2 and not re.search(r",|\band\b|&", seq) and vals[0] < vals[1]:
            vals = list(range(vals[0], vals[1] + 1))
        for v in vals:
            if v not in sems:
                sems.append(v)

    for m in re.finditer(rf"(?<![\w-])({one}(?:{joiner}{one})*)\s*[-.]?\s*{sem}\b", text, re.I):
        take(m.group(1))
    for m in re.finditer(rf"\b{sem}\s*[-:#.]?\s*({one}(?:{joiner}{one})*)", text, re.I):
        take(m.group(1))
    years: List[Any] = []
    for m in re.finditer(r"\b(final|last)[\s-]+year\b", text, re.I):
        years.append("final")
    for m in re.finditer(rf"\b({one})[\s-]*year\b", text, re.I):
        v = _ordinal_value(m.group(1))
        if v and v <= 8 and v not in years:
            years.append(v)
    for m in re.finditer(r"\byear\s*[-:#]?\s*(\d)\b", text, re.I):
        v = int(m.group(1))
        if v not in years:
            years.append(v)
    return sorted(sems), years


# ---------------------------------------------------------------------------------------------- departments
def is_common_word(word: str) -> bool:
    """Ordinary vocabulary (ordinals, numbers, months, people-words, shift words ...) is never a department name."""

    w = word.strip().lower()
    if not w.isalpha():
        return False
    if w in _ordinals() or w in lx.number_words():
        return True
    if w in {m for m in nlu.MONTHS} or w in {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}:
        return True
    for key in ("faculty_nouns", "student_nouns", "shift_name_words", "per_room_nouns", "room_nouns", "duty_nouns"):
        if re.fullmatch(lx.alternation(key), w, re.I):
            return True
    return False


@dataclass
class DeptMention:
    label: str
    start: int
    end: int


DeptFinder = Callable[[str], Tuple[List[DeptMention], List[Tuple[str, List[str]]]]]


def classify_departments(text: str, found: Sequence[DeptMention]) -> Dict[str, List[str]]:
    """
    Which department is the one being EXAMINED and which supplies INVIGILATORS.  A list ("CS and IT") shares the
    role of the words around it.  Returns {"student": [...], "faculty": [...], "exclude": [...], "unknown": [...]}.
    """

    out: Dict[str, List[str]] = {"student": [], "faculty": [], "exclude": [], "unknown": []}
    fac = lx.alternation("faculty_nouns")
    stu = lx.alternation("student_nouns")
    skip = r"(?:(?:department|dept|branch|engineering|engg|wing|side|based|ones?|only)\s+)*"
    fac_before = lx.alternation("faculty_role_before")
    stu_before = lx.alternation("student_role_before")
    excl = r"(?:except|excluding|exclude|without|not\s+from|other\s+than|apart\s+from|but\s+not|leave\s+out|skip)\s*(?:the\s+)?$"
    groups: List[List[DeptMention]] = []
    for m in sorted(found, key=lambda d: d.start):
        if groups and re.fullmatch(r"\s*(?:,|and|&|or|\+|,\s*and)?\s*(?:the\s+)?", text[groups[-1][-1].end:m.start], re.I):
            groups[-1].append(m)
        else:
            groups.append([m])
    for g in groups:
        labels = [d.label for d in g]
        after = text[g[-1].end:g[-1].end + 45]
        before = text[max(0, g[0].start - 45):g[0].start]
        role = "unknown"
        if re.search(excl, before, re.I):
            role = "exclude"
        else:
            ma = re.match(rf"\s*{skip}(?:(?P<f>{fac})|(?P<s>{stu}))\b", after, re.I)
            if ma and ma.group("f"):
                role = "faculty"
            elif ma and ma.group("s"):
                role = "student"
            elif re.search(rf"(?:{fac_before})\s*$", before, re.I):
                role = "faculty"
            elif re.search(rf"(?:{stu_before})\s*$", before, re.I):
                role = "student"
        out[role] += labels
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}


# ---------------------------------------------------------------------------------------------- understanding
@dataclass
class Gap:
    key: str
    question: str


@dataclass
class Understanding:
    req: ExamRequest
    gaps: List[Gap] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.gaps and not self.conflicts and not self.req.ambiguous


class RequestUnderstanding:

    def __init__(self, today: Callable[[], date], settings: Any, dept_finder: Optional[DeptFinder] = None,
                 catalog: Optional[ClassCatalog] = None, llm: Any = None,
                 dept_label: Optional[Callable[[str], Optional[str]]] = None):
        self.today = today
        self.settings = settings
        self.dept_finder = dept_finder
        self.catalog = catalog or NoClasses()
        self.llm = llm
        self.dept_label = dept_label or (lambda x: None)

    # ---- reading --------------------------------------------------------------------------------
    def read(self, text: str, year_hint: Optional[int] = None) -> ExamRequest:
        req = nlu.parse_exam_request(text, self.today(), year_hint, self.settings.default_year_window_days,
                                     self.settings.shift_label_template)
        if req.duty_rule is None:
            rule, _ = duty_rules.parse_duty_rules(text, self.settings.duty_rule_default_basis)
            if rule:
                req.duty_rule = rule.to_dict()
        elif req.duty_rule.get("basis") == "previous":
            pass
        req.semesters, req.years = parse_semesters(text)
        lowered = text.lower()
        if _college_wide(lowered):
            req.college_wide = True
        if lx.compiled("all_departments").search(text):
            req.all_faculty_departments = True
        self._departments(req, text)
        self._unmapped(req, text)
        if self.llm is not None:
            self._merge_llm(req, text)
        self._provenance(req)
        return req

    def _departments(self, req: ExamRequest, text: str) -> None:
        if self.dept_finder is None:
            return
        found, ambiguous = self.dept_finder(text)
        if req.group_split:
            # "divide equally between CS and non-CS faculty": those names are split GROUPS, not who is examined / who invigilates
            split = [(a, b) for a, b in lx.sentence_spans(text)
                     if re.search(r"\b(?:divid\w*|split|between|share|equally|ratio|proportion\w*|%|percent)\b", text[a:b], re.I)]
            found = [d for d in found if not any(a <= d.start < b for a, b in split)]
            ambiguous = [x for x in ambiguous if not any(x[0].lower() in text[a:b].lower() for a, b in split)]
        roles = classify_departments(text, found)
        req.student_departments = list(dict.fromkeys(req.student_departments + roles["student"]))
        req.dept_include = list(dict.fromkeys(req.dept_include + roles["faculty"]))
        req.dept_exclude = list(dict.fromkeys(req.dept_exclude + roles["exclude"]))
        if roles["unknown"]:
            req.ambiguous["department_role"] = roles["unknown"]
        for phrase, options in ambiguous:
            req.ambiguous.setdefault("department", []).append({"phrase": phrase, "options": options})
        if req.all_faculty_departments and req.dept_include:
            # "from all departments" beats a department list only when the list was about students
            req.all_faculty_departments = None if roles["faculty"] else True

    def _unmapped(self, req: ExamRequest, text: str) -> None:
        """Requirement sentences (modal / restriction wording) that none of the readers accounted for."""

        cues = lx.compiled("constraint_cues")
        for a, b in lx.sentence_spans(text):
            sent = text[a:b].strip()
            if not cues.search(sent):
                continue
            if self._accounted(req, sent):
                continue
            req.unmapped.append(sent)
        req.unmapped = list(dict.fromkeys(req.unmapped))

    def _accounted(self, req: ExamRequest, sent: str) -> bool:
        probe = nlu.parse_exam_request(sent, self.today(), None, self.settings.default_year_window_days)
        rule, _ = duty_rules.parse_duty_rules(sent)
        prio, _, _ = duty_rules.parse_priority(sent)
        sems, years = parse_semesters(sent)
        found = []
        if self.dept_finder is not None:
            found, _ = self.dept_finder(sent)
        return any([probe.explicit_dates, probe.start, probe.shifts, probe.room_count, probe.rooms, probe.per_room,
                    probe.min_duties is not None, probe.max_duties is not None, probe.max_per_day,
                    probe.classes_suspended is not None, probe.prohibit_consecutive, probe.avoid_teaching,
                    probe.simultaneous_rooms_forbidden, probe.allow_unverified, probe.dept_include, probe.dept_exclude,
                    probe.group_split, rule, prio, sems, years, found, _college_wide(sent.lower()),
                    lx.compiled("all_departments").search(sent), probe.exam_type])

    def _provenance(self, req: ExamRequest) -> None:
        for k in ("exam_type", "start", "end", "n_days", "room_count", "per_room", "min_duties", "max_duties", "max_per_day",
                  "classes_suspended", "prohibit_consecutive", "avoid_teaching", "duty_rule", "duty_priority",
                  "college_wide", "all_faculty_departments", "allow_unverified"):
            v = getattr(req, k)
            if v not in (None, [], {}) and k not in req.provenance:
                req.mark(k, SRC_USER)
        for k in ("explicit_dates", "shifts", "rooms", "semesters", "years", "student_departments", "dept_include",
                  "dept_exclude", "class_names"):
            if getattr(req, k) and k not in req.provenance:
                req.mark(k, SRC_USER)

    # ---- language-model extraction (optional) -----------------------------------------------------
    def _merge_llm(self, req: ExamRequest, text: str) -> None:
        try:
            payload = self.llm.extract(text, self.context())
        except Exception:
            payload = None
        if not payload:
            return
        from .llm_extract import apply_payload
        apply_payload(req, payload, text, self.today(), self.dept_finder, self.settings)

    def context(self) -> Dict[str, Any]:
        return {"today": self.today().isoformat(), "classes": self.catalog.classes()[:200]}

    # ---- checking ---------------------------------------------------------------------------------
    def check(self, req: ExamRequest, text: str = "") -> Understanding:
        u = Understanding(req)
        # contradictions (never silently choose)
        if req.min_duties is not None and req.max_duties is not None and req.min_duties > req.max_duties:
            u.conflicts.append(f"minimum duties ({req.min_duties}) is greater than maximum duties ({req.max_duties})")
        both = set(map(str.lower, req.dept_include)) & set(map(str.lower, req.dept_exclude))
        if both:
            u.conflicts.append("the same department is both included and excluded: " + ", ".join(sorted(both)))
        if req.classes_suspended is True and req.avoid_teaching:
            u.conflicts.append("you said regular classes are suspended and also asked to avoid faculty who are teaching - "
                               "which one applies?")
        if req.rooms and req.room_count and len(req.rooms) != req.room_count:
            u.conflicts.append(f"you listed {len(req.rooms)} room names but also said {req.room_count} rooms")
        if req.start and req.end and req.end < req.start:
            u.conflicts.append("the end date is before the start date")
        if text:
            counts = {lx.to_number(m.group(1)) for m in re.finditer(rf"(?<![\w:])({lx.number_pattern()})\s+(?:(?!per\b|each\b|every\b|in\b|for\b|a\b|an\b|the\b|and\b)[a-z]+\s+){{0,2}}?(?:rooms?|halls?)\b", text, re.I)}
            counts.discard(None)
            if len(counts) > 1 and not req.rooms:
                u.conflicts.append("different room counts were mentioned: " + ", ".join(map(str, sorted(counts))))
        rule_err = duty_rules.DutyRule.validate(req.duty_rule) if req.duty_rule else None
        if rule_err:
            u.conflicts.append(rule_err)
        for k, v in req.ambiguous.items():
            if k == "department_role":
                u.conflicts.append("is " + ", ".join(v) + " the department whose students are examined, or the department "
                                   "whose faculty should invigilate?")
            elif k == "department":
                for item in v:
                    u.conflicts.append(f"'{item['phrase']}' could mean " + " or ".join(item["options"]))
            elif k.startswith("llm_"):
                continue
            else:
                u.conflicts.append(f"{k}: " + str(v))
        # genuinely missing
        L = lx.lexicon().get("clarification_labels", {})
        if not (req.explicit_dates or (req.start and (req.end or req.n_days))):
            u.gaps.append(Gap("dates", L.get("dates", "the examination date(s)")))
        if not req.shifts:
            named = self._named_shifts(req, text)
            if named:
                req.shifts = named
                req.mark("shifts", SRC_DEFAULT)
                u.notes.append("shift timings taken from the configured examination schedule")
            else:
                u.gaps.append(Gap("shifts", L.get("shifts", "the start and end time of each shift")))
        if not (req.rooms or req.room_count):
            u.gaps.append(Gap("rooms", L.get("rooms", "the number of examination rooms (or their names)")))
        if not req.per_room:
            u.gaps.append(Gap("per_room", L.get("per_room", "how many invigilators each room needs")))
        self.infer_suspension(req, u)
        return u

    def _named_shifts(self, req: ExamRequest, text: str) -> List[Dict[str, str]]:
        """Shifts named without times ("morning and afternoon") - only when an administrator configured their times."""

        table = getattr(self.settings, "named_shifts", None) or {}
        if not table:
            return []
        out = []
        for name, (s, e) in table.items():
            if re.search(rf"\b{re.escape(str(name))}\b", text, re.I):
                out.append({"label": str(name).capitalize(), "start": s, "end": e})
        return out

    def infer_suspension(self, req: ExamRequest, u: Understanding) -> None:
        """Whether regular classes run - from what was said, else from the timetable, else the configured default, else ask."""

        if req.classes_suspended is not None:
            return
        if req.avoid_teaching:
            req.classes_suspended = False
            req.mark("classes_suspended", SRC_INFERRED)
            u.notes.append("classes continue (you asked to avoid faculty who are teaching); the examined classes do not teach")
            return
        if req.college_wide:
            req.classes_suspended = True
            req.mark("classes_suspended", SRC_INFERRED)
            return
        if req.semesters or req.years or req.class_names or req.student_departments:
            req.classes_suspended = False
            req.mark("classes_suspended", SRC_INFERRED)
            u.notes.append("only the classes taking this examination are suspended; every other class keeps teaching")
            return
        default = getattr(self.settings, "default_classes_suspended", None)
        if default is not None:
            req.classes_suspended = bool(default)
            req.mark("classes_suspended", SRC_DEFAULT)
            u.notes.append("regular classes treated as " + ("suspended" if default else "running") + " (configured default)")
            return
        u.gaps.append(Gap("suspension", lx.lexicon().get("clarification_labels", {}).get("suspension",
                          "whether regular classes continue during the examination")))


def _college_wide(low: str) -> bool:
    try:
        from scheduling import duty_history
        return any(re.search(p, low, re.I) for p in duty_history.lexicon().get("college_wide_strong", []))
    except Exception:
        return False