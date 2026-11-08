"""
SQLite storage with versioned, additive migrations.

* The exam database is its OWN file (<state folder>/<db_filename>) so the
  existing `faculty.db` timetable and the JSON stores are never modified and
  no existing record can be lost.
* Migrations only ever ADD tables/columns (CREATE TABLE IF NOT EXISTS ...),
  are recorded in `exm_schema_version`, and are safe to run repeatedly.
* Write transactions use BEGIN IMMEDIATE so two coordinators cannot interleave.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, List, Tuple

_DDL_V1 = """
CREATE TABLE IF NOT EXISTS exm_faculty (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    norm        TEXT NOT NULL,
    department  TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    source      TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);

-- imported historical totals (one row per faculty per source: re-import REPLACES)
CREATE TABLE IF NOT EXISTS exm_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    faculty_id  INTEGER NOT NULL REFERENCES exm_faculty(id),
    duties      REAL NOT NULL,
    source      TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (faculty_id, source)
);

CREATE TABLE IF NOT EXISTS exm_session (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,
    start_date           TEXT NOT NULL,
    end_date             TEXT NOT NULL,
    classes_suspended    INTEGER,                 -- NULL = not yet stated
    invigilators_per_room INTEGER NOT NULL,
    min_duties           INTEGER,
    max_duties           INTEGER,
    options              TEXT NOT NULL DEFAULT '{}',   -- json: dept filter, prefs ...
    status               TEXT NOT NULL DEFAULT 'planning'
                         CHECK (status IN ('planning','approved','completed','cancelled')),
    revision             INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exm_session_date (
    session_id INTEGER NOT NULL REFERENCES exm_session(id),
    exam_date  TEXT NOT NULL,
    PRIMARY KEY (session_id, exam_date)
);

CREATE TABLE IF NOT EXISTS exm_shift (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES exm_session(id),
    label      TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time   TEXT NOT NULL,
    position   INTEGER NOT NULL,
    UNIQUE (session_id, label)
);

CREATE TABLE IF NOT EXISTS exm_room (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES exm_session(id),
    label      TEXT NOT NULL,
    position   INTEGER NOT NULL,
    UNIQUE (session_id, label)
);

CREATE TABLE IF NOT EXISTS exm_admin_responsibility (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    faculty_id  INTEGER NOT NULL REFERENCES exm_faculty(id),
    role        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    start_date  TEXT,
    end_date    TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exm_restriction (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    faculty_id  INTEGER NOT NULL REFERENCES exm_faculty(id),
    kind        TEXT NOT NULL CHECK (kind IN
                  ('permanent','session','date','date_range','shift')),
    category    TEXT NOT NULL DEFAULT 'general',   -- administrative | leave | general
    session_id  INTEGER REFERENCES exm_session(id),
    start_date  TEXT,
    end_date    TEXT,
    shift_label TEXT,
    reason      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','restored','superseded')),
    parent_id   INTEGER REFERENCES exm_restriction(id),
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    restored_at TEXT,
    CHECK (end_date IS NULL OR start_date IS NULL OR end_date >= start_date)
);
CREATE INDEX IF NOT EXISTS ix_restriction_faculty ON exm_restriction(faculty_id, status);

CREATE TABLE IF NOT EXISTS exm_commitment (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    faculty_id  INTEGER NOT NULL REFERENCES exm_faculty(id),
    commit_date TEXT NOT NULL,
    start_time  TEXT NOT NULL,
    end_time    TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS exm_allocation (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES exm_session(id),
    version      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft','approved','superseded','cancelled','completed')),
    feasibility  TEXT NOT NULL DEFAULT 'complete'
                 CHECK (feasibility IN ('complete','partial','infeasible')),
    parent_id    INTEGER REFERENCES exm_allocation(id),
    seed         INTEGER,
    params       TEXT NOT NULL DEFAULT '{}',
    quality      TEXT NOT NULL DEFAULT '{}',
    notes        TEXT NOT NULL DEFAULT '',
    created_by   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    approved_at  TEXT,
    UNIQUE (session_id, version)
);

CREATE TABLE IF NOT EXISTS exm_duty (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    allocation_id INTEGER NOT NULL REFERENCES exm_allocation(id),
    faculty_id    INTEGER NOT NULL REFERENCES exm_faculty(id),
    exam_date     TEXT NOT NULL,
    shift_id      INTEGER NOT NULL REFERENCES exm_shift(id),
    room          TEXT NOT NULL,
    seat          INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft','approved','completed','cancelled')),
    UNIQUE (allocation_id, faculty_id, exam_date, shift_id),
    UNIQUE (allocation_id, exam_date, shift_id, room, seat)
);
CREATE INDEX IF NOT EXISTS ix_duty_faculty ON exm_duty(faculty_id, status);

CREATE TABLE IF NOT EXISTS exm_unfilled (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    allocation_id INTEGER NOT NULL REFERENCES exm_allocation(id),
    exam_date     TEXT NOT NULL,
    shift_id      INTEGER NOT NULL REFERENCES exm_shift(id),
    room          TEXT NOT NULL,
    seat          INTEGER NOT NULL,
    required      INTEGER NOT NULL,
    available     INTEGER NOT NULL,
    reason        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exm_audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    entity    TEXT NOT NULL,
    entity_id INTEGER,
    details   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS exm_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_DDL_V2 = """
-- coordinator-assigned department labels (win over labels learned from the timetable)
CREATE TABLE IF NOT EXISTS exm_dept_override (
    faculty_id INTEGER PRIMARY KEY REFERENCES exm_faculty(id),
    department TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'coordinator',
    updated_at TEXT NOT NULL
);
"""

# (version, description, ddl) - append only; never edit an applied entry
MIGRATIONS: List[Tuple[int, str, str]] = [
    (1, "initial exam-management schema", _DDL_V1),
    (2, "coordinator department overrides (CS / non-CS style groups)", _DDL_V2),
]


def _bootstrap(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS exm_schema_version ("
        "version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )


def migrate(conn: sqlite3.Connection, migrations=None) -> List[int]:
    """Apply pending migrations; returns the versions applied now."""

    migrations = MIGRATIONS if migrations is None else migrations
    _bootstrap(conn)
    done = {r[0] for r in conn.execute("SELECT version FROM exm_schema_version")}
    applied = []
    for version, desc, ddl in sorted(migrations):
        if version in done:
            continue
        with transaction(conn):
            for stmt in _split(ddl):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO exm_schema_version(version, description, applied_at) "
                "VALUES (?,?,datetime('now'))", (version, desc))
        applied.append(version)
    return applied


def _split(ddl: str) -> List[str]:
    out, cur = [], []
    for line in ddl.splitlines():
        if line.strip().startswith("--"):
            continue
        cur.append(line)
        if line.rstrip().endswith(";"):
            out.append("\n".join(cur).strip().rstrip(";"))
            cur = []
    rest = "\n".join(cur).strip()
    if rest:
        out.append(rest)
    return [s for s in out if s]


class Conn(sqlite3.Connection):
    """Connection that remembers its transaction depth."""

    _exm_depth = 0


def connect(path) -> sqlite3.Connection:
    """New connection (one per thread / unit of work)."""

    p = Path(path)
    if str(path) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None,
                           check_same_thread=False, factory=Conn)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """All-or-nothing write block (re-entrant: inner blocks join the outer)."""

    depth = getattr(conn, "_exm_depth", 0)
    if depth:
        conn._exm_depth = depth + 1  # type: ignore[attr-defined]
        try:
            yield conn
        finally:
            conn._exm_depth -= 1     # type: ignore[attr-defined]
        return
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    conn._exm_depth = 1              # type: ignore[attr-defined]
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn._exm_depth = 0          # type: ignore[attr-defined]