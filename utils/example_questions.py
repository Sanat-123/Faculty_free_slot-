"""Example questions for the sidebar, built from the data the user has actually loaded."""

from __future__ import annotations

from typing import Any, Iterable, List


def build_example_questions(records: Iterable[Any]) -> List[str]:
    """
    Example questions using a real teacher / subject / class / day / slot from `records`
    (dicts as produced by the importers).  Until something is loaded, neutral placeholders
    are shown - no teacher, subject or class name is written into the program.
    """

    pick = {"teacher": None, "subject": None, "class_name": None, "day": None, "slot": None}
    for r in records or []:
        if not isinstance(r, dict):
            continue
        for key in pick:
            if not pick[key] and str(r.get(key) or "").strip():
                pick[key] = str(r[key]).strip()
        if all(pick.values()):
            break
    teacher = pick["teacher"] or "<faculty>"
    subject = pick["subject"] or "<subject>"
    klass = pick["class_name"] or "<class>"
    day = (pick["day"] or "<day>").title() if pick["day"] else "<day>"
    slot = pick["slot"] or "<slot>"
    return [
        f"Who is free on {day} slot {slot}?",
        f"Which faculty is free on {day} slot {slot}?",
        f"Is {teacher} free on {day} slot {slot}?",
        f"What is {teacher} teaching on {day}?",
        f"Who teaches {subject}?",
        f"What is the timetable of {klass} on {day}?",
        f"Which room is free on {day} slot {slot}?",
    ]