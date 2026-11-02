"""
A non-working day / college-wide exam / a whole year's exam.

"Tomorrow is a non-working day for the whole college, no classes, assign exam duties to the
teachers with classrooms": every class is suspended, so every teacher and every classroom is
free.  "Final year exam" / "2nd year exam" suspend only that year's classes.  Checked against
the RAW timetable, for any date and time - nothing is tied to one example.

Usage:  python test_college_wide.py [timetable.pdf]
"""
import contextlib, datetime, io, os, re, sys, tempfile, random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp(prefix="college_state_")
os.environ["FACULTY_STATE_DIR"] = STATE

from scheduling import semester_exam as se

fails, checks = [], 0
def check(name, ok, detail=""):
    global checks
    checks += 1
    if not ok:
        fails.append(f"{name} {detail}")

# ---------------- wording ----------------
CL = ["3CSA", "5CSA", "7CSA", "7CSB"]
for text, everything in [
    ("tomorrow is a non working day for the whole college so assign exam duties", True),
    ("declare 5 nov as holiday and assign duties to teachers", True),
    ("no classes are there so all the teachers are free, assign exam duty", True),
    ("entire college has exam on 5 november", True),
    ("college is closed on monday, allot invigilation", True),
    ("campus-wide exam on 5 nov", True),
    ("classes are suspended on 5 nov, assign duty", True),
    ("all the teachers are free on that day, assign duties", True),
    ("5th sem exam, 2 faculties in every class", False),
    ("exam of 5th sem and no classes for it", False),
    ("who is free on monday 9 to 10", False),
    ("assign 2 teachers in each class for 3rd sem exam", False),
]:
    sp = se.find_scope(text, CL)
    check(f"college-wide? '{text[:55]}'", bool(sp and sp.everything) == everything, str(sp))
for text, years in [("final year exam on 5 nov", ["final"]), ("2nd year exam", [2]), ("third year exam", [3]), ("year 1 exam", [1]), ("exam for the year 2026", [])]:
    sp = se.find_scope(text, CL)
    check(f"year '{text}'", (sp.years if sp else []) == years, str(sp))

# ---------------- the loaded timetable ----------------
import pdf_pipeline as pp
pdf = sys.argv[1] if len(sys.argv) > 1 else pp.find_faculty_pdf()
if not pdf:
    sys.exit("no timetable PDF: pass one or put one in the data folder")

with contextlib.redirect_stdout(io.StringIO()):
    from import_engine.import_manager import ImportManager
    from data_engine.canonical_event_matcher import CanonicalEventMatcher
    from faculty_chatbot import FacultyAIChatbot
    res = ImportManager().import_file(pdf)
    matcher = CanonicalEventMatcher(res.get("records", []) if isinstance(res, dict) else res)
    matcher.match()
    bot = FacultyAIChatbot(matcher=matcher)

M = bot.smart_engine.model
faculty = sorted(M.faculty_set)
rnd = random.Random(3)
sheet = {t: rnd.choice([0, 6, 15, 33, 49, 79, 90]) for t in faculty}
bot.exam_duty_store.baseline.save(sheet, source="sheet.pdf", mode="x")
ask = lambda q: str(bot.process_query(q))

d = datetime.date(2026, 11, 2)
while d.strftime("%A").lower() not in M.days:
    d += datetime.timedelta(days=1)
DAY, DATE = d.strftime("%A").lower(), d.strftime("%d %B %Y").lstrip("0")
slot = [(v["start"], v["end"]) for k, v in sorted(M.slot_info.items()) if v.get("start") is not None]
A0, A1 = slot[1][0], slot[1][0] + 90
hh = lambda t: f"{t // 60:02d}:{t % 60:02d}"

def events_in(t0, t1, teacher=None):
    out = []
    for e in M.events:
        if e["day"] == DAY and e["slot"] in M.slot_info and (teacher is None or e["teacher"] == teacher):
            i = M.slot_info[e["slot"]]
            if i.get("start") is not None and min(t1, i["end"]) - max(t0, i["start"]) > 0:
                out.append(e)
    return out

def rows_of(reply):
    body = reply.split("**Exam duties per assigned faculty")[0]
    out = []
    for line in body.split("**Reserve / replacement")[0].splitlines():
        if line.startswith("| ") and not line.startswith(("| Room", "| ---", "| #")):
            out.append([c.strip() for c in line.strip("|").split("|")])
    return out

def names_of(rows):
    return [re.sub(r"\s*\(.*\)$", "", x) for c in rows for x in c[2:] if "not filled" not in x]

# ---- whole college ----
Q = (f"{DATE} is declared a non working day for the whole college as exams are going on so no classes are "
     f"there, assign exam duties to all the teachers with duty count less then 80 along with classrooms, "
     f"timing {hh(A0)} to {hh(A1)}, 2 teachers in each class")
r = ask(Q)
check("whole-college plan built", "Proposed" in r and "whole college" in r, r[:240])
rows = rows_of(r)
rooms = [c[0] for c in rows]
classroom_rooms = {k for k, v in se.classroom_kinds(M).items() if v == "classroom"}
check("EVERY classroom is offered", classroom_rooms <= set(rooms), str(sorted(classroom_rooms - set(rooms))[:6]))
check("no room listed twice", len(rooms) == len(set(rooms)))
who = names_of(rows)
check("nobody is on duty twice", len(who) == len(set(who)), "")
check("everyone has fewer than 80 duties", all(sheet.get(n, 0) < 80 for n in who), str([n for n in who if sheet.get(n, 0) >= 80][:3]))
busy_normally = {e["teacher"] for e in events_in(A0 - 15, A1 + 15)}
check("teachers who would have been teaching are used (their classes are suspended)", bool(set(who) & busy_normally), "")
n_suspended = len({e["class_name"] for e in events_in(A0, A1)})
check("suspended class count matches the timetable", f"**{n_suspended}** class(es) suspended" in r, str(n_suspended))
every_teacher_free = len([t for t in faculty if sheet.get(t, 0) < 80])
m = re.search(r"\*\*(\d+)\*\* teacher\(s\) freed", r)
in_window = {id(e) for e in events_in(A0, A1)}
expected_freed = {e["teacher"] for e in events_in(A0, A1)} - {
    e["teacher"] for e in events_in(A0 - 15, A1 + 15) if id(e) not in in_window
}
check("freed teachers = those whose only classes were in the window", m and int(m.group(1)) == len(expected_freed), f"{m and m.group(1)} vs {len(expected_freed)}")
ask("cancel")

# ---- whole college with students: rooms from the count ----
r = ask(f"{DATE} is a holiday, exam for the whole college {hh(A0)} to {hh(A1)}, 500 students and 25 in each class, 2 teachers per class, duties under 80")
check("500 / 25 = 20 rooms on a college-wide day", "**20 rooms**" in r and len(rows_of(r)) == 20, str(len(rows_of(r))))
ask("cancel")

# ---- a year ----
sems = sorted({se.class_semester(c) for c in M.classes} - {None})
final_year = se.year_of(sems[-1])
final_sems = [x for x in sems if se.year_of(x) == final_year]
r = ask(f"final year exam on {DATE} from {hh(A0)} to {hh(A1)}, 2 invigilators per room, duties less than 80")
check("final year scope named", "final year" in r and "Exam for" in r, r[:240])
classes_final = {c for c in M.classes if se.class_semester(c) in final_sems}
m = re.search(r"\*\*(\d+)\*\* class\(es\) suspended", r)
check("only the final year's classes are suspended", m and int(m.group(1)) == len({e["class_name"] for e in events_in(A0, A1) if e["class_name"] in classes_final}), str(m and m.group(1)))
rows = rows_of(r)
bad = [n for n in names_of(rows) if any(e["class_name"] not in classes_final for e in events_in(A0 - 15, A1 + 15, n))]
check("nobody with a class of another year in the window", not bad, str(bad[:3]))
ask("cancel")

# ---- an ordinary semester exam still behaves ----
r = ask(f"exam of {se.class_semester(sorted(M.classes)[0]) or sems[0]}th sem on {DATE} from {hh(A0)} to {hh(A1)}, 2 invigilators in every class")
check("a semester request is not turned into college-wide", "whole college" not in r, r[:160])
ask("cancel")

print(f"{checks - len(fails)}/{checks} college-wide checks passed")
for f in fails[:15]:
    print("  FAIL", f)
sys.exit(1 if fails else 0)