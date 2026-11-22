"""
Language vocabulary for understanding examination requests.

All wording lives in ``config/exam_lexicon.json`` (override the file with the environment variable
``EXAM_MGMT_EXAM_LEXICON``).  Nothing in it is a department, person, room, date or number belonging to a
college - those come from the database or from the coordinator's own message.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Pattern

_DEFAULT = Path(__file__).resolve().parent.parent / "config" / "exam_lexicon.json"


def _path() -> Path:
    env = os.environ.get("EXAM_MGMT_EXAM_LEXICON")
    return Path(env) if env else _DEFAULT


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> Dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def lexicon() -> Dict[str, Any]:
    p = _path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = 0.0
    return _load(str(p), mtime)


def alternation(key: str, default: List[str] | None = None) -> str:
    """The lexicon list `key` as one regular-expression alternation ``(?:a|b|c)`` (longest first)."""

    items = lexicon().get(key, default or [])
    if isinstance(items, dict):
        items = [x for v in items.values() for x in v]
    items = sorted((str(i) for i in items), key=len, reverse=True)
    return "(?:" + "|".join(items) + ")" if items else r"(?!x)x"


def compiled(key: str, default: List[str] | None = None, flags: int = re.I) -> Pattern[str]:
    return re.compile(alternation(key, default), flags)


def number_words() -> Dict[str, int]:
    return {str(k).lower(): int(v) for k, v in lexicon().get("number_words", {}).items()}


def number_pattern() -> str:
    """A regular-expression fragment matching a count written as digits or as a word (``\\d+|one|two|...``)."""

    words = sorted(number_words(), key=len, reverse=True)
    return r"\d{1,3}" + ("|" + "|".join(words) if words else "")


def to_number(token: str) -> int | None:
    t = str(token).strip().lower()
    if t.isdigit():
        return int(t)
    return number_words().get(t)


_TITLES = {"dr", "prof", "professor", "mr", "mrs", "ms", "miss", "sir", "madam", "shri", "smt", "er", "ar", "doctor",
           "a.m", "p.m", "am", "pm", "no", "approx", "vs", "etc", "sr", "jr"}


def sentence_spans(text: str):
    """[(start, end)] of each sentence.  A full stop after a title (Dr.), inside a number (9.30) or in a.m./p.m. does not end one."""

    cuts = [0]
    for m in re.finditer(r"[.;!?\n]", text):
        i = m.start()
        if text[i] == ".":
            prev = re.findall(r"[A-Za-z]+(?:\.[A-Za-z]+)?", text[max(0, i - 6):i])
            if prev and prev[-1].lower() in _TITLES:
                continue
            if i + 1 < len(text) and text[i + 1] not in " \n":
                continue
            if i > 0 and text[i - 1].isdigit() and i + 1 < len(text) and text[i + 1].isdigit():
                continue
        cuts.append(i + 1)
    cuts.append(len(text))
    return [(a, b) for a, b in zip(cuts, cuts[1:]) if text[a:b].strip()]