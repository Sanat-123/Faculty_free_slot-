"""
Universality test: import a timetable PDF from an imaginary *different*
institution (Sun-Thu week, 9 slots, other header wording, other room / class /
name styles) and compare what the importer extracts with the generated truth.
Nothing in the code base knows any of these names.  Needs: pip install reportlab
"""
import io, os, random, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib import colors
except ImportError:
    print("reportlab not installed - skipping"); sys.exit(0)

import pdf_pipeline as pp
from engine.smart_query import SmartQueryEngine
from engine.timetable_model import TimetableModel

DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu"]            # no Saturday, starts Sunday
FULL = {"Sun": "Sunday", "Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday"}
TIMES = ["09:00 - 09:45", "09:45 - 10:30", "10:45 - 11:30", "11:30 - 12:15", "13:00 - 13:45",
         "13:45 - 14:30", "14:45 - 15:30", "15:30 - 16:15", "16:15 - 17:00"]      # 9 slots
TEACHERS = ["Prof. Alan Turing", "Grace Hopper", "Dr. Ada Lovelace", "Linus Torvalds", "Margaret Hamilton",
            "Prof. Dennis Ritchie", "Katherine Johnson", "Donald Knuth", "Barbara Liskov", "Edsger Dijkstra"]
SUBJECTS = ["Thermodynamics", "Circuit Theory", "Quantum Mechanics", "Linear Algebra", "Data Structures",
            "Physics Lab", "Electronics Lab", "Fluid Mechanics"]
ROOMS = ["B-204", "B-205", "A-110", "LAB-3", "LAB-7", "Auditorium", "Block C Hall"]
CLASSES = ["BSc Physics Y2", "EEE 3B", "ME-2A", "MSc Maths I"]

random.seed(11)
truth = []                       # (teacher, Day, slot, subject, room, class, group)
pages = []

for t in TEACHERS:
    rows = []
    for d in DAYS:
        row = [d]
        for s in range(1, 10):
            if random.random() < 0.28:
                sub = random.choice(SUBJECTS)
                room = random.choice(ROOMS[3:5]) if "Lab" in sub else random.choice(ROOMS)
                cls = random.choice(CLASSES)
                grp = f"Batch {random.randint(1, 2)}" if "Lab" in sub else ""
                cell = f"{sub} {room}\n{cls}" + (f"\n{grp}" if grp else "")
                truth.append((t, FULL[d], s, sub, room, cls, grp))
                row.append(cell)
            else:
                row.append("")
        rows.append(row)
    pages.append((t, rows))

buf = io.BytesIO()
doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=10, rightMargin=10)
styles = getSampleStyleSheet()
story = []
for t, rows in pages:
    story.append(Paragraph("Riverdale Polytechnic - Weekly Timetable", styles["Title"]))
    story.append(Paragraph(f"Faculty Name: {t}", styles["Heading2"]))
    header = [""] + [f"{i}\n{TIMES[i-1]}" for i in range(1, 10)]
    # plain multi-line strings: one field per line, no wrapping (like the real PDFs)
    data = [header] + rows
    tbl = Table(data, colWidths=[30] + [88] * 9)
    tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("FONTSIZE", (0, 0), (-1, -1), 5.5), ("LEADING", (0, 0), (-1, -1), 7)]))
    story += [tbl, PageBreak()]
doc.build(story)

parsed = pp.parse_faculty_pdf(buf.getvalue())
db, stats = pp.build_database(parsed)
print("generated", len(truth), "sessions ->", stats)

import sqlite3
rows = sqlite3.connect(db).execute(
    "select teacher,day,slot,subject,room,class_name,group_name,slot_time from timetable").fetchall()
got = {(r[0], r[1], r[2]): r for r in rows}

fails, checks = [], 0
def check(name, ok, detail=""):
    global checks
    checks += 1
    if not ok: fails.append(f"{name} {detail}")

check("session count", len(rows) == len(truth), f"{len(rows)} vs {len(truth)}")
for t, d, s, sub, room, cls, grp in truth:
    r = got.get((t, d, s))
    if not r:
        check("missing", False, f"{t} {d} {s}"); continue
    check("subject", r[3] == sub, f"{r[3]!r} vs {sub!r}")
    check("room",    r[4] == room, f"{r[4]!r} vs {room!r}")
    check("class",   r[5] == cls, f"{r[5]!r} vs {cls!r}")
    check("group",   r[6] == grp, f"{r[6]!r} vs {grp!r}")
    check("time",    r[7].replace(" ", "") == TIMES[s - 1].replace(" ", ""), f"{r[7]!r}")

m = TimetableModel.from_sqlite(db)
check("days from data", sorted(d.capitalize() for d in m.days) == sorted(FULL.values()), str(m.days))
check("slots from data", list(m.slots) == list(range(1, 10)), str(m.slots))

e = SmartQueryEngine(m)
a = e.answer("Who is free on Sunday slot 1?") or ""
busy = {x[0] for x in truth if x[1] == "Sunday" and x[2] == 1}
check("free list", {n for n in TEACHERS if n not in busy} <= set(a.replace("\n", " ").split(". ")) or all(n in a for n in TEACHERS if n not in busy))
check("no Saturday", "not part of this timetable" in (e.answer("Who is free on Saturday slot 1?") or "").lower()
      or "no" in (e.answer("Who is free on Saturday slot 1?") or "").lower())
check("clock time", "09:00" in (e.answer("what time is slot 1") or ""))

import shutil
shutil.copy(db, os.path.join(tempfile.gettempdir(), "universal_test.db"))
print(f"{checks - len(fails)}/{checks} checks passed")
for f in fails[:15]:
    print("  FAIL", f)
sys.exit(1 if fails else 0)