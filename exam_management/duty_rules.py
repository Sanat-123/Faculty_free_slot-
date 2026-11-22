"""
Duty-count rules: "who have a duty count less than 12", "fewer than 8 duties", "no duties so far",
"the lowest duty count" ...

A rule is DATA (operator, value, strength, basis, period) - never a function of one sentence.  The value always
comes from the coordinator's message; the wording that signals an operator, a duty noun or a preference lives in
``config/exam_lexicon.json``.

Counting basis
    previous   - the rule applies to the duties a person had BEFORE this allocation (history + approved duties of
                 other examinations).  Default; configurable with ``ExamSettings.duty_rule_default_basis``.
    projected  - the rule applies to the total AFTER this allocation (history + new duties); used when the
                 coordinator says "in total", "should not exceed" ...

Period
    The recorded duty history has no dates (a cumulative sheet plus completed examinations), so a period such as
    "this semester" cannot be separated from the rest.  It is kept as stated so reports can say so honestly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import lexicon as lx

OPS = ("lt", "lte", "gt", "gte", "eq", "between")
SYMBOL = {"lt": "<", "lte": "≤", "gt": ">", "gte": "≥", "eq": "=", "between": "between"}
WORDS = {"lt": "less than", "lte": "at most", "gt": "more than", "gte": "at least", "eq": "exactly"}


@dataclass(frozen=True)
class DutyRule:
    op: str
    value: float
    value2: Optional[float] = None            # upper bound when op == "between"
    strength: str = "required"                # required | preferred
    basis: str = "previous"                   # previous | projected
    period: Optional[str] = None              # as stated (None = not stated)
    text: str = ""                            # the words the rule was read from

    def holds(self, count: float) -> bool:
        v = self.value
        return {"lt": count < v, "lte": count <= v, "gt": count > v, "gte": count >= v,
                "eq": abs(count - v) < 1e-9,
                "between": self.value2 is not None and v <= count <= self.value2}[self.op]

    def projected_cap(self, previous: float) -> Optional[int]:
        """For a rule on the TOTAL after this allocation: most NEW duties the person may take (None = no upper limit)."""

        import math
        if self.op == "lt":
            return max(0, math.floor(math.ceil(self.value) - 1 - previous))
        if self.op in ("lte", "eq"):
            return max(0, math.floor(self.value - previous))
        if self.op == "between" and self.value2 is not None:
            return max(0, math.floor(self.value2 - previous))
        return None

    def describe(self) -> str:
        n = lambda x: f"{x:g}"
        core = (f"between {n(self.value)} and {n(self.value2)}" if self.op == "between"
                else f"{WORDS[self.op]} {n(self.value)}")
        basis = "before this allocation" if self.basis == "previous" else "after this allocation (total)"
        mode = "preferred" if self.strength == "preferred" else "required"
        return f"duty count {core} ({basis}; {mode})"

    def to_dict(self) -> Dict[str, Any]:
        return {"op": self.op, "value": self.value, "value2": self.value2, "strength": self.strength,
                "basis": self.basis, "period": self.period, "text": self.text}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "DutyRule":
        return DutyRule(op=str(d["op"]), value=float(d["value"]),
                        value2=None if d.get("value2") is None else float(d["value2"]),
                        strength=str(d.get("strength") or "required"), basis=str(d.get("basis") or "previous"),
                        period=d.get("period"), text=str(d.get("text") or ""))

    @staticmethod
    def validate(d: Any) -> Optional[str]:
        """None when `d` is a well-formed rule, else the problem (used to check model / user supplied data)."""

        if not isinstance(d, dict):
            return "duty rule must be an object"
        if d.get("op") not in OPS:
            return f"duty rule operator must be one of {', '.join(OPS)}"
        try:
            v = float(d.get("value"))
        except (TypeError, ValueError):
            return "duty rule value must be a number"
        if v < 0 or v > 10000:
            return "duty rule value is out of range"
        if d["op"] == "between":
            try:
                v2 = float(d.get("value2"))
            except (TypeError, ValueError):
                return "a 'between' rule needs two numbers"
            if v2 < v:
                return "a 'between' rule's second number is smaller than the first"
        if d.get("strength") not in (None, "required", "preferred"):
            return "duty rule strength must be 'required' or 'preferred'"
        if d.get("basis") not in (None, "previous", "projected"):
            return "duty rule basis must be 'previous' or 'projected'"
        return None


# ---------------------------------------------------------------------------------------------- parsing
@dataclass(frozen=True)
class _Hit:
    start: int
    end: int
    op: str
    value: float
    value2: Optional[float] = None


def _op_hits(text: str) -> List[_Hit]:
    """Every "<comparison phrase> <number>" in the text (the longest phrase at a place wins)."""

    lex = lx.lexicon()
    num = lx.number_pattern()
    hits: List[_Hit] = []
    for op, phrases in lex.get("comparison_operators", {}).items():
        alt = "|".join(sorted(phrases, key=len, reverse=True))
        pat = re.compile(rf"(?<![A-Za-z])(?:{alt})\s*(?:a\s+|an\s+|the\s+)?(?:total\s+(?:of\s+)?)?(?:duty\s+count\s+(?:of\s+)?)?"
                         rf"(?P<n>{num})(?![\d:])\b", re.I)
        for m in pat.finditer(text):
            v = lx.to_number(m.group("n"))
            if v is not None:
                hits.append(_Hit(m.start(), m.end(), op, float(v)))
    for op, phrases in lex.get("postfix_operators", {}).items():
        alt = "|".join(phrases)
        for m in re.finditer(rf"(?P<n>{num})\s*(?:dut(?:y|ies)\s*)?(?:{alt})", text, re.I):
            v = lx.to_number(m.group("n"))
            if v is not None:
                hits.append(_Hit(m.start(), m.end(), op, float(v)))
    for m in re.finditer(rf"between\s+(?P<a>{num})\s+(?:and|to|&|-)\s+(?P<b>{num})\b", text, re.I):
        a, b = lx.to_number(m.group("a")), lx.to_number(m.group("b"))
        if a is not None and b is not None and a <= b:
            hits.append(_Hit(m.start(), m.end(), "between", float(a), float(b)))
    hits.sort(key=lambda h: (h.start, -(h.end - h.start)))
    kept: List[_Hit] = []
    for h in hits:
        if kept and h.start < kept[-1].end:
            continue
        kept.append(h)
    return kept


def _is_duty_value(text: str, h: _Hit) -> Tuple[bool, bool]:
    """(is the number about duties?, is a "who have / having / with" cue in front of it?)."""

    nouns = lx.alternation("duty_nouns")
    holder = lx.alternation("holder_cues")
    after = text[h.end:h.end + 40]
    before = text[max(0, h.start - 70):h.start]
    duty_after = bool(re.match(rf"\s*(?:more\s+|extra\s+|previous\s+|earlier\s+)?(?:{nouns})\b", after, re.I))
    duty_before = bool(re.search(rf"(?:{nouns})(?:\s+(?:is|are|of|=|:|be|being|should|must|shall|will|may|can|to|not|never|cannot))*\s*(?:a\s+|an\s+|the\s+)?$", before, re.I))
    holder_before = bool(re.search(rf"{holder}\s+(?:a\s+|an\s+|the\s+|any\s+)?(?:(?:total\s+)?(?:of\s+)?)?(?:{nouns})?\s*(?:is\s+|are\s+)?$", before, re.I)) \
        or bool(re.search(rf"{holder}\s*(?:a\s+|an\s+|the\s+)?$", before, re.I))
    return duty_after or duty_before, holder_before or duty_before


def _sentence_of(text: str, pos: int) -> str:
    for a, b in lx.sentence_spans(text):
        if a <= pos < b:
            return text[a:b]
    return text


def parse_duty_rules(text: str, default_basis: str = "previous") -> Tuple[Optional[DutyRule], List[Tuple[int, int]]]:
    """
    The duty-count rule in `text` (None when there is none) and the character spans that carry it, so the caller
    can keep those words away from other readers (a per-faculty "maximum 4 duties" cap is a different thing).
    """

    lex = lx.lexicon()
    spans: List[Tuple[int, int]] = []
    rule: Optional[DutyRule] = None
    cap_ops = set(lex.get("cap_language_operators", ["lte", "gte"]))
    cap_cue = lx.compiled("cap_language_cues")
    pref = lx.compiled("preference_words")
    proj = lx.compiled("projected_cues")
    nouns = lx.alternation("duty_nouns")

    for h in _op_hits(text):
        is_duty, holder = _is_duty_value(text, h)
        if not is_duty:
            continue
        sentence = _sentence_of(text, h.start)
        tail = text[h.end:h.end + 30]
        # "maximum 4 duties" / "at least 2 duties each": a per-faculty limit INSIDE this examination, not a rule on history
        if h.op in cap_ops and not holder and not proj.search(sentence):
            continue
        if h.op in cap_ops and cap_cue.search(tail):
            continue
        strength = "preferred" if pref.search(sentence) else "required"
        basis = "projected" if proj.search(sentence) else default_basis
        rule = DutyRule(op=h.op, value=h.value, value2=h.value2, strength=strength, basis=basis,
                        period=_period(sentence), text=text[h.start:h.end].strip())
        spans.append((h.start, h.end))
        break

    if rule is None:                                   # "who have not done any duty so far", "with no duties"
        zero = re.search(
            rf"(?:who\s+(?:have|has|had)|having|with|that\s+have)\s+(?:no|zero|not\s+(?:any|done|performed))\s+(?:{nouns})"
            rf"|(?:who\s+)?(?:have|has)(?:n'?t|\s+not)\s+(?:done|performed|had|received|got|carried\s+out)\s+(?:any\s+)?(?:{nouns})"
            rf"|(?:who\s+)?never\s+(?:done|performed|had)\s+(?:any\s+)?(?:{nouns})", text, re.I)
        if zero:
            sentence = _sentence_of(text, zero.start())
            rule = DutyRule("eq", 0.0, None, "preferred" if pref.search(sentence) else "required",
                            "projected" if proj.search(sentence) else default_basis, _period(sentence), zero.group(0).strip())
            spans.append((zero.start(), zero.end()))
    return rule, spans


def _period(sentence: str) -> Optional[str]:
    for label, pats in lx.lexicon().get("period_phrases", {}).items():
        if any(re.search(p, sentence, re.I) for p in pats):
            return label
    return None


def parse_priority(text: str) -> Tuple[Optional[str], List[Tuple[int, int]], Optional[str]]:
    """("lowest_first" | None, spans, period) - "select faculty with the lowest duty count", "balanced distribution"."""

    spans: List[Tuple[int, int]] = []
    period = None
    for pat in lx.lexicon().get("lowest_first", []):
        for m in re.finditer(pat, text, re.I):
            spans.append((m.start(), m.end()))
            period = period or _period(_sentence_of(text, m.start()))
    return ("lowest_first" if spans else None), spans, period


# ---------------------------------------------------------------------------------------------- evaluation
def count_for(rule: DutyRule, previous: float, new: float = 0.0) -> float:
    """The number the rule is judged on: before this allocation, or the projected total."""

    return previous + (new if rule.basis == "projected" else 0.0)


def check(rule: Optional[DutyRule], previous: float, new: float = 0.0) -> Optional[bool]:
    return None if rule is None else rule.holds(count_for(rule, previous, new))