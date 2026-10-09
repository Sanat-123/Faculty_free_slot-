"""
Typo / spelling robustness, checked for EVERY entity of the loaded timetable.

* every teacher, with ~16 kinds of mistake (no title, lower case, missing /
  extra / wrong / swapped letter in first or last name, reversed order ...):
  never the WRONG person, and (almost) always the right one
* classes and rooms typed in other case / spacing / punctuation
* questions that name nobody must never "find" a teacher, room, class or subject
* typos in the ordinary words (days, "free", "timetable" ...) still give the
  same answer as the correct sentence

Works on any timetable:  python test_typo_robustness.py [timetable.pdf]
(default: newest PDF in the data folder)
"""
import os, random, re, sys, collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pdf_pipeline as pp
from engine.smart_query import SmartQueryEngine
from engine.timetable_model import TimetableModel
import generate_all_questions as g

pdf = sys.argv[1] if len(sys.argv) > 1 else pp.find_faculty_pdf()
if not pdf:
    sys.exit("no timetable PDF: pass one as argument or put one in the data folder")
db, _ = pp.build_database(pp.parse_faculty_pdf(open(pdf, "rb").read()))
M = TimetableModel.from_sqlite(db)
E = SmartQueryEngine(M)
random.seed(7)
LET = "abcdefghijklmnopqrstuvwxyz"
TITLE = re.compile(r"^(dr|mr|mrs|ms|prof|professor)\.?\s*", re.I)
fails = []

def check(name, ok, detail=""):
    if not ok:
        fails.append(f"{name}: {detail}")
    return ok

def ins(w): p = random.randrange(1, len(w) + 1); return w[:p] + random.choice([w[p - 1], "i", "a"]) + w[p:]
def dele(w): p = random.randrange(1, len(w)); return w[:p] + w[p + 1:]
def sub(w): p = random.randrange(1, len(w)); return w[:p] + random.choice([c for c in LET if c != w[p].lower()]) + w[p + 1:]
def swap(w): p = random.randrange(0, len(w) - 1); return w[:p] + w[p + 1] + w[p] + w[p + 2:]

# ---------------- 1. teachers ----------------
teachers = list(M.faculty)
tokens = lambda n: TITLE.sub("", n).split()
total = correct = wrong = 0
for t in teachers:
    toks = tokens(t)
    variants = [t, TITLE.sub("", t), t.lower(), t.upper(), " ".join(reversed(toks))]
    for fn in (ins, dele, sub, swap):
        for idx in (0, -1):
            if len(toks[idx]) >= 4:
                x = toks[:]; x[idx] = fn(x[idx]); variants.append(" ".join(x))
    for v in variants:
        E.reset_context()
        f = E.parse(f"Show timetable of {v}")
        total += 1
        if f.teachers == [t]:
            correct += 1
        elif f.teachers and not f.ambiguous and t not in f.teachers:
            wrong += 1
            fails.append(f"WRONG PERSON: typed {v!r} meant {t!r} got {f.teachers}")
print(f"teachers: {len(teachers)} people, {total} variants -> {correct} correct ({100*correct/total:.1f}%), {wrong} wrong person")
check("teacher accuracy >= 97%", correct / total >= 0.97, f"{100*correct/total:.1f}%")

# ---------------- 2. classes / rooms: case, spaces, punctuation ----------------
bad = 0
for c in M.classes:
    for v in (c.lower(), c.upper(), re.sub(r"[^A-Za-z0-9]", "", c), c.replace("-", " ")):
        E.reset_context()
        if E.parse(f"Show timetable of {v}").classes != [c]:
            bad += 1; fails.append(f"class {v!r} did not resolve to {c!r}")
for r in M.rooms:
    for v in (r.lower(), re.sub(r"[^A-Za-z0-9]", "", r), "room " + r):
        E.reset_context()
        if E.parse(f"Is room {v} free on Monday slot 3?").rooms != [r]:
            bad += 1; fails.append(f"room {v!r} did not resolve to {r!r}")
print(f"classes/rooms re-typed in other case/spacing: {bad} failures")

# ---------------- 3. questions naming nobody must not invent an entity ----------------
data = g.build_data(M, g.Truth(M))
n = invented = 0
for cat, tpl, orc in g.PATTERNS:
    if set(re.findall(r"\{(\w+)\}", tpl)) & {"T", "T2", "TS", "S", "C", "R", "CS", "NT", "NC"}:
        continue
    if "{TIME}" in tpl and not data["slot_times"]:
        continue
    for q, _ in g.expand(tpl, data):
        E.reset_context(); f = E.parse(q); n += 1
        if f.teachers or f.rooms or f.classes or f.subjects:
            invented += 1; fails.append(f"invented entity in {q!r}")
print(f"entity-free questions: {n}, {invented} invented an entity")

# ---------------- 4. typos in ordinary words ----------------
T, C, S = teachers[3], M.classes[2], sorted(M.subjects)[5]
base = ["Who is free on Monday slot 3?", f"Is {T} free on Wednesday slot 4?", f"Show timetable of {T}",
        f"Show the full timetable of {C}", "Which rooms are free on Thursday slot 5?", f"Who teaches {S}?",
        f"What subjects does {T} teach?", f"When is {T} free on Friday?", "Who has the heaviest workload?",
        "Which day is " + T + " most busy?", "Who is free on Monday at 10:30?"]
protect = {x.lower() for ent in (T, C, S) for x in re.findall(r"[A-Za-z0-9]+", ent)}
same = tot = 0
for q in base:
    E.reset_context(); want = E.answer(q)
    for m in re.finditer(r"[A-Za-z]{5,}", q):
        if m.group(0).lower() in protect:
            continue
        for fn in (ins, dele, swap):
            typo = fn(m.group(0))
            if typo.lower() == m.group(0).lower():
                continue
            E.reset_context(); got = E.answer(q[:m.start()] + typo + q[m.end():]); tot += 1
            same += got == want
print(f"typos in ordinary words: {same}/{tot} give the identical answer ({100*same/tot:.1f}%)")
check("ordinary-word typos >= 88%", same / tot >= 0.88, f"{100*same/tot:.1f}%")

print("\n" + ("ALL TYPO CHECKS PASSED" if not fails else f"{len(fails)} PROBLEMS"))
for f in fails[:15]:
    print("  FAIL", f)
sys.exit(1 if fails else 0)