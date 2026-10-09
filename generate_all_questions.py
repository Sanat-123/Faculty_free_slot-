#!/usr/bin/env python3
"""
generate_all_questions.py
=========================

Builds the complete question bank for the timetable chatbot and checks it.

* The question PATTERNS come from test_timetable_questions.py (plus the
  everyday phrasings, Hinglish / messy forms and paraphrases found while
  testing).
* Every pattern is expanded over EVERY teacher, class, room, subject, day
  and slot of the loaded timetable, so nothing is hard-coded to one college
  and nothing is sampled away.
* Each question is run through SmartQueryEngine and classified:
      OK        answered
      ASK_BACK  the bot asked for more information
      UNKNOWN   "couldn't map / couldn't find"
      LEGACY    belongs to the absence / exam-duty planners (not in the PDF bot)
      ERROR     exception
* For the main question types the answer is ALSO verified against an
  independent computation from the raw timetable events (VERIFIED / MISMATCH).

Usage
-----
    python generate_all_questions.py                       # newest PDF in the data folder
    python generate_all_questions.py --pdf my_timetable.pdf --out out_dir
    python generate_all_questions.py --db existing.db

Outputs (in --out, default ./question_bank)
    all_questions.csv   every question + status + verdict
    questions_only.txt  just the questions, grouped by category
    summary.md          counts per category and the failing examples
"""

from __future__ import annotations

import argparse
import csv
import itertools
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))



# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def key(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def names_from(resp):
    """Numbered lines '3. Name — detail' -> [Name, ...]"""
    found = []
    for line in str(resp).splitlines():
        m = re.match(r"^\s*\d+\.\s+(.*?)\s*$", line)
        if m:
            found.append(re.split(r"\s+—\s+", m.group(1))[0].strip())
    return found


def cap(day):
    return day.capitalize()


# ----------------------------------------------------------------------
# question patterns  (category, template, oracle-name or None)
#
# placeholders: {T} teacher  {T2} another teacher  {S} subject  {C} class
#               {R} room  {D} day  {D2} later day  {N} slot  {N2} later slot
#               {CS} a (class, subject) pair that really exists   -> {C}/{S}
#               {TS} a (teacher, subject) pair that really exists -> {T}/{S}
# ----------------------------------------------------------------------

PATTERNS = [
    # ---- 1. free / busy faculty at a time ------------------------------
    ("faculty free/busy", "Who is free on {D} slot {N}?", "free_list"),
    ("faculty free/busy", "Who is busy on {D} slot {N}?", "busy_list"),
    ("faculty free/busy", "Who is not free on {D} slot {N}?", "busy_list"),
    ("faculty free/busy", "Which faculty have no class in slot {N} on {D}?", "free_list"),
    ("faculty free/busy", "Which teachers are free on {D} at slot {N}?", "free_list"),
    ("faculty free/busy", "Who is available on {D} {N}th period?", "free_list"),
    ("faculty free/busy", "free faculty {d} {N}", "free_list"),
    ("faculty free/busy", "{D3} {N}", None),
    ("faculty free/busy", "Who is free on {D} slot {N} or {N2}?", None),
    ("faculty free/busy", "Who is free in slots {N} and {N2} both on {D}?", None),
    ("faculty free/busy", "Who is free from slot {N} to slot {N2} on {D}?", None),
    ("faculty free/busy", "Who is free on {D} and {D2} slot {N}?", None),
    ("faculty free/busy", "Who is free on {D}?", None),
    ("faculty free/busy", "Who is free in the first slot on {D}?", None),
    ("faculty free/busy", "Who is free in the last slot on {D}?", None),
    ("faculty free/busy", "Who is free in all slots on {D}?", None),
    ("faculty free/busy", "Which teachers have no classes on {D}?", None),
    ("faculty free/busy", "Who is free in the first slot every day of the week?", None),
    ("faculty free/busy", "Who is free?", "ask_back"),
    ("faculty free/busy", "who is fre on {d} {N}th period", None),
    ("faculty free/busy", "kaun kaun free hai {d} slot {N}", None),

    # ---- 1b. clock-time questions (only when the data has clock times) ----
    ("time of day", "Who is free on {D} at {TIME}?", "free_list"),
    ("time of day", "who is busy on {D} at {TIME}", "busy_list"),
    ("time of day", "Is {T} free on {D} at {TIME}?", "teacher_free"),
    ("time of day", "What time is slot {N}?", "slot_time"),
    ("time of day", "Which rooms are free on {D} at {TIME}?", "free_rooms"),

    # ---- 2. one teacher: availability -----------------------------------
    ("teacher availability", "Is {T} free on {D} slot {N}?", "teacher_free"),
    ("teacher availability", "Is {T} busy on {D} slot {N}?", "teacher_busy"),
    ("teacher availability", "Is {T} available on {D} slot {N}?", "teacher_free"),
    ("teacher availability", "{T} free on {D} slot {N}?", "teacher_free"),
    ("teacher availability", "When is {T} free on {D}?", None),
    ("teacher availability", "When is {T} free?", None),
    ("teacher availability", "Show all free slots of {T} across the week.", None),
    ("teacher availability", "free slots of {T}", None),
    ("teacher availability", "free periods of {T} on {D}", None),
    ("teacher availability", "How many free slots does {T} have on {D}?", None),
    ("teacher availability", "Which day is {T} completely free?", None),
    ("teacher availability", "Which day is {T} most busy?", None),
    ("teacher availability", "busiest day for {T}", None),
    ("teacher availability", "What is {T}'s first and last class on {D}?", None),
    ("teacher availability", "Does {T} have back-to-back classes on {D}?", None),
    ("teacher availability", "Does {T} have a gap between two classes on {D}?", None),
    ("teacher availability", "Is {T} free in both slot {N} and slot {N2} on {D}?", None),
    ("teacher availability", "What is {T} doing on {D} slot {N}?", None),
    ("teacher availability", "Where will {T} be on {D} slot {N}?", None),
    ("teacher availability", "Which room is {T} in on {D} slot {N}?", None),
    ("teacher availability", "Is {T} free tomorrow?", None),
    ("teacher availability", "{T} kal free hai kya?", None),

    # ---- 3. one teacher: profile ------------------------------------------
    ("teacher profile", "Show the full timetable of {T}.", "teacher_timetable"),
    ("teacher profile", "{T} schedule", "teacher_timetable"),
    ("teacher profile", "What is {T} teaching?", "teacher_timetable"),
    ("teacher profile", "Show {T}'s timetable on {D}.", None),
    ("teacher profile", "What subjects does {T} teach?", "teacher_subjects"),
    ("teacher profile", "Subjects of {T}", "teacher_subjects"),
    ("teacher profile", "Which classes does {T} teach?", "teacher_classes"),
    ("teacher profile", "Which rooms does {T} use?", None),
    ("teacher profile", "How many classes does {T} have per week?", "teacher_count"),
    ("teacher profile", "total periods of {T}", "teacher_count"),
    ("teacher profile", "How many lectures does {T} have on {D}?", None),
    ("teacher profile", "How many lab sessions does {T} take?", None),
    ("teacher profile", "Does {T} teach any lab on {D}?", None),
    ("teacher profile", "Which group or batch does {T} handle in {S}?", None),
    ("teacher profile", "Workload of {T}", "legacy"),
    ("teacher profile", "{T} is absent on {D}. Who can cover their classes?", "legacy"),
    ("teacher profile", "Suggest a substitute for {T}'s {D} slot {N} class.", "legacy"),
    ("teacher profile", "What classes will be affected if {T} is absent on {D}?", "legacy"),

    # ---- 4. faculty comparisons / rankings --------------------------------
    ("faculty rankings", "Who has the maximum free slots in the week?", None),
    ("faculty rankings", "Who has the heaviest workload?", None),
    ("faculty rankings", "Who has the lightest workload?", None),
    ("faculty rankings", "Who has the most lectures?", None),
    ("faculty rankings", "Who has more free slots, {T} or {T2}?", None),
    ("faculty rankings", "Who is free at the same time as {T} on {D}?", None),
    ("faculty rankings", "common free slot of {T} and {T2}", None),
    ("faculty rankings", "When are {T} and {T2} both free?", None),
    ("faculty rankings", "On which day is the number of free faculty highest?", None),
    ("faculty rankings", "Which slot of the week has the most free teachers?", None),
    ("faculty rankings", "Which slot of the week has the fewest free teachers?", None),
    ("faculty rankings", "Which teachers work on all {NDAYS} days?", None),
    ("faculty rankings", "Which teachers have no classes at all?", None),
    ("faculty rankings", "Which teachers have more than 5 consecutive classes on a day?", None),

    # ---- 5. faculty catalogue --------------------------------------------
    ("catalogue", "List all teachers", "list_teachers"),
    ("catalogue", "show all faculty", "list_teachers"),
    ("catalogue", "How many teachers are there?", "list_teachers"),
    ("catalogue", "List all subjects in the timetable.", "list_subjects"),
    ("catalogue", "How many subjects are there?", "list_subjects"),
    ("catalogue", "List all classes and sections.", "list_classes"),
    ("catalogue", "How many classes are there?", "list_classes"),
    ("catalogue", "List all rooms.", "list_rooms"),
    ("catalogue", "How many rooms are there?", "list_rooms"),
    ("catalogue", "Which rooms are used for labs?", None),
    ("catalogue", "Which subjects are taught only as labs?", None),
    ("catalogue", "Which lab sessions span more than one slot?", None),
    ("catalogue", "Which room is the busiest during the week?", None),

    # ---- 6. subjects ------------------------------------------------------
    ("subject", "Who teaches {S}?", "subject_teachers"),
    ("subject", "who takes {S}", "subject_teachers"),
    ("subject", "Which faculty teaches {S}?", "subject_teachers"),
    ("subject", "Where is {S} held?", None),
    ("subject", "Which room is {S} held in?", None),
    ("subject", "Where is {S} held on {D}?", None),
    ("subject", "At what time and on which days is {S} scheduled?", None),
    ("subject", "How many times per week is {S} taught?", None),
    ("subject", "Which classes have {S}?", None),
    ("subject", "Is {S} a lab or a theory subject?", None),
    ("subject", "How many teachers share {S}?", None),
    ("subject", "Which of the teachers of {S} are free on {D} slot {N}?", "subject_free_teachers"),
    ("subject", "Which subjects are taught on {D} slot {N}?", None),

    # ---- 7. classes -------------------------------------------------------
    ("class", "Show the timetable of {C}.", "class_timetable"),
    ("class", "schedule of {C}", "class_timetable"),
    ("class", "What does {C} have on {D} slot {N}?", None),
    ("class", "what is {C} doing on {D} slot {N}", None),
    ("class", "Is {C} free on {D} slot {N}?", "class_free"),
    ("class", "Who takes {C} on {D} slot {N}?", None),
    ("class", "How many lectures does {C} have on {D}?", None),
    ("class", "Which teachers teach {C}?", "class_teachers"),
    ("class", "Who teaches {C}?", "class_teachers"),
    ("class", "Which subjects are taught to {C}?", None),
    ("class", "Which room does {C} use for {S}?", None),
    ("class", "Who teaches {S} to {C}?", None),
    ("class", "Does {C} have any lab on {D}?", None),
    ("class", "Which day is lightest for {C}?", None),
    ("class", "Which class is free on {D} slot {N}?", "free_classes"),
    ("class", "Which class has lecture on {D} slot {N}?", "busy_classes"),
    ("class", "Which batches or groups of {C} have lab in slot {N}?", None),

    # ---- 8. rooms ---------------------------------------------------------
    ("room", "Which rooms are free on {D} slot {N}?", "free_rooms"),
    ("room", "Which rooms are empty on {D} slot {N}?", "free_rooms"),
    ("room", "Is room {R} free on {D} slot {N}?", "room_free"),
    ("room", "Which class occupies {R} on {D} slot {N}?", None),
    ("room", "Who is teaching in room {R} on {D} slot {N}?", None),
    ("room", "Show the weekly occupancy of {R}.", None),
    ("room", "When is {R} free on {D}?", None),
    ("room", "Which rooms are never used on {D}?", None),

    # ---- 9. labs ----------------------------------------------------------
    ("lab", "Which labs are running on {D}?", None),
    ("lab", "Who is in the lab on {D} slot {N}?", None),
    ("lab", "Which lab is happening on {D} slot {N}?", None),
    ("lab", "Which lab has no session on {D}?", None),

    # ---- 10. coordinator / data quality ------------------------------------
    ("coordinator", "Which slot is best for a department meeting? Everyone should be free.", None),
    ("coordinator", "Is any teacher scheduled in two places at the same time?", None),
    ("coordinator", "Is any room double-booked?", None),
    ("coordinator", "Is any class assigned two subjects in the same slot?", None),
    ("coordinator", "Which slots have no classes for any section?", None),

    # ---- 11. relative time / messy / meta -------------------------------------
    ("relative & messy", "Who is free today?", None),
    ("relative & messy", "free teachers today", None),
    ("relative & messy", "Who is teaching right now?", None),
    ("relative & messy", "Who will be free in the next slot?", None),
    ("relative & messy", "Who is free tomorrow?", None),
    ("relative & messy", "hello", None),
    ("relative & messy", "help", None),
    ("relative & messy", "thanks", None),

    # ---- 12. questions that must be refused gracefully ---------------------
    ("negative (must refuse)", "Show timetable of {NT}", "refuse"),
    ("negative (must refuse)", "Show timetable of {NC}", "refuse"),
    ("negative (must refuse)", "Who is free on {ND} slot {N1}?", "refuse"),
    ("negative (must refuse)", "Who is free on {D} slot {NS}?", "refuse"),
    ("negative (must refuse)", "What is the weather today?", "refuse"),
    ("negative (must refuse)", "tell me a joke", "refuse"),
    ("negative (must refuse)", "how many students are there", "refuse"),
]

# human-style name forms (checks that names resolve however they are typed)
NAME_FORM_TEMPLATES = [
    "Show timetable of {X}",
    "When is {X} free on {D1}?",
]


# ----------------------------------------------------------------------
# expansion
# ----------------------------------------------------------------------

def expand(template, data):
    """Yield (question, bindings) for every value combination."""

    holders = set(re.findall(r"\{(\w+)\}", template))

    pair_cs = "S" in holders and "C" in holders
    pair_ts = "S" in holders and "T" in holders

    axes = {}

    faculty = data["faculty"]

    if pair_cs:
        axes["CS"] = [{"C": c, "S": s} for c, s in data["class_subject"]]
    elif pair_ts:
        axes["TS"] = [{"T": t, "S": s} for t, s in data["teacher_subject"]]
    else:
        if "C" in holders:
            axes["C"] = [{"C": c} for c in data["classes"]]
        if "S" in holders:
            axes["S"] = [{"S": s} for s in data["subjects"]]
        if "T" in holders:
            axes["T"] = [{"T": t} for t in faculty]

    if "R" in holders:
        axes["R"] = [{"R": r} for r in data["rooms"]]

    days, slots = data["days"], data["slots"]

    if "D" in holders and "D2" in holders:
        axes["DD"] = [
            {"D": a, "D2": b}
            for i, a in enumerate(days) for b in days[i + 1:]
        ]
    elif "D" in holders:
        axes["D"] = [{"D": d} for d in days]

    if "TIME" in holders:
        axes["N"] = [{"N": n, "TIME": t} for n, t in data["slot_times"]]
    elif "N" in holders and "N2" in holders:
        axes["NN"] = [
            {"N": a, "N2": b}
            for i, a in enumerate(slots) for b in slots[i + 1:]
        ]
    elif "N" in holders:
        axes["N"] = [{"N": n} for n in slots]

    # lower-case / abbreviated day helpers
    wants_d = "d" in holders
    wants_d3 = "D3" in holders

    if (wants_d or wants_d3) and "D" not in holders and "DD" not in axes:
        axes["D"] = [{"D": d} for d in days]

    # single-valued, data-derived constants (a day that does not exist, an
    # impossible slot, a teacher / class that is not in the data, ...)
    for name, value in data["consts"].items():
        if name in holders:
            axes["const_" + name] = [{name: value}] if value is not None else []

    names = list(axes)

    for combo in itertools.product(*(axes[n] for n in names)):

        b = {}
        for part in combo:
            b.update(part)

        if "T2" in holders:
            i = faculty.index(b["T"])
            b["T2"] = faculty[(i + 1) % len(faculty)]

        if "D" in b:
            b["d"] = b["D"].lower()
            b["D3"] = b["D"][:3]

        text = template
        for k, v in b.items():
            text = text.replace("{" + k + "}", str(v))

        # the template used lower-case {d} / {D3}: those keys are not in
        # `holders` of the original check, so they are replaced above anyway
        yield text, b


# ----------------------------------------------------------------------
# independent truth, computed from the raw events
# ----------------------------------------------------------------------

class Truth:

    def __init__(self, model):

        self.model = model
        self.events = list(model.events)
        self.faculty = set(model.faculty)

        self.busy = defaultdict(set)
        self.busy_rooms = defaultdict(set)
        self.busy_classes = defaultdict(set)
        self.by_teacher = defaultdict(list)
        self.by_class = defaultdict(list)
        self.by_subject = defaultdict(list)

        for e in self.events:

            cell = (e["day"], e["slot"])

            if e["teacher"] in self.faculty:
                self.busy[cell].add(e["teacher"])
                self.by_teacher[e["teacher"]].append(e)

            if e["room"]:
                self.busy_rooms[cell].add(e["room"])

            if e["class_name"]:
                self.busy_classes[cell].add(e["class_name"])
                self.by_class[e["class_name"]].append(e)

            if e["subject"]:
                self.by_subject[key(e["subject"])].append(e)

        self.rooms = set(model.rooms)
        self.classes = set(model.classes)

    def periods(self, teacher):
        return {(e["day"], e["slot"]) for e in self.by_teacher[teacher]}


def verdict(oracle, b, truth, text):
    """Return (verdict, note). verdict in VERIFIED / MISMATCH / ''."""

    cell = (b.get("D", "").lower(), b.get("N"))

    def cmp(got, want, what="names"):
        got, want = set(got), set(want)
        if got == want:
            return "VERIFIED", ""
        return "MISMATCH", (
            f"{what}: missing {sorted(want - got)[:3]} "
            f"extra {sorted(got - want)[:3]}"
        )

    yes = text.lstrip("*").startswith("Yes")

    if oracle == "free_list":
        return cmp(names_from(text), truth.faculty - truth.busy[cell])

    if oracle == "busy_list":
        return cmp(names_from(text), truth.busy[cell])

    if oracle == "teacher_free":
        want = cell not in truth.periods(b["T"])
        return ("VERIFIED", "") if yes == want else (
            "MISMATCH", f"expected {'Yes' if want else 'No'}")

    if oracle == "teacher_busy":
        want = cell in truth.periods(b["T"])
        return ("VERIFIED", "") if yes == want else (
            "MISMATCH", f"expected {'Yes' if want else 'No'}")

    if oracle == "teacher_count":
        want = len(truth.periods(b["T"]))
        m = re.search(r"has (\d+) period", text)
        got = int(m.group(1)) if m else None
        return ("VERIFIED", "") if got == want else (
            "MISMATCH", f"periods got {got} want {want}")

    if oracle == "teacher_timetable":
        ok = b["T"] in text.splitlines()[0] or (
            not truth.by_teacher[b["T"]] and "no classes" in text.lower())
        return ("VERIFIED", "") if ok else ("MISMATCH", "wrong heading")

    if oracle == "teacher_subjects":
        want = {key(e["subject"]) for e in truth.by_teacher[b["T"]]
                if e["subject"]}
        got = {key(n) for n in names_from(text)}
        return cmp(got, want, "subjects")

    if oracle == "teacher_classes":
        want = {e["class_name"] for e in truth.by_teacher[b["T"]]
                if e["class_name"]}
        return cmp(names_from(text), want, "classes")

    if oracle == "subject_teachers":
        want = {e["teacher"] for e in truth.by_subject[key(b["S"])]
                if e["teacher"] in truth.faculty}
        if not want:
            return "", ""
        return cmp(names_from(text), want)

    if oracle == "subject_free_teachers":
        teachers = {e["teacher"] for e in truth.by_subject[key(b["S"])]
                    if e["teacher"] in truth.faculty}
        if not teachers:
            return "", ""
        want = {x for x in teachers if cell not in truth.periods(x)}
        got = set()
        for line in text.splitlines():
            if line.startswith("|") and not re.match(r"^\|\s*-{3}", line):
                c = [x.strip() for x in line.strip().strip("|").split("|")]
                if len(c) >= 3 and c[0] in teachers and c[2] not in ("—", ""):
                    got.add(c[0])
        return cmp(got, want)

    if oracle == "slot_time":
        label = truth.model.slot_label(b["N"])
        return ("VERIFIED", "") if label and label in text else (
            "MISMATCH", f"expected {label!r}")

    if oracle == "class_teachers":
        want = {e["teacher"] for e in truth.by_class[b["C"]]
                if e["teacher"] in truth.faculty}
        return cmp(names_from(text), want)

    if oracle == "class_timetable":
        ok = b["C"] in text.splitlines()[0]
        return ("VERIFIED", "") if ok else ("MISMATCH", "wrong heading")

    if oracle == "class_free":
        want = b["C"] not in truth.busy_classes[cell]
        return ("VERIFIED", "") if yes == want else (
            "MISMATCH", f"expected {'Yes' if want else 'No'}")

    if oracle == "free_classes":
        return cmp(names_from(text), truth.classes - truth.busy_classes[cell])

    if oracle == "busy_classes":
        return cmp(names_from(text), truth.busy_classes[cell])

    if oracle == "free_rooms":
        return cmp(names_from(text), truth.rooms - truth.busy_rooms[cell])

    if oracle == "room_free":
        want = b["R"] not in truth.busy_rooms[cell]
        return ("VERIFIED", "") if yes == want else (
            "MISMATCH", f"expected {'Yes' if want else 'No'}")

    if oracle == "list_teachers":
        got = set(names_from(text))
        return cmp(got, truth.faculty)

    if oracle == "list_subjects":
        return cmp({key(n) for n in names_from(text)},
                   {key(s) for s in truth.model.subjects}, "subjects")

    if oracle == "list_classes":
        return cmp(names_from(text), truth.classes)

    if oracle == "list_rooms":
        return cmp(names_from(text), truth.rooms)

    return "", ""


# ----------------------------------------------------------------------
# status classification
# ----------------------------------------------------------------------

def classify(text):

    if text is None:
        return "LEGACY"

    low = text.lower()

    if "couldn't map" in low or "couldn't find" in low \
            or "no slot " in low or "not part of this timetable" in low:
        return "UNKNOWN"

    if low.startswith("please tell me") or "which one do you mean" in low \
            or "matches several" in low:
        return "ASK_BACK"

    return "OK"


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------

def load_engine(args):

    from engine.smart_query import SmartQueryEngine
    from engine.timetable_model import TimetableModel

    db = args.db

    if not db:
        from pdf_pipeline import (
            build_database, find_faculty_pdf, parse_faculty_pdf,
        )

        args.pdf = args.pdf or find_faculty_pdf()

        if not args.pdf:
            sys.exit("No timetable PDF found: use --pdf FILE, --db FILE, or "
                     "put a PDF in the data folder.")

        with open(args.pdf, "rb") as fh:
            data = parse_faculty_pdf(fh.read())

        db, stats = build_database(data)
        print(f"Parsed {args.pdf}: {stats}")

    model = TimetableModel.from_sqlite(db)

    return SmartQueryEngine(model), model


def _weekday_names():
    """Generic calendar words from the language config (not timetable data)."""

    import json

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "config", "nlu_lexicon.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("weekdays") or []
    except Exception:
        return []


def build_data(model, truth):

    pairs_cs = sorted({
        (e["class_name"], e["subject"]) for e in truth.events
        if e["class_name"] and e["subject"]
    })

    pairs_ts = sorted({
        (e["teacher"], e["subject"]) for e in truth.events
        if e["teacher"] in truth.faculty and e["subject"]
    })

    existing = {d.lower() for d in model.days}

    weekdays = _weekday_names()

    missing_day = next((d for d in weekdays if d.lower() not in existing), None)

    def absent(prefix, universe):
        """A made-up name that is not part of any value in `universe`."""

        blob = " ".join(str(u).lower() for u in universe)

        for i in range(1, 1000):
            candidate = f"{prefix}{i:03d}qzx"
            if candidate.lower() not in blob:
                return candidate

    from engine.timetable_model import fmt_minutes

    slot_times = []

    for s_ in model.slots:
        info = model.slot_info.get(s_, {})
        if info.get("start") is None:
            continue
        mid = (info["start"] + info["end"]) // 2
        inside = [x for x in model.slots
                  if model.slot_info[x]["start"] is not None
                  and model.slot_info[x]["start"] <= mid
                  < model.slot_info[x]["end"]]
        if inside == [s_]:                       # unambiguous clock time
            slot_times.append((s_, fmt_minutes(mid)))

    consts = {
        "NDAYS": len(model.days),
        "D1": cap(model.days[0]),
        "N1": model.slots[0],
        "NS": max(model.slots) + 50,            # no timetable has that many
        "ND": missing_day,                      # None if every weekday is used
        "NT": absent("Dr. Nobody", model.faculty),
        "NC": absent("9ZZ-", model.classes),
    }

    return {
        "consts": consts,
        "slot_times": slot_times,
        "faculty": list(model.faculty),
        "classes": list(model.classes),
        "rooms": list(model.rooms),
        "subjects": list(model.subjects),
        "days": [cap(d) for d in model.days],
        "slots": list(model.slots),
        "class_subject": pairs_cs,
        "teacher_subject": pairs_ts,
    }


def name_forms(full):
    """Different ways a person might type the same teacher."""

    base = re.sub(r"^(dr|mr|mrs|ms|prof|miss|er)\.?\s*", "", full,
                  flags=re.I).strip()

    forms = {"full": full, "no title": base, "lower case": full.lower()}

    parts = base.split()
    if len(parts) >= 2:
        forms["first+last"] = f"{parts[0]} {parts[-1]}"

    return forms


def main():

    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default=None,
                    help="timetable PDF (default: newest faculty-wise PDF in the data folder)")
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default="question_bank")
    args = ap.parse_args()

    engine, model = load_engine(args)
    truth = Truth(model)
    data = build_data(model, truth)

    os.makedirs(args.out, exist_ok=True)

    rows = []
    seen = set()

    def run(category, question, oracle, bind, pattern=""):

        assert "{" not in question, f"unfilled placeholder in {question!r}"

        if question in seen:
            return

        seen.add(question)

        engine.reset_context()

        try:
            text = engine.answer(question)
            route = engine.last_route or ""
            status = classify(text)

            # None = "not owned by the smart layer". Only questions the
            # absence / exam-duty / lab-shift planners own are LEGACY; the
            # rest are simply not understood.
            if text is None and not engine._is_legacy_owned(
                    engine.parse(question)):
                status = "UNKNOWN"
        except Exception as exc:                      # noqa: BLE001
            text, route, status = f"{type(exc).__name__}: {exc}", "", "ERROR"

        ver, note = "", ""

        if oracle == "legacy":
            status = "LEGACY" if text is None else status

        elif oracle == "refuse":
            ver = "VERIFIED" if status in ("UNKNOWN", "LEGACY") else "MISMATCH"
            note = "" if ver == "VERIFIED" else "should have been refused"

        elif oracle == "ask_back":
            ver = "VERIFIED" if status == "ASK_BACK" else "MISMATCH"

        elif oracle and status == "OK":
            ver, note = verdict(oracle, bind, truth, text or "")

        elif oracle and status != "OK":
            ver, note = "MISMATCH", f"status {status}"

        rows.append({
            "id": len(rows) + 1,
            "category": category,
            "pattern": pattern,
            "question": question,
            "status": status,
            "route": route,
            "verdict": ver,
            "note": note,
            "answer_head": (text or "").replace("\n", " ")[:140],
        })

    # ---- every pattern x every entity ---------------------------------
    for category, template, oracle in PATTERNS:
        if "{TIME}" in template and not data["slot_times"]:
            continue
        if category == "time of day" and "{TIME}" not in template \
                and not data["slot_times"]:
            continue
        for question, bind in expand(template, data):
            run(category, question, oracle, bind, template)

    # ---- name forms --------------------------------------------------------
    for teacher in data["faculty"]:
        for form_name, form in name_forms(teacher).items():
            for tpl in NAME_FORM_TEMPLATES:

                q = tpl.replace("{X}", form).replace(
                    "{D1}", str(data["consts"]["D1"])
                )

                assert "{" not in q, f"unfilled placeholder in {q!r}"
                engine.reset_context()
                text = engine.answer(q) or ""
                status = classify(text)

                ok = key(teacher) in key(text.splitlines()[0] if text else "")
                ambiguous = "matches several" in text.lower()

                rows.append({
                    "id": len(rows) + 1,
                    "category": f"name form: {form_name}",
                    "pattern": tpl,
                    "question": q,
                    "status": "ASK_BACK" if ambiguous else status,
                    "route": engine.last_route or "",
                    "verdict": "VERIFIED" if (ok or ambiguous) else "MISMATCH",
                    "note": "ambiguous name (asked which one)"
                            if ambiguous else "",
                    "answer_head": text.replace("\n", " ")[:140],
                })

    # ---- time-of-day questions (only when the timetable has clock times) ----
    labels = [model.slot_label(s) for s in model.slots]
    has_times = any(labels)

    # ---- write outputs -----------------------------------------------------
    with open(os.path.join(args.out, "all_questions.csv"), "w",
              newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r["question"])

    with open(os.path.join(args.out, "questions_only.txt"), "w",
              encoding="utf-8") as fh:
        for cat, qs in by_cat.items():
            fh.write(f"### {cat}  ({len(qs)})\n")
            fh.write("\n".join(qs) + "\n\n")

    # ---- summary ------------------------------------------------------------
    stats = defaultdict(Counter)
    bad = defaultdict(list)

    for r in rows:
        c = stats[r["category"]]
        c["total"] += 1
        c[r["status"]] += 1
        if r["verdict"]:
            c[r["verdict"]] += 1
        if r["verdict"] == "MISMATCH" or r["status"] in ("ERROR",):
            bad[r["category"]].append(r)

    tot = Counter()
    for c in stats.values():
        tot.update(c)

    lines = [
        "# Question bank — summary", "",
        f"Timetable: {len(data['faculty'])} faculty, {len(data['classes'])} "
        f"classes, {len(data['rooms'])} rooms, {len(data['subjects'])} "
        f"subjects, {len(data['days'])} days × {len(data['slots'])} slots.", "",
        f"**{tot['total']} questions generated.**  "
        f"OK {tot['OK']} · ask-back {tot['ASK_BACK']} · "
        f"unknown/refused {tot['UNKNOWN']} · legacy {tot['LEGACY']} · "
        f"errors {tot['ERROR']}", "",
        f"Checked against raw data: {tot['VERIFIED']} verified, "
        f"{tot['MISMATCH']} mismatches.", "",
        "" if has_times else
        "_The timetable has no clock times, so time-of-day questions "
        "were not generated._", "",
        "| Category | Total | OK | Ask-back | Unknown | Legacy | Error "
        "| Verified | Mismatch |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for cat, c in stats.items():
        lines.append(
            f"| {cat} | {c['total']} | {c['OK']} | {c['ASK_BACK']} | "
            f"{c['UNKNOWN']} | {c['LEGACY']} | {c['ERROR']} | "
            f"{c['VERIFIED']} | {c['MISMATCH']} |"
        )

    if bad:
        lines += ["", "## Mismatches (first 5 per category)", ""]
        for cat, items in bad.items():
            lines.append(f"### {cat} — {len(items)}")
            for r in items[:5]:
                lines.append(f"- `{r['question']}` → {r['note']} "
                             f"| {r['answer_head'][:90]}")
            lines.append("")

    with open(os.path.join(args.out, "summary.md"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    print("\n".join(lines[:12]))
    print(f"\nWrote {len(rows)} questions to {args.out}/")


if __name__ == "__main__":
    main()