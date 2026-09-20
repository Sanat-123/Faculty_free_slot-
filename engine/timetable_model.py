"""
TimetableModel
==============

A single, data-driven view of the loaded timetable.

Everything here is *learned from the events that were loaded*:
the list of days, the list of slots and their clock times, the faculty,
subjects, classes, rooms and groups.  No name, day count or slot count is
hard-coded, so the same code works for any institution's timetable.

Definition of "free" (the one rule the whole assistant uses)
------------------------------------------------------------
A faculty member is FREE in a (day, slot) cell when they have no scheduled
class in that cell.  A faculty member is BUSY when they do.  Explicit
"free" records in the source files are only used to discover which
people and which slots exist - a scheduled class always wins over a free
record, so the two can never disagree.

Who counts as "faculty"
-----------------------
* Entries that are not real names (initial-only codes such as "AS", or
  names containing digits such as "XE2") are kept in the schedule
  (a class is still a class) but are not reported as free/busy faculty or
  offered as substitutes - see utils.faculty_names.is_placeholder_name.
* A name that is exactly two other names glued together is split back
  into the two people (see utils.faculty_names.find_composites).
"""

import calendar
import re
from collections import Counter, defaultdict

from utils.faculty_names import (
    alnum_key,
    find_composites,
    is_placeholder_name,
    load_lexicon,
    name_key,
)

DAY_ORDER = [d.lower() for d in calendar.day_name]


# ----------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------

def normalize_day(value):
    """'Mon' / 'MONDAY' / 'monday' -> 'monday' (or None)."""

    text = str(value or "").strip().lower()

    if text in DAY_ORDER:
        return text

    if len(text) >= 3:
        for day in DAY_ORDER:
            if day.startswith(text[:3]):
                return day

    return None


def parse_clock_range(text):
    """'08:15 - 09:15' -> (495, 555) minutes since midnight, or None."""

    found = re.findall(r"(\d{1,2})[:.](\d{2})", str(text or ""))

    if len(found) < 2:
        return None

    (h1, m1), (h2, m2) = found[0], found[1]

    return int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)


def fmt_minutes(minutes):

    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def keys_compatible(a, b):
    """
    Two spelling keys refer to the same thing when they are equal or one
    is a prefix of the other (parser artefacts append stray text, e.g.
    'Seminar' vs 'Seminar ASh').
    """

    if not a or not b:
        return False

    return a == b or a.startswith(b) or b.startswith(a)


def text_compatible(a, b):
    """
    Two raw labels are variants of one thing when their spelling keys are
    prefix-related, or the tokens of the shorter are an in-order subsequence
    of the longer ('SPT E' inside 'SPT VKS E' - parser artefacts insert
    stray initials).
    """

    ka, kb = alnum_key(a), alnum_key(b)

    if keys_compatible(ka, kb):
        return True

    ta = re.findall(r"[a-z0-9]+", str(a).lower())
    tb = re.findall(r"[a-z0-9]+", str(b).lower())

    if not ta or not tb:
        return False

    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)

    it = iter(long_)

    return all(token in it for token in short)


def consecutive_runs(numbers):
    """[1,2,3,5,6] -> [[1,2,3],[5,6]]"""

    runs = []

    for n in sorted(set(numbers)):

        if runs and n == runs[-1][-1] + 1:
            runs[-1].append(n)
        else:
            runs.append([n])

    return runs


# ----------------------------------------------------------------------
# model
# ----------------------------------------------------------------------

class TimetableModel:

    def __init__(self, events, free_records=(), lexicon=None,
                 room_free_records=()):

        self.lexicon = lexicon or load_lexicon()

        raw_events = [self._normalize_event(e) for e in events]
        raw_events = [e for e in raw_events if e is not None]

        free_records = list(free_records or [])
        room_free_records = list(room_free_records or [])

        # ---------------- teacher universe ----------------

        names = {e["teacher"] for e in raw_events if e["teacher"]}

        for record in free_records:
            teacher = str(record.get("teacher") or "").strip()
            if teacher:
                names.add(teacher)

        self.composites = find_composites(sorted(names), self.lexicon)

        self.codes = sorted(
            (
                n for n in names
                if n not in self.composites
                and is_placeholder_name(n, self.lexicon)
            ),
            key=str.lower,
        )

        self.faculty = sorted(
            (
                n for n in names
                if n not in self.composites
                and n not in set(self.codes)
            ),
            key=str.lower,
        )

        self.faculty_set = set(self.faculty)
        self.code_set = set(self.codes)

        # ---------------- events (composites split) ----------------

        events_out = []

        for e in raw_events:

            if e["teacher"] in self.composites:

                for part in self.composites[e["teacher"]]:
                    copy = dict(e)
                    copy["teacher"] = part
                    events_out.append(copy)

            else:
                events_out.append(e)

        # ---------------- canonical spellings ----------------

        self._canonical_spelling(events_out, "subject")
        self._canonical_spelling(events_out, "class_name")
        self._canonical_spelling(events_out, "room")

        self.events = sorted(
            events_out,
            key=lambda e: (
                self._day_rank(e["day"]),
                e["slot"],
                e["teacher"],
                e["subject"],
            ),
        )

        # ---------------- days / slots ----------------

        present_days = {e["day"] for e in self.events}

        for record in list(free_records) + room_free_records:
            day = normalize_day(record.get("day"))
            if day:
                present_days.add(day)

        self.days = [d for d in DAY_ORDER if d in present_days]

        slot_numbers = {e["slot"] for e in self.events}

        for record in list(free_records) + room_free_records:
            slot = self._to_int(record.get("slot"))
            if slot is not None:
                slot_numbers.add(slot)

        self.slots = sorted(slot_numbers)

        self.teaching_slots = sorted({e["slot"] for e in self.events})

        self.slot_info = self._build_slot_info(
            self.events, list(free_records) + room_free_records
        )

        # ---------------- indexes ----------------

        self.by_teacher = defaultdict(list)
        self.cell_events = defaultdict(list)
        self.busy = defaultdict(set)

        self.by_subject = defaultdict(list)
        self.by_class = defaultdict(list)
        self.by_room = defaultdict(list)

        for e in self.events:

            if e["teacher"]:
                self.by_teacher[e["teacher"]].append(e)

            self.cell_events[(e["day"], e["slot"])].append(e)

            if e["teacher"] in self.faculty_set:
                self.busy[(e["day"], e["slot"])].add(e["teacher"])

            if e["subject"]:
                self.by_subject[alnum_key(e["subject"])].append(e)

            if e["class_name"]:
                self.by_class[alnum_key(e["class_name"])].append(e)

            if e["room"]:
                self.by_room[alnum_key(e["room"])].append(e)

        self.subjects = self._display_list(self.by_subject, "subject")
        self.classes = self._display_list(self.by_class, "class_name")
        self.rooms = self._display_list(self.by_room, "room")

        # A room that appears only in a "room is free" record (e.g. a
        # Location-wise timetable's empty cell) is still a room.
        known = {alnum_key(r) for r in self.rooms}

        extra = {}

        for record in room_free_records:

            name = re.sub(r"\s+", " ", str(record.get("room") or "")).strip()

            if name and alnum_key(name) not in known:
                extra.setdefault(alnum_key(name), name)

        self.rooms = sorted(set(self.rooms) | set(extra.values()),
                            key=str.lower)

        self.groups = sorted(
            {e["group_name"] for e in self.events if e["group_name"]},
            key=str.lower,
        )

        self.types = sorted(
            {e["type"] for e in self.events if e["type"]},
            key=str.lower,
        )

    # ==================================================================
    # construction helpers
    # ==================================================================

    @classmethod
    def from_matcher(cls, matcher, lexicon=None):
        """Build from a CanonicalEventMatcher (or anything with .events)."""

        events = [
            e for e in getattr(matcher, "events", [])
            if e.get("record_type", "SCHEDULED_EVENT")
            == "SCHEDULED_EVENT"
        ]

        free_records = []

        getter = getattr(matcher, "get_faculty_free_slots", None)

        if callable(getter):
            try:
                free_records = list(getter())
            except Exception:
                free_records = []

        room_free = []

        room_getter = getattr(matcher, "get_room_free_slots", None)

        if callable(room_getter):
            try:
                room_free = list(room_getter())
            except Exception:
                room_free = []

        return cls(events, free_records, lexicon=lexicon,
                   room_free_records=room_free)

    @classmethod
    def from_sqlite(cls, db_path, lexicon=None):
        """Build from the temporary SQLite DB written by pdf_pipeline."""

        import sqlite3

        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row

        try:
            table = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='timetable'"
            ).fetchone()

            if table is None:
                return cls([], lexicon=lexicon)

            rows = connection.execute(
                "SELECT * FROM timetable"
            ).fetchall()

            events = []

            for row in rows:

                r = dict(row)

                events.append({
                    "teacher": r.get("teacher") or r.get("faculty") or "",
                    "day": r.get("day"),
                    "slot": r.get("slot"),
                    "subject": r.get("subject") or "",
                    "room": r.get("room") or "",
                    "class_name": (
                        r.get("class_name") or r.get("class") or ""
                    ),
                    "group_name": (
                        r.get("group_name") or r.get("group") or ""
                    ),
                    "type": r.get("type") or "",
                    "slot_time": r.get("slot_time") or "",
                })

            return cls(events, lexicon=lexicon)

        finally:
            connection.close()

    @staticmethod
    def _to_int(value):

        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _day_rank(day):

        return DAY_ORDER.index(day) if day in DAY_ORDER else 99

    def _normalize_event(self, event):

        day = normalize_day(event.get("day"))
        slot = self._to_int(event.get("slot"))

        if day is None or slot is None:
            return None

        def clean(value):
            return re.sub(r"\s+", " ", str(value or "")).strip()

        return {
            "teacher": clean(event.get("teacher")),
            "day": day,
            "slot": slot,
            "subject": clean(event.get("subject")),
            "room": clean(event.get("room")),
            "class_name": clean(event.get("class_name")),
            "group_name": clean(
                event.get("group_name") or event.get("group")
            ),
            "type": clean(event.get("type")),
            "slot_time": clean(event.get("slot_time")),
        }

    @staticmethod
    def _canonical_spelling(events, field):
        """Merge spelling variants ('A-JAVA Lab/X' vs 'A-JAVA Lab X')."""

        variants = defaultdict(Counter)

        for e in events:
            if e[field]:
                variants[alnum_key(e[field])][e[field]] += 1

        best = {
            key: counter.most_common(1)[0][0]
            for key, counter in variants.items()
        }

        for e in events:
            if e[field]:
                e[field] = best[alnum_key(e[field])]

    @staticmethod
    def _display_list(index, field):

        names = set()

        for events in index.values():
            names.add(events[0][field])

        return sorted(names, key=str.lower)

    def _build_slot_info(self, events, free_records):

        seen = defaultdict(Counter)

        for e in events:
            if e["slot_time"]:
                seen[e["slot"]][e["slot_time"]] += 1

        for record in free_records:

            slot = self._to_int(record.get("slot"))
            label = str(record.get("slot_time") or "").strip()

            if slot is not None and label:
                seen[slot][label] += 1

        info = {}

        for slot in self.slots:

            if seen[slot]:
                label = seen[slot].most_common(1)[0][0]
            else:
                label = ""

            clock = parse_clock_range(label)

            info[slot] = {
                "label": label,
                "start": clock[0] if clock else None,
                "end": clock[1] if clock else None,
            }

        return info

    # ==================================================================
    # slot / time helpers
    # ==================================================================

    def slot_label(self, slot):
        """Clock text such as '08:15 - 09:15', or '' if the source has none."""

        return self.slot_info.get(slot, {}).get("label", "")

    def slot_paren(self, slot):
        """' (08:15 - 09:15)' - or '' when there is no clock time."""

        label = self.slot_label(slot)

        return f" ({label})" if label else ""

    def range_paren(self, first, last):

        label = self.range_label(first, last)

        return f" ({label})" if label else ""

    def range_label(self, first, last):
        """Clock span of consecutive slots first..last."""

        a = self.slot_info.get(first, {})
        b = self.slot_info.get(last, {})

        if a.get("start") is not None and b.get("end") is not None:
            return f"{fmt_minutes(a['start'])} - {fmt_minutes(b['end'])}"

        return ""

    def slots_overlapping(self, start, end):
        """
        Slots covered by a clock range [start, end].

        A slot counts when it lies INSIDE the range.  Only if no slot fits
        entirely inside (an unaligned range such as 10:00-11:00) do slots
        that merely overlap it count - so "10:15 to 12:15" means the slots
        that run 10:15-11:15 and 11:15-12:15, not a slot that starts at
        12:00 (some timetables have slots that overlap in time).
        """

        inside, touching = [], []

        for slot in self.slots:

            info = self.slot_info[slot]

            if info["start"] is None:
                continue

            if info["start"] >= start and info["end"] <= end:
                inside.append(slot)

            if min(end, info["end"]) - max(start, info["start"]) > 0:
                touching.append(slot)

        return inside or touching

    def slots_at_minute(self, minute):
        """Slots in progress at a given minute of the day."""

        return [
            s for s in self.slots
            if self.slot_info[s]["start"] is not None
            and self.slot_info[s]["start"] <= minute
            < self.slot_info[s]["end"]
        ]

    def next_slot_after(self, minute):
        """First slot starting after `minute` (today), else None."""

        upcoming = [
            (self.slot_info[s]["start"], s)
            for s in self.teaching_slots
            if self.slot_info[s]["start"] is not None
            and self.slot_info[s]["start"] > minute
        ]

        return min(upcoming)[1] if upcoming else None

    # ==================================================================
    # availability
    # ==================================================================

    def busy_faculty(self, day, slot):

        return set(self.busy.get((day, slot), set()))

    def free_faculty(self, day, slot):

        return self.faculty_set - self.busy.get((day, slot), set())

    def periods(self, teacher, day=None):
        """Sorted DISTINCT slots in which `teacher` has a class."""

        return sorted({
            e["slot"] for e in self.by_teacher.get(teacher, [])
            if day is None or e["day"] == day
        })

    def free_slots(self, teacher, day):

        busy = set(self.periods(teacher, day))

        return [s for s in self.slots if s not in busy]

    def class_periods(self, class_key, day=None):

        return sorted({
            e["slot"] for e in self.by_class.get(class_key, [])
            if day is None or e["day"] == day
        })

    def busy_rooms(self, day, slot):

        return {
            e["room"] for e in self.cell_events.get((day, slot), [])
            if e["room"]
        }

    def free_rooms(self, day, slot):

        return set(self.rooms) - self.busy_rooms(day, slot)

    def busy_classes(self, day, slot):

        return {
            e["class_name"]
            for e in self.cell_events.get((day, slot), [])
            if e["class_name"]
        }

    def is_lab(self, event):

        return "lab" in (event.get("type") or "").lower()

    # ==================================================================
    # blocks (consecutive periods of the same session)
    # ==================================================================

    def blocks(self, events, by=("subject", "class_name", "room",
                                 "group_name", "teacher", "type")):
        """
        Merge events that are the same session on consecutive slots.

        Returns a list of dicts sorted by (day, first slot).
        """

        buckets = defaultdict(list)

        for e in events:
            buckets[(e["day"],) + tuple(e[f] for f in by)].append(e)

        merged = []

        for key, group in buckets.items():

            slots = sorted({e["slot"] for e in group})

            for run in consecutive_runs(slots):

                sample = next(e for e in group if e["slot"] == run[0])

                merged.append({
                    "day": key[0],
                    "slots": run,
                    "first": run[0],
                    "last": run[-1],
                    "subject": sample["subject"],
                    "class_name": sample["class_name"],
                    "room": sample["room"],
                    "group_name": sample["group_name"],
                    "teacher": sample["teacher"],
                    "type": sample["type"],
                    "time": self.range_label(run[0], run[-1]),
                })

        merged.sort(
            key=lambda b: (
                self._day_rank(b["day"]),
                b["first"],
                b["subject"],
                b["class_name"],
            )
        )

        return merged

    def sessions(self, events):
        """
        A *session* is one class meeting: same day, subject, class, room and
        group on consecutive slots.  Unlike blocks(), several teachers of
        the same session are merged into one row.
        """

        buckets = defaultdict(list)

        for e in events:
            buckets[
                (e["day"], e["subject"], e["class_name"], e["room"],
                 e["group_name"], e["type"])
            ].append(e)

        out = []

        for key, group in buckets.items():

            slots = sorted({e["slot"] for e in group})

            for run in consecutive_runs(slots):

                teachers = sorted(
                    {
                        e["teacher"] for e in group
                        if e["slot"] in run and e["teacher"]
                    },
                    key=str.lower,
                )

                out.append({
                    "day": key[0],
                    "slots": run,
                    "first": run[0],
                    "last": run[-1],
                    "subject": key[1],
                    "class_name": key[2],
                    "room": key[3],
                    "group_name": key[4],
                    "type": key[5],
                    "teacher": ", ".join(teachers),
                    "time": self.range_label(run[0], run[-1]),
                })

        out.sort(
            key=lambda b: (
                self._day_rank(b["day"]), b["first"], b["subject"],
                b["class_name"],
            )
        )

        return out

    # ==================================================================
    # conflicts / data quality
    # ==================================================================

    def teacher_conflicts(self):
        """
        A teacher is double-booked when, in one cell, they are placed in
        two different rooms (rooms whose names are not just variants of
        each other).  Same room + several sections = a merged class, not
        a conflict.
        """

        conflicts = []

        cells = defaultdict(list)

        for e in self.events:
            if e["teacher"] and e["room"]:
                cells[(e["teacher"], e["day"], e["slot"])].append(e)

        for (teacher, day, slot), group in cells.items():

            rooms = sorted({e["room"] for e in group})

            distinct = []

            for room in rooms:
                key = alnum_key(room)
                if not any(keys_compatible(key, alnum_key(r))
                           for r in distinct):
                    distinct.append(room)

            if len(distinct) > 1:
                conflicts.append({
                    "teacher": teacher,
                    "day": day,
                    "slot": slot,
                    "rooms": distinct,
                    "subjects": sorted({e["subject"] for e in group}),
                })

        conflicts.sort(
            key=lambda c: (
                self._day_rank(c["day"]), c["slot"], c["teacher"]
            )
        )

        return conflicts

    def room_conflicts(self):
        """
        A room is double-booked when, in one cell, it hosts two
        unrelated sessions: different subjects AND different classes AND
        no teacher in common.
        """

        conflicts = []

        cells = defaultdict(list)

        for e in self.events:
            if e["room"]:
                cells[(e["room"], e["day"], e["slot"])].append(e)

        for (room, day, slot), group in cells.items():

            found = None

            for i in range(len(group)):
                for j in range(i + 1, len(group)):

                    a, b = group[i], group[j]

                    if not a["subject"] or not b["subject"]:
                        continue

                    if not a["class_name"] or not b["class_name"]:
                        continue

                    if text_compatible(a["subject"], b["subject"]):
                        continue

                    if text_compatible(a["class_name"], b["class_name"]):
                        continue

                    if a["teacher"] and a["teacher"] == b["teacher"]:
                        continue

                    found = (a, b)
                    break

                if found:
                    break

            if found:
                conflicts.append({
                    "room": room,
                    "day": day,
                    "slot": slot,
                    "sessions": [
                        f"{x['subject']} ({x['class_name']})"
                        for x in found
                    ],
                })

        conflicts.sort(
            key=lambda c: (
                self._day_rank(c["day"]), c["slot"], c["room"]
            )
        )

        return conflicts

    def class_conflicts(self):
        """
        A class is double-assigned when, in one cell, it has two
        different subjects for the same group (or ungrouped) - parallel
        batches with different group names are legitimate.
        """

        conflicts = []

        cells = defaultdict(list)

        for e in self.events:
            if e["class_name"] and e["subject"]:
                cells[
                    (alnum_key(e["class_name"]), e["day"], e["slot"])
                ].append(e)

        for (ckey, day, slot), group in cells.items():

            found = None

            for i in range(len(group)):
                for j in range(i + 1, len(group)):

                    a, b = group[i], group[j]

                    if text_compatible(a["subject"], b["subject"]):
                        continue

                    if (
                        a["group_name"] and b["group_name"]
                        and a["group_name"] != b["group_name"]
                    ):
                        continue

                    found = (a, b)
                    break

                if found:
                    break

            if found:
                conflicts.append({
                    "class_name": group[0]["class_name"],
                    "day": day,
                    "slot": slot,
                    "subjects": [found[0]["subject"], found[1]["subject"]],
                })

        conflicts.sort(
            key=lambda c: (
                self._day_rank(c["day"]), c["slot"], c["class_name"]
            )
        )

        return conflicts

    def faculty_without_classes(self):

        return [
            f for f in self.faculty
            if not self.by_teacher.get(f)
        ]

    def empty_cells(self):
        """(day, slot) cells in which no section has any class."""

        return [
            (d, s)
            for d in self.days
            for s in self.slots
            if not self.cell_events.get((d, s))
        ]

    def data_quality(self):
        """A structured summary used by the 'data quality' question."""

        return {
            "faculty": len(self.faculty),
            "codes": list(self.codes),
            "composites": dict(self.composites),
            "no_classes": self.faculty_without_classes(),
            "teacher_conflicts": self.teacher_conflicts(),
            "room_conflicts": self.room_conflicts(),
            "class_conflicts": self.class_conflicts(),
            "empty_cells": self.empty_cells(),
        }