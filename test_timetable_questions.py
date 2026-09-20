"""
test_timetable_questions.py
===========================

End-to-end test of the timetable chatbot for every kind of question a
timetable coordinator asks (free / busy lists, multi-slot conditions, one
teacher's availability, teacher / subject / class / room / lab questions,
rankings, coordinator scenarios, conflict checks, follow-ups, messy input).

Nothing here is hard-coded to one college:

* the teacher, subject, class, room, day and slot used in each question are
  PICKED FROM THE LOADED DATA (most-loaded teacher, busiest class, ...);
* the expected answer is computed INDEPENDENTLY from the raw scheduled
  events with the simple rules below - it never calls the engine under test.

Rules used for the expected answers
    free   = a real faculty member with no scheduled class in that cell
    period = one distinct slot (parallel sections count once)
    real faculty = a name with a real word (>= 4 letters) and no digit;
                   a name that is two other names glued together is split

Run:   python3 test_timetable_questions.py
       (also collectable by pytest: test_all_questions)
"""

import contextlib
import datetime
import io
import re
import sys
from collections import Counter, defaultdict

# a fixed "now" (a weekday, inside a teaching slot) keeps
# "right now", "today", "tomorrow" deterministic
FIXED_NOW = datetime.datetime(2026, 9, 18, 10, 30)   # Friday 10:30

DAY_ORDER = [
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
]

TITLES = {"dr", "mr", "mrs", "ms", "miss", "prof", "professor", "er",
          "shri", "smt", "sir", "madam", "mam"}


# ----------------------------------------------------------------------
# tiny helpers
# ----------------------------------------------------------------------

def key(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def tokens_of(name):
    toks = re.findall(r"[A-Za-z0-9]+", str(name).replace(".", " "))
    toks = [t.lower() for t in toks]
    while toks and toks[0] in TITLES:
        toks = toks[1:]
    return toks


def is_real_name(name):
    toks = tokens_of(name)
    return bool(toks) and not any(re.search(r"\d", t) for t in toks) \
        and any(len(re.sub(r"[^a-z]", "", t)) >= 4 for t in toks)


def runs(numbers):
    out = []
    for n in sorted(set(numbers)):
        if out and n == out[-1][-1] + 1:
            out[-1].append(n)
        else:
            out.append([n])
    return out


def names_from(resp):
    """Numbered lines '3. Name — detail' -> {Name}"""
    found = []
    for line in str(resp).splitlines():
        m = re.match(r"^\s*\d+\.\s+(.*?)\s*$", line)
        if m:
            found.append(re.split(r"\s+—\s+", m.group(1))[0].strip())
    return found


def table_rows(resp):
    rows = []
    for line in str(resp).splitlines():
        if line.startswith("|") and not re.match(r"^\|\s*-{3}", line):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows[1:] if rows else []


def slot_numbers(text):
    """'slot 1 (..), slots 3-4 (..), slot 8' -> {1,3,4,8}; '1, 2, 4' -> ..."""
    found = set()
    for a, b in re.findall(r"slots?\s+(\d+)(?:\s*-\s*(\d+))?", text):
        lo, hi = int(a), int(b or a)
        found.update(range(lo, hi + 1))
    return found


def cell_numbers(text):
    return {int(x) for x in re.findall(r"\d+", text)}


# ----------------------------------------------------------------------
# ground truth built from the RAW events
# ----------------------------------------------------------------------

class Truth:

    def __init__(self, matcher):

        raw = [
            e for e in matcher.events
            if e.get("record_type", "SCHEDULED_EVENT") == "SCHEDULED_EVENT"
        ]

        try:
            free_records = list(matcher.get_faculty_free_slots())
        except Exception:
            free_records = []

        names = {e["teacher"] for e in raw if e.get("teacher")}
        names |= {r["teacher"] for r in free_records if r.get("teacher")}

        # names that are two other names glued together
        by_key = {"".join(tokens_of(n)): n for n in names}
        self.composites = {}
        for n in names:
            t = tokens_of(n)
            for i in range(1, len(t)):
                a, b = "".join(t[:i]), "".join(t[i:])
                if a in by_key and b in by_key \
                        and by_key[a] != n and by_key[b] != n:
                    self.composites[n] = (by_key[a], by_key[b])
                    break

        self.faculty = sorted(
            n for n in names
            if n not in self.composites and is_real_name(n)
        )
        self.fset = set(self.faculty)

        events = []
        for e in raw:
            e = dict(e)
            e["day"] = str(e["day"]).lower()
            e["slot"] = int(e["slot"])
            if e.get("teacher") in self.composites:
                for part in self.composites[e["teacher"]]:
                    c = dict(e)
                    c["teacher"] = part
                    events.append(c)
            else:
                events.append(e)

        # spelling-variant merge for subjects / classes / rooms
        for field in ("subject", "class_name", "room"):
            best = {}
            cnt = defaultdict(Counter)
            for e in events:
                if e.get(field):
                    cnt[key(e[field])][e[field]] += 1
            for k, c in cnt.items():
                best[k] = c.most_common(1)[0][0]
            for e in events:
                if e.get(field):
                    e[field] = best[key(e[field])]

        self.events = events

        self.days = [d for d in DAY_ORDER
                     if d in {e["day"] for e in events}]

        slot_set = {e["slot"] for e in events}
        slot_set |= {int(r["slot"]) for r in free_records
                     if str(r.get("slot", "")).isdigit()}
        self.slots = sorted(slot_set)
        self.teaching = sorted({e["slot"] for e in events})

        self.slot_time = {}
        for e in events:
            if e.get("slot_time"):
                self.slot_time[e["slot"]] = e["slot_time"]
        for r in free_records:
            if r.get("slot_time") and int(r["slot"]) not in self.slot_time:
                self.slot_time[int(r["slot"])] = r["slot_time"]

        self.busy = defaultdict(set)
        self.cell = defaultdict(list)
        for e in events:
            self.cell[(e["day"], e["slot"])].append(e)
            if e["teacher"] in self.fset:
                self.busy[(e["day"], e["slot"])].add(e["teacher"])

        self.by_teacher = defaultdict(list)
        for e in events:
            if e["teacher"]:
                self.by_teacher[e["teacher"]].append(e)

        self.subjects = sorted({e["subject"] for e in events if e["subject"]})
        self.classes = sorted({e["class_name"] for e in events
                               if e["class_name"]})
        rooms = {e["room"] for e in events if e["room"]}

        # a room can also be known only from "room is free" records
        # (e.g. a room that is never used) - it is still a room
        try:
            room_records = list(matcher.get_room_free_slots())
        except Exception:
            room_records = []

        known = {key(r) for r in rooms}
        for rec_ in room_records:
            name = str(rec_.get("room") or "").strip()
            if name and key(name) not in known:
                known.add(key(name))
                rooms.add(name)

        self.rooms = sorted(rooms)

    # -------- availability
    def free(self, day, slot):
        return self.fset - self.busy[(day, slot)]

    def free_all(self, cells):
        sets = [self.free(d, s) for d, s in cells]
        return set.intersection(*sets)

    def busy_all(self, cells):
        sets = [set(self.busy[(d, s)]) for d, s in cells]
        return set.intersection(*sets)

    def periods(self, teacher, day=None):
        return sorted({e["slot"] for e in self.by_teacher[teacher]
                       if day is None or e["day"] == day})

    def minutes(self, slot):
        m = re.findall(r"(\d+):(\d+)", self.slot_time.get(slot, ""))
        if len(m) < 2:
            return None
        return (int(m[0][0]) * 60 + int(m[0][1]),
                int(m[1][0]) * 60 + int(m[1][1]))


# ----------------------------------------------------------------------
# bot loading and picks
# ----------------------------------------------------------------------

def load_bot():

    sys.path.insert(0, ".")

    with contextlib.redirect_stdout(io.StringIO()):
        from faculty_chatbot import FacultyAIChatbot
        bot = FacultyAIChatbot()

    bot.smart_engine.now_fn = lambda: FIXED_NOW

    return bot


class Picks:
    """Every value used in a question is taken from the data."""

    def __init__(self, truth):

        t = truth

        d = t.days
        s = t.teaching

        def day(i):
            return d[i % len(d)]

        def slot(i):
            return s[i % len(s)]

        self.days = day
        self.slot = slot

        load = Counter(e["teacher"] for e in t.events
                       if e["teacher"] in t.fset)

        with_lab = [
            n for n, _ in load.most_common()
            if any((e.get("type") or "").lower() == "lab"
                   for e in t.by_teacher[n])
        ]

        self.T = with_lab[0] if with_lab else load.most_common(1)[0][0]
        self.T2 = next(n for n, _ in load.most_common() if n != self.T)

        # a day on which T has classes, the first busy slot on it,
        # and a slot on that day where T is free
        per_day = Counter()
        for e in t.by_teacher[self.T]:
            per_day[e["day"]] += 1
        self.D = per_day.most_common(1)[0][0]
        self.N = t.periods(self.T, self.D)[0]
        free_here = [x for x in t.slots if x not in t.periods(self.T, self.D)]
        self.NFREE = free_here[0]

        # lab subject of T, and a lab subject taught by exactly one other
        t_labs = Counter(e["subject"] for e in t.by_teacher[self.T]
                         if (e.get("type") or "").lower() == "lab")
        self.S_T = t_labs.most_common(1)[0][0] if t_labs else None

        lab_teachers = defaultdict(set)
        for e in t.events:
            if (e.get("type") or "").lower() == "lab" and e["subject"] \
                    and e["teacher"]:
                lab_teachers[e["subject"]].add(e["teacher"])
        singles = sorted(
            s_ for s_, ts in lab_teachers.items()
            if len(ts) == 1 and self.T not in ts and key(s_) != key(self.S_T)
        )
        t_subjects = {key(e["subject"]) for e in t.by_teacher[self.T]}
        others = [x for x in t.subjects if key(x) not in t_subjects]
        self.S = singles[0] if singles else others[0]

        cls_load = Counter(e["class_name"] for e in t.events
                           if e["class_name"])
        self.C = cls_load.most_common(1)[0][0]

        room_load = Counter(e["room"] for e in t.events if e["room"])
        self.R = room_load.most_common(1)[0][0]


# ----------------------------------------------------------------------
# the checks
# ----------------------------------------------------------------------

class Suite:

    def __init__(self, bot=None, legacy=True):

        self.bot = bot or load_bot()
        self.legacy = legacy
        self.t = Truth(self.bot.matcher)
        self.p = Picks(self.t)
        self.results = []

    # ------------------------------------------------------------------
    def ask(self, question, keep_context=False):

        if not keep_context:
            self.bot.smart_engine.reset_context()

        with contextlib.redirect_stdout(io.StringIO()):
            return self.bot.process_query(question)

    def safe(self, section, name):
        """Run one group of checks; a crash counts as a failure."""

        try:
            section()
        except Exception as exc:                      # noqa: BLE001
            self.record(f"ERR:{name}", name, False,
                        f"{type(exc).__name__}: {exc}")

    def record(self, qid, question, ok, detail=""):
        self.results.append((qid, question, bool(ok), detail))

    def eq_set(self, qid, question, got, expected, what="names"):
        got, expected = set(got), set(expected)
        ok = got == expected
        detail = "" if ok else (
            f"{what}: missing {sorted(expected - got)[:5]} "
            f"({len(expected - got)}), extra {sorted(got - expected)[:5]} "
            f"({len(got - expected)})"
        )
        self.record(qid, question, ok, detail)
        return ok

    def contains(self, qid, question, resp, *needles):
        low = str(resp).lower()
        missing = [n for n in needles if str(n).lower() not in low]
        self.record(qid, question, not missing,
                    "" if not missing else f"missing text {missing}")
        return not missing

    # ------------------------------------------------------------------
    def run(self):

        t, p, ask = self.t, self.p, self.ask
        D = lambda i: p.days(i)
        cap = lambda d: d.capitalize()
        S = lambda i: p.slot(i)
        eq, rec = self.eq_set, self.record
        last_slot = t.teaching[-1]
        first_slot = t.teaching[0]

        a_day, a_slot = D(0), S(2)                # "Monday slot 3"
        b_day, b_slot = D(4), S(1)                # "Friday slot 2"
        c_day, c_slot = D(-1), first_slot         # "Saturday slot 1"


        # ---- values shared by several sections --------------------------
        T, T2, D0, N = p.T, p.T2, p.D, p.N
        S_ = p.S
        ev_s = [e for e in t.events if key(e["subject"]) == key(S_)]
        C = p.C
        ev_c = [e for e in t.events if e["class_name"] == C]
        R = p.R
        ev_r = [e for e in t.events if e["room"] == R]
        rd, rs = ev_r[0]["day"], ev_r[0]["slot"]
        today = DAY_ORDER[FIXED_NOW.weekday()]
        minute = FIXED_NOW.hour * 60 + FIXED_NOW.minute

        def headline(r):
            return r.splitlines()[0]

        # ============ 1. free / busy at a time ==========
        def section_0():
            q = f"Who is free on {cap(a_day)} slot {a_slot}?"
            r = ask(q)
            eq(1, q, names_from(r), t.free(a_day, a_slot))

            q = f"Who is busy on {cap(b_day)} slot {b_slot}?"
            eq(2, q, names_from(ask(q)), t.busy[(b_day, b_slot)])

            q = f"Which faculty have no class in slot {c_slot} on {cap(c_day)}?"
            eq(3, q, names_from(ask(q)), t.free(c_day, c_slot))

            s1, s2 = S(3), S(3) + 1
            if s2 in t.slots:
                q = f"Who is free in slots {s1} and {s2} both on {cap(D(2))}?"
                eq(4, q, names_from(ask(q)), t.free_all([(D(2), s1), (D(2), s2)]))

            lo = last_slot - 1
            hi = max(t.slots)
            q = f"Who is free from slot {lo} to slot {hi} on {cap(D(3))}?"
            eq(5, q, names_from(ask(q)),
               t.free_all([(D(3), x) for x in range(lo, hi + 1)
                           if x in t.slots]))

            q = "Who is free in the first slot every day of the week?"
            eq(6, q, names_from(ask(q)),
               t.free_all([(d, first_slot) for d in t.days]))

            q = f"Who is free in the last slot on {cap(D(1))}?"
            eq(7, q, names_from(ask(q)), t.free(D(1), last_slot))

            q = (f"Who is free on {cap(a_day)} slot {S(1)} "
                 f"but busy on {cap(a_day)} slot {a_slot}?")
            eq(8, q, names_from(ask(q)),
               t.free(a_day, S(1)) & t.busy[(a_day, a_slot)])

            q = f"Who is free in all slots on {cap(c_day)}?"
            eq(9, q, names_from(ask(q)),
               t.free_all([(c_day, x) for x in t.slots]))

            # "now" / "next" with the fixed clock
            now_slots = [x for x in t.slots if t.minutes(x)
                         and t.minutes(x)[0] <= minute < t.minutes(x)[1]]
            q = "Who is teaching right now?"
            r = ask(q)
            if today in t.days and now_slots:
                eq(10, q, names_from(r),
                   set().union(*[t.busy[(today, x)] for x in now_slots]))
            else:
                self.contains(10, q, r, "no class")

            q = "Who will be free in the next slot?"
            r = ask(q)
            upcoming = sorted(
                (t.minutes(x)[0], x) for x in t.teaching
                if t.minutes(x) and t.minutes(x)[0] > minute
            )
            if today in t.days and upcoming:
                eq(11, q, names_from(r), t.free(today, upcoming[0][1]))
            else:
                rec(11, q, bool(r), "")

        self.safe(section_0, '1. free / busy at a time')

        # ============ 2. one teacher availability ==========
        def section_1():

            q = f"Is {T} free on {cap(D0)} slot {N}?"
            r = ask(q)
            ev = [e for e in t.by_teacher[T] if e["day"] == D0 and e["slot"] == N]
            rec("12", q, r.lstrip("*").startswith("No") and
                ev[0]["subject"].lower() in r.lower(), r[:120])

            q = f"Is {T} free on {cap(D0)} slot {p.NFREE}?"
            r = ask(q)
            rec("12b", q, r.lstrip("*").startswith("Yes"), r[:120])

            free_slots = [x for x in t.slots if x not in t.periods(T, D0)]
            q = f"When is {T} free on {cap(D0)}?"
            r = ask(q)
            free_line = next((l for l in r.splitlines()
                              if l.startswith("Free:")), "")
            eq(13, q, slot_numbers(free_line), free_slots, "slots")

            q = f"Show all free slots of {T} across the week."
            r = ask(q)
            ok = True
            for row in table_rows(r):
                day = row[0].lower()
                if day in t.days:
                    exp = {x for x in t.slots if x not in t.periods(T, day)}
                    ok &= cell_numbers(row[1]) == exp
            rec(14, q, ok and len(table_rows(r)) == len(t.days), r[:80])

            q = f"Which day is {T} completely free?"
            r = ask(q)
            empty = [d for d in t.days if not t.periods(T, d)]
            if empty:
                ok = all(cap(d) in r for d in empty)
            else:
                lightest = min(len(t.periods(T, d)) for d in t.days)
                ok = "No day is completely free" in r and any(
                    cap(d) in r.split("Lightest:")[1]
                    for d in t.days if len(t.periods(T, d)) == lightest)
            rec(15, q, ok, r[:120])

            q = f"Which day is {T} most busy?"
            r = ask(q)
            cnt = {d: len(t.periods(T, d)) for d in t.days}
            top = [d for d in t.days if cnt[d] == max(cnt.values())]
            rec(16, q, all(cap(d) in r.splitlines()[0] for d in top), r[:100])

            q = f"What is {T}'s first and last class on {cap(D0)}?"
            r = ask(q)
            per = t.periods(T, D0)
            m1 = re.search(r"First class: slot (\d+)", r)
            m2 = re.search(r"Last class: slot (\d+)", r)
            rec(18 - 1, q, bool(m1 and m2) and int(m1.group(1)) == per[0]
                and int(m2.group(1)) == per[-1], r[:120])

            q = f"Does {T} have back-to-back classes on {cap(D0)}?"
            r = ask(q)
            has = any(len(x) >= 2 for x in runs(per))
            rec(18, q, r.lstrip("*").startswith("Yes") == has, r[:100])

            q = f"Does {T} have a gap between two classes on {cap(D0)}?"
            r = ask(q)
            gap = any(x not in per for x in t.slots if per[0] < x < per[-1])
            rec(19, q, r.lstrip("*").startswith("Yes") == gap, r[:100])

            q = (f"Is {T} free in both slot {p.NFREE} and slot {p.NFREE} on "
                 f"{cap(D0)}?")
            # a real two-slot version: two consecutive slots if T is free
            pair = next(((x, x + 1) for x in t.slots
                         if x + 1 in t.slots and x not in per
                         and x + 1 not in per), None)
            if pair:
                q = f"Is {T} free in both slot {pair[0]} and slot {pair[1]} " \
                    f"on {cap(D0)}?"
                rec(20, q, ask(q).lstrip("*").startswith("Yes"), "")
            busy_pair = next(((x, x + 1) for x in per if x + 1 in t.slots
                              and x + 1 not in per), None)
            if busy_pair:
                q = f"Is {T} free in both slot {busy_pair[0]} and slot " \
                    f"{busy_pair[1]} on {cap(D0)}?"
                rec("20b", q, ask(q).lstrip("*").startswith("No"), "")

            q = f"What is {T} doing on {cap(D0)} slot {N}?"
            r = ask(q)
            ev = [e for e in t.by_teacher[T] if e["day"] == D0 and e["slot"] == N]
            ok = all(e["subject"].lower() in r.lower() for e in ev)
            rec(21, q, ok, r[:120])

        self.safe(section_1, '2. one teacher availability')

        # ============ 3. teacher details ==========
        def section_2():
            q = f"Show the full timetable of {T}."
            r = ask(q)
            got = set()
            for row in table_rows(r):
                a, b = (row[1].split("-") + [row[1]])[:2] if "-" in row[1] \
                    else (row[1], row[1])
                for x in range(int(a), int(b) + 1):
                    got.add((row[0].lower(), x))
            exp = {(e["day"], e["slot"]) for e in t.by_teacher[T]}
            eq(22, q, got, exp, "cells")

            q = f"What subjects does {T} teach?"
            eq(23, q, {key(x) for x in names_from(ask(q))},
               {key(e["subject"]) for e in t.by_teacher[T] if e["subject"]})

            q = f"Which classes does {T} teach?"
            eq(24, q, {key(x) for x in names_from(ask(q))},
               {key(e["class_name"]) for e in t.by_teacher[T]
                if e["class_name"]})

            q = f"Which rooms does {T} use?"
            eq(25, q, {key(x) for x in names_from(ask(q))},
               {key(e["room"]) for e in t.by_teacher[T] if e["room"]})

            q = f"How many classes does {T} have per week?"
            r = ask(q)
            periods = len({(e["day"], e["slot"]) for e in t.by_teacher[T]})
            entries = len(t.by_teacher[T])
            rec(26, q, f"{periods} period" in r and f"{entries} class entr" in r,
                r[:130])

            q = f"How many lab sessions does {T} take?"
            r = ask(q)
            lab_ev = [e for e in t.by_teacher[T]
                      if (e.get("type") or "").lower() == "lab"]
            sessions = sum(len(runs([e["slot"] for e in lab_ev
                                     if e["day"] == d])) for d in t.days)
            lab_periods = len({(e["day"], e["slot"]) for e in lab_ev})
            rec(27, q, f"{sessions} lab session" in r
                and f"{lab_periods} lab period" in r, r[:130])

            lab_days = sorted({e["day"] for e in lab_ev})
            if lab_days:
                ld = lab_days[0]
                q = f"Does {T} teach any lab on {cap(ld)}?"
                r = ask(q)
                n_runs = len(runs([e["slot"] for e in lab_ev if e["day"] == ld]))
                rec(28, q, r.lstrip("*").startswith("Yes")
                    and f"{n_runs} lab session" in r, r[:120])
            no_lab_day = next((d for d in t.days if d not in lab_days), None)
            if no_lab_day:
                q = f"Does {T} teach any lab on {cap(no_lab_day)}?"
                rec("28b", q, ask(q).lstrip("*").startswith("No"), "")

            if p.S_T:
                q = f"Which group or batch does {T} handle in {p.S_T}?"
                r = ask(q)
                groups = {e["group_name"] for e in t.by_teacher[T]
                          if key(e["subject"]) == key(p.S_T) and e["group_name"]}
                rec("29", q, all(g in r for g in groups), r[:120])

            q = f"Which group or batch does {T} handle in {p.S}?"
            r = ask(q)
            rec("29b", q, "does not teach" in r, r[:100])

            q = f"Where will {T} be on {cap(D0)} slot {N}?"
            r = ask(q)
            ev = [e for e in t.by_teacher[T] if e["day"] == D0 and e["slot"] == N]
            rec(30, q, all((e["room"] or "").lower() in r.lower() for e in ev),
                r[:120])

        self.safe(section_2, '3. teacher details')

        # ============ 4. compare / rank ==========
        def section_3():
            total = len(t.days) * len(t.slots)
            load = {n: len({(e["day"], e["slot"]) for e in t.by_teacher[n]})
                    for n in t.faculty}


            q = "Who has the maximum free slots in the week?"
            r = ask(q)
            best = min(load.values())
            winners = sorted(n for n in t.faculty if load[n] == best)
            rec(31, q, all(w in headline(r) for w in winners[:6]), headline(r))

            q = "Who has the heaviest workload?"
            r = ask(q)
            best = max(load.values())
            winners = sorted(n for n in t.faculty if load[n] == best)
            rec(32, q, all(w in headline(r) for w in winners[:6]), headline(r))

            q = "Who has the lightest workload?"
            r = ask(q)
            best = min(load.values())
            winners = sorted(n for n in t.faculty if load[n] == best)
            rec(33, q, all(w in headline(r) for w in winners[:6]), headline(r))

            q = f"Who has more free slots, {T} or {T2}?"
            r = ask(q)
            win = T if load[T] < load[T2] else T2 if load[T2] < load[T] else None
            rec(34, q, (win in headline(r)) if win else "tied" in headline(r),
                headline(r))

            q = f"Who is free at the same time as {T} on {cap(D0)}?"
            r = ask(q)
            tfree = [x for x in t.slots if x not in t.periods(T, D0)]
            exp = set.intersection(*[t.free(D0, x) for x in tfree]) - {T} \
                if tfree else set()
            eq(35, q, names_from(r), exp)

            q = "On which day is the number of free faculty highest?"
            r = ask(q)
            tot = {d: sum(len(t.free(d, x)) for x in t.teaching) for d in t.days}
            top = [d for d in t.days if tot[d] == max(tot.values())]
            rec(36, q, all(cap(d) in headline(r) for d in top), headline(r))

            cells = [(len(t.free(d, x)), d, x)
                     for d in t.days for x in t.teaching]
            q = "Which slot of the week has the most free teachers?"
            r = ask(q)
            best = max(c[0] for c in cells)
            rec(37, q, f"{best} of {len(t.faculty)}" in headline(r), headline(r))

            q = "Which slot of the week has the fewest free teachers?"
            r = ask(q)
            worst = min(c[0] for c in cells)
            rec(38, q, f"{worst} of {len(t.faculty)}" in headline(r), headline(r))

            q = f"Which teachers have no classes on {cap(c_day)}?"
            eq(39, q, names_from(ask(q)),
               {n for n in t.faculty
                if not any(e["day"] == c_day for e in t.by_teacher[n])})

            q = "Which teachers work on all six days?"
            eq(40, q, names_from(ask(q)),
               {n for n in t.faculty
                if all(t.periods(n, d) for d in t.days)})

        self.safe(section_3, '4. compare / rank')

        # ============ 5. subjects ==========
        def section_4():

            q = f"Who teaches {S_}?"
            eq(41, q, names_from(ask(q)),
               {e["teacher"] for e in ev_s if e["teacher"]})

            held_days = sorted({e["day"] for e in ev_s},
                               key=lambda d: DAY_ORDER.index(d))
            q = f"Where is {S_} held on {cap(held_days[0])}?"
            r = ask(q)
            rooms = {e["room"] for e in ev_s
                     if e["day"] == held_days[0] and e["room"]}
            rec(42, q, all(x in r for x in rooms), r[:120])
            other = next((d for d in t.days if d not in held_days), None)
            if other:
                q = f"Where is {S_} held on {cap(other)}?"
                r = ask(q)
                rec("42b", q, "no session" in r.lower(), r[:100])

            q = f"At what time and on which days is {S_} scheduled?"
            r = ask(q)
            ok = all(cap(d) in r for d in held_days)
            rec(43, q, ok, r[:120])

            q = f"How many times per week is {S_} taught?"
            r = ask(q)
            buckets = defaultdict(list)
            for e in ev_s:
                buckets[(e["day"], e["class_name"], e["room"],
                         e["group_name"], e["teacher"])].append(e["slot"])
            times = sum(len(runs(v)) for v in buckets.values())
            periods = len({(e["day"], e["slot"]) for e in ev_s})
            rec(44, q, f"{times} time" in r and f"{periods} period" in r, r[:120])

            q = f"Which classes have {S_}?"
            eq(45, q, {key(x) for x in names_from(ask(q))},
               {key(e["class_name"]) for e in ev_s if e["class_name"]})

            q = f"Is {S_} a lab or a theory subject?"
            r = ask(q)
            kinds = Counter(e["type"] for e in ev_s if e["type"])
            rec(46, q, kinds.most_common(1)[0][0].lower() in r.lower(), r[:100])

            q = f"How many teachers share {S_}?"
            r = ask(q)
            n_t = len({e["teacher"] for e in ev_s if e["teacher"]})
            rec(47, q, re.search(rf"\b{n_t} teachers?\b", r) is not None, r[:100])

            q = f"Which subjects are taught on {cap(a_day)} slot {S(1)}?"
            eq(48, q, {key(x) for x in names_from(ask(q))},
               {key(e["subject"]) for e in t.cell[(a_day, S(1))]
                if e["subject"]})

            q = "List all subjects in the timetable."
            r = ask(q)
            eq(49, q, {key(x) for x in names_from(r)}, {key(s) for s in t.subjects})

            q = "Which subjects are taught only as labs?"
            typ = defaultdict(set)
            for e in t.events:
                if e["subject"] and e["type"]:
                    typ[e["subject"]].add(e["type"].lower() == "lab")
            eq(50, q, {key(x) for x in names_from(ask(q))},
               {key(s) for s, v in typ.items() if v == {True}})

        self.safe(section_4, '5. subjects')

        # ============ 6. classes ==========
        def section_5():

            q = f"Show the timetable of {C}."
            r = ask(q)
            got = set()
            for row in table_rows(r):
                a, b = (row[1].split("-") + [row[1]])[:2] if "-" in row[1] \
                    else (row[1], row[1])
                for x in range(int(a), int(b) + 1):
                    got.add((row[0].lower(), x, key(row[3])))
            exp = {(e["day"], e["slot"], key(e["subject"])) for e in ev_c}
            eq(51, q, got, exp, "cells")

            cd = t.days[0]
            cs = sorted({e["slot"] for e in ev_c if e["day"] == cd})[0]
            q = f"What does {C} have on {cap(cd)} slot {cs}?"
            r = ask(q)
            ev = [e for e in ev_c if e["day"] == cd and e["slot"] == cs]
            rec(52, q, all(e["subject"].lower() in r.lower() for e in ev), r[:120])

            q = f"Which class is free on {cap(cd)} slot {cs}?"
            busy_classes = {e["class_name"] for e in t.cell[(cd, cs)]
                            if e["class_name"]}
            eq(53, q, names_from(ask(q)), set(t.classes) - busy_classes)

            q = f"Who takes {C} on {cap(cd)} slot {cs}?"
            eq(54, q, names_from(ask(q)), {e["teacher"] for e in ev
                                            if e["teacher"]})

            q = f"How many lectures does {C} have on {cap(cd)}?"
            r = ask(q)
            n_p = len({e["slot"] for e in ev_c if e["day"] == cd})
            rec(55, q, f"{n_p} lecture period" in r, r[:100])

            q = f"Which teachers teach {C}?"
            eq(56, q, names_from(ask(q)), {e["teacher"] for e in ev_c
                                            if e["teacher"]})

            q = f"Which subjects are taught to {C}?"
            eq(57, q, {key(x) for x in names_from(ask(q))},
               {key(e["subject"]) for e in ev_c if e["subject"]})

            c_subj = next(e["subject"] for e in ev_c if e["subject"]
                          and e["room"])
            q = f"Which room does {C} use for {c_subj}?"
            r = ask(q)
            rooms = {e["room"] for e in ev_c if e["subject"] == c_subj
                     and e["room"]}
            rec(58, q, all(x in r for x in rooms), r[:120])

            lab_c_days = sorted({e["day"] for e in ev_c
                                 if (e["type"] or "").lower() == "lab"})
            if lab_c_days:
                q = f"Does {C} have any lab on {cap(lab_c_days[0])}?"
                rec(59, q, ask(q).lstrip("*").startswith("Yes"), "")
            no_lab_c = next((d for d in t.days if d not in lab_c_days), None)
            if no_lab_c:
                q = f"Does {C} have any lab on {cap(no_lab_c)}?"
                rec("59b", q, ask(q).lstrip("*").startswith("No"), "")

            q = f"Which day is lightest for {C}?"
            r = ask(q)
            cc = {d: len({e["slot"] for e in ev_c if e["day"] == d})
                  for d in t.days}
            low = [d for d in t.days if cc[d] == min(cc.values())]
            rec(60, q, all(cap(d) in headline(r) for d in low), headline(r))

            q = "List all classes and sections."
            eq(61, q, names_from(ask(q)), set(t.classes))

        self.safe(section_5, '6. classes')

        # ============ 7. rooms ==========
        def section_6():
            q = f"Which rooms are free on {cap(D(1))} slot {S(3)}?"
            busy_rooms = {e["room"] for e in t.cell[(D(1), S(3))] if e["room"]}
            eq(62, q, names_from(ask(q)), set(t.rooms) - busy_rooms)

            q = f"Which room is {S_} held in?"
            r = ask(q)
            rooms = {e["room"] for e in ev_s if e["room"]}
            rec(63, q, all(x in headline(r) for x in rooms), headline(r))

            q = f"Which class occupies {R} on {cap(rd)} slot {rs}?"
            r = ask(q)
            cl = {e["class_name"] for e in ev_r if e["day"] == rd
                  and e["slot"] == rs and e["class_name"]}
            rec(64, q, all(c in r for c in cl), r[:120])

            q = f"Show the weekly occupancy of {R}."
            r = ask(q)
            used = len({(e["day"], e["slot"]) for e in ev_r})
            rec(65, q, f"{used} of" in r, r[:120])

            q = f"When is {R} free on {cap(rd)}?"
            r = ask(q)
            busy_slots = {e["slot"] for e in ev_r if e["day"] == rd}
            free_slots_r = {x for x in t.slots if x not in busy_slots}
            row = next(x for x in table_rows(r) if x[0].lower() == rd)
            eq(66, q, cell_numbers(row[1]), free_slots_r, "slots")

            q = "Which rooms are used for labs?"
            eq(67, q, {key(x) for x in names_from(ask(q))},
               {key(e["room"]) for e in t.events
                if e["room"] and (e["type"] or "").lower() == "lab"})

            q = "Which room is the busiest during the week?"
            r = ask(q)
            use = Counter()
            for rr in t.rooms:
                use[rr] = len({(e["day"], e["slot"]) for e in t.events
                               if e["room"] == rr})
            top = [rr for rr, n in use.items() if n == max(use.values())]
            rec(68, q, all(x in headline(r) for x in sorted(top)[:6]),
                headline(r))

            q = f"Which rooms are never used on {cap(c_day)}?"
            eq(69, q, names_from(ask(q)),
               set(t.rooms) - {e["room"] for e in t.events
                               if e["day"] == c_day and e["room"]})

            q = "List all rooms."
            eq(70, q, names_from(ask(q)), set(t.rooms))

        self.safe(section_6, '7. rooms')

        # ============ 8. labs ==========
        def section_7():
            q = f"Which labs are running on {cap(D(2))}?"
            r = ask(q)
            lab_subj = {key(e["subject"]) for e in t.events
                        if e["day"] == D(2) and (e["type"] or "").lower() == "lab"
                        and e["subject"]}
            eq(71, q, {key(x) for x in names_from(r)}, lab_subj)

            q = "Which lab sessions span more than one slot?"
            r = ask(q)
            groups_ = defaultdict(list)
            for e in t.events:
                if (e["type"] or "").lower() == "lab":
                    groups_[(e["day"], e["subject"], e["class_name"], e["room"],
                             e["group_name"], e["type"])].append(e["slot"])
            all_s = [x for v in groups_.values() for x in runs(v)]
            multi = [x for x in all_s if len(x) >= 2]
            rec(72, q, f"{len(multi)} lab session" in r, headline(r))

            q = f"Who is in the lab on {cap(rd)} slot {rs}?"
            r = ask(q)
            lab_cell = {e["teacher"] for e in t.cell[(rd, rs)]
                        if (e["type"] or "").lower() == "lab" and e["teacher"]}
            # (use a cell that really has a lab)
            lab_cells = [(d, x) for d in t.days for x in t.teaching
                         if any((e["type"] or "").lower() == "lab"
                                for e in t.cell[(d, x)])]
            if lab_cells:
                d_, x_ = lab_cells[0]
                q = f"Who is in the lab on {cap(d_)} slot {x_}?"
                r = ask(q)
                exp = {e["teacher"] for e in t.cell[(d_, x_)]
                       if (e["type"] or "").lower() == "lab" and e["teacher"]}
                got = {row[2] for row in table_rows(r) if len(row) > 2
                       and row[2] != "—"}
                eq(73, q, got, exp)

            lab_cls = [e for e in ev_c if (e["type"] or "").lower() == "lab"]
            if lab_cls:
                ls = lab_cls[0]["slot"]
                q = f"Which batches or groups of {C} have lab in slot {ls}?"
                r = ask(q)
                gr = {e["group_name"] for e in lab_cls
                      if e["slot"] == ls and e["group_name"]}
                rec(74, q, all(g in headline(r) for g in gr), headline(r))

            q = f"Which lab has no session on {cap(b_day)}?"
            r = ask(q)
            lab_all = {key(e["subject"]) for e in t.events
                       if (e["type"] or "").lower() == "lab" and e["subject"]}
            running = {key(e["subject"]) for e in t.events
                       if e["day"] == b_day and (e["type"] or "").lower() == "lab"
                       and e["subject"]}
            eq(75, q, {key(x) for x in names_from(r)}, lab_all - running)

        self.safe(section_7, '8. labs')

        # ============ 9. coordinator ==========
        def section_8():
            if self.legacy:

                q = f"{T} is absent on {cap(D0)}. Who can cover their classes?"
                r = ask(q)
                self.check_cover(76, q, r, T, D0, None)

                q = f"Suggest a substitute for {T}'s {cap(D0)} slot {N} class."
                r = ask(q)
                self.check_cover(77, q, r, T, D0, N)

            q = (f"Who is free and teaches {S_} who could take an extra class "
                 f"on {cap(D(5))}?")
            r = ask(q)
            teachers = {e["teacher"] for e in ev_s if e["teacher"] in t.fset}
            got = {row[0] for row in table_rows(r)}
            eq(78, q, got, teachers)
            ok = True
            for row in table_rows(r):
                exp = {x for x in t.teaching
                       if x not in t.periods(row[0], D(5))}
                ok &= cell_numbers(row[2]) == exp
            rec("78b", q, ok, "free slots per teacher")

            q = (f"I need a slot when {T} and {T2} are both free. "
                 f"When can we meet?")
            r = ask(q)
            ok = True
            for row in table_rows(r):
                d = row[0].lower()
                exp = {x for x in t.teaching
                       if x not in t.periods(T, d) and x not in t.periods(T2, d)}
                ok &= cell_numbers(row[1]) == exp
            rec(79, q, ok and len(table_rows(r)) == len(t.days), r[:100])

            q = (f"Find a common free slot for {C} and {T} to schedule an "
                 f"extra class.")
            r = ask(q)
            ok = True
            for row in table_rows(r):
                d = row[0].lower()
                cbusy = {e["slot"] for e in ev_c if e["day"] == d}
                exp = {x for x in t.teaching
                       if x not in cbusy and x not in t.periods(T, d)}
                ok &= cell_numbers(row[1]) == exp
            rec(80, q, ok, r[:100])

            q = (f"Where can I hold a make-up class for {C} on {cap(D(3))}? "
                 f"I need a free room and a free teacher.")
            r = ask(q)
            cbusy = {e["slot"] for e in ev_c if e["day"] == D(3)}
            exp_slots = {x for x in t.teaching if x not in cbusy}
            got_slots = {int(row[0]) for row in table_rows(r)
                         if row and row[0].isdigit()}
            ok = got_slots == exp_slots
            for row in table_rows(r):
                if row[0].isdigit():
                    x = int(row[0])
                    busy_r = {e["room"] for e in t.cell[(D(3), x)] if e["room"]}
                    ok &= int(row[3]) == len(set(t.rooms) - busy_r)
            rec(81, q, ok, r[:100])

            q = (f"Which two-slot block on {cap(D(5))} has the most free "
                 f"faculty for a meeting?")
            r = ask(q)
            best = max(len(t.free(D(5), x) & t.free(D(5), x + 1))
                       for x in t.teaching if x + 1 in t.teaching)
            rec(82, q, f"{best} of {len(t.faculty)}" in headline(r), headline(r))

            q = "Which slot is best for a department meeting? Everyone should be free."
            r = ask(q)
            best = max(len(t.free(d, x)) for d in t.days for x in t.teaching)
            if best == len(t.faculty):
                rec(83, q, "Everyone is free" in r, headline(r))
            else:
                rec(83, q, f"{best} free" in r, headline(r))

        self.safe(section_8, '9. coordinator')

        # ============ 10. data quality ==========
        def section_9():
            q = "Is any teacher scheduled in two places at the same time?"
            r = ask(q)
            self.check_teacher_conflicts(84, q, r)

            q = "Is any room double-booked?"
            r = ask(q)
            self.check_room_conflicts(85, q, r)

            q = "Is any class assigned two subjects in the same slot?"
            r = ask(q)
            self.check_class_conflicts(86, q, r)

            q = "Which teachers have no classes at all?"
            eq(87, q, names_from(ask(q)),
               {n for n in t.faculty if not t.by_teacher[n]})

            q = "Which slots have no classes for any section?"
            r = ask(q)
            empty = sorted({x for d in t.days for x in t.slots
                            if not t.cell[(d, x)]})
            rec(88, q, all(f"Slot {x}" in r for x in empty), r[:120])

            q = "Which teachers have more than 5 consecutive classes on a day?"
            r = ask(q)
            longest = max(
                (len(x) for n in t.faculty for d in t.days
                 for x in runs(t.periods(n, d))), default=0)
            if longest > 5:
                rec(89, q, "consecutive" in r, r[:100])
            else:
                rec(89, q, "Nobody" in r and f"{longest} periods" in r, r[:120])

        self.safe(section_9, '10. data quality')

        # ============ 11. follow-ups ==========
        def section_10():
            eng = self.bot.smart_engine

            eng.reset_context()
            first = ask(f"Who is free on {cap(a_day)} slot {a_slot}?",
                        keep_context=True)
            nxt = a_slot + 1 if a_slot + 1 in t.slots else a_slot - 1
            follow = ask(f"What about slot {nxt}?", keep_context=True)
            direct = ask(f"Who is free on {cap(a_day)} slot {nxt}?")
            rec(90, f"[follow-up] What about slot {nxt}?", follow == direct,
                follow[:100])

            eng.reset_context()
            ask(f"Who teaches {S_}?", keep_context=True)
            follow = ask("Where is it held?", keep_context=True)
            direct = ask(f"Where is {S_} held?")
            rec(91, "[follow-up] Where is it held?", follow == direct,
                follow[:100])

            eng.reset_context()
            ask(f"Show {T}'s timetable.", keep_context=True)
            follow = ask(f"Only {cap(D0)}.", keep_context=True)
            direct = ask(f"Show {T}'s timetable on {cap(D0)}.")
            rec(92, f"[follow-up] Only {cap(D0)}.", follow == direct,
                follow[:100])

            eng.reset_context()
            ask(f"Which rooms are free on {cap(b_day)} slot {b_slot}?",
                keep_context=True)
            follow = ask("And which teachers?", keep_context=True)
            direct = ask(f"Who is free on {cap(b_day)} slot {b_slot}?")
            rec(93, "[follow-up] And which teachers?", follow == direct,
                follow[:100])

            eng.reset_context()
            first = ask(f"Who is busy on {cap(a_day)} slot {first_slot}?",
                        keep_context=True)
            prev = set(names_from(first))
            follow = ask("Which of them teach labs?", keep_context=True)
            got = set(names_from(follow))
            exp = {n for n in prev
                   if any((e["type"] or "").lower() == "lab"
                          and e["day"] == a_day and e["slot"] == first_slot
                          for e in t.by_teacher[n])}
            eq(94, "[follow-up] Which of them teach labs?", got, exp)

        self.safe(section_10, '11. follow-ups')

        # ============ 12. messy input ==========
        def section_11():
            base = ask(f"Who is free on {cap(a_day)} slot {a_slot}?")
            typo_day = cap(a_day)[0] + cap(a_day)[2:]      # drop 2nd letter
            q = f"who is fre on {typo_day.lower()} {a_slot}rd period"
            r = ask(q)
            rec(95, q, set(names_from(r)) == set(names_from(base)), r[:80])

            q = f"is dr. {tokens_of(T)[0]} free tmrw"
            r = ask(q)
            tomorrow = DAY_ORDER[(FIXED_NOW.weekday() + 1) % 7]
            want = ask(f"Is {T} free on {cap(tomorrow)}?") \
                if tomorrow in t.days else None
            # a partial first name may be ambiguous: accept a disambiguation
            ok = (r == want) if want else ("not part of this timetable" in r)
            ok = ok or "matches several" in r
            rec(96, q, ok, r[:100])

            q = "free teachers today"
            r = ask(q)
            want = ask(f"Who is free on {cap(today)}?")
            rec(97, q, r == want, r[:100])

            q = f"{T} kal free hai kya?"
            r = ask(q)
            rec(98, q, r == ask(f"Is {T} free on {cap(tomorrow)}?")
                if tomorrow in t.days else "not part of this timetable" in r,
                r[:100])

            q = f"{cap(a_day)[:3]} {a_slot}"
            r = ask(q)
            rec(99, q, set(names_from(r)) == set(names_from(base)), r[:80])

            missing_day = next((d for d in DAY_ORDER if d not in t.days),
                               "sunday")
            q = f"{cap(missing_day)} slot 2"
            r = ask(q)
            rec(100, q, cap(missing_day) in r and "not part" in r, r[:100])

            q = f"{cap(a_day)} slot {max(t.slots) + 1}"
            r = ask(q)
            rec(101, q, f"no slot {max(t.slots) + 1}" in r.lower(), r[:100])

            q = f"Who is free on {cap(a_day)} slot {a_slot} in {p.C}-XX-Z?"
            r = ask(q)
            rec(102, q, "couldn't find a class" in r, r[:100])

            q = "Show timetable of Dr. Nobodyatall"
            r = ask(q)
            rec(103, q, "couldn't find a teacher" in r, r[:100])

            q = "Who is free?"
            r = ask(q)
            rec(104, q, "day" in r.lower() and "slot" in r.lower(), r[:100])

            for qid, q in (("105a", "Hello"), ("105b", "thanks"),
                           ("105c", "help")):
                r = ask(q)
                rec(qid, q, len(r) > 10 and "could not understand" not in r,
                    r[:80])

        self.safe(section_11, '12. messy input')

        # ============ extra: never leak junk into lists ==========
        def section_12():
            codes = {n for n in ({e["teacher"] for e in t.events
                                  if e["teacher"]} | set(t.composites))
                     if n not in t.fset}
            big = names_from(ask(f"Who is free on {cap(a_day)} slot {a_slot}?"))
            rec("X1", "no placeholder codes / merged names in free lists",
                not (set(big) & codes) and len(big) == len(set(big)),
                f"{sorted(set(big) & codes)}")

        self.safe(section_12, 'extra: never leak junk into lists')

        # ============ regression: original pipeline still works ==========
        def section_13():
            if self.legacy:

                r = ask(f"What classes will be affected if {T} is absent on "
                        f"{cap(D0)}?")
                rec("R1", "legacy: affected classes still answered",
                    "Total affected classes" in r, r[:80])

                r = ask(f"Workload of {T}")
                rec("R2", "legacy: workload dashboard still answered",
                    "could not understand" not in r and len(r) > 30, r[:80])



        self.safe(section_13, 'regression: original pipeline still works')

        # ============ 13. paraphrases and edge phrasings ===========
        def section_paraphrases():

            q = f"Who is not free on {cap(a_day)} slot {a_slot}?"
            eq("P1", q, names_from(ask(q)), t.busy[(a_day, a_slot)])

            if b_slot + 1 in t.slots:
                q = (f"Who is free on {cap(b_day)} slot {b_slot} "
                     f"or {b_slot + 1}?")
                eq("P2", q, names_from(ask(q)),
                   t.free(b_day, b_slot) | t.free(b_day, b_slot + 1))

            q = f"How many free slots does {T} have on {cap(D0)}?"
            r = ask(q)
            n_free = len([x for x in t.slots if x not in t.periods(T, D0)])
            rec("P3", q, f"{n_free} free slot" in r, r[:100])

            # a clock range made of two consecutive whole slots means
            # exactly those slots (even if a neighbouring slot overlaps)
            pair = next(((x, x + 1) for x in t.slots
                         if x + 1 in t.slots and t.minutes(x)
                         and t.minutes(x + 1)), None)
            if pair:
                a0 = t.minutes(pair[0])[0]
                b1 = t.minutes(pair[1])[1]
                fmt = lambda m_: f"{m_ // 60}:{m_ % 60:02d}"
                q = (f"Which rooms are free from {fmt(a0)} to {fmt(b1)} "
                     f"on {cap(D(2))}?")
                busy_r = set()
                for x in pair:
                    busy_r |= {e["room"] for e in t.cell[(D(2), x)]
                               if e["room"]}
                eq("P4", q, names_from(ask(q)), set(t.rooms) - busy_r)

                q = (f"Who is free from {fmt(a0)} to {fmt(b1)} "
                     f"on {cap(D(2))}?")
                eq("P5", q, names_from(ask(q)),
                   t.free_all([(D(2), x) for x in pair]))

            q = f"Is {T} busy on {cap(D0)} slot {N}?"
            rec("P6", q, ask(q).lstrip("*").startswith("Yes"), "")

            q = f"Who is free on {cap(a_day)} slot {a_slot}"
            eq("P7", q, names_from(ask(q)), t.free(a_day, a_slot))

            q = f"free faculty {a_day} {a_slot}"
            eq("P8", q, names_from(ask(q)), t.free(a_day, a_slot))

        self.safe(section_paraphrases, "13. paraphrases")

        return self.results
    # ------------------------------------------------------------------
    def check_cover(self, qid, q, resp, teacher, day, slot):
        """Every suggested substitute must be free for the whole block."""

        t = self.t

        blocks = defaultdict(list)
        for e in t.by_teacher[teacher]:
            if e["day"] == day and (slot is None or e["slot"] == slot):
                blocks[(e["class_name"], key(e["subject"]))].append(e["slot"])

        line_re = re.compile(
            r"^\d+\.\s+(.+?)\s+\((\d\d:\d\d) - (\d\d:\d\d)\)")

        # map start time -> slot
        start = {}
        for x, label in t.slot_time.items():
            start[label.split(" - ")[0]] = x

        bad = []
        listed = 0
        for line in resp.splitlines():
            m = line_re.match(line)
            if not m:
                continue
            listed += 1
            name, st = m.group(1), m.group(2)
            s0 = start.get(st)
            if name not in t.fset:
                bad.append((name, "not a real faculty"))
            elif s0 is not None and s0 in t.periods(name, day):
                bad.append((name, f"busy at slot {s0}"))

        if slot is not None:
            wrong_scope = any(
                start.get(m.group(2)) not in
                {e["slot"] for e in t.by_teacher[teacher]
                 if e["day"] == day and e["slot"] == slot}
                for m in (line_re.match(l) for l in resp.splitlines()) if m
            )
        else:
            wrong_scope = False

        self.record(qid, q, listed > 0 and not bad and not wrong_scope,
                    f"{listed} candidates; problems: {bad[:3]}"
                    f"{' (slot scope ignored)' if wrong_scope else ''}")

    def check_teacher_conflicts(self, qid, q, resp):

        t = self.t
        # independent recomputation: teacher in >1 different room in a cell
        found = set()
        cells = defaultdict(set)
        for e in t.events:
            if e["teacher"] and e["room"]:
                cells[(e["teacher"], e["day"], e["slot"])].add(e["room"])
        for k, rooms in cells.items():
            ks = sorted(key(r) for r in rooms)
            distinct = []
            for r in ks:
                if not any(r == d or r.startswith(d) or d.startswith(r)
                           for d in distinct):
                    distinct.append(r)
            if len(distinct) > 1:
                found.add(k)
        m = re.search(r"Yes — (\d+) teacher conflict", resp)
        if not found:
            self.record(qid, q, "No teacher conflicts" in resp, resp[:80])
        else:
            self.record(qid, q, bool(m) and int(m.group(1)) == len(found),
                        f"expected {len(found)}, headline {resp[:80]}")

    def check_room_conflicts(self, qid, q, resp):

        t = self.t
        listed = re.findall(r"Room (.+?) — (\w+) slot (\d+):", resp)
        ok = True
        for room, day, slot in listed:
            evs = [e for e in t.cell[(day.lower(), int(slot))]
                   if e["room"] == room]
            subs = {key(e["subject"]) for e in evs if e["subject"]}
            ok &= len(subs) >= 2
        m = re.search(r"Yes — (\d+) room conflict", resp)
        self.record(qid, q, (ok and (bool(listed) or "No room" in resp))
                    and (bool(m) or "No room conflicts" in resp),
                    resp[:80])

    def check_class_conflicts(self, qid, q, resp):

        t = self.t
        listed = re.findall(r"^\d+\. (.+?) — (\w+) slot (\d+):",
                            resp, re.M)
        ok = True
        for cls, day, slot in listed:
            evs = [e for e in t.cell[(day.lower(), int(slot))]
                   if e["class_name"] == cls]
            subs = {key(e["subject"]) for e in evs if e["subject"]}
            ok &= len(subs) >= 2
        self.record(qid, q, ok and (bool(listed)
                                    or "No class conflicts" in resp),
                    resp[:80])


# ----------------------------------------------------------------------
# a second, completely different timetable (proves nothing is hard-coded)
# ----------------------------------------------------------------------

def build_synthetic_events(seed=11):
    """
    5 days, 6 slots at different clock times, different names, class /
    room / subject naming, its own initials-only code and placeholder,
    one merged name, and two teachers with no classes.
    """

    import random

    rnd = random.Random(seed)

    days = ["monday", "tuesday", "wednesday", "thursday", "friday"]

    times = {1: "09:00 - 10:00", 2: "10:00 - 11:00", 3: "11:00 - 12:00",
             4: "13:00 - 14:00", 5: "14:00 - 15:00", 6: "15:00 - 16:00"}

    people = ["Prof. Alice Warren", "Mr. Bob Fernandez", "Ms. Carol Nguyen",
              "Dr. Dev Patel", "Dr. Eva Rossi", "Mr. Frank Ito",
              "Ms. Grace Okafor", "Dr. Hana Kim", "Mr. Ivan Petrov",
              "Ms. Julia Santos", "Dr. Kofi Mensah", "Mr. Liam Obrien"]
    idle = ["Dr. Zara Idle", "Mr. Quinn Spare"]           # no classes
    codes = ["QX", "Guest2"]                              # not people

    classes = ["Y1-A", "Y1-B", "Y2-A", "Y2-B", "Y3-Core"]
    rooms = [f"R{n}" for n in (101, 102, 103, 201, 202, 203, 301, 302)] \
        + ["Main Auditorium"]
    labs = [("Physics Lab", "Lab"), ("Chem Lab", "Lab"), ("Code Lab", "Lab"),
            ("Robotics Lab", "Lab")]
    theory = [("Calculus", "Theory"), ("Mechanics", "Theory"),
              ("Ethics", "Theory"), ("Algorithms", "Theory"),
              ("Databases", "Theory"), ("Signals", "Theory")]

    teacher_of = {}
    pool = people + codes
    for i, (sub, _) in enumerate(labs + theory):
        teacher_of[sub] = [pool[(2 * i) % len(pool)],
                           pool[(2 * i + 1) % len(pool)]]

    busy_t, busy_r = set(), set()
    events = []

    def place(day, slot_list, cls, sub, typ, group):

        for teacher in teacher_of[sub]:
            if any((teacher, day, x) in busy_t for x in slot_list):
                continue
            for room in rooms:
                if not any((room, day, x) in busy_r for x in slot_list):
                    for x in slot_list:
                        busy_t.add((teacher, day, x))
                        busy_r.add((room, day, x))
                        events.append({
                            "record_type": "SCHEDULED_EVENT",
                            "teacher": teacher, "day": day, "slot": x,
                            "subject": sub, "room": room,
                            "class_name": cls, "group_name": group,
                            "type": typ, "slot_time": times[x],
                        })
                    return True
        return False

    for cls in classes:
        for day in days:
            slot = 1
            while slot <= 6:
                roll = rnd.random()
                if roll < 0.30 and slot <= 5:
                    sub, typ = rnd.choice(labs)
                    grp = rnd.choice(["Group 1", "Group 2"])
                    if place(day, [slot, slot + 1], cls, sub, typ, grp):
                        slot += 2
                        continue
                elif roll < 0.85:
                    sub, typ = rnd.choice(theory)
                    place(day, [slot], cls, sub, typ, "")
                slot += 1

    # one merged name (two people glued together) on an idle cell
    events.append({
        "record_type": "SCHEDULED_EVENT",
        "teacher": "Alice Warren Bob Fernandez", "day": "friday", "slot": 6,
        "subject": "Faculty Seminar", "room": "Main Auditorium",
        "class_name": "", "group_name": "", "type": "Seminar",
        "slot_time": times[6],
    })

    return events, idle, times, days


class SyntheticMatcher:

    def __init__(self):

        events, idle, times, days = build_synthetic_events()

        self.events = events

        teachers = {e["teacher"] for e in events} | set(idle)

        busy = {(e["teacher"], e["day"], e["slot"]) for e in events}

        self._free = [
            {"record_type": "FACULTY_FREE_SLOT", "teacher": t, "day": d,
             "slot": s, "slot_time": times[s]}
            for t in sorted(teachers) for d in days for s in times
            if (t, d, s) not in busy
        ]

    def get_faculty_free_slots(self):
        return list(self._free)


class SyntheticBot:
    """Just enough of FacultyAIChatbot for the suite."""

    def __init__(self):

        sys.path.insert(0, ".")

        from engine.smart_query import SmartQueryEngine

        self.matcher = SyntheticMatcher()
        self.smart_engine = SmartQueryEngine.from_matcher(
            self.matcher, now_fn=lambda: FIXED_NOW, debug=True)

    def process_query(self, query):

        answer = self.smart_engine.answer(query)

        return answer if answer is not None \
            else self.smart_engine.fallback_text()


# ----------------------------------------------------------------------
# entry points
# ----------------------------------------------------------------------

def run_all():

    suite = Suite()

    return suite, suite.run()


def report(results):

    failed = [r for r in results if not r[2]]

    print(f"{len(results) - len(failed)}/{len(results)} checks passed")

    for qid, question, _, detail in failed:
        print(f"  FAIL [{qid}] {question}\n        {detail}")

    return failed


def run_synthetic():

    suite = Suite(bot=SyntheticBot(), legacy=False)

    return suite, suite.run()


def test_all_questions_on_a_different_timetable():
    """The same questions, a different college: nothing is hard-coded."""

    _, results = run_synthetic()

    failed = [r for r in results if not r[2]]

    assert not failed, "\n".join(
        f"[{qid}] {q} -> {d}" for qid, q, _, d in failed
    )


def test_all_questions():
    """pytest entry point."""

    _, results = run_all()

    failed = [r for r in results if not r[2]]

    assert not failed, "\n".join(
        f"[{qid}] {q} -> {d}" for qid, q, _, d in failed
    )


if __name__ == "__main__":

    _, results = run_all()

    print("--- bundled timetable ---")
    failed_main = report(results)

    print("--- different (synthetic) timetable ---")
    _, results = run_synthetic()
    failed_synth = report(results)

    sys.exit(1 if (failed_main or failed_synth) else 0)