"""
Handlers for questions about faculty availability, one teacher's
schedule, comparisons, rankings and coordinator scenarios.

Every handler reads only the TimetableModel (`self.model`), so answers are
always consistent with the loaded data.  Handlers return Markdown text.
"""

import re
from collections import defaultdict

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
from engine.timetable_model import consecutive_runs


def _sorted(names):

    return sorted(names, key=str.lower)


class FacultyHandlers:

    # ------------------------------------------------------------------
    # shared helpers
    # ------------------------------------------------------------------

    def _ask_when(self, what="day and slot"):

        examples = self.examples()

        sample = f" For example: “{examples[0]}”" if examples else ""

        return f"Please tell me the {what} you mean.{sample}"

    def _cells_for(self, f):
        """(days, slots, message) with the standard 'ask' fallbacks."""

        days, slots, message = self.resolve_cells(f)

        return days, slots, message

    def _pool(self, f):
        """Faculty considered by a list question (optionally per class)."""

        m = self.model

        pool = set(m.faculty_set)

        if f.classes:

            keys = {c for c in f.classes}

            teaching = {
                e["teacher"] for e in m.events
                if e["class_name"] in keys and e["teacher"] in pool
            }

            pool &= teaching

        return pool

    # ------------------------------------------------------------------
    # who is free / busy
    # ------------------------------------------------------------------

    def _availability_list(self, f, polarity):

        m = self.model

        days, slots, message = self._cells_for(f)

        if message:
            return message

        if not days:

            if slots:
                return self._ask_when("day")

            return self._ask_when()

        text = f.text

        no_class_phrase = re.search(
            r"\bno class(?:es)?\b|not teaching|without (?:any )?class",
            text,
        )

        mode = f.slot_mode

        if not slots:

            if polarity == "busy" or (polarity == "free" and no_class_phrase):
                slots = list(m.slots)
                mode = "any" if polarity == "busy" else "all"

            else:
                return self._day_summary(f, days)

        pool = self._pool(f)

        cells = [(d, s) for d in days for s in slots]

        sets = []

        for d, s in cells:

            base = (
                m.free_faculty(d, s) if polarity == "free"
                else m.busy_faculty(d, s)
            )

            sets.append(base & pool)

        if mode == "any":
            result = set().union(*sets) if sets else set()
        else:
            result = set.intersection(*sets) if sets else set()

        names = _sorted(result)

        label = "Free" if polarity == "free" else "Busy"

        scope = scope_text(m, days, slots)

        whole_day = set(slots) == set(m.slots) and len(slots) > 1

        joiner = ""

        if polarity == "busy" and mode == "any" and len(slots) > 1:
            joiner = " (with a class in at least one slot)"
        elif whole_day and mode == "all":
            joiner = " (free in every slot)"
        elif len(cells) > 1 and mode == "all":
            joiner = " (in every one of these slots)"
        elif len(cells) > 1 and mode == "any":
            joiner = " (in at least one of these slots)"

        if polarity == "free" and no_class_phrase and whole_day:
            label = "Faculty with no classes"
            scope = days_text(m, days)
            joiner = ""

        if f.classes:
            joiner += f" — only faculty who teach {', '.join(f.classes)}"

        title = (
            f"{label} — {scope}" if label.startswith("Faculty")
            else f"{label} faculty — {scope}"
        )

        head = f"**{title}**{joiner} · {len(names)} found"

        if not names:
            return head + "\n\nNobody matches."

        self._last_names = names

        lines = [head, ""] + numbered(names)

        if not (f.cues & {"free", "busy"}):

            other = (
                m.busy_faculty(days[0], slots[0]) if polarity == "free"
                else m.free_faculty(days[0], slots[0])
            ) & pool

            other_label = "busy" if polarity == "free" else "free"

            lines += [
                "",
                f"*{len(other)} other faculty are {other_label} then — "
                f"ask “Who is {other_label} on …” to see them.*",
            ]

        return "\n".join(lines)

    def h_free_list(self, f):

        return self._availability_list(f, "free")

    def h_busy_list(self, f):

        return self._availability_list(f, "busy")

    def _day_summary(self, f, days):

        m = self.model

        rows = []

        pool = self._pool(f)

        for d in days:
            for s in m.teaching_slots:
                rows.append([
                    title_day(d),
                    s,
                    m.slot_label(s),
                    len(m.free_faculty(d, s) & pool),
                    len(m.busy_faculty(d, s) & pool),
                ])

        lines = [
            f"**Faculty availability — {days_text(m, days)}**",
            "",
        ] + md_table(["Day", "Slot", "Time", "Free", "Busy"], rows)

        example = (
            f"Who is free on {title_day(days[0])} slot "
            f"{m.teaching_slots[0]}?"
        )

        lines += ["", f"*Add a slot to see names, e.g. “{example}”*"]

        return "\n".join(lines)

    def h_free_but_busy(self, f):

        m = self.model

        parts = self._split_free_busy(f)

        if not parts:
            return None

        cells_by_pol = {}

        for text, polarity in parts:

            sub = f.copy()

            sub.slots, sub.slot_ref, sub.bad_slots = [], None, []
            sub.time_range, sub.time_point = None, None
            sub.days, sub.all_days = [], False

            self._extract_days(sub, text)
            self._extract_slots(sub, text)

            if not sub.days and not sub.all_days:
                sub.days = list(f.days)
                sub.all_days = f.all_days

            days, slots, message = self.resolve_cells(sub)

            if message:
                return message

            if not days or not slots:
                return self._ask_when()

            cells_by_pol[polarity] = (days, slots)

        pool = self._pool(f)

        def gather(polarity):

            days, slots = cells_by_pol[polarity]

            sets = []

            for d in days:
                for s in slots:
                    base = (
                        m.free_faculty(d, s) if polarity == "free"
                        else m.busy_faculty(d, s)
                    )
                    sets.append(base & pool)

            return set.intersection(*sets)

        result = gather("free") & gather("busy")

        names = _sorted(result)

        free_scope = scope_text(m, *cells_by_pol["free"])
        busy_scope = scope_text(m, *cells_by_pol["busy"])

        head = (
            f"**Free on {free_scope} but busy on {busy_scope}** "
            f"· {len(names)} found"
        )

        if not names:
            return head + "\n\nNobody matches."

        self._last_names = names

        return "\n".join([head, ""] + numbered(names))

    # ------------------------------------------------------------------
    # one teacher: availability
    # ------------------------------------------------------------------

    def h_teacher_status(self, f):

        m = self.model

        teacher = f.teachers[0]

        days, slots, message = self._cells_for(f)

        if message:
            return message

        ask_busy = f.polarity == "busy"

        if not days:

            if slots:
                return self._ask_when("day")

            return self._ask_when("day (and slot, if you like)")

        if not slots:
            return self._teacher_day_view(teacher, days)

        cells = [(d, s) for d in days for s in slots]

        busy_cells = [
            (d, s) for d, s in cells if s in m.periods(teacher, d)
        ]

        free_cells = [c for c in cells if c not in busy_cells]

        is_free = not busy_cells

        verdict = (not is_free) if ask_busy else is_free

        word = "Yes" if verdict else "No"

        scope = scope_text(m, days, slots)

        if len(cells) == 1:

            d, s = cells[0]

            if busy_cells:

                events = [
                    e for e in m.by_teacher[teacher]
                    if e["day"] == d and e["slot"] == s
                ]

                detail = "; ".join(event_line(e) for e in events)

                return (
                    f"**{word} — {teacher} is busy on {title_day(d)}, "
                    f"slot {s}{m.slot_paren(s)}:** {detail}"
                ) if ask_busy or not verdict else (
                    f"**{word} — {teacher} is busy on {title_day(d)}, "
                    f"slot {s}{m.slot_paren(s)}:** {detail}"
                )

            return (
                f"**{word} — {teacher} is free on {title_day(d)}, "
                f"slot {s}{m.slot_paren(s)}.**"
            )

        lines = []

        if ask_busy:
            lines.append(
                f"**{word} — {teacher} "
                f"{'has a class' if verdict else 'has no class'} during "
                f"{scope}.**"
            )
        else:
            lines.append(
                f"**{word} — {teacher} is "
                f"{'free' if verdict else 'not free'} for all of "
                f"{scope}.**"
            )

        lines.append("")

        for d, s in cells:

            if (d, s) in busy_cells:

                events = [
                    e for e in m.by_teacher[teacher]
                    if e["day"] == d and e["slot"] == s
                ]

                detail = "; ".join(event_line(e) for e in events)

                lines.append(
                    f"- {title_day(d)} slot {s}{m.slot_paren(s)}: "
                    f"busy — {detail}"
                )

            else:

                lines.append(
                    f"- {title_day(d)} slot {s}{m.slot_paren(s)}: free"
                )

        return "\n".join(lines)

    def _teacher_day_view(self, teacher, days):

        m = self.model

        if len(days) == 1:

            d = days[0]

            free = m.free_slots(teacher, d)
            busy = m.periods(teacher, d)

            lines = [f"**{teacher} — {title_day(d)}**", ""]

            if not busy:
                lines.append(
                    f"No classes: {teacher} is free all day "
                    f"({slots_text(m, free)})."
                )
                return "\n".join(lines)

            lines.append(f"Free: {slots_text(m, free, False)}")
            lines.append(f"Busy: {slots_text(m, busy, False)}")

            lines.append("")

            events = [e for e in m.by_teacher[teacher] if e["day"] == d]

            blocks = m.blocks(events)

            lines += md_table(
                ["Slot", "Time", "Subject", "Class", "Room", "Group"],
                block_rows(
                    m, blocks,
                    ["Slot", "Time", "Subject", "Class", "Room", "Group"],
                ),
            )

            return "\n".join(lines)

        rows = []

        for d in days:

            free = m.free_slots(teacher, d)
            busy = m.periods(teacher, d)

            rows.append([
                title_day(d),
                ", ".join(map(str, free)) or "—",
                ", ".join(map(str, busy)) or "—",
            ])

        lines = [
            f"**{teacher} — free and busy slots · {days_text(m, days)}**",
            "",
        ] + md_table(["Day", "Free slots", "Busy slots"], rows)

        times = [
            f"{s} = {m.slot_label(s)}" for s in m.slots if m.slot_label(s)
        ]

        if times:
            lines += ["", "*Slot times: " + "; ".join(times) + "*"]

        return "\n".join(lines)

    def h_teacher_at(self, f):

        m = self.model

        teacher = f.teachers[0]

        days, slots, message = self._cells_for(f)

        if message:
            return message

        if not slots:
            return self.h_teacher_timetable(f)

        if not days:
            return self._ask_when("day")

        lines = []

        for d in days:
            for s in slots:

                events = [
                    e for e in m.by_teacher[teacher]
                    if e["day"] == d and e["slot"] == s
                ]

                head = (
                    f"**{teacher} — {title_day(d)}, slot {s}"
                    f"{m.slot_paren(s)}**"
                )

                if events:

                    lines.append(head)

                    for e in events:
                        lines.append("- " + event_line(e))

                else:
                    lines.append(head + ": no class — free at that time.")

        return "\n".join(lines)

    def h_teacher_first_last(self, f):

        m = self.model

        teacher = f.teachers[0]

        days, _, message = self._cells_for(f)

        if message:
            return message

        days = days or list(m.days)

        rows = []

        for d in days:

            periods = m.periods(teacher, d)

            if not periods:
                rows.append([title_day(d), "—", "—", 0])
                continue

            def detail(slot):

                events = [
                    e for e in m.by_teacher[teacher]
                    if e["day"] == d and e["slot"] == slot
                ]

                return (
                    f"slot {slot}{m.slot_paren(slot)}: "
                    + "; ".join(event_line(e) for e in events)
                )

            rows.append([
                title_day(d), detail(periods[0]), detail(periods[-1]),
                len(periods),
            ])

        if len(days) == 1:

            d = days[0]

            periods = m.periods(teacher, d)

            if not periods:
                return f"**{teacher}** has no classes on {title_day(d)}."

            return (
                f"**{teacher} on {title_day(d)}**\n\n"
                f"- First class: {rows[0][1]}\n"
                f"- Last class: {rows[0][2]}\n"
                f"- Periods: {len(periods)}"
            )

        return "\n".join(
            [f"**{teacher} — first and last class per day**", ""]
            + md_table(["Day", "First class", "Last class", "Periods"], rows)
        )

    def h_teacher_back_to_back(self, f):

        m = self.model

        teacher = f.teachers[0]

        days, _, message = self._cells_for(f)

        if message:
            return message

        days = days or list(m.days)

        found = []

        for d in days:

            for run in consecutive_runs(m.periods(teacher, d)):

                if len(run) >= 2:
                    found.append((d, run))

        scope = days_text(m, days)

        if not found:
            return (
                f"**No** — {teacher} has no back-to-back classes "
                f"({scope})."
            )

        lines = [
            f"**Yes** — {teacher} has back-to-back classes "
            f"({plural(len(found), 'stretch', 'stretches')}):",
            "",
        ]

        for d, run in found:

            lines.append(
                f"- {title_day(d)}: slots {run[0]}-{run[-1]}"
                f"{m.range_paren(run[0], run[-1])}, "
                f"{len(run)} periods in a row"
            )

        return "\n".join(lines)

    def h_teacher_gap(self, f):

        m = self.model

        teacher = f.teachers[0]

        days, _, message = self._cells_for(f)

        if message:
            return message

        days = days or list(m.days)

        found = []

        for d in days:

            periods = m.periods(teacher, d)

            if len(periods) < 2:
                continue

            gaps = [
                s for s in m.slots
                if periods[0] < s < periods[-1] and s not in periods
            ]

            if gaps:
                found.append((d, gaps))

        scope = days_text(m, days)

        if not found:
            return (
                f"**No** — {teacher} has no free gap between two classes "
                f"({scope})."
            )

        lines = [
            f"**Yes** — {teacher} has a free gap between classes:",
            "",
        ]

        for d, gaps in found:

            lines.append(
                f"- {title_day(d)}: free {slots_text(m, gaps, False)} "
                f"between the first and last class"
            )

        return "\n".join(lines)

    def h_teacher_day_rank(self, f):

        m = self.model

        teacher = f.teachers[0]

        c = f.cues

        counts = {d: len(m.periods(teacher, d)) for d in m.days}

        rows = [
            [title_day(d), counts[d], len(m.slots) - counts[d]]
            for d in m.days
        ]

        table = md_table(["Day", "Periods", "Free slots"], rows)

        if "completely" in c and ("free" in c or "least" in c):

            empty = [d for d in m.days if counts[d] == 0]

            if empty:
                return (
                    f"**{teacher} is completely free on "
                    f"{', '.join(title_day(d) for d in empty)}.**\n\n"
                    + "\n".join(table)
                )

            lightest = min(counts.values())

            days = [d for d in m.days if counts[d] == lightest]

            return (
                f"**No day is completely free** — {teacher} has classes on "
                f"every day. Lightest: "
                f"{', '.join(title_day(d) for d in days)} "
                f"({plural(lightest, 'period')}).\n\n" + "\n".join(table)
            )

        want_max = (
            ("most" in c and "free" not in c)
            or ("least" in c and "free" in c)
        )

        target = max(counts.values()) if want_max else min(counts.values())

        days = [d for d in m.days if counts[d] == target]

        word = "busiest" if want_max else "lightest"

        return (
            f"**{teacher}'s {word} day: "
            f"{', '.join(title_day(d) for d in days)}** "
            f"({plural(target, 'period')}).\n\n" + "\n".join(table)
        )

    # ------------------------------------------------------------------
    # one teacher: workload / details
    # ------------------------------------------------------------------

    def h_teacher_count(self, f):

        m = self.model

        teacher = f.teachers[0]

        c = f.cues

        days, _, message = self._cells_for(f)

        if message:
            return message

        scope_days = days or list(m.days)

        events = [
            e for e in m.by_teacher[teacher] if e["day"] in scope_days
        ]

        scope = (
            "per week" if scope_days == list(m.days)
            else f"on {days_text(m, scope_days)}"
        )

        if "free" in c:

            rows = []
            total_free = 0

            for d in scope_days:

                free = m.free_slots(teacher, d)

                total_free += len(free)

                rows.append([
                    title_day(d), len(free),
                    ", ".join(map(str, free)) or "—",
                ])

            return "\n".join(
                [
                    f"**{teacher} has {plural(total_free, 'free slot')} "
                    f"{scope}** (out of {len(scope_days) * len(m.slots)}).",
                    "",
                ]
                + md_table(["Day", "Free slots", "Which"], rows)
            )

        if "lab_words" in c:

            lab = [e for e in events if m.is_lab(e)]

            sessions = self._lab_runs(lab, scope_days)

            lab_periods = sum(len(run) for _, run in sessions)

            lines = [
                f"**{teacher} takes {plural(len(sessions), 'lab session')} "
                f"{scope}** ({plural(lab_periods, 'lab period')}).",
            ]

            if sessions:

                lines.append("")

                for d, run in sessions:

                    subjects = _sorted({
                        e["subject"] for e in lab
                        if e["day"] == d and e["slot"] in run
                    })

                    lines.append(
                        f"- {title_day(d)}: slots {run[0]}-{run[-1]}"
                        f"{m.range_paren(run[0], run[-1])} — "
                        f"{', '.join(subjects)}"
                    )

            return "\n".join(lines)

        if "theory_words" in c:

            theory = [e for e in events if not m.is_lab(e)]

            periods = len({(e["day"], e["slot"]) for e in theory})

            return (
                f"**{teacher} has {plural(periods, 'theory period')} "
                f"{scope}** ({plural(len(theory), 'class entry', 'class entries')})."
            )

        periods = len({(e["day"], e["slot"]) for e in events})

        rows = []

        for d in scope_days:

            day_events = [e for e in events if e["day"] == d]

            rows.append([
                title_day(d),
                len({e["slot"] for e in day_events}),
                len(day_events),
            ])

        lines = [
            f"**{teacher} has {plural(periods, 'period')} {scope}** "
            f"({plural(len(events), 'class entry', 'class entries')} "
            f"counting parallel sections separately).",
            "",
        ] + md_table(["Day", "Periods", "Class entries"], rows)

        return "\n".join(lines)

    @staticmethod
    def _lab_runs(lab_events, days):
        """Continuous runs of lab periods per day -> [(day, [slots])]."""

        runs = []

        for d in days:

            slots = sorted({e["slot"] for e in lab_events if e["day"] == d})

            for run in consecutive_runs(slots):
                runs.append((d, run))

        return runs

    def h_teacher_lab_check(self, f):

        m = self.model

        teacher = f.teachers[0]

        days, _, message = self._cells_for(f)

        if message:
            return message

        scope_days = days or list(m.days)

        lab = [
            e for e in m.by_teacher[teacher]
            if e["day"] in scope_days and m.is_lab(e)
        ]

        scope = days_text(m, scope_days)

        if not lab:
            return f"**No** — {teacher} has no lab session ({scope})."

        blocks = m.blocks(lab)

        runs = self._lab_runs(lab, scope_days)

        cols = ["Day", "Slot", "Time", "Subject", "Class", "Room", "Group"]

        return "\n".join(
            [
                f"**Yes** — {teacher} teaches "
                f"{plural(len(runs), 'lab session')} ({scope}):",
                "",
            ]
            + md_table(cols, block_rows(m, blocks, cols))
        )

    def _teacher_events(self, f):

        m = self.model

        days, _, message = self._cells_for(f)

        scope_days = days or list(m.days)

        events = [
            e for e in m.by_teacher[f.teachers[0]]
            if e["day"] in scope_days
        ]

        return events, scope_days, message

    def h_teacher_subjects(self, f):

        m = self.model

        events, days, message = self._teacher_events(f)

        if message:
            return message

        teacher = f.teachers[0]

        counts = defaultdict(set)

        for e in events:
            if e["subject"]:
                counts[e["subject"]].add((e["day"], e["slot"]))

        scope = "" if days == list(m.days) else f" on {days_text(m, days)}"

        if not counts:
            return f"{teacher} has no subjects{scope}."

        lines = [
            f"**Subjects taught by {teacher}{scope}** · {len(counts)}",
            "",
        ]

        for i, name in enumerate(_sorted(counts), 1):
            lines.append(
                f"{i}. {name} — {plural(len(counts[name]), 'period')}"
            )

        return "\n".join(lines)

    def h_teacher_classes(self, f):

        m = self.model

        events, days, message = self._teacher_events(f)

        if message:
            return message

        teacher = f.teachers[0]

        counts = defaultdict(set)

        for e in events:
            if e["class_name"]:
                counts[e["class_name"]].add((e["day"], e["slot"]))

        scope = "" if days == list(m.days) else f" on {days_text(m, days)}"

        if not counts:
            return f"No class is recorded for {teacher}{scope}."

        lines = [
            f"**Classes taught by {teacher}{scope}** · {len(counts)}",
            "",
        ]

        for i, name in enumerate(_sorted(counts), 1):
            lines.append(
                f"{i}. {name} — {plural(len(counts[name]), 'period')}"
            )

        return "\n".join(lines)

    def h_teacher_rooms(self, f):

        m = self.model

        events, days, message = self._teacher_events(f)

        if message:
            return message

        teacher = f.teachers[0]

        counts = defaultdict(set)

        for e in events:
            if e["room"]:
                counts[e["room"]].add((e["day"], e["slot"]))

        scope = "" if days == list(m.days) else f" on {days_text(m, days)}"

        if not counts:
            return f"No room is recorded for {teacher}{scope}."

        lines = [
            f"**Rooms used by {teacher}{scope}** · {len(counts)}",
            "",
        ]

        for i, name in enumerate(_sorted(counts), 1):
            lines.append(
                f"{i}. {name} — {plural(len(counts[name]), 'period')}"
            )

        return "\n".join(lines)

    def h_teacher_subject_group(self, f):

        m = self.model

        teacher = f.teachers[0]

        keys = set(f.subjects)

        events = [
            e for e in m.by_teacher[teacher] if e["subject"] in keys
        ]

        subject_text = ", ".join(f.subjects)

        if not events:

            others = _sorted({
                e["teacher"] for e in m.events
                if e["subject"] in keys and e["teacher"]
            })

            lines = [f"**{teacher} does not teach {subject_text}.**"]

            if others:
                lines += ["", f"It is taught by: {', '.join(others)}."]

            return "\n".join(lines)

        blocks = m.blocks(events)

        by_group = defaultdict(list)

        for b in blocks:
            by_group[b["group_name"] or "(no group recorded)"].append(
                title_day(b["day"])
            )

        summary = "; ".join(
            f"{g} on {', '.join(dict.fromkeys(days))}"
            for g, days in sorted(by_group.items())
        )

        cols = ["Day", "Slot", "Time", "Group", "Class", "Room"]

        return "\n".join(
            [f"**{teacher} handles {summary} in {subject_text}.**", ""]
            + md_table(cols, block_rows(m, blocks, cols))
        )

    def h_teacher_timetable(self, f):

        m = self.model

        events, days, message = self._teacher_events(f)

        if message:
            return message

        teacher = f.teachers[0]

        scope = "" if days == list(m.days) else f" — {days_text(m, days)}"

        if not events:
            return f"**{teacher}** has no classes{scope.replace(' — ', ' on ')}."

        blocks = m.blocks(events)

        periods = len({(e["day"], e["slot"]) for e in events})

        cols = ["Day", "Slot", "Time", "Subject", "Class", "Room", "Group"]

        return "\n".join(
            [
                f"**Timetable — {teacher}{scope}** "
                f"({plural(len(blocks), 'session')}, "
                f"{plural(periods, 'period')})",
                "",
            ]
            + md_table(cols, block_rows(m, blocks, cols))
        )

    # ------------------------------------------------------------------
    # several teachers / coordinator scenarios
    # ------------------------------------------------------------------

    def h_free_same_time(self, f):

        m = self.model

        teacher = f.teachers[0]

        days, _, message = self._cells_for(f)

        if message:
            return message

        if not days:
            return self._ask_when("day")

        lines = []

        for d in days:

            slots = [s for s in m.free_slots(teacher, d)]

            if not slots:
                lines.append(f"{teacher} has no free slot on {title_day(d)}.")
                continue

            sets = [
                m.free_faculty(d, s) - {teacher} for s in slots
            ]

            common = _sorted(set.intersection(*sets))

            lines.append(
                f"**Faculty free at the same time as {teacher} on "
                f"{title_day(d)}** — {teacher} is free in "
                f"{slots_text(m, slots, False)}; "
                f"{len(common)} others are free in all of those slots:"
            )

            lines.append("")

            lines += numbered(common) if common else ["Nobody."]

            lines += [""] + md_table(
                ["Slot", "Time", "Others free"],
                [[s, m.slot_label(s), len(x)] for s, x in zip(slots, sets)],
            )

            self._last_names = common

        return "\n".join(lines)

    def h_common_free_teachers(self, f):

        m = self.model

        teachers = list(f.teachers)

        days, slots, message = self._cells_for(f)

        if message:
            return message

        scope_days = days or list(m.days)

        scope_slots = slots or list(m.teaching_slots)

        rows = []
        total = 0

        for d in scope_days:

            common = [
                s for s in scope_slots
                if all(s not in m.periods(t, d) for t in teachers)
            ]

            total += len(common)

            rows.append([
                title_day(d),
                ", ".join(map(str, common)) or "—",
                "; ".join(x for x in (m.slot_label(s) for s in common) if x) or "—",
            ])

        names = " and ".join(teachers)

        head = (
            f"**{names} are free together in "
            f"{plural(total, 'slot')}** ({days_text(m, scope_days)})"
        )

        if total == 0:
            return head + "\n\nThere is no common free slot."

        return "\n".join(
            [head, ""] + md_table(["Day", "Common free slots", "Times"], rows)
        )

    def h_compare_teachers(self, f):

        m = self.model

        c = f.cues

        days, _, message = self._cells_for(f)

        if message:
            return message

        scope_days = days or list(m.days)

        total = len(scope_days) * len(m.slots)

        stats = []

        for t in f.teachers:

            periods = sum(len(m.periods(t, d)) for d in scope_days)

            stats.append((t, periods, total - periods))

        workload = "workload" in c or (
            "busy" in c and "free" not in c
        )

        metric = 1 if workload else 2

        want_more = "least" not in c or "most" in c

        best = (max if want_more else min)(s[metric] for s in stats)

        winners = [s[0] for s in stats if s[metric] == best]

        what = "classes (periods)" if workload else "free slots"

        direction = "more" if want_more else "fewer"

        if len(winners) == len(stats):
            verdict = f"**They are tied on {what}.**"
        else:
            verdict = f"**{' and '.join(winners)} has {direction} {what}.**"

        rows = [[t, p, fr] for t, p, fr in stats]

        return "\n".join(
            [verdict, ""]
            + md_table(["Faculty", "Periods with class", "Free slots"], rows)
            + ["", f"*Scope: {days_text(m, scope_days)} · "
                   f"{len(m.slots)} slots per day.*"]
        )

    def h_common_free_class_teacher(self, f):

        m = self.model

        teacher = f.teachers[0]
        class_name = f.classes[0]

        days, slots, message = self._cells_for(f)

        if message:
            return message

        from utils.faculty_names import alnum_key

        ckey = alnum_key(class_name)

        scope_days = days or list(m.days)

        scope_slots = slots or list(m.teaching_slots)

        rows = []
        total = 0

        for d in scope_days:

            common = [
                s for s in scope_slots
                if s not in m.class_periods(ckey, d)
                and s not in m.periods(teacher, d)
            ]

            total += len(common)

            rows.append([
                title_day(d),
                ", ".join(map(str, common)) or "—",
                "; ".join(x for x in (m.slot_label(s) for s in common) if x) or "—",
            ])

        head = (
            f"**{class_name} and {teacher} are free together in "
            f"{plural(total, 'slot')}** ({days_text(m, scope_days)})"
        )

        if total == 0:
            return head + "\n\nThere is no common free slot."

        return "\n".join(
            [head, ""]
            + md_table(["Day", "Common free slots", "Times"], rows)
        )

    def h_makeup_class(self, f):

        m = self.model

        from utils.faculty_names import alnum_key

        class_name = f.classes[0]

        ckey = alnum_key(class_name)

        days, slots, message = self._cells_for(f)

        if message:
            return message

        if not days:
            return self._ask_when("day")

        class_faculty = {
            e["teacher"] for e in m.by_class[ckey]
            if e["teacher"] in m.faculty_set
        }

        usual_rooms = {
            e["room"] for e in m.by_class[ckey] if e["room"]
        }

        scope_slots = slots or list(m.teaching_slots)

        lines = []

        for d in days:

            rows = []

            for s in scope_slots:

                if s in m.class_periods(ckey, d):
                    continue

                free_teachers = _sorted(
                    m.free_faculty(d, s) & class_faculty
                )

                free_rooms = m.free_rooms(d, s)

                usual = _sorted(usual_rooms & free_rooms)

                rows.append([
                    s,
                    m.slot_label(s),
                    ", ".join(free_teachers) or "—",
                    len(free_rooms),
                    ", ".join(usual) or "—",
                ])

            head = (
                f"**Slots on {title_day(d)} when {class_name} has no class** "
                f"· {len(rows)}"
            )

            lines.append(head)
            lines.append("")

            if not rows:
                lines.append("No free slot.")
                continue

            lines += md_table(
                [
                    "Slot", "Time",
                    f"Free faculty who teach {class_name}",
                    "Free rooms",
                    "Rooms this class normally uses that are free",
                ],
                rows,
            )

            lines.append("")

        return "\n".join(lines).rstrip()

    def h_subject_free_teachers(self, f):

        m = self.model

        keys = set(f.subjects)

        teachers = _sorted({
            e["teacher"] for e in m.events
            if e["subject"] in keys and e["teacher"] in m.faculty_set
        })

        subject_text = ", ".join(f.subjects)

        if not teachers:
            return f"No faculty member is recorded as teaching {subject_text}."

        days, slots, message = self._cells_for(f)

        if message:
            return message

        if not days:
            return self._ask_when("day")

        scope_slots = slots or list(m.teaching_slots)

        lines = [
            f"**Faculty who teach {subject_text} and are free on "
            f"{days_text(m, days)}**",
            "",
        ]

        rows = []

        for t in teachers:

            for d in days:

                free = [
                    s for s in scope_slots if s not in m.periods(t, d)
                ]

                rows.append([
                    t, title_day(d),
                    ", ".join(map(str, free)) or "—",
                    "; ".join(x for x in (m.slot_label(s) for s in free) if x) or "—",
                ])

        lines += md_table(["Faculty", "Day", "Free slots", "Times"], rows)

        self._last_names = teachers

        return "\n".join(lines)

    def _windows(self, days, size):

        m = self.model

        teaching = set(m.teaching_slots)

        out = []

        for d in days:

            for start in m.teaching_slots:

                block = list(range(start, start + size))

                if all(s in teaching for s in block):

                    free = set.intersection(
                        *[m.free_faculty(d, s) for s in block]
                    )

                    out.append((len(free), d, block))

        return out

    def h_meeting_blocks(self, f):

        m = self.model

        days, _, message = self._cells_for(f)

        if message:
            return message

        scope_days = days or list(m.days)

        size = f.block_size or 2

        windows = self._windows(scope_days, size)

        if not windows:
            return f"No {size}-slot block exists in {days_text(m, scope_days)}."

        best = max(w[0] for w in windows)

        ranked = sorted(
            windows,
            key=lambda w: (-w[0], m.days.index(w[1]), w[2][0]),
        )

        top = [w for w in ranked if w[0] == best]

        head = (
            f"**Best {size}-slot block{'s' if len(top) > 1 else ''} for a "
            f"meeting: {best} of {len(m.faculty)} faculty free**"
        )

        rows = [
            [
                title_day(d),
                f"{b[0]}-{b[-1]}",
                m.range_label(b[0], b[-1]),
                n,
                len(m.faculty) - n,
            ]
            for n, d, b in ranked[:max(len(top), 5)]
        ]

        return "\n".join(
            [head, ""]
            + md_table(["Day", "Slots", "Time", "Free", "Busy"], rows)
            + [
                "",
                f"*Considered {days_text(m, scope_days)}; only slots in "
                f"which classes actually run "
                f"({', '.join(map(str, m.teaching_slots))}).*",
            ]
        )

    def h_meeting_best(self, f):

        m = self.model

        days, slots, message = self._cells_for(f)

        if message:
            return message

        scope_days = days or list(m.days)

        scope_slots = slots or list(m.teaching_slots)

        cells = [
            (len(m.free_faculty(d, s)), d, s)
            for d in scope_days for s in scope_slots
        ]

        best = max(c[0] for c in cells)

        everyone = len(m.faculty)

        ranked = sorted(
            cells, key=lambda c: (-c[0], m.days.index(c[1]), c[2])
        )

        lines = []

        if best == everyone:

            lines.append(
                "**Everyone is free at these times:**"
            )
            lines.append("")

            lines += [
                f"- {title_day(d)}, slot {s}{m.slot_paren(s)}"
                for n, d, s in ranked if n == everyone
            ]

        else:

            lines.append(
                f"**No regular slot has all {everyone} faculty free.** "
                f"The best slots have {best} free "
                f"({everyone - best} busy):"
            )
            lines.append("")

            rows = [
                [title_day(d), s, m.slot_label(s), n, everyone - n]
                for n, d, s in ranked[:5]
            ]

            lines += md_table(["Day", "Slot", "Time", "Free", "Busy"], rows)

        empty = m.empty_cells()

        if empty:

            by_slot = defaultdict(list)

            for d, s in empty:
                by_slot[s].append(d)

            for s, ds in sorted(by_slot.items()):
                lines += [
                    "",
                    f"*Slot {s}{m.slot_paren(s)} has no classes at all "
                    f"on {days_text(m, ds)}, so everyone is free then.*",
                ]

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # rankings / aggregates over all faculty
    # ------------------------------------------------------------------

    def h_faculty_rank(self, f):

        m = self.model

        c = f.cues

        days, _, message = self._cells_for(f)

        if message:
            return message

        scope_days = days or list(m.days)

        total = len(scope_days) * len(m.slots)

        stats = []

        for t in m.faculty:

            periods = sum(len(m.periods(t, d)) for d in scope_days)

            stats.append((t, periods, total - periods))

        free_metric = "free" in c and "workload" not in c

        want_most = "most" in c and "least" not in c

        index = 2 if free_metric else 1

        best = (max if want_most else min)(s[index] for s in stats)

        winners = [s for s in stats if s[index] == best]

        ordered = sorted(
            stats,
            key=lambda s: (-s[index] if want_most else s[index], s[0].lower()),
        )

        what = "free slots" if free_metric else "periods"

        if free_metric:
            extreme = "Most" if want_most else "Fewest"
        else:
            extreme = "Heaviest workload — most" if want_most \
                else "Lightest workload — fewest"

        scope = (
            "in the week" if scope_days == list(m.days)
            else f"on {days_text(m, scope_days)}"
        )

        head = (
            f"**{extreme} {what} {scope}: "
            f"{', '.join(w[0] for w in winners[:6])}"
            f"{' …' if len(winners) > 6 else ''}** "
            f"({best} of {total})"
        )

        rows = [
            [i, s[0], s[1], s[2]]
            for i, s in enumerate(ordered[:10], 1)
        ]

        self._last_names = [w[0] for w in winners]

        return "\n".join(
            [head, ""]
            + md_table(["#", "Faculty", "Periods", "Free slots"], rows)
            + ["", f"*{total} slots per faculty {scope}; parallel sections "
                   f"count as one period.*"]
        )

    def h_day_rank_faculty(self, f):

        m = self.model

        c = f.cues

        rows = []

        stats = {}

        for d in m.days:

            free_total = sum(
                len(m.free_faculty(d, s)) for s in m.teaching_slots
            )

            busy_total = len(m.faculty) * len(m.teaching_slots) - free_total

            stats[d] = (free_total, busy_total)

            rows.append([
                title_day(d), free_total,
                round(free_total / len(m.teaching_slots), 1), busy_total,
            ])

        index = 1 if "busy" in c and "free" not in c else 0

        want_most = "most" in c and "least" not in c

        best = (max if want_most else min)(v[index] for v in stats.values())

        days = [d for d in m.days if stats[d][index] == best]

        what = "busy" if index else "free"

        return "\n".join(
            [
                f"**{'Highest' if want_most else 'Lowest'} number of "
                f"{what} faculty: "
                f"{', '.join(title_day(d) for d in days)}**",
                "",
            ]
            + md_table(
                ["Day", "Free faculty-slots", "Avg free per slot",
                 "Busy faculty-slots"],
                rows,
            )
            + ["", f"*Counted over the {len(m.teaching_slots)} slots in "
                   f"which classes run; {len(m.faculty)} faculty.*"]
        )

    def h_slot_rank_faculty(self, f):

        m = self.model

        c = f.cues

        cells = [
            (len(m.free_faculty(d, s)), d, s)
            for d in m.days for s in m.teaching_slots
        ]

        want_most = "most" in c and "least" not in c

        if "busy" in c and "free" not in c:
            cells = [(len(m.faculty) - n, d, s) for n, d, s in cells]

        best = (max if want_most else min)(x[0] for x in cells)

        ranked = sorted(
            cells,
            key=lambda x: (-x[0] if want_most else x[0],
                           m.days.index(x[1]), x[2]),
        )

        top = [x for x in ranked if x[0] == best]

        kind = "busy" if "busy" in c and "free" not in c else "free"

        head = (
            f"**{'Most' if want_most else 'Fewest'} {kind} faculty: "
            f"{best} of {len(m.faculty)}** — "
            + "; ".join(
                f"{title_day(d)} slot {s}{m.slot_paren(s)}"
                for _, d, s in top[:8]
            )
            + (" …" if len(top) > 8 else "")
        )

        rows = [
            [title_day(d), s, m.slot_label(s), n, len(m.faculty) - n]
            for n, d, s in ranked[:6]
        ]

        return "\n".join(
            [head, ""]
            + md_table(["Day", "Slot", "Time", kind.capitalize(), "Other"],
                       rows)
            + ["", f"*Only slots in which classes run "
                   f"({', '.join(map(str, m.teaching_slots))}) are ranked.*"]
        )

    def h_faculty_no_classes(self, f):

        m = self.model

        days, _, message = self._cells_for(f)

        if message:
            return message

        if days:

            names = [
                t for t in m.faculty
                if not any(m.periods(t, d) for d in days)
            ]

            scope = f" on {days_text(m, days)}"

        else:

            names = m.faculty_without_classes()
            scope = " at all"

        head = f"**Faculty with no classes{scope}** · {len(names)}"

        if not names:
            return head + "\n\nEveryone has at least one class."

        self._last_names = names

        return "\n".join([head, ""] + numbered(names))

    def h_faculty_all_days(self, f):

        m = self.model

        names = [
            t for t in m.faculty
            if all(m.periods(t, d) for d in m.days)
        ]

        head = (
            f"**Faculty with classes on all {len(m.days)} days "
            f"— {days_text(m, m.days)}** · {len(names)}"
        )

        if not names:
            return head + "\n\nNobody."

        self._last_names = names

        return "\n".join([head, ""] + numbered(names))

    def h_faculty_consecutive(self, f):

        m = self.model

        threshold = f.number if f.number is not None else 3

        days, _, message = self._cells_for(f)

        if message:
            return message

        scope_days = days or list(m.days)

        found = []
        longest = 0

        for t in m.faculty:

            for d in scope_days:

                for run in consecutive_runs(m.periods(t, d)):

                    longest = max(longest, len(run))

                    if len(run) > threshold:
                        found.append((t, d, run))

        if not found:
            return (
                f"**Nobody has more than {threshold} consecutive periods** "
                f"({days_text(m, scope_days)}). The longest stretch is "
                f"{longest} periods."
            )

        rows = [
            [t, title_day(d), f"{r[0]}-{r[-1]}", len(r)]
            for t, d, r in found
        ]

        return "\n".join(
            [
                f"**{plural(len(found), 'stretch', 'stretches')} of more "
                f"than {threshold} consecutive periods**",
                "",
            ]
            + md_table(["Faculty", "Day", "Slots", "Periods"], rows)
        )

    def h_filter_last(self, f):
        """'Which of them teach labs?' - filter the previous answer."""

        m = self.model

        names = list(self.last["names"])

        prev = self.last["frame"]

        days, slots, _ = self.resolve_cells(prev)

        lab_cue = "lab_words" in f.cues

        matches = []

        for name in names:

            events = m.by_teacher.get(name, [])

            if days and slots:
                events = [
                    e for e in events
                    if e["day"] in days and e["slot"] in slots
                ]

            if lab_cue and any(m.is_lab(e) for e in events):
                matches.append(name)

            elif not lab_cue and events:
                matches.append(name)

        when = scope_text(m, days, slots) if days and slots else "the week"

        what = "in a lab session" if lab_cue else "with a class"

        head = (
            f"**Of the {len(names)} faculty in the previous answer, "
            f"{len(matches)} are {what} — {when}**"
        )

        if not matches:
            return head + "\n\nNone of them."

        self._last_names = matches

        return "\n".join([head, ""] + numbered(matches))