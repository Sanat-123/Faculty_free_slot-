"""
Handlers for questions about subjects, classes, rooms and labs, plus the
data-quality / conflict checks.

Like the faculty handlers these only read the TimetableModel.
"""

from collections import Counter, defaultdict

from engine.smart_format import (
    block_rows,
    days_text,
    event_line,
    md_table,
    numbered,
    plural,
    scope_text,
    slots_text,
    title_day,
)
from utils.faculty_names import alnum_key


def _sorted(names):

    return sorted(names, key=str.lower)


class CatalogHandlers:

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _subject_events(self, f):

        m = self.model

        events = []

        for subject in f.subjects:
            events += m.by_subject.get(alnum_key(subject), [])

        return events

    def _class_events(self, f, keys=None):

        m = self.model

        events = []

        for name in (keys or f.classes):
            events += m.by_class.get(alnum_key(name), [])

        return events

    def _room_events(self, f):

        m = self.model

        events = []

        for name in f.rooms:
            events += m.by_room.get(alnum_key(name), [])

        return events

    def _day_filter(self, f):
        """(days, message): requested days, or every day."""

        days, _, message = self.resolve_cells(f)

        return (days or list(self.model.days)), message

    def _cells(self, f):
        """Cells (day, slot) requested; (cells, days, slots, message)."""

        days, slots, message = self.resolve_cells(f)

        if message:
            return None, days, slots, message

        if not days:
            return None, days, slots, self._ask_when("day and slot")

        if not slots:
            return None, days, slots, self._ask_when("slot")

        return [(d, s) for d in days for s in slots], days, slots, None

    # ------------------------------------------------------------------
    # subjects
    # ------------------------------------------------------------------

    def _subject_title(self, f):

        return ", ".join(f.subjects)

    def h_subject_teachers(self, f):

        events = self._subject_events(f)

        by_subject = defaultdict(set)

        for e in events:
            if e["teacher"]:
                by_subject[e["subject"]].add(e["teacher"])

        if not by_subject:
            return f"No teacher is recorded for {self._subject_title(f)}."

        lines = []

        all_names = set()

        for subject in _sorted(by_subject):

            names = _sorted(by_subject[subject])

            all_names |= set(names)

            lines.append(
                f"**Teachers of {subject}** · {len(names)}"
            )
            lines.append("")
            lines += numbered(names)
            lines.append("")

        self._last_names = _sorted(all_names)

        return "\n".join(lines).rstrip()

    def h_subject_teacher_count(self, f):

        events = self._subject_events(f)

        names = _sorted({e["teacher"] for e in events if e["teacher"]})

        verb = "teaches" if len(names) == 1 else "teach"

        head = (
            f"**{plural(len(names), 'teacher')} {verb} "
            f"{self._subject_title(f)}**"
        )

        if not names:
            return head

        self._last_names = names

        return "\n".join([head, ""] + numbered(names))

    def _subject_table(self, f, days=None):

        m = self.model

        events = self._subject_events(f)

        if days:
            events = [e for e in events if e["day"] in days]

        blocks = m.blocks(events)

        cols = ["Day", "Slot", "Time", "Class", "Room", "Faculty", "Group"]

        return blocks, md_table(cols, block_rows(m, blocks, cols))

    def h_subject_where(self, f):

        m = self.model

        days, _, message = self.resolve_cells(f)

        if message:
            return message

        title = self._subject_title(f)

        events = self._subject_events(f)

        if not events:
            return f"{title} has no scheduled sessions."

        held_days = [
            d for d in m.days if any(e["day"] == d for e in events)
        ]

        if days and not any(e["day"] in days for e in events):

            return (
                f"**{title} has no session on {days_text(m, days)}.** "
                f"It is held on {', '.join(title_day(d) for d in held_days)}."
            )

        blocks, table = self._subject_table(f, days or None)

        rooms = _sorted({b["room"] for b in blocks if b["room"]})

        scope = f" on {days_text(m, days)}" if days else ""

        head = (
            f"**{title} is held{scope} in: "
            f"{', '.join(rooms) if rooms else 'no room recorded'}**"
        )

        return "\n".join([head, ""] + table)

    def h_subject_schedule(self, f):

        m = self.model

        days, _, message = self.resolve_cells(f)

        if message:
            return message

        title = self._subject_title(f)

        events = self._subject_events(f)

        if not events:
            return f"{title} has no scheduled sessions."

        scope_days = days or list(m.days)

        blocks, table = self._subject_table(f, scope_days)

        if not blocks:
            return f"{title} has no session on {days_text(m, scope_days)}."

        parts = []

        for d in scope_days:

            slots = sorted({
                e["slot"] for e in events if e["day"] == d
            })

            if slots:
                parts.append(
                    f"{title_day(d)}: {slots_text(m, slots, False)}"
                )

        head = f"**When {title} is scheduled** — " + "; ".join(parts)

        return "\n".join([head, ""] + table)

    def h_subject_count(self, f):

        m = self.model

        title = self._subject_title(f)

        events = self._subject_events(f)

        if not events:
            return f"{title} has no scheduled sessions."

        sessions = m.blocks(
            events, by=("class_name", "room", "group_name", "teacher")
        )

        periods = len({(e["day"], e["slot"]) for e in events})

        return (
            f"**{title} is held {plural(len(sessions), 'time')} per week** "
            f"({plural(periods, 'period')} in total; "
            f"{plural(len(events), 'class entry', 'class entries')} "
            f"counting parallel sections separately)."
        )

    def h_subject_classes(self, f):

        events = self._subject_events(f)

        counts = defaultdict(set)

        for e in events:
            if e["class_name"]:
                counts[e["class_name"]].add((e["day"], e["slot"]))

        title = self._subject_title(f)

        if not counts:
            return f"No class is recorded for {title}."

        lines = [f"**Classes that have {title}** · {len(counts)}", ""]

        for i, name in enumerate(_sorted(counts), 1):
            lines.append(
                f"{i}. {name} — {plural(len(counts[name]), 'period')}"
            )

        return "\n".join(lines)

    def h_subject_type(self, f):

        title = self._subject_title(f)

        events = self._subject_events(f)

        counts = Counter(e["type"] for e in events if e["type"])

        if not counts:
            return f"The type of {title} is not recorded."

        total = sum(counts.values())

        if len(counts) == 1:

            kind = next(iter(counts))

            return (
                f"**{title} is a {kind} subject** "
                f"({total} of {total} recorded periods)."
            )

        parts = ", ".join(f"{k}: {v}" for k, v in counts.most_common())

        return f"**{title} has mixed types** — {parts} periods."

    def h_class_subject_room(self, f):

        m = self.model

        keys = {alnum_key(c) for c in f.classes}

        subjects = set(f.subjects)

        events = [
            e for e in self._class_events(f)
            if e["subject"] in subjects
        ]

        title = (
            f"{', '.join(f.subjects)} for {', '.join(f.classes)}"
        )

        if not events:
            return f"{', '.join(f.classes)} has no session of " \
                   f"{', '.join(f.subjects)}."

        blocks = m.blocks(events)

        rooms = _sorted({b["room"] for b in blocks if b["room"]})

        cols = ["Day", "Slot", "Time", "Room", "Faculty", "Group"]

        head = (
            f"**Room for {title}: "
            f"{', '.join(rooms) if rooms else 'not recorded'}**"
        )

        return "\n".join(
            [head, ""] + md_table(cols, block_rows(m, blocks, cols))
        )

    def h_subjects_at_cell(self, f):

        m = self.model

        cells, days, slots, message = self._cells(f)

        if message:
            return message

        subjects = set()

        for d, s in cells:
            subjects |= {
                e["subject"] for e in m.cell_events.get((d, s), [])
                if e["subject"]
            }

        names = _sorted(subjects)

        head = (
            f"**Subjects taught — {scope_text(m, days, slots)}** "
            f"· {len(names)}"
        )

        if not names:
            return head + "\n\nNo class is scheduled then."

        return "\n".join([head, ""] + numbered(names))

    def h_list_subjects(self, f):

        names = list(self.model.subjects)

        return "\n".join(
            [f"**All subjects in the timetable** · {len(names)}", ""]
            + numbered(names)
        )

    def h_lab_only_subjects(self, f):

        m = self.model

        by_subject = defaultdict(set)

        for e in m.events:
            if e["subject"] and e["type"]:
                by_subject[e["subject"]].add(m.is_lab(e))

        names = _sorted(s for s, kinds in by_subject.items() if kinds == {True})

        head = f"**Subjects taught only as labs** · {len(names)}"

        if not names:
            return head + "\n\nNone."

        return "\n".join([head, ""] + numbered(names))

    # ------------------------------------------------------------------
    # classes
    # ------------------------------------------------------------------

    def _class_title(self, f):

        return ", ".join(f.classes)

    def h_class_timetable(self, f):

        m = self.model

        days, message = self._day_filter(f)

        if message:
            return message

        title = self._class_title(f)

        events = [
            e for e in self._class_events(f) if e["day"] in days
        ]

        scope = "" if days == list(m.days) else f" — {days_text(m, days)}"

        if not events:
            return f"**{title}** has no classes{scope.replace(' — ', ' on ')}."

        blocks = m.blocks(events)

        periods = len({(e["day"], e["slot"]) for e in events})

        cols = ["Day", "Slot", "Time", "Subject", "Faculty", "Room", "Group"]

        return "\n".join(
            [
                f"**Timetable — {title}{scope}** "
                f"({plural(len(blocks), 'session')}, "
                f"{plural(periods, 'period')})",
                "",
            ]
            + md_table(cols, block_rows(m, blocks, cols))
        )

    def h_class_at(self, f):

        m = self.model

        cells, days, slots, message = self._cells(f)

        if message:
            return message

        title = self._class_title(f)

        keys = {alnum_key(c) for c in f.classes}

        lines = []

        for d, s in cells:

            events = [
                e for e in m.cell_events.get((d, s), [])
                if alnum_key(e["class_name"]) in keys
            ]

            head = (
                f"**{title} — {title_day(d)}, slot {s}"
                f"{m.slot_paren(s)}**"
            )

            if not events:
                lines.append(head + ": no class — free.")
                continue

            lines.append(head)

            for e in events:
                lines.append("- " + event_line(e, include_teacher=True))

        return "\n".join(lines)

    def h_class_teacher_at(self, f):

        m = self.model

        cells, days, slots, message = self._cells(f)

        if message:
            return message

        title = self._class_title(f)

        keys = {alnum_key(c) for c in f.classes}

        found = []

        for d, s in cells:

            for e in m.cell_events.get((d, s), []):

                if alnum_key(e["class_name"]) in keys and e["teacher"]:
                    found.append((d, s, e))

        names = _sorted({e["teacher"] for _, _, e in found})

        head = (
            f"**Who takes {title} — {scope_text(m, days, slots)}** "
            f"· {len(names)}"
        )

        if not names:
            return head + "\n\nNo teacher: the class has no session then."

        lines = [head, ""]

        for i, name in enumerate(names, 1):

            subjects = _sorted({
                e["subject"] for _, _, e in found
                if e["teacher"] == name and e["subject"]
            })

            lines.append(f"{i}. {name} — {', '.join(subjects)}")

        self._last_names = names

        return "\n".join(lines)

    def h_class_teachers(self, f):

        title = self._class_title(f)

        by_teacher = defaultdict(set)

        for e in self._class_events(f):
            if e["teacher"]:
                by_teacher[e["teacher"]].add(e["subject"])

        names = _sorted(by_teacher)

        head = f"**Teachers of {title}** · {len(names)}"

        if not names:
            return head

        lines = [head, ""]

        for i, name in enumerate(names, 1):

            subjects = _sorted(s for s in by_teacher[name] if s)

            lines.append(f"{i}. {name} — {', '.join(subjects)}")

        self._last_names = names

        return "\n".join(lines)

    def h_class_subjects(self, f):

        title = self._class_title(f)

        counts = defaultdict(set)

        for e in self._class_events(f):
            if e["subject"]:
                counts[e["subject"]].add((e["day"], e["slot"]))

        head = f"**Subjects taught to {title}** · {len(counts)}"

        if not counts:
            return head

        lines = [head, ""]

        for i, name in enumerate(_sorted(counts), 1):
            lines.append(
                f"{i}. {name} — {plural(len(counts[name]), 'period')}"
            )

        return "\n".join(lines)

    def h_class_count(self, f):

        m = self.model

        days, message = self._day_filter(f)

        if message:
            return message

        title = self._class_title(f)

        events = [
            e for e in self._class_events(f) if e["day"] in days
        ]

        periods = len({(e["day"], e["slot"]) for e in events})

        scope = (
            "per week" if days == list(m.days)
            else f"on {days_text(m, days)}"
        )

        rows = []

        for d in days:

            day_events = [e for e in events if e["day"] == d]

            rows.append([
                title_day(d),
                len({e["slot"] for e in day_events}),
                len(day_events),
            ])

        return "\n".join(
            [
                f"**{title} has {plural(periods, 'lecture period')} "
                f"{scope}** "
                f"({plural(len(events), 'class entry', 'class entries')} "
                f"counting parallel batches separately).",
                "",
            ]
            + md_table(["Day", "Periods", "Class entries"], rows)
        )

    def h_class_day_rank(self, f):

        m = self.model

        c = f.cues

        title = self._class_title(f)

        events = self._class_events(f)

        counts = {
            d: len({e["slot"] for e in events if e["day"] == d})
            for d in m.days
        }

        want_max = "most" in c and "least" not in c

        target = (max if want_max else min)(counts.values())

        days = [d for d in m.days if counts[d] == target]

        word = "busiest" if want_max else "lightest"

        rows = [[title_day(d), counts[d]] for d in m.days]

        return "\n".join(
            [
                f"**{title}'s {word} day: "
                f"{', '.join(title_day(d) for d in days)}** "
                f"({plural(target, 'period')}).",
                "",
            ]
            + md_table(["Day", "Periods"], rows)
        )

    def h_class_lab_check(self, f):

        m = self.model

        days, message = self._day_filter(f)

        if message:
            return message

        title = self._class_title(f)

        lab = [
            e for e in self._class_events(f)
            if e["day"] in days and m.is_lab(e)
        ]

        scope = days_text(m, days)

        if not lab:
            return f"**No** — {title} has no lab session ({scope})."

        blocks = m.blocks(lab)

        cols = ["Day", "Slot", "Time", "Subject", "Faculty", "Room", "Group"]

        return "\n".join(
            [
                f"**Yes** — {title} has "
                f"{plural(len(blocks), 'lab session')} ({scope}):",
                "",
            ]
            + md_table(cols, block_rows(m, blocks, cols))
        )

    def h_class_lab_groups(self, f):

        m = self.model

        days, slots, message = self.resolve_cells(f)

        if message:
            return message

        title = self._class_title(f)

        scope_days = days or list(m.days)

        lab = [
            e for e in self._class_events(f)
            if e["day"] in scope_days and m.is_lab(e)
            and (not slots or e["slot"] in slots)
        ]

        when = scope_text(m, days, slots) if (days or slots) else "the week"

        if not lab:
            return f"No lab session of {title} runs in {when}."

        groups = _sorted({e["group_name"] or "(no group)" for e in lab})

        rows = []

        for e in sorted(
            lab, key=lambda x: (m.days.index(x["day"]), x["slot"])
        ):
            rows.append([
                title_day(e["day"]), e["slot"], m.slot_label(e["slot"]),
                e["group_name"] or "—", e["subject"], e["room"],
                e["teacher"],
            ])

        return "\n".join(
            [
                f"**Lab batches of {title} in {when}: "
                f"{', '.join(groups)}**",
                "",
            ]
            + md_table(
                ["Day", "Slot", "Time", "Group", "Subject", "Room",
                 "Faculty"],
                rows,
            )
        )

    def h_class_status(self, f):

        m = self.model

        c = f.cues

        days, slots, message = self.resolve_cells(f)

        if message:
            return message

        if not days:
            return self._ask_when("day")

        title = self._class_title(f)

        keys = [alnum_key(x) for x in f.classes]

        def periods(day):

            return sorted({
                s for k in keys for s in m.class_periods(k, day)
            })

        if not slots:

            lines = [f"**{title} — free and busy slots**", ""]

            rows = []

            for d in days:

                busy = periods(d)
                free = [s for s in m.slots if s not in busy]

                rows.append([
                    title_day(d),
                    ", ".join(map(str, free)) or "—",
                    ", ".join(map(str, busy)) or "—",
                ])

            return "\n".join(
                lines + md_table(["Day", "Free slots", "Busy slots"], rows)
            )

        cells = [(d, s) for d in days for s in slots]

        busy_cells = [(d, s) for d, s in cells if s in periods(d)]

        is_free = not busy_cells

        ask_busy = f.polarity == "busy"

        verdict = (not is_free) if ask_busy else is_free

        word = "Yes" if verdict else "No"

        state = "free" if is_free else "busy"

        return (
            f"**{word} — {title} is {state} during "
            f"{scope_text(m, days, slots)}.**"
        )

    def h_free_classes(self, f):

        return self._class_availability(f, free=True)

    def h_busy_classes(self, f):

        return self._class_availability(f, free=False)

    def _class_availability(self, f, free):

        m = self.model

        cells, days, slots, message = self._cells(f)

        if message:
            return message

        universe = set(m.classes)

        sets = []

        for d, s in cells:

            busy = m.busy_classes(d, s)

            sets.append(universe - busy if free else busy)

        result = set.intersection(*sets)

        names = _sorted(result)

        label = "Free" if free else "Busy"

        head = (
            f"**{label} classes — {scope_text(m, days, slots)}** "
            f"· {len(names)}"
        )

        if not names:
            return head + "\n\nNone."

        return "\n".join([head, ""] + numbered(names))

    def h_list_classes(self, f):

        names = list(self.model.classes)

        return "\n".join(
            [f"**All classes and sections** · {len(names)}", ""]
            + numbered(names)
        )

    # ------------------------------------------------------------------
    # rooms
    # ------------------------------------------------------------------

    def _room_title(self, f):

        return ", ".join(f.rooms)

    def h_room_at(self, f):

        m = self.model

        cells, days, slots, message = self._cells(f)

        if message:
            return message

        title = self._room_title(f)

        keys = {alnum_key(r) for r in f.rooms}

        lines = []

        for d, s in cells:

            events = [
                e for e in m.cell_events.get((d, s), [])
                if alnum_key(e["room"]) in keys
            ]

            head = (
                f"**Room {title} — {title_day(d)}, slot {s}"
                f"{m.slot_paren(s)}**"
            )

            if not events:
                lines.append(head + ": free — no class.")
                continue

            lines.append(head)

            classes = _sorted({
                e["class_name"] for e in events if e["class_name"]
            })

            if classes:
                lines.append(f"Occupied by: {', '.join(classes)}")

            for e in events:
                lines.append("- " + event_line(e, include_teacher=True))

        return "\n".join(lines)

    def h_room_status(self, f):

        m = self.model

        days, slots, message = self.resolve_cells(f)

        if message:
            return message

        title = self._room_title(f)

        keys = {alnum_key(r) for r in f.rooms}

        cells = [(d, s) for d in days for s in slots]

        busy = [
            (d, s) for d, s in cells
            if any(
                alnum_key(e["room"]) in keys
                for e in m.cell_events.get((d, s), [])
            )
        ]

        is_free = not busy

        ask_busy = f.polarity == "busy"

        verdict = (not is_free) if ask_busy else is_free

        return (
            f"**{'Yes' if verdict else 'No'} — room {title} is "
            f"{'free' if is_free else 'occupied'} during "
            f"{scope_text(m, days, slots)}.**"
        )

    def h_room_free_slots(self, f):

        m = self.model

        days, message = self._day_filter(f)

        if message:
            return message

        title = self._room_title(f)

        keys = {alnum_key(r) for r in f.rooms}

        events = self._room_events(f)

        rows = []

        for d in days:

            busy = sorted({e["slot"] for e in events if e["day"] == d})
            free = [s for s in m.slots if s not in busy]

            rows.append([
                title_day(d),
                ", ".join(map(str, free)) or "—",
                ", ".join(map(str, busy)) or "—",
            ])

        return "\n".join(
            [f"**When room {title} is free — {days_text(m, days)}**", ""]
            + md_table(["Day", "Free slots", "Occupied slots"], rows)
        )

    def h_room_schedule(self, f):

        m = self.model

        days, message = self._day_filter(f)

        if message:
            return message

        title = self._room_title(f)

        events = [e for e in self._room_events(f) if e["day"] in days]

        if not events:
            return f"Room {title} has no classes ({days_text(m, days)})."

        blocks = m.blocks(events)

        used = len({(e["day"], e["slot"]) for e in events})

        capacity = len(days) * len(m.teaching_slots)

        cols = ["Day", "Slot", "Time", "Class", "Subject", "Faculty", "Group"]

        return "\n".join(
            [
                f"**Occupancy of room {title}** — {used} of {capacity} "
                f"teaching periods in use ({days_text(m, days)})",
                "",
            ]
            + md_table(cols, block_rows(m, blocks, cols))
        )

    def h_free_rooms(self, f):

        return self._room_availability(f, free=True)

    def h_busy_rooms(self, f):

        return self._room_availability(f, free=False)

    def _room_availability(self, f, free):

        m = self.model

        cells, days, slots, message = self._cells(f)

        if message:
            return message

        universe = set(m.rooms)

        sets = []

        for d, s in cells:

            busy = m.busy_rooms(d, s)

            sets.append(universe - busy if free else busy)

        names = _sorted(set.intersection(*sets))

        label = "Rooms free on" if free else "Rooms occupied on"

        head = (
            f"**{label} {scope_text(m, days, slots)}** "
            f"· {len(names)}"
        )

        if not names:
            return head + "\n\nNone."

        return "\n".join([head, ""] + numbered(names))

    def h_lab_rooms(self, f):

        m = self.model

        counts = defaultdict(set)

        for e in m.events:
            if e["room"] and m.is_lab(e):
                counts[e["room"]].add((e["day"], e["slot"]))

        names = _sorted(counts)

        head = f"**Rooms used for labs** · {len(names)}"

        if not names:
            return head

        lines = [head, ""]

        for i, name in enumerate(names, 1):
            lines.append(
                f"{i}. {name} — {plural(len(counts[name]), 'lab period')}"
            )

        return "\n".join(lines)

    def h_unused_rooms(self, f):

        m = self.model

        days, message = self._day_filter(f)

        if message:
            return message

        used = {
            e["room"] for e in m.events
            if e["room"] and e["day"] in days
        }

        names = _sorted(set(m.rooms) - used)

        head = (
            f"**Rooms never used on {days_text(m, days)}** · {len(names)}"
        )

        if not names:
            return head + "\n\nEvery room is used."

        return "\n".join([head, ""] + numbered(names))

    def h_room_rank(self, f):

        m = self.model

        c = f.cues

        usage = {}

        for room in m.rooms:

            events = m.by_room.get(alnum_key(room), [])

            usage[room] = len({(e["day"], e["slot"]) for e in events})

        want_most = "most" in c and "least" not in c

        best = (max if want_most else min)(usage.values())

        winners = _sorted(r for r, n in usage.items() if n == best)

        ordered = sorted(
            usage.items(),
            key=lambda kv: (-kv[1] if want_most else kv[1], kv[0].lower()),
        )

        capacity = len(m.days) * len(m.teaching_slots)

        head = (
            f"**{'Busiest' if want_most else 'Least used'} room: "
            f"{', '.join(winners[:6])}** ({best} of {capacity} "
            f"teaching periods)"
        )

        rows = [[i, r, n] for i, (r, n) in enumerate(ordered[:8], 1)]

        return "\n".join(
            [head, ""] + md_table(["#", "Room", "Periods in use"], rows)
        )

    def h_list_rooms(self, f):

        names = list(self.model.rooms)

        return "\n".join(
            [f"**All rooms** · {len(names)}", ""] + numbered(names)
        )

    # ------------------------------------------------------------------
    # labs
    # ------------------------------------------------------------------

    def h_labs_on_day(self, f):

        m = self.model

        days, message = self._day_filter(f)

        if message:
            return message

        lab = [
            e for e in m.events if e["day"] in days and m.is_lab(e)
        ]

        scope = days_text(m, days)

        subjects = defaultdict(set)

        for e in lab:
            subjects[e["subject"]].add((e["day"], e["slot"], e["class_name"],
                                        e["group_name"]))

        names = _sorted(subjects)

        head = f"**Labs running — {scope}** · {len(names)} lab subjects"

        if not names:
            return head + "\n\nNo lab runs then."

        lines = [head, ""]

        for i, name in enumerate(names, 1):

            sessions = m.sessions([e for e in lab if e["subject"] == name])

            lines.append(
                f"{i}. {name} — {plural(len(sessions), 'session')}"
            )

        return "\n".join(lines)

    def h_lab_multislot(self, f):

        m = self.model

        blocks = m.sessions(
            [e for e in m.events if m.is_lab(e)]
        )

        multi = [b for b in blocks if len(b["slots"]) >= 2]

        lengths = Counter(len(b["slots"]) for b in multi)

        summary = ", ".join(
            f"{n} × {k} slots" for k, n in sorted(lengths.items())
        )

        if len(multi) == len(blocks):
            head = (
                f"**All {plural(len(multi), 'lab session')} span more than "
                f"one slot** ({summary})"
            )
        else:
            head = (
                f"**{plural(len(multi), 'lab session')} span more than one "
                f"slot** of {len(blocks)} lab sessions ({summary})"
            )

        if not multi:
            return head

        cols = ["Day", "Slot", "Time", "Subject", "Class", "Room", "Faculty"]

        shown = multi[:20]

        lines = [head, ""] + md_table(cols, block_rows(m, shown, cols))

        if len(multi) > len(shown):
            lines += ["", f"*Showing the first {len(shown)} of {len(multi)}.*"]

        return "\n".join(lines)

    def h_lab_at(self, f):

        m = self.model

        cells, days, slots, message = self._cells(f)

        if message:
            return message

        rows = []

        names = set()

        for d, s in cells:

            for e in m.cell_events.get((d, s), []):

                if m.is_lab(e):

                    rows.append([
                        title_day(d), s, e["teacher"], e["subject"],
                        e["class_name"], e["group_name"], e["room"],
                    ])

                    if e["teacher"]:
                        names.add(e["teacher"])

        head = (
            f"**In a lab — {scope_text(m, days, slots)}** · "
            f"{plural(len(names), 'faculty member')}"
        )

        if not rows:
            return head + "\n\nNo lab session runs then."

        self._last_names = _sorted(names)

        return "\n".join(
            [head, ""]
            + md_table(
                ["Day", "Slot", "Faculty", "Subject", "Class", "Group",
                 "Room"],
                rows,
            )
        )

    def h_lab_no_session(self, f):

        m = self.model

        days, message = self._day_filter(f)

        if message:
            return message

        lab_subjects = {
            e["subject"] for e in m.events if m.is_lab(e) and e["subject"]
        }

        running = {
            e["subject"] for e in m.events
            if m.is_lab(e) and e["day"] in days and e["subject"]
        }

        names = _sorted(lab_subjects - running)

        head = (
            f"**Labs with no session on {days_text(m, days)}** "
            f"· {len(names)} of {len(lab_subjects)}"
        )

        if not names:
            return head + "\n\nEvery lab meets then."

        return "\n".join([head, ""] + numbered(names))

    # ------------------------------------------------------------------
    # quality / conflicts
    # ------------------------------------------------------------------

    def h_conflicts(self, f):

        m = self.model

        c = f.cues

        wants = []

        if "room_words" in c:
            wants.append("room")

        if "class_words" in c and "teacher_words" not in c:
            wants.append("class")

        if "teacher_words" in c:
            wants.append("teacher")

        if not wants:

            text = f.text

            if "two places" in text:
                wants = ["teacher"]
            elif "two subjects" in text or "assigned two" in text:
                wants = ["class"]
            else:
                wants = ["teacher", "room", "class"]

        lines = []

        if "teacher" in wants:

            found = m.teacher_conflicts()

            lines += self._conflict_block(
                "teacher", found,
                lambda x: (
                    f"{x['teacher']} — {title_day(x['day'])} slot "
                    f"{x['slot']}: rooms {', '.join(x['rooms'])} "
                    f"({'; '.join(x['subjects'])})"
                ),
                "is scheduled in two different rooms at the same time",
            )

        if "room" in wants:

            found = m.room_conflicts()

            lines += self._conflict_block(
                "room", found,
                lambda x: (
                    f"Room {x['room']} — {title_day(x['day'])} slot "
                    f"{x['slot']}: {' vs '.join(x['sessions'])}"
                ),
                "hosts two unrelated sessions at once",
            )

        if "class" in wants:

            found = m.class_conflicts()

            lines += self._conflict_block(
                "class", found,
                lambda x: (
                    f"{x['class_name']} — {title_day(x['day'])} slot "
                    f"{x['slot']}: {' vs '.join(x['subjects'])}"
                ),
                "has two different subjects in the same slot",
            )

        return "\n".join(lines).rstrip()

    def _conflict_block(self, kind, found, render, meaning):

        if not found:
            return [f"**No {kind} conflicts found.**", ""]

        lines = [
            f"**Yes — {plural(len(found), kind + ' conflict')}** "
            f"(a {kind} {meaning}):",
            "",
        ]

        for i, item in enumerate(found[:15], 1):
            lines.append(f"{i}. {render(item)}")

        if len(found) > 15:
            lines.append(f"… and {len(found) - 15} more.")

        lines.append("")

        return lines

    def h_empty_slots(self, f):

        m = self.model

        empty = m.empty_cells()

        if not empty:
            return "**Every slot has at least one class.**"

        by_slot = defaultdict(list)

        for d, s in empty:
            by_slot[s].append(d)

        lines = [
            f"**Slots in which no section has any class** · "
            f"{plural(len(empty), 'cell')}",
            "",
        ]

        for s, days in sorted(by_slot.items()):

            lines.append(
                f"- Slot {s}{m.slot_paren(s)}: {days_text(m, days)}"
            )

        return "\n".join(lines)

    def h_data_quality(self, f):

        m = self.model

        q = m.data_quality()

        lines = [
            "**Data quality report**",
            "",
            f"- Faculty (real people): {q['faculty']}",
            f"- Entries treated as codes / placeholders (not reported as "
            f"free or busy): {len(q['codes'])}"
            + (f" — {', '.join(q['codes'])}" if q["codes"] else ""),
            f"- Merged names split into two people: {len(q['composites'])}"
            + (
                " — " + "; ".join(
                    f"{k} → {a} + {b}"
                    for k, (a, b) in q["composites"].items()
                ) if q["composites"] else ""
            ),
            f"- Faculty with no classes: {len(q['no_classes'])}"
            + (f" — {', '.join(q['no_classes'])}" if q["no_classes"] else ""),
            f"- Teacher in two rooms at once: {len(q['teacher_conflicts'])}",
            f"- Room double-booked: {len(q['room_conflicts'])}",
            f"- Class with two subjects in one slot: "
            f"{len(q['class_conflicts'])}",
            f"- Slots with no classes at all: {len(q['empty_cells'])}",
        ]

        return "\n".join(lines)