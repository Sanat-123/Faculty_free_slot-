"""
duty_history.py - read a coordinator's "who has done how many exam duties" sheet
================================================================================

The coordinator uploads ONE file (PDF / Excel / CSV, any name, any layout that
has a name column plus numbers) listing every teacher and the exam duties
already assigned to them - as a single total, or split by month / day / exam.

Nothing about the file is assumed:

  * the file name is irrelevant (it is read, not looked up by name);
  * the name column is the column that is mostly text;
  * the count column is, in order of preference
        1. the column the coordinator named ("use the Total column"),
        2. a column whose header says total / duties / count ... (wording lives
           in config/duty_lexicon.json),
        3. if there are several numeric columns with no such header (month-wise
           or day-wise sheets) - the SUM of all of them,
        4. if the cells hold marks instead of numbers (a tick, "D", a date) -
           the number of marked cells in the row;
  * serial-number / id columns and "Total" rows are recognised and skipped;
  * a teacher on several rows or pages is added up.

The counts are saved as a *baseline* next to the confirmed exam duties
(exam_duty_baseline.json in the state folder).  ExamDutyStore.duty_counts()
adds the baseline to the duties confirmed in the chat, so every fairness rule
and every "fewer than N duties" constraint sees the true running total.
A new upload REPLACES the old baseline (new semester / new sheet).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_LEXICON_CACHE: Optional[dict] = None

_FALLBACK_LEXICON = {
    "limit_operators": {"<": ["less than", "fewer than", "under", "below", "<"],
                        "<=": ["at most", "up to", "<="],
                        ">": ["more than", "over", "above", ">"],
                        ">=": ["at least", "minimum", ">="],
                        "==": ["exactly", "equal to", "=="]},
    "limit_subject_words": ["duty", "duties", "assigned", "count"],
    "not_a_limit_followers": ["faculty", "teachers", "invigilators", "rooms", "hours", "am", "pm"],
    "name_header_words": ["name", "faculty", "teacher"],
    "count_header_words": ["total", "duties", "duty", "count", "assigned"],
    "serial_header_words": ["sr", "s.no", "sno", "serial", "#"],
    "ignore_header_words": ["id", "code", "phone", "email", "department"],
    "total_row_words": ["total", "grand total"],
    "clear_words": ["clear", "reset", "remove", "delete"],
    "history_words": ["duty history", "duty sheet", "duty record"],
}


def lexicon() -> dict:
    """config/duty_lexicon.json (wording only); built-in minimum if it is missing."""

    global _LEXICON_CACHE

    if _LEXICON_CACHE is None:

        lex = json.loads(json.dumps(_FALLBACK_LEXICON))
        here = Path(__file__).resolve().parent.parent / "config" / "duty_lexicon.json"

        try:
            with here.open(encoding="utf-8") as f:
                lex.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
        except (OSError, json.JSONDecodeError):
            pass

        _LEXICON_CACHE = lex

    return _LEXICON_CACHE


# ======================================================================
# reading tables from any file type
# ======================================================================

def _clean(cell: Any) -> str:
    return re.sub(r"\s+", " ", str(cell if cell is not None else "")).strip()


def _tables_from_pdf(path: str, max_pages: Optional[int] = None) -> List[List[List[str]]]:

    import pdfplumber

    tables: List[List[List[str]]] = []
    text_lines: List[str] = []

    with pdfplumber.open(path) as pdf:
        for page in (pdf.pages[:max_pages] if max_pages else pdf.pages):

            found = page.extract_tables() or []

            for t in found:
                rows = [[_clean(c) for c in r] for r in t if r and any(_clean(c) for c in r)]
                if rows:
                    tables.append(rows)

            if not found:
                text_lines += (page.extract_text() or "").splitlines()

    # no ruled tables anywhere: rebuild columns from the text lines
    # ("Dr. A Sharma   12   9   21"  ->  [name, 12, 9, 21])
    if not tables and text_lines:

        rows = []

        for line in text_lines:

            parts = re.split(r"\s{2,}|\t", line.strip())

            if len(parts) < 2:
                m = re.match(r"^(.*?[A-Za-z].*?)\s+((?:\d+(?:\.\d+)?\s*)+)$", line.strip())
                parts = [m.group(1)] + m.group(2).split() if m else []

            if len(parts) >= 2:
                rows.append([_clean(p) for p in parts])

        if rows:
            tables.append(rows)

    return tables


def _tables_from_excel(path: str) -> List[List[List[str]]]:

    import pandas as pd

    out = []

    for _, frame in pd.read_excel(path, sheet_name=None, header=None, dtype=str).items():
        rows = [[_clean(c) if str(c) != "nan" else "" for c in r] for r in frame.values.tolist()]
        rows = [r for r in rows if any(r)]
        if rows:
            out.append(rows)

    return out


def _tables_from_csv(path: str) -> List[List[List[str]]]:

    import csv

    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = [[_clean(c) for c in r] for r in csv.reader(f, dialect) if any(_clean(c) for c in r)]

    return [rows] if rows else []


def read_tables(path: str, max_pages: Optional[int] = None) -> List[List[List[str]]]:

    ext = os.path.splitext(path)[1].lower()

    if ext == ".pdf":
        return _tables_from_pdf(path, max_pages)
    if ext in (".xlsx", ".xlsm", ".xls"):
        return _tables_from_excel(path)
    if ext in (".csv", ".tsv", ".txt"):
        return _tables_from_csv(path)

    raise ValueError(f"unsupported file type: {ext or 'unknown'}")


# ======================================================================
# understanding one table
# ======================================================================

def _int_prefix(cell: str) -> Optional[float]:
    """'12' -> 12 ; '12 (Nov 3, Nov 4)' -> 12 ; 'D' -> None"""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\b(?!\s*[/\-:]\s*\d)", cell or "")
    return float(m.group(1)) if m else None


def _is_name_like(cell: str) -> bool:
    return bool(re.search(r"[A-Za-z]{2,}", cell or "")) and _int_prefix(cell) is None


def _header_has(header: str, words: List[str]) -> bool:
    h = header.lower()
    return any(re.search(rf"(?<![a-z]){re.escape(w.lower())}(?![a-z])", h) for w in words)


def _find_header(rows: List[List[str]]) -> int:
    """
    Index of the header row, -1 if there is none: the first of the top few rows
    that has at least two cells, no purely numeric cell (a data row has the
    counts), and is followed by another multi-cell row.  Title lines (one cell)
    are skipped.
    """

    for i, row in enumerate(rows[:6]):

        cells = [c for c in row if c]

        if len(cells) < 2 or i + 1 >= len(rows):
            continue

        if any(re.fullmatch(r"\d+(?:\.\d+)?", c) for c in cells):
            continue

        if sum(1 for c in rows[i + 1] if c) >= 2 and any(_is_name_like(c) for c in cells):
            return i

    return -1


class ParseReport(dict):
    """counts + how the file was understood (shown to the coordinator)."""


def parse_table(rows: List[List[str]], count_column: Optional[str] = None) -> Optional[ParseReport]:

    lex = lexicon()

    if len(rows) < 2:
        return None

    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]

    h = _find_header(rows)
    header = rows[h] if h >= 0 else [""] * width
    body = rows[h + 1:] if h >= 0 else rows

    body = [r for r in body if any(r)]
    if len(body) < 2:
        return None

    # ---- the name column: mostly text; header wording only breaks ties ----
    def text_share(ci):
        cells = [r[ci] for r in body if r[ci]]
        return sum(_is_name_like(c) for c in cells) / len(cells) if cells else 0

    name_ci = max(
        range(width),
        key=lambda ci: (round(text_share(ci), 1), _header_has(header[ci], lex["name_header_words"]), -ci),
    )

    if text_share(name_ci) < 0.6:
        return None

    # ---- candidate count columns ----
    numeric, marks = [], []

    for ci in range(width):

        if ci == name_ci or _header_has(header[ci], lex["ignore_header_words"]):
            continue

        cells = [r[ci] for r in body if r[ci]]
        if not cells:
            continue

        nums = [_int_prefix(c) for c in cells]
        share = sum(n is not None for n in nums) / len(cells)

        if share >= 0.7:

            values = [n for n in nums if n is not None]
            sequential = values == [float(k) for k in range(1, len(values) + 1)] and len(values) >= 3
            county = _header_has(header[ci], lex["count_header_words"])
            is_serial = not county and (
                _header_has(header[ci], lex["serial_header_words"]) or sequential)

            if not is_serial:
                numeric.append(ci)

        elif len(set(cells)) <= max(3, len(cells) // 3) and not any(_is_name_like(c) and len(c) > 12 for c in cells):
            marks.append(ci)                        # tick / "D" / "✓" style cells

    mode, chosen = "", []

    if count_column:
        chosen = [ci for ci in numeric if count_column.lower() in header[ci].lower()]
        mode = f"column “{count_column}”" if chosen else ""

    if not chosen and numeric:

        hinted = [ci for ci in numeric if _header_has(header[ci], lex["count_header_words"])]

        if hinted:
            chosen = hinted[-1:] if len(hinted) > 1 else hinted     # the right-most "Total"
            mode = f"column “{header[chosen[0]]}”"
        elif len(numeric) == 1:
            chosen = numeric
            mode = f"column “{header[numeric[0]] or numeric[0] + 1}”"
        else:
            chosen = numeric
            mode = f"sum of {len(numeric)} columns (" + ", ".join(header[c] or str(c + 1) for c in numeric[:6]) \
                   + (", …" if len(numeric) > 6 else "") + ")"

    if not chosen and marks:
        chosen, mode = marks, f"marked cells across {len(marks)} columns"
        as_marks = True
    else:
        as_marks = False

    if not chosen:
        return None

    counts: Dict[str, float] = {}
    total_words = lex["total_row_words"]

    for r in body:

        name = _clean(r[name_ci])

        if not name or not _is_name_like(name):
            continue

        if re.fullmatch(rf"(?i)\W*(?:{'|'.join(map(re.escape, total_words))})\W*", name):
            continue                                                    # a "Total" row

        if as_marks:
            value = sum(1 for ci in chosen if r[ci])
        else:
            value = sum(_int_prefix(r[ci]) or 0 for ci in chosen)

        counts[name] = counts.get(name, 0) + value

    if len(counts) < 2:
        return None

    return ParseReport(counts=counts, mode=mode, header=[header[c] for c in chosen])


def parse_duty_history(path: str, count_column: Optional[str] = None) -> Optional[ParseReport]:
    """
    {name: duties} for the file at `path`, or None when it is not a duty sheet.
    Tables from every page / sheet are merged; the same name is added up.
    """

    merged: Dict[str, float] = {}
    modes: List[str] = []

    for rows in read_tables(path):

        if is_timetable_table(rows):
            continue

        got = parse_table(rows, count_column)

        if not got:
            continue

        modes.append(got["mode"])

        for name, n in got["counts"].items():
            merged[name] = merged.get(name, 0) + n

    if len(merged) < 2:
        return None

    return ParseReport(
        counts={k: int(v) if float(v).is_integer() else v for k, v in merged.items()},
        mode="; ".join(dict.fromkeys(modes)),
        source=os.path.basename(path),
    )


_TIME_RANGE = re.compile(r"\d{1,2}[:.]\d{2}\s*(?:am|pm)?\s*[-–—to]+\s*\d{1,2}[:.]\d{2}", re.I)


def _weekday_forms() -> set:
    """Day names, and their 2- and 3-letter forms, from the project's own lexicon."""

    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    try:
        here = Path(__file__).resolve().parent.parent / "config" / "nlu_lexicon.json"
        with here.open(encoding="utf-8") as f:
            days = json.load(f).get("weekdays") or days
    except (OSError, json.JSONDecodeError):
        pass

    forms = set()

    for d in days:
        d = str(d).lower()
        forms |= {d, d[:2], d[:3]}

    return forms


def is_timetable_table(rows: List[List[str]]) -> bool:
    """A day x time-slot grid (or a list of weekdays): never a duty sheet."""

    head = [c for r in rows[:3] for c in r if c]

    if sum(1 for c in head if _TIME_RANGE.search(c)) >= 3:
        return True

    forms = _weekday_forms()

    for ci in range(max(len(r) for r in rows)):
        col = [r[ci].strip().lower().rstrip(".") for r in rows if ci < len(r) and r[ci].strip()]
        if sum(1 for c in col if c in forms) >= 3:
            return True

    for r in rows[:2]:
        if sum(1 for c in r if c.strip().lower().rstrip(".") in forms) >= 3:
            return True

    return False


def looks_like_duty_history(path: str) -> bool:
    """
    True for a name + duty-count sheet; False for timetables (day / time-slot
    grids, one page per teacher ...).  Used to route an upload to the right
    importer; decided from the file's CONTENT, never its name.
    """

    try:
        tables = read_tables(path, max_pages=3)      # a quick look is enough
    except Exception:
        return False

    if not tables or any(is_timetable_table(rows) for rows in tables):
        return False

    return any(parse_table(rows) is not None for rows in tables)


# ======================================================================
# the saved baseline
# ======================================================================

class DutyBaselineStore:
    """Persistent {name: duties-before-this-chat}, next to exam_duties.json."""

    def __init__(self, path):
        self.path = Path(path)

    def _read(self) -> Dict[str, Any]:
        try:
            with self.path.open(encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def exists(self) -> bool:
        return bool(self._read().get("counts"))

    def counts(self) -> Dict[str, float]:
        return dict(self._read().get("counts") or {})

    def info(self) -> Dict[str, Any]:
        d = self._read()
        return {k: v for k, v in d.items() if k != "counts"} | {"people": len(d.get("counts") or {})}

    def save(self, counts: Dict[str, float], source: str = "", mode: str = "", **extra) -> None:

        self.path.parent.mkdir(parents=True, exist_ok=True)

        tmp = self.path.with_suffix(self.path.suffix + ".tmp")

        with tmp.open("w", encoding="utf-8") as f:
            json.dump({"source": source, "mode": mode,
                       "loaded_at": datetime.now(timezone.utc).isoformat(),
                       "counts": counts, **extra}, f, indent=2, ensure_ascii=False)

        tmp.replace(self.path)

    def clear(self) -> bool:
        try:
            self.path.unlink()
            return True
        except OSError:
            return False

    def reconcile(self, faculty, titles: set) -> Dict[str, List[str]]:
        """
        Rewrite names to the timetable's spelling (typos / missing titles
        tolerated) when exactly one timetable teacher matches.  Names that match
        nobody - or several people - are kept as written and listed.
        """

        from scheduling.exam_duty_requests import resolve_faculty_phrase

        data = self._read()
        counts = data.get("counts") or {}

        if not counts or data.get("reconciled_with") == len(faculty):
            return {"unmatched": data.get("unmatched", []), "ambiguous": data.get("ambiguous", [])}

        merged: Dict[str, float] = {}
        unmatched, ambiguous = [], []

        for raw, n in counts.items():

            exact = [f for f in faculty if re.sub(r"\s+", " ", f).strip().lower()
                     == re.sub(r"\s+", " ", raw).strip().lower()]

            hits = exact if len(exact) == 1 else resolve_faculty_phrase(raw, faculty, titles)

            if len(hits) == 1:
                merged[hits[0]] = merged.get(hits[0], 0) + n
            else:
                merged[raw] = merged.get(raw, 0) + n
                (ambiguous if hits else unmatched).append(raw)

        data.update(counts=merged, unmatched=unmatched, ambiguous=ambiguous,
                    reconciled_with=len(faculty))

        self.path.parent.mkdir(parents=True, exist_ok=True)

        tmp = self.path.with_suffix(self.path.suffix + ".tmp")

        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        tmp.replace(self.path)

        return {"unmatched": unmatched, "ambiguous": ambiguous}


# ======================================================================
# the chat constraint:  "fewer than 50 duties", "at most 40", "< 30"
# ======================================================================

_OPS = {
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
}


def limit_text(op: str, value) -> str:
    words = {"<": "fewer than", "<=": "at most", ">": "more than", ">=": "at least", "==": "exactly"}
    v = int(value) if float(value).is_integer() else value
    return f"{words[op]} {v}"


def passes_limit(count: float, limit: Optional[Tuple[str, float]]) -> bool:
    return True if not limit else _OPS[limit[0]](count, limit[1])


def parse_duty_limit(text: str) -> Optional[Tuple[Tuple[str, float], Tuple[int, int]]]:
    """
    ((operator, number), (start, end)) for the first duty-count constraint in
    `text`, or None.  `(start, end)` is the span of the phrase so the number is
    not read again as a date / time / head-count.

    A number only counts as a limit when it is NOT followed by a people / room /
    time word ("at most 5 faculty" is a head-count, "under 3 rooms" a room count).
    The operator words come from config/duty_lexicon.json.
    """

    lex = lexicon()
    low = text.lower()

    # "50 or fewer duties", "49 or less", "20 and above": the number comes first
    blockers_post = "|".join(re.escape(w) for w in sorted(lex["not_a_limit_followers"], key=len, reverse=True))

    for op, phrases_post in lex.get("limit_postfix_operators", {}).items():
        for phrase in sorted(phrases_post, key=len, reverse=True):
            m = re.search(
                rf"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:exam\s+)?(?:dut(?:y|ies)\s*)?{re.escape(phrase)}\b"
                rf"(?!\s*(?:{blockers_post})\b)",
                low,
            )
            if m:
                return (op, float(m.group(1))), (m.start(), m.end())

    phrases = sorted(((p, op) for op, ps in lex["limit_operators"].items() for p in ps),
                     key=lambda x: -len(x[0]))

    blockers = "|".join(re.escape(w) for w in sorted(lex["not_a_limit_followers"], key=len, reverse=True))

    for phrase, op in phrases:

        word = re.search(r"[a-z]", phrase) is not None
        escaped = re.escape(phrase).replace("than", "th[ae]n")      # "less then 50"
        lead = rf"(?<![a-z]){escaped}(?![a-z])" if word else escaped

        for m in re.finditer(
            rf"{lead}\s*(?:(?:the\s+)?(?:exam\s+)?(?:dut(?:y|ies)|count|assigned)\s*(?:of|as|is|than|to)?\s*)?"
            rf"(\d+(?:\.\d+)?)(?!\s*[:/\-]\s*\d)(?![\d.]*\s*(?:{blockers})\b)",
            low,
        ):
            return (op, float(m.group(1))), (m.start(), m.end())

    return None