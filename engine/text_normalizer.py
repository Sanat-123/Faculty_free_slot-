"""
text_normalizer.py - turn typo-ridden human wording into something the parsers can read
=======================================================================================

A faculty coordinator types fast: "1: to 2:30", "faculities", "less then 50",
"9.00 to 10.30", "0900-1030", "teachrs".  Rather than teaching every parser every
slip, the message is cleaned ONCE before it is read:

  1. clock slips   "1: to 2:30" -> "1 to 2:30";  "9.00 to 10.30" -> "9:00 to 10:30";
                   "0900-1030" -> "09:00 to 10:30";  "a.m." -> "am"
  2. "less then"   -> "less than" (only after a comparison word)
  3. spelling      words close to a word of the project's vocabulary are corrected
                   ("faculities" -> "faculties", "invigilaters" -> "invigilators").

The vocabulary is WORDING, not data: it lives in config/duty_lexicon.json
("spell_vocabulary") so a new phrasing needs no code change.  Names are never
touched - pass the faculty names (and any other word to keep) as `protected`.
"""

from __future__ import annotations

import difflib
import re
from typing import Iterable, List, Set

_COMPARISON = r"(?:less|lesser|fewer|lower|more|greater|higher|smaller|bigger|larger|fewer)"


def fix_clock_slips(text: str) -> str:
    """Repair time notations without changing what they mean."""

    t = text

    # "a.m." / "p.m." -> am / pm
    t = re.sub(r"\b([ap])\.\s?m\b\.?", lambda m: m.group(1).lower() + "m", t, flags=re.I)

    # "1: to 2:30", "9 : to" - a colon with no minutes
    t = re.sub(r"(?<![\d:])(\d{1,2})\s*:(?!\s*\d)", r"\1", t)

    # "9 : 30" -> "9:30"
    t = re.sub(r"(?<![\d:.])(\d{1,2})\s+:\s*([0-5]\d)(?!\d)", r"\1:\2", t)

    # "0900-1030", "0900 to 1030" -> "09:00 to 10:30"
    t = re.sub(
        r"(?<![\d:./-])([01]\d|2[0-3])([0-5]\d)\s*(?:-|–|—|to|till|until|upto)\s*([01]\d|2[0-3])([0-5]\d)(?![\d:./-])",
        r"\1:\2 to \3:\4",
        t,
        flags=re.I,
    )

    # "9.00 to 10.30" -> "9:00 to 10:30", only when the sentence talks about times
    if re.search(r"\b(?:to|till|until|upto|am|pm|between|from)\b|[-–—]", t, re.I):
        t = re.sub(
            r"(?<![\d.:/-])(\d{1,2})\.([0-5]\d)(?![\d.:/]|\s*(?:%|duties|duty|faculty))",
            r"\1:\2",
            t,
        )

    return t


def fix_comparison_then(text: str) -> str:
    """'less then 50' / 'more then 10' -> 'than' (but not 'and then')."""

    return re.sub(rf"\b({_COMPARISON})\s+then\b", r"\1 than", text, flags=re.I)


def fix_spelling(
    text: str,
    vocabulary: Iterable[str],
    protected: Iterable[str] = (),
    cutoff: float = 0.84,
    min_length: int = 5,
) -> str:
    """Correct words that are one or two slips away from a vocabulary word."""

    vocab: List[str] = sorted({str(w).lower() for w in vocabulary if len(str(w)) >= min_length})
    known: Set[str] = {str(w).lower() for w in vocabulary} | {str(w).lower() for w in protected}

    if not vocab:
        return text

    def repair(match: "re.Match[str]") -> str:

        word = match.group(0)
        low = word.lower()

        if low in known or len(low) < min_length:
            return word

        best = difflib.get_close_matches(low, vocab, n=1, cutoff=cutoff)

        if not best:
            return word

        pick = best[0]

        # "confirmed" / "teaches" are other forms of "confirm" / "teacher"-like
        # words, not misspellings: when the two words differ only in their last
        # two letters, the word is left alone
        stem = len(__import__("os").path.commonprefix([low, pick]))

        if low != pick and stem >= max(len(low), len(pick)) - 2:
            return word

        # same first letter and nearly the same length: a slip, not another word
        if pick[0] != low[0] or abs(len(pick) - len(low)) > 2:
            return word

        return pick.capitalize() if word[:1].isupper() else pick

    return re.sub(r"[A-Za-z]{%d,}" % min_length, repair, text)


def normalize(text: str, vocabulary: Iterable[str] = (), protected: Iterable[str] = ()) -> str:
    """The full cleanup, in a safe order."""

    out = fix_clock_slips(str(text or ""))
    out = fix_comparison_then(out)
    out = fix_spelling(out, vocabulary, protected)

    return out