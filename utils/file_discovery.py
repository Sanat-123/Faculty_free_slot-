"""
Locating timetable files without naming any of them.

A timetable changes every semester, so no code may mention a specific file.
Files are found by LOOKING in a folder:

    FACULTY_DATA_DIR      folder that holds the timetable files   (default: data)
    FACULTY_STATE_DIR     folder for saved state - exam duties, room shifts,
                          assignments                              (default: data)
    FACULTY_TIMETABLE_PDF one PDF to use instead of searching      (optional)

Typical semester change: put the new files in a new folder (for example
data/2026_odd) and start the app with FACULTY_DATA_DIR=data/2026_odd, or
simply replace the files in `data/`.  Nothing in the code needs editing.
"""

from __future__ import annotations

import os
from pathlib import Path

TIMETABLE_EXTENSIONS = (".pdf", ".xlsx", ".xls", ".csv")


def data_dir() -> Path:
    return Path(os.environ.get("FACULTY_DATA_DIR") or "data")


def state_dir() -> Path:
    return Path(os.environ.get("FACULTY_STATE_DIR") or data_dir())


def state_file(name: str) -> Path:
    """Path of a saved-state file ("exam_duties.json") in the state folder."""

    return state_dir() / name


def discover_timetable_files(directory=None, extensions=TIMETABLE_EXTENSIONS):
    """Every timetable file directly inside `directory`, sorted by name."""

    folder = Path(directory) if directory else data_dir()

    if not folder.is_dir():
        return []

    found = [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in extensions
        and not p.name.startswith(("~", "."))
    ]

    return sorted(found, key=lambda p: p.name.lower())


def list_timetable_pdfs(directory=None):
    """PDFs in the folder, newest first (the current semester's on top)."""

    pdfs = discover_timetable_files(directory, extensions=(".pdf",))

    return sorted(pdfs, key=lambda p: p.stat().st_mtime, reverse=True)


def latest_timetable_pdf(directory=None):
    """
    The PDF to use when none is named: $FACULTY_TIMETABLE_PDF, else the most
    recently modified PDF in the data folder.  None when there is none.
    """

    explicit = os.environ.get("FACULTY_TIMETABLE_PDF")

    if explicit and Path(explicit).is_file():
        return Path(explicit)

    pdfs = list_timetable_pdfs(directory)

    return pdfs[0] if pdfs else None