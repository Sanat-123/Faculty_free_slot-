"""
Time-of-day questions, checked against an INDEPENDENT computation.

For every weekday of the loaded timetable and a large set of time windows,
the question is asked in many wordings ("from 9 to 11", "8am to 2pm",
"between 10 and 12", "9-11", "after 12", "at 10", with typos in the day name
...).  The expected answer is worked out here from the raw events and the raw
slot times, using the human reading of the time:

    a slot is covered by a window when it lies inside it or at least half of it
    does; the people listed must be free in EVERY covered slot; a window that
    covers no slot must be answered with a "no slot" message.

Usage:  python test_time_questions.py [timetable.pdf | --db file.db | --app]
        (--app = every file in the data folder, like the web app does)
"""
import os, re, sys, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pdf_pipeline as pp
from engine.smart_query import SmartQueryEngine
from engine.timetable_model import TimetableModel

if "--app" in sys.argv:
    # exactly what app.py does: import EVERY timetable file of the data folder,
    # match them canonically, and use the full chatbot's engine
    import contextlib, io
    from utils.file_discovery import discover_timetable_files
    with contextlib.redirect_stdout(io.StringIO()):
        from import_engine.import_manager import ImportManager
        from data_engine.canonical_event_matcher import CanonicalEventMatcher
        from faculty_chatbot import FacultyAIChatbot
        records = []
        for path in discover_timetable_files():
            r = ImportManager().import_file(str(path))
            records += r.get("records", []) if isinstance(r, dict) else r
        matcher = CanonicalEventMatcher(records)
        matcher.match()
        E = FacultyAIChatbot(matcher=matcher).smart_engine
    M = E.model
else:
    if len(sys.argv) > 2 and sys.argv[1] == "--db":
        db = sys.argv[2]
    else:
        pdf = sys.argv[1] if len(sys.argv) > 1 else pp.find_faculty_pdf()
        if not pdf:
            sys.exit("no timetable PDF: pass one, or put one in the data folder")
        db, _ = pp.build_database(pp.parse_faculty_pdf(open(pdf, "rb").read()))
    M = TimetableModel.from_sqlite(db)
    E = SmartQueryEngine(M)

# ---- independent truth -------------------------------------------------
CLOCK = re.compile(r"(\d{1,2})[:.](\d{2})\D+(\d{1,2})[:.](\d{2})")
slot_time = {}
for e in M.events:
    m = CLOCK.search(str(e.get("slot_time") or ""))
    if m and e["slot"] not in slot_time:
        slot_time[e["slot"]] = (int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4]))
# a slot in which nobody teaches (e.g. a short last slot) has no event, but
# its time is still in the timetable's slot table
for s, v in M.slot_info.items():
    if s not in slot_time and v.get("start") is not None:
        slot_time[s] = (v["start"], v["end"])

if not slot_time:
    print("this timetable has no clock times - nothing to test"); sys.exit(0)

faculty = set(M.faculty)
busy = collections.defaultdict(set)
for e in M.events:
    if e["teacher"] in faculty:
        busy[(e["day"], e["slot"])].add(e["teacher"])

def covered(a, b):
    inside, touching = [], []
    for s, (st, en) in sorted(slot_time.items()):
        ov = min(b, en) - max(a, st)
        if ov > 0:
            touching.append(s)
            if (st >= a and en <= b) or ov * 2 >= (en - st):
                inside.append(s)
    return inside or touching

def free_in_all(day, slots):
    return faculty - set().union(*(busy[(day, s)] for s in slots))

def names(text):
    out = []
    for line in str(text).splitlines():
        m = re.match(r"^\s*\d+\.\s+(.*?)\s*$", line)
        if m: out.append(re.split(r"\s+—\s+", m.group(1))[0].strip())
    return set(out)

nslots = max(M.slots)
first, last = min(v[0] for v in slot_time.values()), max(v[1] for v in slot_time.values())
fails, checks = [], 0
def check(q, want_slots, day, ans):
    global checks
    checks += 1
    if not want_slots:
        ok = ans is not None and re.search(r"no slot", ans, re.I)
        detail = "expected a 'no slot' message"
    else:
        want = free_in_all(day, want_slots)
        got = names(ans or "")
        ok = got == want
        detail = f"slots {want_slots}: missing {sorted(want-got)[:2]} extra {sorted(got-want)[:2]}"
    if not ok:
        fails.append(f"{q!r} -> {detail}")

def h12(h):  # 24h -> (number, am/pm)
    return (h if h <= 12 else h - 12), ("am" if h < 12 else "pm")

def typo(day):
    d = day.lower()
    return [d.capitalize(), d, d[:2] + "x" + d[3:], d[:3] + d[4] + d[3] + d[5:], d[:4] + d[5:], d.upper()]

hours = [h for h in range(7, 18) if first - 60 <= h * 60 <= last + 60]
windows = [(a, b) for a in hours for b in hours if a < b]

for day in M.days:
    D = day.capitalize()
    for a, b in windows:
        want = covered(a * 60, b * 60)
        (a12, ap_a), (b12, ap_b) = h12(a), h12(b)
        phr = [
            f"Who is free on {D} {a12}{ap_a} to {b12}{ap_b}?",
            f"show me who is free on {D} from {a12} {ap_a} to {b12} {ap_b}",
            f"teachers available between {a12}{ap_a} and {b12}{ap_b} on {D}",
            f"{D} {a12}:00 {ap_a} - {b12}:00 {ap_b} who is free",
            f"free faculty {D} {a:02d}:00 to {b:02d}:00",
        ]
        # Bare numbers ("from 9 to 11") are read as clock hours, except when
        # both are valid slot numbers in increasing order ("from 2 to 4"),
        # which is the documented slot reading - so those are not asked here.
        slot_reading = a12 <= nslots and b12 <= nslots and a12 < b12
        if not slot_reading:
            phr += [f"who is free on {D} from {a12} to {b12}",
                    f"{D} {a12}-{b12} who is free",
                    f"who is free from {a12} to {b12} on {D}"]
        for p in phr:
            E.reset_context(); check(p, want, D.lower(), E.answer(p))
        for t in typo(D)[1:4]:
            p = f"who is free on {t} {a12}{ap_a} to {b12}{ap_b}"
            E.reset_context(); check(p, want, D.lower(), E.answer(p))

    # single times and open ranges
    for h in hours:
        h12n, ap = h12(h)
        t = h * 60 + 30
        inside = [s for s, (st, en) in slot_time.items() if st <= t < en]
        if len(inside) == 1:
            for p in (f"who is free on {D} at {h12n}:30 {ap}", f"who is free on {D} at {h:02d}:30"):
                E.reset_context(); check(p, inside, D.lower(), E.answer(p))
        if h12n > nslots:
            for p, w in ((f"who is free on {D} after {h12n} {ap}", covered(h * 60, 24 * 60)),
                         (f"who is free on {D} before {h12n} {ap}", covered(0, h * 60))):
                E.reset_context(); check(p, w, D.lower(), E.answer(p))
    for word, (lo, hi) in (("morning", (0, 720)), ("afternoon", (720, 1020))):
        p = f"who is free on {D} {word}"
        E.reset_context(); check(p, covered(lo, hi), D.lower(), E.answer(p))

print(f"{checks - len(fails)}/{checks} time questions answered correctly")
for f in fails[:12]:
    print("  FAIL", f)
sys.exit(1 if fails else 0)