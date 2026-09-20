"""
Formatting helpers shared by the smart query handlers.

All output is Markdown, in one consistent style:

* a bold headline with the answer's scope and a count,
* numbered lines ("1. name") for name lists,
* pipe tables for schedules,
* short italic notes for assumptions.
"""

from engine.timetable_model import consecutive_runs


def title_day(day):

    return str(day).capitalize()


def plural(count, word, plural_word=None):

    if count == 1:
        return f"{count} {word}"

    return f"{count} {plural_word or word + 's'}"


def numbered(items, start=1):

    return [f"{i}. {item}" for i, item in enumerate(items, start=start)]


def md_table(headers, rows):

    lines = [
        "| " + " | ".join(str(h) for h in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]

    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                str(c).replace("|", "/") if c not in (None, "") else "—"
                for c in row
            )
            + " |"
        )

    return lines


def slot_span_text(model, slots):
    """[5,6,7] -> '5-7'; [3] -> '3'"""

    slots = sorted(slots)

    if len(slots) == 1:
        return str(slots[0])

    return f"{slots[0]}-{slots[-1]}"


def slots_text(model, slots, all_slots_word=True):
    """
    Human text for a set of slots with clock times, merging runs:
    'slot 3 (10:15 - 11:15)'  /  'slots 5-7 (12:00 - 15:00)'  /
    'slot 1 (...), slots 3-4 (...)'.
    """

    slots = sorted(set(slots))

    if not slots:
        return "no slots"

    if all_slots_word and len(slots) > 1 and slots == list(model.slots):
        return f"all slots ({slot_span_text(model, slots)})"

    parts = []

    for run in consecutive_runs(slots):

        clock = model.range_paren(run[0], run[-1])

        if len(run) == 1:
            parts.append(f"slot {run[0]}{clock}")
        else:
            parts.append(f"slots {run[0]}-{run[-1]}{clock}")

    return ", ".join(parts)


def days_text(model, days):

    days = list(days)

    if len(days) > 1 and set(days) == set(model.days):
        return (
            f"every day, {title_day(model.days[0])}"
            f"–{title_day(model.days[-1])}"
        )

    return ", ".join(title_day(d) for d in days)


def scope_text(model, days, slots):
    """'Monday, slot 3 (10:15 - 11:15)'"""

    bits = []

    if days:
        bits.append(days_text(model, days))

    if slots:
        bits.append(slots_text(model, slots))

    return ", ".join(bits)


def event_line(event, include_teacher=False):
    """'DE — 3CS-F — room 304 — Group 1' for one event."""

    bits = []

    if event.get("subject"):
        bits.append(event["subject"])

    if event.get("class_name"):
        bits.append(event["class_name"])

    if event.get("group_name"):
        bits.append(event["group_name"])

    if event.get("room"):
        bits.append(f"room {event['room']}")

    if include_teacher and event.get("teacher"):
        bits.append(event["teacher"])

    return " — ".join(bits) if bits else "class"


def block_rows(model, blocks, columns):
    """Rows for a schedule table from merged blocks."""

    getters = {
        "Day": lambda b: title_day(b["day"]),
        "Slot": lambda b: slot_span_text(model, b["slots"]),
        "Time": lambda b: b["time"],
        "Subject": lambda b: b["subject"],
        "Class": lambda b: b["class_name"],
        "Room": lambda b: b["room"],
        "Group": lambda b: b["group_name"],
        "Faculty": lambda b: b["teacher"],
        "Type": lambda b: b["type"],
    }

    return [[getters[c](b) for c in columns] for b in blocks]