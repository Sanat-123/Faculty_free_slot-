"""Glue between the exam-management package and this project's chatbot / timetable."""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Callable, Optional


def build_exam_management(model_getter: Callable, branch_getter: Callable,
                          real_faculty_getter: Optional[Callable] = None,
                          history_store_getter: Optional[Callable] = None,
                          today_getter: Callable[[], date] = date.today,
                          db_path: Any = None):
    """Return an ExamManagementHandler wired to the loaded timetable, or None on failure."""

    try:
        from utils.file_discovery import state_file
        from .chat_handler import ExamManagementHandler
        from .config import ExamSettings, load_settings
        from .providers import ProjectDirectory, ProjectHistory, ProjectTimetable
        from .service import ExamService
        try:
            from config.settings import FREE_RANGE_BUFFER_MINUTES as buffer
        except Exception:
            buffer = 0
        settings = ExamSettings()
        path = db_path or os.environ.get("EXAM_MGMT_DB_PATH") or state_file(ExamSettings().db_filename)
        svc = ExamService(
            path,
            directory=ProjectDirectory(model_getter, branch_getter, real_faculty_getter),
            timetable=ProjectTimetable(model_getter, buffer),
            history=ProjectHistory(history_store_getter) if history_store_getter else None,
            settings=settings,
        )
        svc.reload_settings()
        return ExamManagementHandler(svc, today_getter)
    except Exception as error:      # never let this feature stop the chatbot starting
        print(f"Exam management unavailable: {error}")
        return None


def open_exam_service(db_path: Any = None):
    """An ExamService on the SAME database file the chatbot uses (no timetable needed to import files)."""
    from utils.file_discovery import state_file
    from .config import ExamSettings
    from .service import ExamService
    path = db_path or os.environ.get("EXAM_MGMT_DB_PATH") or state_file(ExamSettings().db_filename)
    svc = ExamService(path, settings=ExamSettings())
    svc.reload_settings()
    return svc


def import_college_uploads(uploaded_files, db_path: Any = None) -> dict:
    """
    Route uploaded roster / duty-count files to the exam-management importer, judged by CONTENT.

    `uploaded_files` are objects with `.name` and `.getbuffer()` (Streamlit uploads).  Returns
    {file name: report} only for files that ARE roster / duty-count data; everything else (timetables ...)
    is left for the existing timetable pipeline.  The roster is always imported before duty counts, whatever
    the upload order, and an identical file uploaded again is recognised and not re-imported.
    Never raises: a failure is reported against the file.
    """

    reports: dict = {}
    try:
        imp = open_exam_service(db_path).importer
    except Exception as error:
        return {}
    found = []
    for up in uploaded_files:
        try:
            data = bytes(up.getbuffer())
            quick = imp.probe(data, up.name, quick=True)
            if quick.purpose in ("faculty_roster", "duty_counts", "roster_with_counts"):
                found.append((up, imp.probe(data, up.name)))
        except Exception:
            continue
    order = {"faculty_roster": 0, "roster_with_counts": 0, "duty_counts": 1}
    for up, probe in sorted(found, key=lambda x: order.get(x[1].purpose, 2)):
        try:
            res = imp.import_probe(probe, mode="replace")
        except Exception as error:
            reports[up.name] = {"name": up.name, "file_type": probe.purpose.upper(), "status": "failed", "success": False,
                                "record_count": 0, "message": f"import failed: {error}", "purpose": probe.purpose}
            continue
        label = {"faculty_roster": "COLLEGE_FACULTY_ROSTER", "duty_counts": "EXAM_DUTY_HISTORY",
                 "roster_with_counts": "COLLEGE_FACULTY_ROSTER"}[probe.purpose]
        if res["status"] == "duplicate":
            msg, ok = "already imported (identical file) - nothing changed", True
        elif res["status"] == "imported":
            parts = []
            if "roster" in res:
                r = res["roster"]
                parts.append(f"{r['added']} faculty added, {r['updated']} updated, {r['unchanged']} unchanged "
                             f"({r['not_in_this_file']} earlier roster records not in this file were kept)")
                if r.get("parked_not_on_roster"):
                    parts.append(f"{r['parked_not_on_roster']} other name(s) (e.g. from a timetable) are NOT on this roster and were "
                                 f"set aside, not deleted, until you accept them")
            if "history" in res:
                h = res["history"]
                parts.append(f"duty counts applied to {h['applied']} faculty ({h['total_duties']:g} duties)"
                             + (f"; {h['not_applied']} NOT applied - see import issues" if h["not_applied"] else ""))
            msg, ok = "; ".join(parts), True
        else:
            msg, ok = res.get("message", res["status"]), False
        reports[up.name] = {"name": up.name, "file_type": label, "status": "success" if ok else res["status"],
                            "success": ok, "purpose": probe.purpose, "message": msg,
                            "record_count": probe.row_count}
    return reports