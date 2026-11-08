"""Glue between the exam-management package and this project's chatbot / timetable."""

from __future__ import annotations

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
        path = db_path or state_file(ExamSettings().db_filename)
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