"""
Semester exams: "5th sem has exams on 2 November 9 to 10:30 and 1 to 2:30 - allot
faculty with fewer than 50 duties who teach 5th sem".

While a semester sits its exam its normal classes do not run, so its teachers and
classrooms are free.  Checked against the RAW timetable events, for any semester /
date / time (nothing below is fixed to one):

  * the semester's classes are found from the class names ("5CSA" -> 5);
  * only teachers whose every class in the window is a suspended class of that
    semester are "freed" - nobody with another class there is proposed;
  * "belonging to the 5th sem / teaching only" restricts to that semester's teachers;
  * the exam rooms are the rooms the suspended classes would have used;
  * the duty-count rule, "nobody in both sessions" and confirming all still work;
  * an unknown semester is reported, and an explicit class name ("5CSA") works.

Usage:  python test_semester_exam.py [timetable.pdf]
"""
import contextlib, datetime, io, os, re, sys, tempfile, random

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp(prefix="sem_state_")
os.environ["FACULTY_STATE_DIR"] = STATE

from scheduling import duty_history as dh, semester_exam as se

fails, checks = [], 0
def check(name, ok, detail=""):
    global checks
    checks += 1
    if not ok:
        fails.append(f"{name} {detail}")

# ---- class name -> semester, whatever the naming ----
for name, want in [("5CSA", 5), ("3CS-AI-B", 3), ("3-CS-IOT", 3), ("7CS-DS-A", 7), ("Sem-5 CSE", 5), ("CSE-6A", 6), ("MBA-I", None)]:
    check(f"class_semester {name}", se.class_semester(name) == want, str(se.class_semester(name)))
CL = ["5CSA", "5CS-AI-A", "3CSA"]
for text, sem, only in [
    ("5th sem is having exams", 5, False), ("fifth semester exam", 5, False), ("sem V exam", 5, False),
    ("semester 3 exam", 3, False), ("exam for 3 rd sem", 3, False),
    ("faculty should belong to the 5th sem teaching only", 5, True),
    ("assign teachers for 5th sem exam", 5, False), ("teachers of 3rd semester only", 3, True),
]:
    spec = se.find_scope(text, CL)
    check(f"find_scope '{text}'", (spec.semester if spec else None) == sem and (spec.only_teachers if spec else False) == only,
          str(spec))
check("explicit class name", (se.find_scope("exam of 5CSA tomorrow", CL) or se.ScopeSpec()).class_names == ["5CSA"])

# ---- the loaded timetable ----
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
rnd = random.Random(11)
sheet = {t: rnd.choice([0, 8, 20, 35, 49, 50, 62]) for t in faculty}
bot.exam_duty_store.baseline.save(sheet, source="sheet.pdf", mode="x")

H = bot.exam_duty_requests
ask = lambda q: str(bot.process_query(q))

sems = sorted({se.class_semester(c) for c in M.classes} - {None})
check("timetable has semesters", len(sems) >= 2, str(sems))
SEM = sems[1] if len(sems) > 1 else sems[0]
ORD = {1: "1st", 2: "2nd", 3: "3rd"}.get(SEM, f"{SEM}th")

# next date that falls on a weekday present in the timetable
d = datetime.date(2026, 11, 2)
while d.strftime("%A").lower() not in M.days:
    d += datetime.timedelta(days=1)
DAY = d.strftime("%A").lower()
DATE = d.strftime("%d %B %Y").lstrip("0")

slot = [(v["start"], v["end"]) for k, v in sorted(M.slot_info.items()) if v.get("start") is not None]
A0, A1 = slot[1][0], slot[1][0] + 90                  # window 1
B0, B1 = slot[-2][0], slot[-2][0] + 90                # window 2
hh = lambda t: f"{t // 60:02d}:{t % 60:02d}"
WIN = f"time {hh(A0)} to {hh(A1)} and from {hh(B0)} to {hh(B1)}"

sem_classes = {c for c in M.classes if se.class_semester(c) == SEM}
sem_teachers = {e["teacher"] for e in M.events if e["class_name"] in sem_classes}

def events_in(teacher, t0, t1):
    out = []
    for e in M.events:
        if e["teacher"] == teacher and e["day"] == DAY and e["slot"] in M.slot_info:
            i = M.slot_info[e["slot"]]
            if i.get("start") is not None and min(t1, i["end"]) - max(t0, i["start"]) > 0:
                out.append(e)
    return out

def names_in(text):
    return [n for n in re.findall(r"\| ([^|(]+?) \(", text) if n.strip() in sheet] and \
           [n.strip() for n in re.findall(r"\| ([^|(]+?) \(", text) if n.strip() in sheet]

# ---- the user's sentence, in one go ----
Q = (f"{ORD} sem is having exams on {DATE} {WIN} so please allot faculty for this exam and faculty "
     f"must have duty count less then 50 and sholud belong to the {ORD} sem teaching only")
r = ask(Q)
check("scope recognised", f"Exam for semester {SEM}" in r and "suspended" in r, r[:200])
check("asks per room, shows freed rooms", "How many invigilators per room" in r and "room(s) free" in r, r[-300:])
r = ask("2 per room")
check("allocation built for both windows", r.count("| Room |") == 2 and "Proposed" in r, r[:200])

blocks = [b.split("**Exam duties per assigned faculty")[0] for b in re.split(r"\n\*\*\w+day, ", r)[1:]]
for (w0, w1), block in zip([(A0, A1), (B0, B1)], blocks):
    who = [n.strip() for n in re.findall(r"\| ([^|(]+?) \(", block)]
    who = [n for n in who if n in sheet]
    check(f"[{hh(w0)}] seats filled", len(who) >= 2, str(len(who)))
    check(f"[{hh(w0)}] only teachers of semester {SEM}", all(n in sem_teachers for n in who), str([n for n in who if n not in sem_teachers]))
    check(f"[{hh(w0)}] fewer than 50 duties", all(sheet[n] < 50 for n in who), str([n for n in who if sheet[n] >= 50]))
    bad = [n for n in who if any(e["class_name"] not in sem_classes for e in events_in(n, w0 - 15, w1 + 15))]
    check(f"[{hh(w0)}] nobody has another class in the window", not bad, str(bad))
    rooms = set(re.findall(r"^\| ([^|]+?) \|", block, re.M)) - {"Room", "---"}
    exam_rooms = set(se.exam_rooms(M, se.resolve(se.ScopeSpec(semester=SEM), M), DAY, w0, w1))
    check(f"[{hh(w0)}] exam rooms are the freed classrooms", rooms and rooms <= exam_rooms, str(rooms - exam_rooms))
r = ask("confirm")
check("semester plan confirms", "Confirmed" in r, r[:150])

# ---- the sentence as a coordinator really writes it: every freed room, 2 teachers in 1 room ----
Q2 = (f"here is exam of {ORD} semester on {DATE} {WIN} assign exam duty and also find classrooms available "
      f"for {ORD} sem assign teachers whose assigned duties are less then 50 and no teacher must have duties "
      f"in both the slot find number of rooms available for exam for {ORD} sem and allot the teachers in "
      f"then two teacher in 1 room")
r = ask(Q2)
check("one-shot request builds both sessions", r.count("| Normally used by |") == 2, r[:200])
blocks2 = [b.split("**Exam duties per assigned faculty")[0] for b in re.split(r"\n\*\*\w+day, ", r)[1:]]
sc = se.resolve(se.ScopeSpec(semester=SEM), M)
seen = []
for (w0, w1), block in zip([(A0, A1), (B0, B1)], blocks2):
    free_rooms = se.exam_rooms(M, sc, DAY, w0, w1)
    listed = [m.group(1).strip() for m in re.finditer(r"^\| ([^|]+?) \|", block, re.M) if m.group(1).strip() not in ("Room", "---")]
    check(f"[{hh(w0)}] EVERY freed classroom is listed ({len(free_rooms)})", set(listed) == set(free_rooms) and len(listed) == len(free_rooms), f"{len(listed)} vs {len(free_rooms)}")
    used = se.room_classes(M, sc, DAY, w0, w1)
    check(f"[{hh(w0)}] 'normally used by' is a class of the semester", all(set(used[r_]) <= sem_classes for r_ in listed if r_ in used))
    row_sizes = [len(re.findall(r"\(\w[^)]*\)", row)) for row in block.splitlines() if row.startswith("| ") and not row.startswith(("| Room", "| ---"))]
    check(f"[{hh(w0)}] two invigilators per room", max(row_sizes or [0]) == 2, str(row_sizes[:5]))
    who = [n.strip() for n in re.findall(r"\| ([^|(]+?) \(", block) if n.strip() in sheet]
    seen.append(set(who))
    check(f"[{hh(w0)}] fewer than 50 duties", all(sheet[n] < 50 for n in who))
check("nobody in both sessions", not (seen[0] & seen[1]), str(seen[0] & seen[1]))
if "places could be filled" in r:
    check("shortage is explained, with a way out", "duty-count rule" in r and "To fill every room" in r, r[-500:])
ask("cancel")

# ---- the same request without the 'teaching only' restriction: freed + other free teachers ----
r = ask(f"{ORD} sem exam on {DATE} time {hh(A0)} to {hh(A1)}, 6 faculty for 3 rooms, duties less than 50")
who = names_in(r)
check("unrestricted request builds", "Proposed" in r and len(who) == 6, r[:200])
check("rooms named from freed classrooms", not re.search(r"\| Room \d", r), r[:600])
ask("cancel")

# ---- who is free (no assignment), and an explicit class name ----
r = ask(f"which teachers are free for the {ORD} semester exam on {DATE} {hh(A0)} to {hh(A1)} who teach {ORD} sem only")
check("free list is restricted to the semester", r.count("| 1 |") >= 1 and "Only teachers who teach" in r, r[:200])
if sem_classes:
    one = sorted(sem_classes)[0]
    r = ask(f"exam of {one} on {DATE} {hh(A0)} to {hh(A1)} which faculty are free")
    check("explicit class: scope is that class", f"Exam for {one}" in r, r[:200])

# ---- an unknown semester is reported, not guessed ----
r = ask(f"9th sem exam on {DATE} {hh(A0)} to {hh(A1)} 4 faculty")
check("unknown semester reported", "couldn't find any classes for semester 9" in r, r[:160])

print(f"{checks - len(fails)}/{checks} semester-exam checks passed")
for f in fails[:15]:
    print("  FAIL", f)
sys.exit(1 if fails else 0)