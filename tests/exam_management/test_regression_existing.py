"""
Existing features must keep working: the project's own test scripts are executed
unchanged, and the real chatbot is driven with and without the new handler.
"""
import contextlib
import glob
import io
import os
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EXISTING = ["test_exam_duty_allocation.py", "test_exam_duty_requests.py", "test_college_wide.py",
            "test_duty_history.py"]


@pytest.mark.parametrize("script", EXISTING)
def test_existing_project_script_still_passes(script):
    path = os.path.join(ROOT, script)
    if not os.path.exists(path):
        pytest.skip(f"{script} not present")
    r = subprocess.run([sys.executable, script], cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-800:] + r.stderr[-800:]
    assert "passed" in r.stdout


@pytest.fixture(scope="module")
def bot():
    pdfs = glob.glob(os.path.join(ROOT, "data", "*.pdf"))
    if not pdfs:
        pytest.skip("no timetable PDFs in data/")
    os.environ["FACULTY_STATE_DIR"] = tempfile.mkdtemp(prefix="exm_reg_")
    with contextlib.redirect_stdout(io.StringIO()):
        from import_engine.import_manager import ImportManager
        from data_engine.canonical_event_matcher import CanonicalEventMatcher
        from faculty_chatbot import FacultyAIChatbot
        recs = []
        for f in sorted(pdfs):
            r = ImportManager().import_file(f)
            recs += r.get("records", []) if isinstance(r, dict) else r
        m = CanonicalEventMatcher(recs)
        m.match()
        return FacultyAIChatbot(matcher=m)


def test_new_handler_is_attached(bot):
    assert bot.exam_management is not None


def test_free_slot_questions_still_answer_and_are_not_stolen(bot):
    for q in ["who is free on monday at 10", "free faculty on friday from 2 pm to 3 pm"]:
        assert bot.exam_management.handle(q) is None
        assert str(bot.process_query(q)).strip()


def test_legacy_single_day_exam_duty_flow_still_works(bot):
    q = "allocate exam duty on monday from 10:00 to 12:00 for 3 rooms with 2 invigilators each"
    assert bot.exam_management.handle(q) is None            # single day -> legacy flow keeps ownership
    assert str(bot.process_query(q)).strip()


def test_new_system_uses_every_department_in_the_loaded_timetable(bot):
    from datetime import date
    h = bot.exam_management
    h.today = lambda: date(2026, 11, 6)
    reply = bot.process_query("Allocate examination duties from 10 November to 12 November. 6 examination rooms, "
                              "2 invigilators per room, maximum 3 duties, morning shift 9 am to 12 pm, "
                              "regular classes are suspended.")
    assert "awaiting your approval" in reply
    repo = h.repo
    depts = set(repo.departments())
    aid = h._alloc()["id"]
    used = {d["department"] for d in repo.duties(aid)}
    assert (used - {''}) <= depts and len(repo.faculty_all()) > 50   # whole faculty list, not one department's