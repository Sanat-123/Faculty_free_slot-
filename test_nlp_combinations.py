"""
Hundreds of DIFFERENT sentences, built by combining many ways of saying each part of an
exam-duty request, must all be read the same way.

Parts (each with several wordings, any combination is possible):
  the exam      "there is exam of 5th sem on 5th november", "fifth semester is having exams ..."
  the action    "assign exam duty", "allot invigilation duties", ...
  the rule      "whose total duty count is less then 100", "100 or fewer", ...
  the students  "there are 500 students and in each class there will be 50 students", ...
  the timings   "from timing 8 to 9:30 and 1 to 2:30", "8am-9:30am and 1pm-2:30pm", ...
  the staffing  "two teachers in each room", "each room needs 2 invigilators", ...
  extras        reserves / "no teacher in both slots"

The numbers, dates and times are drawn at random, so nothing is tuned to one example.

Usage:  python test_nlp_combinations.py [how-many]
"""
import datetime, itertools, os, random, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scheduling import exam_duty_requests as er, semester_exam as se

N = int(sys.argv[1]) if len(sys.argv) > 1 else 600
rnd = random.Random(int(os.environ.get("NLP_SEED", "2026")))
TODAY = datetime.date(2026, 10, 28)
CLASSES = ["3CSA", "5CSA", "5CSB", "5CS-AI-A", "7CSA", "7CSB"]

SEMS = [(5, ["5th sem", "5th semester", "fifth semester", "semester 5", "sem 5", "sem V", "5 sem", "5th sem students"]),
        (3, ["3rd sem", "3rd semester", "third semester", "semester 3", "sem III"]),
        (7, ["7th sem", "7th semester", "seventh semester", "semester 7"])]

def date_forms(day, month_name, mon3):
    return [f"{day}th {month_name}", f"{day} {mon3}", f"{month_name} {day}", f"{day}/{11:02d}/2026",
            f"{day:02d}-11-2026", f"{day}th of {month_name}", f"{day} {month_name}"]

INTRO = ["there is exam of {sem} on {date}", "{sem} is having exams on {date}", "exam for {sem} on {date}",
         "{sem} exam {date}", "we have {sem} examination on {date}", "here is exam of {sem} on {date}",
         "i have to conduct the {sem} exam on {date}", "exams of {sem} will be held on {date}"]
ACTION = ["assign exam duty", "allot exam duties", "please assign duties", "allocate invigilation duty",
          "arrange exam duty", "so assign exam duty", "kindly allot invigilation duties"]
RULE = [("to the teachers whose total duty count is less then {n}", "<"),
        ("to teachers having duties below {n}", "<"),
        ("to faculty with fewer than {n} duties", "<"),
        ("only to those whose duty count is not more than {n}", "<="),
        ("to teachers who have {n} or fewer duties", "<="),
        ("to teachers with assigned duties under {n}", "<"),
        ("whose total duties are at most {n}", "<="),
        ("to teachers having less than {n} exam duties", "<")]
STUD = ["there are {s} students and in each class there will be {c} students",
        "{s} students and each room can seat {c}",
        "total students are {s} and every classroom will have {c} students",
        "with {s} candidates, one room holds {c}",
        "{s} students appearing and class size is {c}",
        "{s} students, {c} per room",
        "each room will have {c} students and total strength is {s}",
        "in a class only {c} students can sit and there are {s} students",
        "i have {s} students and a room can take only {c}",
        "{s} students, room capacity {c}"]
TIMING = [("from timing {a} to {b} and {c} to {d}", None),
          ("at {a}:00 to {b} and {c}:00 pm to {d} pm", None),
          ("morning {a} to {b} afternoon {c} to {d}", None),
          ("between {a} and {b} and again between {c} and {d}", None),
          ("{a}-{b} & {c}-{d}", None),
          ("time {a} to {b} and from {c} to {d}", None)]
STAFF = [("two teachers in each room", 2), ("2 invigilators per room", 2), ("each room needs 3 invigilators", 3),
         ("allot the teachers in then two teacher in 1 room", 2), ("2 faculty in a room", 2),
         ("one invigilator in every room", 1), ("4 teachers per room", 4),
         ("2 faculties must be assigned in each class", 2), ("assign 2 faculties to each class", 2),
         ("each class must have 3 faculties", 3), ("2 invigilators must be appointed for each room", 2),
         ("allot two teachers for every classroom", 2), ("keep 3 teachers in each hall", 3),
         ("2 teachers should be there in every room", 2), ("two faculty are needed per class", 2)]
RESERVE = [("", None), ("keep {r} reserve teachers", "r"), ("suppose {r} teachers are absent so replacement must also be there", "r"),
           ("with {r} replacements", "r"), ("and keep {r} standby invigilators", "r")]
DISTINCT = [("", False), ("no teacher must have duties in both the slots", True), ("nobody should be repeated in both slots", True),
            ("same teacher should not be in both the slots", True), ("different teachers in each slot", True)]

def hhmm(h, m=0):
    return f"{h:02d}:{m:02d}"

fails = []
total = 0

for i in range(N):

    sem, sem_forms = rnd.choice(SEMS)
    day = rnd.randint(2, 28)
    sem_text = rnd.choice(sem_forms)
    date_text = rnd.choice(date_forms(day, "november", "nov"))
    n = rnd.choice([40, 50, 60, 80, 100, 120])
    s = rnd.choice([200, 300, 450, 500, 640, 1000, 1200])
    c = rnd.choice([20, 25, 30, 40, 45, 50, 60])
    a, b = rnd.choice([(8, 9), (9, 10), (10, 11)])
    cc = rnd.choice([1, 2, 3])
    dm = rnd.choice([(":30"), ""])
    tpl, _ = rnd.choice(TIMING)
    pm_form = "pm" in tpl
    timing_text = tpl.format(a=a, b=f"{b}{dm}" if dm else b, c=cc, d=f"{cc + 1}{dm}" if dm else cc + 1)
    rule_tpl, op = rnd.choice(RULE)
    stud_tpl = rnd.choice(STUD)
    staff_tpl, per = rnd.choice(STAFF)
    res_tpl, res_kind = rnd.choice(RESERVE)
    r = rnd.choice([1, 2, 3, 4])
    dis_tpl, dis = rnd.choice(DISTINCT)

    parts = [rnd.choice(INTRO).format(sem=sem_text, date=date_text), rnd.choice(ACTION), rule_tpl.format(n=n),
             "and " + stud_tpl.format(s=s, c=c), timing_text, staff_tpl, res_tpl.format(r=r), dis_tpl]
    rnd.shuffle(parts[2:])                                   # the order of the clauses varies too
    text = ", ".join(p for p in parts if p) if rnd.random() < 0.5 else " ".join(p for p in parts if p)

    want = {
        "dates": [datetime.date(2026, 11, day)],
        "times": [(hhmm(a), hhmm(b, 30 if dm else 0)), (hhmm(cc + 12), hhmm(cc + 13, 30 if dm else 0))],
        "limit": (op, float(n)),
        "students": s, "capacity": c, "rooms_needed": -(-s // c),
        "per_room": per,
        "reserves": r if res_kind else None,
        "distinct_per_day": dis,
    }

    req = er.understand(text, TODAY)
    spec = se.find_scope(er.clean_text(text), CLASSES)

    got = {k: getattr(req, k) for k in want}
    got["semester"] = spec.semester if spec else None
    want["semester"] = sem

    for k, v in want.items():
        total += 1
        if got[k] != v:
            fails.append((k, v, got[k], text))

by_field = {}
for k, v, g, t in fails:
    by_field.setdefault(k, []).append((v, g, t))

print(f"{total - len(fails)}/{total} field checks passed over {N} sentences")
for k, items in by_field.items():
    print(f"\n[{k}] {len(items)} wrong, e.g.")
    for v, g, t in items[:3]:
        print(f"   want {v}  got {g}\n   > {t[:230]}")

sys.exit(1 if fails else 0)