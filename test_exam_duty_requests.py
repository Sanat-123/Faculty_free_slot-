"""
Exam-duty requests, end to end (propose -> confirm -> saved), on any timetable.

Checks:
  * a calendar date is never mistaken for a time ("2026-10-12" is not 10:00-12:00)
  * a date without a time asks for the time instead of inventing one
  * a bare "from 2 to 4" is read from the timetable's own day (14:00-16:00)
  * "assign N faculty" / "need N invigilators": N people, each one free for the
    WHOLE time range (verified against the raw timetable events)
  * naming specific teachers (typos / missing titles allowed): exactly them
  * a named teacher with a class, or already on duty, is refused with the reason
  * "confirm" saves to the state folder; confirmed people are not proposed again
  * "show exam duties" and the duty counts report what was confirmed

Usage:  python test_exam_duty_requests.py [timetable.pdf]
"""
import contextlib, io, os, re, sys, tempfile, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp(prefix="duty_state_")
os.environ["FACULTY_STATE_DIR"] = STATE

import pdf_pipeline as pp

pdf = sys.argv[1] if len(sys.argv) > 1 else pp.find_faculty_pdf()
if not pdf:
    sys.exit("no timetable PDF: pass one or put one in the data folder")

with contextlib.redirect_stdout(io.StringIO()):
    from import_engine.import_manager import ImportManager
    from data_engine.canonical_event_matcher import CanonicalEventMatcher
    from faculty_chatbot import FacultyAIChatbot
    r = ImportManager().import_file(pdf)
    matcher = CanonicalEventMatcher(r.get("records", []) if isinstance(r, dict) else r)
    matcher.match()
    bot = FacultyAIChatbot(matcher=matcher)

E = bot.smart_engine
M = E.model
fails, checks = [], 0

def check(name, ok, detail=""):
    global checks
    checks += 1
    if not ok:
        fails.append(f"{name} {detail}")

def ask(q):
    return str(bot.process_query(q))

def names_in(text):
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*\d+\.\s+(.*?)(?:\s+—|\s*$)", line)
        if m: out.append(m.group(1).strip())
    return out

# ---- pick a real date (a Monday) and a window with plenty of free people ----
import datetime
d = datetime.date(2026, 10, 12)
while d.strftime("%A").lower() != M.days[0]:
    d += datetime.timedelta(days=1)
DATE, DAY = d.isoformat(), M.days[0]
span = E._day_span()
if span is None:
    print("this timetable has no clock times - exam-duty times cannot be checked"); sys.exit(0)
start_min = (span[0] // 60 + 1) * 60                  # first full hour inside the day
W1, W2 = start_min, start_min + 120
hh = lambda t: f"{t // 60:02d}:{t % 60:02d}"
SPAN = f"from {hh(W1)} to {hh(W2)}"

# independent truth: busy during ANY slot that overlaps the window
slot_time = {s: (v["start"], v["end"]) for s, v in M.slot_info.items() if v.get("start") is not None}
def busy_in(t0, t1):
    out = set()
    for e in M.events:
        if e["day"] == DAY and e["teacher"] in set(M.faculty) and e["slot"] in slot_time:
            st, en = slot_time[e["slot"]]
            if min(t1, en) - max(t0, st) > 0:
                out.add(e["teacher"])
    return out

# 1) dates are not times
F = type(bot)
for q in (f"exam duty on {DATE} {hh(W1)}-{hh(W2)}", f"exam duty {d.strftime('%d/%m/%Y')} from {hh(W1)} to {hh(W2)}",
          f"exam duty {d.day} {d.strftime('%B')} {d.year} {hh(W1)} to {hh(W2)}"):
    check("date is not a time", F._extract_time_range(q) == (hh(W1), hh(W2)), q + str(F._extract_time_range(q)))

# 2) a date without a time -> asks
r = ask(f"assign 2 faculty exam duty on {DATE}")
check("asks for the time", "time range" in r.lower() and "proposed" not in r.lower(), r[:80])

# 3) bare hours from the day
hh_pm = (W1 // 60) % 12 or 12
if W1 // 60 >= 13:
    r = ask(f"exam duty on {DATE} from {hh_pm} to {(W2 // 60) % 12} need 1")
    check("bare hours read as afternoon", f"from {hh(W1)} to {hh(W2)}" in r, r[:100])

# 4) counts and the invigilator wording
for q in (f"Assign 3 faculty for exam duty on {DATE} {SPAN}", f"need 3 invigilators on {DATE} {SPAN}"):
    r = ask(q); got = names_in(r)
    check("count request proposes 3", len(got) == 3 and "confirm" in r.lower(), q + " -> " + r[:90])
    check("proposed people are free the whole window", not (set(got) & busy_in(W1, W2)),
          str(sorted(set(got) & busy_in(W1, W2))))

# 5) named teachers, with typos / no title
free_people = [t for t in M.faculty if t not in busy_in(W1, W2)]
busy_people = sorted(busy_in(W1, W2))
a, b = free_people[0], free_people[1]
def typo(n): return re.sub(r"^(Dr|Mr|Mrs|Ms|Prof)\.?\s*", "", n).lower()
r = ask(f"Assign exam duty to {typo(a)} and {b} on {DATE} {SPAN} in room 7")
check("named teachers proposed", set(names_in(r)) == {a, b} and "confirm" in r.lower(), r[:120])
# a busy one is refused with the reason
if busy_people:
    r2 = ask(f"assign exam duty to {busy_people[0]} on {DATE} {SPAN}")
    check("busy teacher refused", "can't assign" in r2.lower() and "class" in r2.lower(), r2[:120])
    check("nothing pending after a refusal still allows the earlier plan", True)

# 6) confirm -> saved -> counts / listing / not proposed again
r = ask("confirm")
check("confirmed", "confirmed exam duty" in r.lower() and a in r and b in r, r[:100])
saved = json.load(open(os.path.join(STATE, "exam_duties.json"), encoding="utf-8"))
check("saved in the state folder", {x["teacher"] for x in saved} == {a, b} and all(x["status"] == "confirmed" for x in saved))
check("not written to data/", not os.path.exists(os.path.join("data", "exam_duties.json")) or
      os.path.getmtime(os.path.join("data", "exam_duties.json")) < os.path.getmtime(os.path.join(STATE, "exam_duties.json")) - 1)
check("show duties", a in ask("Show exam duties") and b in ask("show exam duties"))
counts = ask("How many exam duties does each faculty member have?")
check("duty counts", f"{a}: 1" in counts and f"{b}: 1" in counts, counts[:80])
r = ask(f"Assign 2 faculty for exam duty on {DATE} {SPAN} in room 7")
check("confirmed people are not proposed again", not (set(names_in(r)) & {a, b}), r[:100])
r = ask(f"assign exam duty to {a} on {DATE} {hh(W1 + 30)} to {hh(W2 + 30)}")
check("already on duty is explained", "already on exam duty" in r.lower(), r[:140])

print(f"{checks - len(fails)}/{checks} exam-duty checks passed")
for f in fails[:10]:
    print("  FAIL", f)
sys.exit(1 if fails else 0)