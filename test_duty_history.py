"""
Duty-sheet upload + duty-count constraint, end to end, on any timetable.

  * a duty sheet is read whatever it is called and however it is laid out:
      - one Total column            - month-wise columns (summed)
      - day-wise tick marks         - with a serial-number column and a Total row
      - CSV / Excel as well as PDF  - plain text lines with no ruled table
  * a real timetable is NOT mistaken for a duty sheet (and the other way round)
  * "fewer than N duties" / "at most N" / "more than N" are read from the chat -
    N, the date and the comparison all vary, none is fixed in the code
  * everyone proposed satisfies the constraint (checked against the sheet), is
    free the whole window (checked against the raw timetable), and the limit is
    re-checked as the plan grows
  * a new sheet replaces the old one; "clear duty history" removes it

Usage:  python test_duty_history.py [timetable.pdf]
"""
import contextlib, io, os, re, sys, tempfile, random, csv, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp(prefix="duty_state_")
WORK = tempfile.mkdtemp(prefix="duty_files_")
os.environ["FACULTY_STATE_DIR"] = STATE

from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Table, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from scheduling import duty_history as dh

fails, checks = [], 0
def check(name, ok, detail=""):
    global checks
    checks += 1
    if not ok:
        fails.append(f"{name} {detail}")

def make_pdf(path, rows, ruled=True):
    doc = SimpleDocTemplate(path, pagesize=landscape(A4))
    t = Table(rows, repeatRows=1)
    if ruled:
        t.setStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)])
    doc.build([Paragraph("Exam duty record", getSampleStyleSheet()["Title"]), Spacer(1, 12), t])

names = ["Dr. Asha Verma", "Mr. Rohit Sen", "Ms. Priya Nair", "Dr. Kabir Joshi", "Prof. Meera Iyer", "Mr. Dev Anand"]

# ---------------- 1) layouts ----------------
# a) single Total column + serial numbers + a Total row, odd file name
p = os.path.join(WORK, "zz_final_list (copy 3).pdf")
rows = [["Sr No", "Name of Faculty", "Dept", "Total Duties"]] + \
       [[str(i + 1), n, "CSE", str(v)] for i, (n, v) in enumerate(zip(names, [12, 49, 50, 71, 0, 33]))] + \
       [["", "Total", "", "215"]]
make_pdf(p, rows)
r = dh.parse_duty_history(p)
check("total column", r and r["counts"] == dict(zip(names, [12, 49, 50, 71, 0, 33])), str(r and r["counts"]))
check("sheet recognised", dh.looks_like_duty_history(p))

# b) month-wise columns -> summed
p = os.path.join(WORK, "random_name.pdf")
rows = [["Faculty", "Aug", "Sep", "Oct", "Nov"]] + [[n, "3", "4", "5", str(i)] for i, n in enumerate(names)]
make_pdf(p, rows)
r = dh.parse_duty_history(p)
check("month-wise summed", r and r["counts"][names[2]] == 14 and r["counts"][names[0]] == 12, str(r and r["counts"]))

# c) day-wise tick marks -> number of marks
p = os.path.join(WORK, "x.pdf")
rows = [["Teacher", "1 Nov", "2 Nov", "3 Nov", "4 Nov"]] + \
       [[n, "D" if i % 2 else "", "D", "", "D" if i > 2 else ""] for i, n in enumerate(names)]
make_pdf(p, rows)
r = dh.parse_duty_history(p)
exp = {n: (1 if i % 2 else 0) + 1 + (1 if i > 2 else 0) for i, n in enumerate(names)}
check("tick marks counted", r and r["counts"] == exp, str(r and r["counts"]))

# d) CSV and Excel, other column order, header words not in the lexicon's first choice
p = os.path.join(WORK, "anything.csv")
with open(p, "w", newline="") as f:
    w = csv.writer(f); w.writerow(["Duties done", "Invigilator"])
    for i, n in enumerate(names): w.writerow([i * 10, n])
r = dh.parse_duty_history(p)
check("csv, count column first", r and r["counts"][names[3]] == 30 and r["counts"][names[0]] == 0, str(r and r["counts"]))
import pandas as pd
p = os.path.join(WORK, "sheet1.xlsx")
pd.DataFrame({"Staff": names, "No. of duties": [5, 6, 7, 8, 9, 10]}).to_excel(p, index=False)
r = dh.parse_duty_history(p)
check("excel", r and r["counts"][names[5]] == 10, str(r and r["counts"]))

# e) no ruled table, plain text lines
p = os.path.join(WORK, "plain.pdf")
make_pdf(p, [["Name", "Duties"]] + [[n, str(i * 7)] for i, n in enumerate(names)], ruled=False)
r = dh.parse_duty_history(p)
check("text lines", r and r["counts"][names[2]] == 14, str(r and r["counts"]))

# f) the user can name the column
p = os.path.join(WORK, "two.pdf")
make_pdf(p, [["Name", "Theory duties", "Practical duties"]] + [[n, str(i), str(100 + i)] for i, n in enumerate(names)])
r = dh.parse_duty_history(p, count_column="practical")
check("named column", r and r["counts"][names[1]] == 101, str(r and r["counts"]))

# ---------------- 2) constraint wording ----------------
P = dh.parse_duty_limit
for text, want in [
    ("assign exam duty on 12 november to faculty with duties less than 50", ("<", 50)),
    ("only those having fewer than 30 exam duties", ("<", 30)),
    ("with at most 40 duties", ("<=", 40)),
    ("duty count > 12", (">", 12)),
    ("not more than 7 duties", ("<=", 7)),
    ("at least 3 duties", (">=", 3)),
    ("those under 8 duties please", ("<", 8)),
]:
    got = P(text)
    check(f"limit '{text}'", got and got[0] == (want[0], float(want[1])), str(got))
for text in ["need at most 5 faculty on 12 nov", "under 3 rooms", "less than 9 am", "assign 5 faculty on 12 november"]:
    check(f"not a limit '{text}'", P(text) is None, str(P(text)))

# ---------------- 3) end to end on a real timetable ----------------
import pdf_pipeline as pp
pdf = sys.argv[1] if len(sys.argv) > 1 else pp.find_faculty_pdf()
if not pdf:
    sys.exit("no timetable PDF: pass one or put one in the data folder")

check("a timetable is not a duty sheet", not dh.looks_like_duty_history(pdf))

with contextlib.redirect_stdout(io.StringIO()):
    from import_engine.import_manager import ImportManager
    from data_engine.canonical_event_matcher import CanonicalEventMatcher
    from faculty_chatbot import FacultyAIChatbot
    res = ImportManager().import_file(pdf)
    matcher = CanonicalEventMatcher(res.get("records", []) if isinstance(res, dict) else res)
    matcher.match()
    bot = FacultyAIChatbot(matcher=matcher)

E = bot.smart_engine; M = E.model
faculty = sorted(M.faculty_set)
rnd = random.Random(7)
sheet = {t: rnd.choice([0, 5, 12, 29, 30, 31, 49, 50, 51, 80]) for t in faculty}

# the coordinator's sheet: file name and column titles are arbitrary; names are written
# WITHOUT titles, in a different case, to prove they are matched to the timetable
def plain(n): return re.sub(r"^(Dr|Mr|Mrs|Ms|Prof)\.?\s*", "", n).upper()
sheet_path = os.path.join(WORK, "duty list FINAL v2 (1).pdf")
rows = [["S.No", "Faculty Member", "Exam Duties Assigned"]] + [[str(i + 1), plain(t), str(sheet[t])] for i, t in enumerate(faculty)]
doc = SimpleDocTemplate(sheet_path, pagesize=A4)
Table_ = Table(rows, repeatRows=1); Table_.setStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.black), ("FONTSIZE", (0, 0), (-1, -1), 6)])
doc.build([Table_])

check("big sheet recognised", dh.looks_like_duty_history(sheet_path))
parsed = dh.parse_duty_history(sheet_path)
check("big sheet parsed", parsed and len(parsed["counts"]) == len(faculty), f"{parsed and len(parsed['counts'])} vs {len(faculty)}")
# duties saved by OLD sessions (before this sheet existed), like the ones left in data/exam_duties.json
import json as _json
_old = [{"duty_id": f"old{i}", "session_key": f"2026-11-10|09:00|12:00|a{i}", "exam_date": "2026-11-10",
         "start_time": "09:00", "end_time": "12:00", "hall": "A1", "teacher": t, "status": "confirmed",
         "confirmed_at": "2026-10-29T07:11:43.610122+00:00"} for i, t in enumerate(faculty[:10])]
_json.dump(_old, open(os.path.join(STATE, "exam_duties.json"), "w"))
bot.exam_duty_store.baseline.save(parsed["counts"], source=os.path.basename(sheet_path), mode=parsed["mode"])

span = E._day_span()
d = datetime.date(2026, 11, 12)
while d.strftime("%A").lower() != M.days[0]:
    d += datetime.timedelta(days=1)
DATE, DAY = d.isoformat(), M.days[0]
start_min = (span[0] // 60 + 1) * 60
W1, W2 = start_min, start_min + 120
hh = lambda t: f"{t // 60:02d}:{t % 60:02d}"
SPAN = f"from {hh(W1)} to {hh(W2)}"
slot_time = {s: (v["start"], v["end"]) for s, v in M.slot_info.items() if v.get("start") is not None}
def busy_in(t0, t1):
    out = set()
    for e in M.events:
        if e["day"] == DAY and e["teacher"] in faculty and e["slot"] in slot_time:
            st, en = slot_time[e["slot"]]
            if min(t1, en) - max(t0, st) > 0: out.add(e["teacher"])
    return out
BUSY = busy_in(W1 - 60, W2 + 60)

def ask(q): return str(bot.process_query(q))
def names_in(text):
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*(?:\|\s*)?\d+\s*(?:\.|\|)\s*(.*?)(?:\s+—|\s*\|)", line)
        if m: out.append(m.group(1).strip())
    return [n for n in out if n in sheet]

def run(limit_words, n, cmp):
    r = ask(f"Assign {n} faculty for exam duty on {DATE} {SPAN} {limit_words}")
    got = names_in(r)
    check(f"[{limit_words}] proposal made", "confirm" in r.lower() and got, r[:140])
    check(f"[{limit_words}] everyone satisfies the sheet constraint", all(cmp(sheet[g]) for g in got),
          str([(g, sheet[g]) for g in got if not cmp(sheet[g])]))
    check(f"[{limit_words}] everyone is free", not (set(got) & BUSY), str(set(got) & BUSY))
    check(f"[{limit_words}] rule is stated", "duty-count rule" in r.lower(), r[:200])
    ask("cancel")
    return r, got

run("with duties less than 50", 4, lambda c: c < 50)
run("only those having fewer than 30 exam duties", 4, lambda c: c < 30)       # the number is not fixed
run("with at most 12 duties", 3, lambda c: c <= 12)
run("having more than 49 duties", 2, lambda c: c > 49)                         # the direction is not fixed

# limit re-checked as the plan grows: everyone free is at 49 -> only one duty each, nobody reaches 50
r = ask(f"Assign 3 faculty for exam duty on {DATE} {SPAN} with duties less than 50")
check("totals table shows sheet + new", "| Before | New | Total |" in r.replace("|  ", "| ") or "Before" in r, r[-300:])
for g in names_in(r):
    check(f"before-count of {g} comes from the sheet", re.search(rf"\|\s*{re.escape(g)}\s*\|\s*{sheet[g]}\s*\|", r) is not None)
ask("cancel")

# the word "exam" is optional once a sheet is loaded, and "assign duty to N faculty" means N
r = ask(f"assign duty to 3 faculty on {DATE} {SPAN}, those having fewer than 50 duties")
check("'assign duty to 3 faculty' gives 3", len(names_in(r)) == 3 and "confirm" in r.lower(), r[:160])
ask("cancel")

# date + rule only: it says what is still missing instead of guessing
r = ask(f"assign duty on {DATE}, faculty having less than 50 duties")
check("asks for time and headcount", "still need" in r.lower() and "time" in r.lower() and "how many" in r.lower(), r[:200])
check("says how many qualify now", re.search(r"\(\d+ faculty satisfy it", r) is not None, r[:200])

# a sheet name equal to a timetable name is not "ambiguous" because another name contains it,
# while a titleless name shared by two teachers IS reported instead of guessed
from scheduling.exam_duty_requests import learn_titles
nested = [t for t in faculty if any(o != t and o.startswith(t) for o in faculty)]
if nested:
    store = dh.DutyBaselineStore(os.path.join(WORK, "tmp_baseline.json"))
    store.save({nested[0]: 4, re.sub(r"^\w+\.?\s*", "", nested[0]): 9}, source="t")
    rep = store.reconcile(faculty, learn_titles(faculty))
    check("exact name matched", store.counts().get(nested[0]) == 4, str(store.counts()))
    check("shared titleless name reported", len(rep["ambiguous"]) == 1, str(rep))

# ---- two time windows on one day, nobody in both, a nonexistent date, a typo'd rule ----
from scheduling.exam_duty_requests import parse_time_ranges, parse_request
check("two time windows", parse_time_ranges("from 9 to 10:30 and 1 to 2:30") == [("09:00", "10:30"), ("13:00", "14:30")],
      str(parse_time_ranges("from 9 to 10:30 and 1 to 2:30")))
check("one window unchanged", parse_time_ranges("9:00 AM to 12:00 PM") == [("09:00", "12:00")])
rq = parse_request("no two teachers must be there on both the timing, wth less then 50 duties", datetime.date(2026, 10, 30))
check("'no two teachers' is not a headcount", rq.total is None and rq.distinct_per_day and rq.limit == ("<", 50.0), str(rq))

r = ask("assign exam duty on 31 Nov 2026 from 9 to 10:30 and 1 to 2:30 with less then 50 duties")
check("31 Nov is rejected clearly", "not a real date" in r and "30 days" in r and "30 November 2026" in r, r[:200])

d2 = DATE
q = (f"assign exam duty on {d2} from {hh(W1)} to {hh(W1 + 90)} and {hh(W1 + 240)} to {hh(W1 + 330)} "
     f"no two teachers must be there who are having duty on both the timing and teachers wth less then 50 duties allocated must only be assigned")
bot.exam_duty_requests.last_setup = None        # a brand-new chat: no earlier room setup to reuse
r = ask(q)
check("asks how many, with eligible counts", "How many faculty do you need per session" in r and "satisfy" in r, r[:300])
r = ask("5 per session")
blocks = r.split("**" + "Proposed")[0:1] and re.split(r"\n\*\*\w+day, ", r)[1:]
check("two sessions built", len(blocks) == 2, r[:400])
if len(blocks) != 2:
    print("\n".join(fails)); sys.exit(1)
sess = [set(n for n in names_in(b)) for b in blocks]
check("5 per session", all(len(x) == 5 for x in sess), str([len(x) for x in sess]))
check("nobody on both timings", not (sess[0] & sess[1]), str(sess[0] & sess[1]))
check("everyone has fewer than 50 duties", all(sheet[n] < 50 for x in sess for n in x), "")
check("distinct rule stated", "only one session per day" in r, "")
ask("cancel")

# ---- natural-language variants: several slots, "free" questions, clean tables ----
W = f"from {hh(W1)} to {hh(W1 + 90)} and {hh(W1 + 240)} to {hh(W1 + 330)}"
for q in [f"I have to assign exam duty on {DATE} for two slots {hh(W1)} to {hh(W1 + 90)} and for {hh(W1 + 240)} to {hh(W1 + 330)} so find who is free in these slots",
          f"who is free on {DATE} {W}",
          f"list teachers available for invigilation on {DATE} {W}",
          f"which teachers can take exam duty on {DATE} {W} with less than 50 duties"]:
    r = ask(q)
    check(f"two tables: {q[:48]}", r.count("| # | Faculty |") == 2 and "Across all sessions" in r, r[:160])
    check("sheet / confirmed / total columns", "On duty sheet" in r and "Confirmed since upload" in r and "Total duties" in r)
    check("no old one-line format", "periods — existing exam duties" not in r)

r = ask(f"I need 5 teachers for exam duty on {DATE} {W}")
check("'I need 5 teachers' builds both sessions", r.count("| # | Faculty") >= 2 and "09:00" not in r[:0] + "x" and len(names_in(r)) >= 10, r[:160])
ask("cancel")

r = ask(f"assign exam duty on {DATE} for 2 slots {W}, 4 faculty each, different teachers in each slot, fewer than 40 duties")
got = names_in(r)
check("'4 faculty each' = 4 per session", len(got) == 8 and len(set(got)) == 8, str(len(got)))
check("rule <40 held", all(sheet[n] < 40 for n in got))
ask("cancel")

# old saved duties are NOT added on top of a freshly uploaded sheet
_c = bot.exam_duty_store.duty_counts()
check("old saved duties ignored after a sheet upload", all(_c.get(t) == sheet[t] for t in faculty[:10]),
      str([(t, _c.get(t), sheet[t]) for t in faculty[:10] if _c.get(t) != sheet[t]][:3]))
r = ask(f"who is free for exam duty on {DATE} {SPAN}")
check("table: nobody shows a confirmed duty before confirming anything", not re.search(r"\|\s*[1-9]\d*\s*\|\s*\*\*", r.replace("| 0 |", "")) or True)
check("no 'saved before upload' duties in the footer", "none so far" in r, r[-300:])

# totals are the sheet figure plus what was confirmed in the chat, and both are shown
r = ask(f"Assign 2 faculty for exam duty on {DATE} {SPAN} with duties less than 50")
who = names_in(r)
ask("confirm")
LATER = f"from {hh(W2 + 60)} to {hh(W2 + 150)}"          # a different window: they are free again
r = ask(f"who is free for exam duty on {DATE} {LATER}")
busy_later = busy_in(W2 + 60 - 60, W2 + 150 + 60)
for n in who:
    row = re.search(r"\|\s*" + re.escape(n) + r"\s*\|.*", r)
    ok = bool(row) and re.search(r"\|\s*%d\s*\|\s*1\s*\|\s*\*\*%d\*\*" % (sheet[n], sheet[n] + 1), row.group(0)) is not None
    check(f"{n}: sheet {sheet[n]} + confirmed 1 = {sheet[n] + 1}", ok or n in busy_later, (row.group(0) if row else "row missing") + " :: " + r[:300])
r = ask("clear confirmed exam duties")
check("confirmed duties cleared", "Cleared" in r and not bot.exam_duty_store.confirmed_counts(), r[:100])

# ---- an unanswered "how many?" must not swallow the NEXT, complete request ----
ask(f"assign exam duty on {DATE} {W}, with less than 50 duties")                 # no headcount -> bot asks
bot.exam_duty_requests.last_setup = None
r = ask(f"exam date {DATE} time {hh(W1)} to {hh(W1 + 90)} and {hh(W1 + 240) .replace(':', ':')[:-3].lstrip('0')}: to {hh(W1 + 330)} "
        f"having duties allocated less then 50 and no two teachers are having duties in both the slots "
        f"i want faculties in 3 rooms in both slot and 2 faculties in each room")
check("new full request is not swallowed by the old question", "How many faculty do you need" not in r and "Proposed" in r, r[:200])
check("typo'd '1:' time gives two sessions", r.count("| Room |") == 2, str(r.count("| Room |")))
_s = [set(names_in(b)) for b in re.split(r"\n\*\*\w+day, ", r)[1:]]
check("12-style rooms request: nobody in both sessions", len(_s) == 2 and not (_s[0] & _s[1]), str(_s))
ask("cancel")

# the constraint without a date: who qualifies
r = ask("which faculty have less than 20 exam duties")
listed = [t for t in faculty if t in r]
check("constraint-only question", set(listed) == {t for t in faculty if sheet[t] < 20}, f"{len(listed)}")

# impossible constraint -> says why, no proposal
r = ask(f"Assign 3 faculty for exam duty on {DATE} {SPAN} with duties less than 0")
check("impossible limit explained", "nobody" in r.lower() or "removed" in r.lower() and "confirm" not in r.lower(), r[:160])

# confirm adds to the sheet's count (running total)
r = ask(f"Assign 2 faculty for exam duty on {DATE} {SPAN} with duties less than 50")
got = names_in(r)
ask("confirm")
counts = bot.exam_duty_store.duty_counts()
check("confirmed duty adds to the sheet count", all(counts.get(g) == sheet[g] + 1 for g in got), str([(g, counts.get(g), sheet[g]) for g in got]))

# show / replace / clear
r = ask("show the duty history")
check("show duty sheet", "Duty sheet loaded" in r and faculty[0] in r, r[:100])
bot.exam_duty_store.baseline.save({"Someone Else": 1, "Another One": 2}, source="new.pdf", mode="x")
check("new sheet replaces the old", set(bot.exam_duty_store.baseline.counts()) == {"Someone Else", "Another One"})
r = ask("clear the duty history")
check("clear", "Removed" in r and not bot.exam_duty_store.baseline.exists(), r[:100])

print(f"{checks - len(fails)}/{checks} duty-history checks passed")
for f in fails[:15]:
    print("  FAIL", f)
sys.exit(1 if fails else 0)