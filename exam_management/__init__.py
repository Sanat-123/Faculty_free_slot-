"""
exam_management - college-wide, fairness-based examination duty management.

Layers (each usable without the chatbot):

    config.py        central, overridable settings (weights, limits, policies)
    db.py            SQLite schema + safe, versioned migrations + transactions
    providers.py     adapters to the outside world (faculty roster, timetable,
                     historical duty counts) - the only part that touches the
                     existing project
    repository.py    all SQL (sessions, restrictions, allocations, audit ...)
    restrictions.py  exclusion / availability / restoration (bulk, audited)
    eligibility.py   who may be assigned to which date + shift, and WHY
    fairness.py      workload targets + measurable fairness metrics
    optimizer.py     CP-SAT allocation engine (pure data in, pure data out)
    service.py       orchestration: generate, regenerate, approve, reallocate
    reports.py       reports + explanations built only from stored records
    nlu.py           natural-language -> structured requests (no invention)
    chat_handler.py  the chatbot integration
"""

from .config import ExamSettings, OptimizerWeights, load_settings  # noqa: F401