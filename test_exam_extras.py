"""
Student numbers + room size, extra free rooms, stand-by teachers and replacement.

  "500 students, only 30 can sit in a room"  -> 17 rooms: the semester's own freed
        rooms first, then OTHER rooms that nobody uses in that window (checked
        against the raw timetable), never a room already booked for another exam;
  "2 teachers may be absent, keep replacements" -> stand-by teachers per session,
        eligible by the same rules, not counted as duties until used;
  "Dr. X is absent"  -> X's duty goes to the next stand-by teacher, before or after
        the plan is confirmed.

Nothing is fixed to one semester, date, time, number of students or room size.

Usage:  python test_exam_extras.py [timetable.pdf]
"""
import contextlib, datetime, io, os, re, sys, tempfile, random, math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp(prefix="extras_state_")
os.environ["FACULTY_STATE_DIR"] = STATE

from scheduling import exam_duty_requests as er, semester_exam as se

fails, checks = [], 0
def check(name, ok, detail=""):
    global checks
    checks += 1
    if not ok:
        fails.append(f"{name} {detail}")

# ---------------- wording ----------------
T = datetime.date(2026, 10, 28)
P = [
 ("exam 5 nov 9 to 12, 500 students and in a class only 30 students can sit, two teachers in 1 room",
  dict(students=500, capacity=30, rooms_needed=17, per_room=2)),
 ("exam 5 nov 9 to 12, 120 students, 30 per room, 2 invigilators per room", dict(students=120, capacity=30, rooms_needed=4, per_room=2)),
 ("exam 5 nov 9 to 12, 1,200 students, each room seats 40, two teachers in each room", dict(students=1200, capacity=40, rooms_needed=30)),
 ("exam 5 nov 9 to 12, 90 students, room capacity 30, 2 per room", dict(students=90, capacity=30, rooms_needed=3)),
 ("exam 5 nov 9 to 12, 200 students, each room fits 25, 2 faculty in each room", dict(students=200, capacity=25, rooms_needed=8)),
 ("exam 5 nov 9 to 12, 2 invigilators per room", dict(students=None, capacity=None)),
 ("exam 5 nov 9 to 12 and suppose 2 teachers are also absent so replacement must be there", dict(reserves=2)),
 ("exam 5 nov 9 to 12, keep 3 reserve teachers", dict(reserves=3)),
 ("exam 5 nov 9 to 12, 10% extra as backup", dict(reserve_pct=10)),
 ("exam 5 nov 9 to 12, replacement also needed", dict(reserve_flag=True, reserves=None)),
 ("exam 5 nov 9 to 12, 6 rooms 2 per room", dict(reserves=None, reserve_flag=False, students=None)),
]
for text, exp in P:
    r = er.understand(text, T)
    got = {k: getattr(r, k) for k in exp}
    check(f"wording: {text[:60]}", got == exp, str(got))

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
rnd = random.Random(5)
sheet = {t: rnd.choice([0, 5, 12, 29, 49, 79]) for t in faculty}
bot.exam_duty_store.baseline.save(sheet, source="sheet.pdf", mode="x")
ask = lambda q: str(bot.process_query(q))

sems = sorted({se.class_semester(c) for c in M.classes} - {None})
SEM = sems[1] if len(sems) > 1 else sems[0]
ORD = {1: "1st", 2: "2nd", 3: "3rd"}.get(SEM, f"{SEM}th")
sem_classes = {c for c in M.classes if se.class_semester(c) == SEM}

d = datetime.date(2026, 11, 2)
while d.strftime("%A").lower() not in M.days:
    d += datetime.timedelta(days=1)
DAY, DATE = d.strftime("%A").lower(), d.strftime("%d %B %Y").lstrip("0")
slot = [(v["start"], v["end"]) for k, v in sorted(M.slot_info.items()) if v.get("start") is not None]
A0, A1 = slot[1][0], slot[1][0] + 90
B0, B1 = slot[-2][0], slot[-2][0] + 90
hh = lambda t: f"{t // 60:02d}:{t % 60:02d}"
WIN = f"from {hh(A0)} to {hh(A1)} and {hh(B0)} to {hh(B1)}"

def busy_rooms(t0, t1, ignore=()):
    out = set()
    for e in M.events:
        if e["day"] == DAY and e["slot"] in M.slot_info and e["class_name"] not in ignore and e.get("room"):
            i = M.slot_info[e["slot"]]
            if i.get("start") is not None and min(t1, i["end"]) - max(t0, i["start"]) > 0:
                out.add(e["room"].strip())
    return out

def split_sessions(r):
    return [b.split("**Exam duties per assigned faculty")[0] for b in re.split(r"\n\*\*\w+day, ", r)[1:]]

def table_rows(block):
    rows = []
    for line in block.split("**Reserve / replacement")[0].splitlines():
        if line.startswith("| ") and not line.startswith(("| Room", "| ---", "| #")):
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows.append(cells)
    return rows

STUD, CAP = 400, 30
NEED = math.ceil(STUD / CAP)
Q = (f"exam of {ORD} semester on {DATE} {WIN}, assign exam duty to teachers with duties less then 80, nobody in both "
     f"slots, i have {STUD} students and in a class only {CAP} students can sit so i want more free rooms in "
     f"those slots, two teachers in 1 room, and suppose 2 teachers are absent so replacement must also be there")
r = ask(Q)
check("plan built", "Proposed" in r and f"**{NEED} rooms**" in r, r[:300])
blocks = split_sessions(r)
check("two sessions", len(blocks) == 2, str(len(blocks)))

assigned = []
for (w0, w1), block in zip([(A0, A1), (B0, B1)], blocks):
    rows = table_rows(block)
    rooms_rows = [c for c in rows if len(c) >= 4]
    rooms = [c[0] for c in rooms_rows]
    sc = se.resolve(se.ScopeSpec(semester=SEM), M)
    freed = se.exam_rooms(M, sc, DAY, w0, w1)
    expected_total = min(NEED, len(freed) + len(se.vacant_rooms(M, DAY, w0, w1, skip=freed, ignore_classes=sem_classes)))
    check(f"[{hh(w0)}] {expected_total} rooms (needed {NEED})", len(rooms) == expected_total, f"{len(rooms)}")
    check(f"[{hh(w0)}] semester's own freed rooms come first", rooms[:min(len(freed), len(rooms))] == freed[:len(rooms)], str(rooms[:6]))
    extra = [c[0] for c in rooms_rows if c[1].startswith("free ")]
    others_busy = busy_rooms(w0, w1, ignore=sem_classes)
    check(f"[{hh(w0)}] extra rooms really have no class then", not (set(extra) & others_busy), str(set(extra) & others_busy))
    check(f"[{hh(w0)}] no room listed twice", len(rooms) == len(set(rooms)))
    people = [re.sub(r"\s*\(.*\)$", "", x) for c in rooms_rows for x in c[2:] if "not filled" not in x]
    assigned.append(set(people))
    check(f"[{hh(w0)}] fewer than 80 duties", all(sheet.get(p, 0) < 80 for p in people), "")
    sb = block.split("**Reserve / replacement teachers")[-1] if "Reserve / replacement" in block else ""
    reserves = [re.sub(r"\s*\(.*\)$", "", c[1]) for c in [[x.strip() for x in l.strip("|").split("|")] for l in sb.splitlines() if l.startswith("| ")] if len(c) == 4 and c[0].isdigit()]
    check(f"[{hh(w0)}] at most 2 reserves", len(reserves) <= 2, str(reserves))
    check(f"[{hh(w0)}] reserves are not also assigned", not (set(reserves) & set(people)), str(set(reserves) & set(people)))
    assigned[-1] |= set()
    globals().setdefault("RES", []).append(set(reserves))
check("nobody in both sessions", not (assigned[0] & assigned[1]), str(assigned[0] & assigned[1]))
check("reserves are not on duty in the other session", not (RES[0] & assigned[1]) and not (RES[1] & assigned[0]))
check("at least one reserve exists", any(RES), str(RES))

# ---- absent BEFORE confirm: replaced from the stand-by list ----
victim = sorted(assigned[0])[0]
sess1_reserves = sorted(RES[0])
r2 = ask(f"{victim} is absent, please replace")
check("replacement arranged before confirm", "Replacement arranged" in r2 and victim in r2 and "proposal" in r2, r2[:300])
if sess1_reserves:
    check("replacement comes from the stand-by list", any(x in r2 for x in sess1_reserves), r2[:300])
r3 = ask("confirm")
check("confirm saves", "Confirmed" in r3, r3[:120])
saved = {x["teacher"] for x in bot.exam_duty_store.all()}
check("absent teacher's duty is gone, replacement saved", victim not in saved, victim)

# ---- absent AFTER confirm: persisted duty is handed over ----
victim2 = sorted(assigned[1] - {victim})[0] if assigned[1] else sorted(assigned[0] - {victim})[0]
before = bot.exam_duty_store.duty_counts()
r4 = ask(f"{victim2} cannot come on {DATE}, replace")
check("replacement after confirm", "Replacement arranged" in r4 and victim2 in r4, r4[:300])
after = bot.exam_duty_store.duty_counts()
check("absent teacher's count dropped by one", after.get(victim2, 0) == before.get(victim2, 0) - 1, f"{before.get(victim2)} -> {after.get(victim2)}")
check("absent teacher is marked unavailable for that date", any(victim2.lower() in str(x).lower() for x in bot.exam_duty_requests.unavailable_store.all()))

# ---- a hall already booked for another exam is not offered again ----
booked = {x["hall"] for x in bot.exam_duty_store.all() if x["start_time"] == hh(A0)}
r5 = ask(f"exam on {DATE} from {hh(A0)} to {hh(A1)}, {STUD} students, {CAP} per room, two teachers in each room")
rows5 = [c[0] for b in (split_sessions(r5) or [r5]) for c in table_rows(b) if len(c) >= 3] if "Proposed" in r5 else []
check("booked halls are not offered again", not (set(rows5) & booked), str(set(rows5) & booked))
ask("cancel")

# ---- no semester: other rooms with no class at that time ----
bot.exam_duty_store.clear()
r6 = ask(f"exam on {DATE} from {hh(A0)} to {hh(A1)}, 100 students, 25 per room, 2 invigilators per room")
rooms6 = [c[0] for c in table_rows(r6.split("**Exam duties per assigned faculty")[0]) if len(c) >= 3] if "Proposed" in r6 else []
check("no-semester exam uses vacant rooms only", rooms6 and not (set(rooms6) & busy_rooms(A0, A1)), str(set(rooms6) & busy_rooms(A0, A1)))
check("4 rooms for 100 students / 25", len(rooms6) == min(4, len(se.vacant_rooms(M, DAY, A0, A1))), str(len(rooms6)))
ask("cancel")

# ---- students given but no room size: asks, then continues ----
r7 = ask(f"exam on {DATE} from {hh(A0)} to {hh(A1)}, 100 students, 2 invigilators per room")
check("asks for the room size", "how many students can sit in one room" in r7.lower(), r7[:200])
r8 = ask("25")
check("continues after the answer", "Proposed" in r8 and "100 students ÷ 25 per room" in r8, r8[:200])
ask("cancel")

# ---- more students than rooms: says so ----
r9 = ask(f"exam on {DATE} from {hh(A0)} to {hh(A1)}, 5000 students, 30 per room, 2 per room")
check("not enough rooms is reported", "rooms needed were available" in r9 or "No free room" in r9 or "couldn't find any free room" in r9, r9[:300])
ask("cancel")

print(f"{checks - len(fails)}/{checks} extras checks passed")
for f in fails[:15]:
    print("  FAIL", f)
sys.exit(1 if fails else 0)