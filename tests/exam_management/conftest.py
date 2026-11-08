import os
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from exam_management.config import ExamSettings          # noqa: E402
from exam_management.providers import StaticDirectory     # noqa: E402
from exam_management.service import ExamService, SessionSpec  # noqa: E402
from tests.exam_management import dataset as ds           # noqa: E402

FAST = ExamSettings(solver_deterministic_time=0.6, solver_max_seconds=2.0, candidate_runs=4)
START = date(2026, 11, 10)


class World:
    """A small realistic college: several departments, history, a session factory."""

    def __init__(self, path, rows, settings=FAST, timetable=None):
        self.rows = rows
        self.svc = ExamService(str(path), directory=StaticDirectory(rows), settings=settings,
                               timetable=timetable)
        self.svc.sync_roster()
        self.hist = ds.history(rows)
        self.svc.repo.import_history(self.hist, "duty_sheet")
        self.ids = {f["name"]: f["id"] for f in self.svc.repo.faculty_all()}
        self.names = [n for n, _ in rows]

    def session(self, days=6, shifts=ds.SHIFTS_ONE, rooms=6, per=2, mn=None, mx=4, suspended=True, **opts):
        return self.svc.create_session(SessionSpec(
            "Final", ds.days(START, days), shifts, per_room=per, room_count=rooms, min_duties=mn,
            max_duties=mx, classes_suspended=suspended, options=opts))

    def fid(self, i):
        return self.ids[self.names[i]]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path / "exm.db", ds.roster(10))


@pytest.fixture
def make_world(tmp_path):
    def build(rows=None, **kw):
        return World(tmp_path / f"exm_{len(list(tmp_path.iterdir()))}.db", rows or ds.roster(10), **kw)
    return build