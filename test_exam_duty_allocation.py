"""
Exam-duty allocation in plain English, on ANY timetable (nothing is hard-coded).

The date, the time window, the people and the rooms are all derived from the
timetable in data/ (or the PDFs passed as arguments).  Every allocation is
re-checked against the raw timetable events, not against the reply text.

Checks
  * the "find free faculty ... check existing exam duties" wording gets an
    eligibility table (name, department, reason) - not a duty-list lookup
  * N rooms x M invigilators: every room filled, nobody in two rooms,
    nobody teaching in the duty window (+ buffer)
  * "exclude <surname>": that surname is never assigned; an unknown name is reported
  * more seats than eligible faculty -> unfilled rooms are named, no double booking
  * a request that gives no rooms reuses the previous room setup
  * confirm -> saved, not offered again, and next-day plan avoids them
  * "mark X unavailable" -> X is never proposed on that date

Usage:  python test_exam_duty_allocation.py [timetable.pdf ...]
"""
import contextlib, datetime, glob, io, os, re, sys, tempfile

os.environ["FACULTY_STATE_DIR"] = tempfile.mkdtemp(prefix="duty_alloc_")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

files = sys.argv[1:] or sorted(glob.glob("data/*.pdf"))
if not files:
    sys.exit("no timetable PDF: pass one or put one in the data folder")

with contextlib.redirect_stdout(io.StringIO()):
    from import_engine.import_manager import ImportManager
    from data_engine.canonical_event_matcher import CanonicalEventMatcher
    from faculty_chatbot import FacultyAIChatbot
    recs = []
    for f in files:
        r = ImportManager().import_file(f)
        recs += r.get("records", []) if isinstance(r, dict) else r
    matcher = CanonicalEventMatcher(recs)
    matcher.match()
    bot = FacultyAIChatbot(matcher=matcher)

M = bot.smart_engine.model
C = bot.exam_duty_coordinator
fails, checks = [], 0


def check(name, ok, detail=""):
    global checks
    checks += 1
    if not ok:
        fails.append(f"{name} {detail}")


def ask(q):
    return str(bot.process_query(q))


def seat_names(text, row_prefix):
    out = []
    for line in text.splitlines():
        if re.match(rf"\|\s*{row_prefix}", line):
            out += [x.strip() for x in re.findall(r"\|\s*([^|(]+?)\s*\(", line)]
    return out


def teaching(teacher, weekday, lo, hi):
    for e in M.events:
        if e.get("teacher") == teacher and e.get("day") == weekday \
                and e.get("slot") in M.slot_info:
            i = M.slot_info[e["slot"]]
            if i.get("start") is not None and \
                    min(hi, i["end"]) - max(lo, i["start"]) > 0:
                return True
    return False


# ---- pick a real date/window with plenty of free people (from the data) ----
day = None
for offset in range(0, 21):
    d = datetime.date(2026, 11, 2) + datetime.timedelta(days=offset)
    cand = C.candidates(d.isoformat(), "09:00", "12:00")
    if cand.get("success") and cand["count"] >= 14:
        day, free = d, cand
        break

if day is None:
    sys.exit("no weekday with 14+ free faculty in 09:00-12:00 - cannot run")

weekday = free["day"]
pretty = f"{day.day} {day.strftime('%B %Y')}"
nxt = day + datetime.timedelta(days=1)
nxt_pretty = f"{nxt.day} {nxt.strftime('%B %Y')}"
window = "from 9:00 AM to 12:00 PM"

# 1. eligibility wording that contains the phrase "existing exam duties"
a = ask(f"Find all faculty members who are free on {pretty}, {window}, for "
        f"examination duty. Check their teaching timetable and existing exam "
        f"duties. Show their names, departments, and reasons they are eligible.")
check("eligibility table, not a duty lookup",
      "Department" in a and "Why eligible" in a and "No confirmed exam duties" not in a,
      a[:80])

# 2. rooms x invigilators
a = ask(f"Assign examination duties for {pretty}, {window}. We have 3 examination "
        f"rooms: A101, A102 and A103. Assign 2 invigilators to each room. Select "
        f"only available faculty and ensure that no faculty member is assigned to "
        f"more than one room during the same time slot.")
people = seat_names(a, "A10")
check("3 rooms x 2 invigilators", len(people) == 6 and len(set(people)) == 6, str(people))
check("nobody teaching in the window",
      not any(teaching(p, weekday, 8 * 60 + 45, 12 * 60 + 15) for p in people))
check("unfilled-rooms line", "Unfilled rooms" in a)

# 3. exclusion by surname + unknown name; room setup reused
surname = re.findall(r"[A-Za-z]+", people[0])[-1]
a = ask(f"Prepare the exam duty allocation for {pretty}, {window}. Exclude "
        f"Dr. {surname} and Prof. Zzqxv as requested. List any unfilled rooms.")
p2 = seat_names(a, "A10")
check("room setup reused", len(p2) == 6, str(p2))
check("excluded surname never assigned",
      not any(surname.lower() in p.lower().split() or surname.lower() in p.lower()
              for p in p2), str(p2))
check("unknown name reported", "Zzqxv" in a and "no faculty" in a)

# 4. more seats than eligible faculty
a = ask(f"Assign 2 invigilators to each of rooms R1-R40 for exam duty on {pretty} "
        f"{window}")
pc = seat_names(a, "R\\d")
check("shortage reported", re.search(r"Unfilled rooms:\*\* .*R\d", a) is not None)
check("no double booking under shortage", len(pc) == len(set(pc)), str(len(pc)))
check("never more seats than eligible", len(pc) <= free["count"])
ask("cancel")

# 5. confirm -> saved -> not offered again, next day avoids them
ask(f"Assign 2 invigilators to each room B1 and B2 for exam duty on {pretty} {window}")
conf = ask("confirm")
check("confirm saves", "Confirmed 4 exam duties" in conf, conf[:60])
saved = re.findall(r"\d+\. (.*?) —", ask("show exam duties"))
check("saved duties listed", len(saved) == 4, str(saved))
e = ask(f"Which faculty are free on {pretty} {window} for examination duty? "
        f"Check their timetable and existing exam duties, with departments.")
check("duty holders not offered again", not any(s in e for s in saved))
f = ask(f"Allocate exam duties on {nxt_pretty} {window}, 2 rooms (C1, C2), "
        f"2 invigilators per room. Prioritize faculty with fewer duties and avoid "
        f"consecutive duties.")
p5 = seat_names(f, "C\\d")
check("next day avoids yesterday's holders", not (set(p5) & set(saved)), str(set(p5) & set(saved)))
check("totals table", "| Faculty | Before | New | Total |" in f)
ask("cancel")

# 6. marked unavailable
victim = next((c["teacher"] for c in C.candidates(nxt.isoformat(), "09:00", "12:00")["results"]), None)
if victim:
    ask(f"Mark {victim} unavailable for exam duty on {nxt_pretty}")
    after = C.candidates(nxt.isoformat(), "09:00", "12:00")
    check("marked unavailable never proposed",
          victim not in [c["teacher"] for c in after["results"]], victim)

print(f"{checks - len(fails)}/{checks} exam-duty allocation checks passed")
for item in fails:
    print("  FAIL", item)
sys.exit(1 if fails else 0)