"""
Optional language-model extraction of an examination request.

The model only READS the message into a JSON object; it never allocates anyone.  Its output is

  1. parsed and validated against a schema (types, ranges, allowed operators); invalid output is retried once
     with the validation error, then ignored;
  2. GROUNDED in the coordinator's own words - a number, date or clock time that does not appear in the message is
     rejected (the model may not invent values);
  3. merged BELOW the rule reader: it only fills what the rules left empty.  Where both stated different values the
     disagreement is kept in ``req.ambiguous`` for the coordinator to settle.

Configuration (same as the rest of the project): ``ANTHROPIC_API_KEY``, ``FACULTY_LLM_MODEL``, ``FACULTY_LLM_OFF=1``.
Without a key the layer is simply absent and the rule reader works alone.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import duty_rules
from . import lexicon as lx
from .nlu import SRC_LLM, ExamRequest, MONTHS

SCHEMA_HELP = """Return ONE JSON object and nothing else, with exactly these keys (use null / [] when the message does not say):
{
 "exam_type": string|null,
 "dates": ["YYYY-MM-DD", ...],              // every examination date written or implied by a range
 "semesters": [int, ...],                    // 1,3 for "first and third semester"
 "years": [int|"final", ...],
 "student_departments": [string, ...],       // departments whose STUDENTS are examined, as written
 "faculty_departments": [string, ...],       // departments whose FACULTY should invigilate, as written
 "all_faculty_departments": bool|null,       // true for "from all departments"
 "shifts": [{"label": string, "start": "HH:MM", "end": "HH:MM"}],   // 24-hour clock
 "room_count": int|null, "rooms": [string], "per_room": int|null,
 "duty_rule": {"op": "lt|lte|gt|gte|eq|between", "value": number, "value2": number|null,
               "strength": "required|preferred", "basis": "previous|projected", "period": string|null}|null,
 "duty_priority": "lowest_first"|null,
 "min_duties": int|null, "max_duties": int|null, "max_per_day": int|null,
 "classes_suspended": bool|null, "avoid_teaching": bool|null, "prohibit_consecutive": bool|null,
 "exclude_names": [string], "additional_constraints": [string]
}
Rules: copy only what the message states. Never invent a value, a date, a name or a time. "duty count less than 12" is a
duty_rule on faculty history, whereas "maximum 4 duties" is max_duties for this examination."""


# ---------------------------------------------------------------------------------------------- validation
def validate_payload(p: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(clean payload, None) or (None, the first problem)."""

    if not isinstance(p, dict):
        return None, "the reply must be a JSON object"
    out: Dict[str, Any] = {}

    def ints(key: str, lo: int, hi: int) -> Optional[str]:
        v = p.get(key)
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            return f"{key} must be an integer between {lo} and {hi}"
        out[key] = v
        return None

    for key, lo, hi in (("room_count", 1, 500), ("per_room", 1, 50), ("min_duties", 0, 200), ("max_duties", 0, 200),
                        ("max_per_day", 1, 10)):
        err = ints(key, lo, hi)
        if err:
            return None, err
    for key in ("classes_suspended", "avoid_teaching", "prohibit_consecutive", "all_faculty_departments"):
        v = p.get(key)
        if v is not None:
            if not isinstance(v, bool):
                return None, f"{key} must be true, false or null"
            out[key] = v
    for key in ("student_departments", "faculty_departments", "rooms", "exclude_names", "additional_constraints"):
        v = p.get(key) or []
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            return None, f"{key} must be a list of non-empty strings"
        out[key] = [x.strip() for x in v]
    if p.get("exam_type") is not None:
        if not isinstance(p["exam_type"], str):
            return None, "exam_type must be a string"
        out["exam_type"] = p["exam_type"].strip() or None
    dates = []
    for d in p.get("dates") or []:
        try:
            dates.append(date.fromisoformat(str(d)))
        except ValueError:
            return None, f"'{d}' is not a YYYY-MM-DD date"
    out["dates"] = dates
    sems = p.get("semesters") or []
    if not isinstance(sems, list) or not all(isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= 20 for x in sems):
        return None, "semesters must be a list of integers between 1 and 20"
    out["semesters"] = sems
    yrs = p.get("years") or []
    if not isinstance(yrs, list) or not all(x == "final" or (isinstance(x, int) and 1 <= x <= 8) for x in yrs):
        return None, "years must be integers 1-8 or \"final\""
    out["years"] = yrs
    shifts = []
    for s in p.get("shifts") or []:
        if not (isinstance(s, dict) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(s.get("start", "")))
                and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(s.get("end", "")))):
            return None, "each shift needs 24-hour 'start' and 'end' as HH:MM"
        if s["end"] <= s["start"]:
            return None, "a shift ends before it starts"
        shifts.append({"label": str(s.get("label") or "").strip(), "start": s["start"], "end": s["end"]})
    out["shifts"] = shifts
    if p.get("duty_rule") is not None:
        err = duty_rules.DutyRule.validate(p["duty_rule"])
        if err:
            return None, err
        out["duty_rule"] = p["duty_rule"]
    if p.get("duty_priority") not in (None, "lowest_first"):
        return None, "duty_priority must be 'lowest_first' or null"
    out["duty_priority"] = p.get("duty_priority")
    return out, None


# ---------------------------------------------------------------------------------------------- grounding
def _numbers_in(text: str) -> set:
    nums = {int(x) for x in re.findall(r"\d+", text)}
    nums |= {v for w, v in lx.number_words().items() if re.search(rf"\b{w}\b", text, re.I)}
    ords = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8}
    nums |= {v for w, v in ords.items() if re.search(rf"\b{w}\b", text, re.I)}
    nums |= {1, 2, 3}      # "i", "ii", "iii" roman numerals are handled by the rule reader; low counts via words above
    return nums


def _grounded_date(d: date, text: str) -> bool:
    low = text.lower()
    month_names = [m for m, i in MONTHS.items() if i == d.month]
    day_ok = re.search(rf"(?<!\d){d.day}(?!\d)", low) or d.day in {lx.to_number(w) for w in re.findall(r"[a-z]+", low) if lx.to_number(w)}
    month_ok = any(m in low for m in month_names) or re.search(rf"(?<!\d){d.month}(?!\d)", low)
    return bool(day_ok and month_ok)


def _grounded_time(hhmm: str, text: str) -> bool:
    h = int(hhmm[:2])
    twelve = h % 12 or 12
    return bool(re.search(rf"(?<!\d)(?:{h}|{twelve:d}|{h:02d})(?!\d)", text))


def apply_payload(req: ExamRequest, payload: Dict[str, Any], text: str, today: date,
                  dept_finder: Optional[Callable], settings: Any) -> None:
    """Merge a validated model payload into `req` below the rule reader, rejecting ungrounded values."""

    clean, err = validate_payload(payload)
    if clean is None:
        req.ambiguous.setdefault("llm_error", err)
        return
    nums = _numbers_in(text)

    def fill(attr: str, value: Any, grounded: bool = True) -> None:
        if value in (None, [], {}) or not grounded:
            return
        cur = getattr(req, attr)
        if cur in (None, [], {}):
            setattr(req, attr, value)
            req.mark(attr, SRC_LLM)
        elif cur != value and attr in ("room_count", "per_room", "min_duties", "max_duties"):
            req.ambiguous[f"llm_{attr}"] = {"rules": cur, "model": value}

    for k in ("room_count", "per_room", "min_duties", "max_duties", "max_per_day"):
        fill(k, clean.get(k), clean.get(k) in nums)
    for k in ("classes_suspended", "avoid_teaching", "prohibit_consecutive"):
        if clean.get(k) is not None and getattr(req, k) is None:
            setattr(req, k, clean[k])
            req.mark(k, SRC_LLM)
    fill("exam_type", clean.get("exam_type"), bool(clean.get("exam_type")) and clean["exam_type"].split()[0].lower()[:4] in text.lower())
    ds = [d for d in clean["dates"] if _grounded_date(d, text)]
    if ds and not (req.explicit_dates or req.start):
        req.explicit_dates = sorted(set(ds))
        req.mark("explicit_dates", SRC_LLM)
    sh = [s for s in clean["shifts"] if _grounded_time(s["start"], text) and _grounded_time(s["end"], text)]
    if sh and not req.shifts:
        used: Dict[str, int] = {}
        for i, s in enumerate(sh):
            label = s["label"] or settings.shift_label_template.format(n=i + 1)
            used[label.lower()] = used.get(label.lower(), 0) + 1
            if used[label.lower()] > 1:
                label = f"{label} {used[label.lower()]}"
            s["label"] = label.capitalize()
        req.shifts = sh
        req.mark("shifts", SRC_LLM)
    fill("semesters", [s for s in clean["semesters"] if s in nums])
    fill("years", clean["years"])
    if clean.get("rooms") and not req.rooms and all(r.lower() in text.lower() for r in clean["rooms"]):
        req.rooms = clean["rooms"]
        req.mark("rooms", SRC_LLM)
    if clean.get("duty_rule") and not req.duty_rule and float(clean["duty_rule"]["value"]) in {float(n) for n in nums}:
        req.duty_rule = {**{"strength": "required", "basis": settings.duty_rule_default_basis, "period": None, "text": "", "value2": None},
                         **clean["duty_rule"]}
        req.mark("duty_rule", SRC_LLM)
    if clean.get("duty_priority") and not req.duty_priority:
        req.duty_priority = "lowest_first"
        req.mark("duty_priority", SRC_LLM)
    if clean.get("all_faculty_departments") and req.all_faculty_departments is None:
        req.all_faculty_departments = True
        req.mark("all_faculty_departments", SRC_LLM)
    # departments are only accepted when they resolve against the database's own departments
    if dept_finder is not None:
        for key, attr in (("student_departments", "student_departments"), ("faculty_departments", "dept_include")):
            for phrase in clean.get(key, []):
                found, _ = dept_finder(f"{phrase} faculty")
                for f in found:
                    if f.label not in getattr(req, attr):
                        getattr(req, attr).append(f.label)
                        req.mark(attr, SRC_LLM)
    req.exclude_names = list(dict.fromkeys(req.exclude_names + [n for n in clean.get("exclude_names", []) if n.lower() in text.lower()]))
    for c in clean.get("additional_constraints", []):
        if c not in req.unmapped and not any(c.lower() in u.lower() or u.lower() in c.lower() for u in req.unmapped):
            req.unmapped.append(c)


# ---------------------------------------------------------------------------------------------- the model call
class LLMExtractor:
    """Anthropic client wrapper.  ``client`` may be injected (tests use a scripted fake)."""

    def __init__(self, client: Any = None, model: Optional[str] = None, max_retries: int = 1):
        self._client = client
        self.model = model or os.environ.get("FACULTY_LLM_MODEL", "claude-sonnet-5-5")
        self.max_retries = max_retries

    @staticmethod
    def available() -> bool:
        if os.environ.get("FACULTY_LLM_OFF") == "1" or not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        try:
            import anthropic  # noqa: F401
            return True
        except Exception:
            return False

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(timeout=30.0, max_retries=1)
        return self._client

    def _call(self, system: str, messages: List[Dict[str, str]]) -> str:
        r = self.client.messages.create(model=self.model, max_tokens=900, system=system, messages=messages)
        return "".join(getattr(b, "text", "") for b in r.content)

    def extract(self, text: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        system = (f"You read examination-duty requests from a university faculty coordinator. Today is {context.get('today')}. "
                  "Resolve dates written without a year to the next occurrence. " + SCHEMA_HELP)
        messages = [{"role": "user", "content": text}]
        for attempt in range(self.max_retries + 1):
            raw = self._call(system, messages)
            m = re.search(r"\{.*\}", raw, re.S)
            problem = None
            if not m:
                problem = "no JSON object found"
            else:
                try:
                    clean, problem = validate_payload(json.loads(m.group(0)))
                    if clean is not None:
                        return json.loads(m.group(0))
                except ValueError as e:
                    problem = f"invalid JSON: {e}"
            messages = messages + [{"role": "assistant", "content": raw},
                                   {"role": "user", "content": f"That was not valid: {problem}. Return only the corrected JSON object."}]
        return None