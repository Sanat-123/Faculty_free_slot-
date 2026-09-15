"""
response_formatter.py

PRESENTATION LAYER ONLY.

This module turns already-computed, structured result data into the
final Markdown text shown to the user. It contains no scheduling,
matching, or query logic of its own - every number it prints comes
from a dict handed to it by the caller (e.g. faculty_chatbot.py or
engine/response_generator.py).

Keeping this separate from response_generator.py means:
  - response_generator.py / faculty_chatbot.py decide WHAT the
    answer is (which records, which totals, which breakdowns).
  - response_formatter.py decides how that answer LOOKS (Markdown
    tables, headings, ordering, spacing).

Nothing in this file should reference faculty names, class names,
subjects, days, semesters, or counts as literals - every formatter
function takes that information as an argument and renders whatever
it is given.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# =============================================================
# SHARED HELPERS
# =============================================================

_DAY_ORDER = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _ordinal(number: Any) -> str:

    """
    1 -> "1st", 2 -> "2nd", 3 -> "3rd", 5 -> "5th", ...
    Falls back to the original value as a string if it isn't a
    plain integer (e.g. an already-formatted label).
    """

    try:
        number = int(number)
    except (TypeError, ValueError):
        return str(number)

    if 10 <= (number % 100) <= 20:
        suffix = "th"
    else:
        suffix = {
            1: "st",
            2: "nd",
            3: "rd",
        }.get(number % 10, "th")

    return f"{number}{suffix}"


def _sorted_days(by_day: Dict[str, Any]) -> List[str]:

    """
    Order the days that actually exist in by_day using the
    natural Monday -> Sunday sequence as a PRESENTATION rule
    only. Any day name not in the standard week (or already in
    a different form) is appended afterwards in its original
    order rather than dropped.
    """

    present = list(by_day.keys())

    known = [
        d for d in _DAY_ORDER
        if d in {p.lower() for p in present}
    ]

    # Map back to the original-cased keys for known days, in
    # natural week order.
    lower_to_original = {p.lower(): p for p in present}

    ordered = [lower_to_original[d] for d in known]

    # Anything not recognized as a standard week day is kept,
    # appended in its original relative order, rather than
    # silently discarded.
    leftover = [
        p for p in present
        if p.lower() not in known
    ]

    return ordered + leftover


# =============================================================
# WORKLOAD DASHBOARD
# =============================================================

def format_workload_response(
    data: Dict[str, Any]
) -> str:

    """
    Render a structured workload result as a compact,
    dashboard-style Markdown response.

    Expected shape of `data` (all keys optional except
    "faculty" and "total" - anything missing is simply not
    shown, never invented):

        {
            "faculty": "Mr. Rajesh Rajaan",
            "total": 18,
            "theory": 8,
            "lab": 10,
            "by_day": {"monday": 3, "tuesday": 2, ...},
            "by_semester": {"5": 18, ...},
        }

    Only sections backed by real data are rendered. No
    fabricated rows, no assumed days/semesters.
    """

    faculty = str(data.get("faculty", "")).strip()
    total = data.get("total")

    lines: List[str] = []

    # ---------------------------------------------------
    # HEADER
    # ---------------------------------------------------

    lines.append("**Workload Analysis**")

    if faculty:
        lines.append(faculty)

    lines.append("")

    # ---------------------------------------------------
    # SUMMARY METRICS (TOTAL / THEORY / LAB)
    #
    # Rendered as a single-row Markdown table so it reads as
    # a compact metrics strip rather than a paragraph. Only
    # the metrics actually present in `data` are shown - if
    # theory/lab aren't available, only TOTAL is shown rather
    # than fabricating a 0.
    # ---------------------------------------------------

    metric_labels: List[str] = []
    metric_values: List[str] = []

    if total is not None:
        metric_labels.append("TOTAL")
        metric_values.append(f"**{total}**")

    if data.get("theory") is not None:
        metric_labels.append("THEORY")
        metric_values.append(f"**{data['theory']}**")

    if data.get("lab") is not None:
        metric_labels.append("LAB")
        metric_values.append(f"**{data['lab']}**")

    if metric_labels:

        lines.append(
            "| " + " | ".join(metric_labels) + " |"
        )
        lines.append(
            "|" + "|".join(
                ":---:" for _ in metric_labels
            ) + "|"
        )
        lines.append(
            "| " + " | ".join(metric_values) + " |"
        )
        lines.append(
            "| " + " | ".join(
                "Classes" for _ in metric_labels
            ) + " |"
        )
        lines.append("")

    # ---------------------------------------------------
    # WEEKLY TEACHING LOAD
    #
    # Only the days actually present in the data are shown -
    # a day with no classes simply doesn't appear, rather than
    # being invented as a zero row.
    # ---------------------------------------------------

    by_day = data.get("by_day") or {}

    if by_day:

        lines.append("**Weekly Teaching Load**")
        lines.append("")
        lines.append("| Day | Classes |")
        lines.append("|---|---:|")

        for day_key in _sorted_days(by_day):

            day_label = str(day_key).strip().capitalize()
            lines.append(
                f"| {day_label} | {by_day[day_key]} |"
            )

        lines.append("")

    # ---------------------------------------------------
    # SEMESTER DISTRIBUTION
    #
    # Supports any number of semesters - shows every semester
    # actually present, sorted numerically where possible.
    # ---------------------------------------------------

    by_semester = data.get("by_semester") or {}

    if by_semester:

        lines.append("**Semester Distribution**")
        lines.append("")
        lines.append("| Semester | Classes |")
        lines.append("|---|---:|")

        def _semester_sort_key(item):
            key = item[0]
            try:
                return (0, int(key))
            except (TypeError, ValueError):
                return (1, str(key))

        for sem_key, count in sorted(
            by_semester.items(),
            key=_semester_sort_key
        ):

            label = f"{_ordinal(sem_key)} Semester"
            lines.append(f"| {label} | {count} |")

        lines.append("")

    return "\n".join(lines).rstrip()