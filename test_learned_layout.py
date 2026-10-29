"""
Regression test for the document-learned layout pass.

Nothing here names a teacher, class, room or file: it checks invariants
that must hold for ANY timetable PDF dropped in data/ (or passed as
arguments), so it keeps working when the data changes every semester.
"""
import sys, glob, re
from import_engine.import_manager import ImportManager


def load(path):
    r = ImportManager().import_file(path)
    return r.get("records", []) if isinstance(r, dict) else r


def check(path):
    problems = []
    records = load(path)
    scheduled = [r for r in records if str(r.get("record_type", "")).endswith("SCHEDULED")]

    for r in scheduled:
        cls_name, subject = r.get("class_name", ""), r.get("subject", "")
        canon = re.sub(r"\s*-\s*|\s+", "-", cls_name)

        # 1. a class must never stay glued onto the subject
        if cls_name and canon and canon.lower() in re.sub(r"\s*-\s*|\s+", "-", subject).lower():
            problems.append(("class glued in subject", subject, cls_name))

        # 2. the cell's own class line must end up in class_name
        lines = (r.get("raw_cell") or "").split("\n")
        if r.get("page_identity_type") == "teacher" and len(lines) > 1 and not cls_name:
            problems.append(("class line lost", r.get("raw_cell")))

    # 3. every page of a class/room-wise file gets an identity
    kinds = {r.get("page_identity_type") for r in records if r.get("page_identity_type") != "teacher"}
    if kinds - {"class", "room"} and kinds & {"class", "room"}:
        problems.append(("unidentified pages", kinds))

    return len(scheduled), problems


if __name__ == "__main__":
    files = sys.argv[1:] or sorted(glob.glob("data/*.pdf"))
    failed = False
    for f in files:
        n, problems = check(f)
        print(f"{f}: {n} scheduled records, {len(problems)} problems")
        for p in problems[:5]:
            print("   ", p)
        failed |= bool(problems)
    sys.exit(1 if failed else 0)