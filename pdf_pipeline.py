"""
=====================================================================
PDF Timetable Ingestion + Chat Answering Pipeline
=====================================================================
Parses a faculty-wise timetable PDF (same format as
``data/Facultywise TT 20 sep.pdf``) into a fresh SQLite database and
activates it for the NLP engine.

How it works
------------
1. ``parse_faculty_pdf``  -> reads the uploaded PDF with pdfplumber,
   finds one "Teacher <name>" header per page, extracts the day/slot
   table and cleans every cell with the project's own CellParser.
2. ``build_database``     -> writes the entries into a brand-new
   SQLite database (same schema as database/faculty.db).
3. ``activate``           -> re-points database.db_manager.DB_FILE
   (and the small engines that keep their own copy of the path) at
   the new database, so every repository / matcher / engine works
   against the uploaded PDF without touching the project files.
4. ``answer_query``       -> runs the existing NLP pipeline
   (QueryTokenizer -> StopWordFilter -> DaySlotExtractor ->
   EntityExtractor -> IntentDetector -> QueryPlanner ->
   ResponseGenerator) and returns a ready-to-render answer.

Usage
-----
    from pdf_pipeline import parse_faculty_pdf, build_database, activate, answer_query

    database = parse_faculty_pdf(pdf_bytes)      # {teacher: {day: [...]}}
    db_path, stats = build_database(database)    # fresh SQLite file
    activate(db_path)                            # engine now uses it
    answer = answer_query("Who is free on Monday slot 3?")
"""

from __future__ import annotations

import io
import os
import re
import sqlite3
import tempfile

import pdfplumber

from parser.data_cleaner import parse_cell
from utils.validator import is_valid_teacher

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

DAY_MAP = {
    "Mo": "Monday",
    "Tu": "Tuesday",
    "We": "Wednesday",
    "Th": "Thursday",
    "Fr": "Friday",
    "Sa": "Saturday",
}

# "Teacher Dr. Mehul Mahrishi" appears at the top of every page
TEACHER_HEADER = re.compile(r"Teacher\s+(.+)", re.IGNORECASE)

SLOTS = range(1, 9)  # the timetable has 8 slots per day

TIMETABLE_SCHEMA = """
CREATE TABLE timetable(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    teacher TEXT,
    day TEXT,
    slot INTEGER,
    subject TEXT,
    room TEXT,
    class_name TEXT,
    group_name TEXT,
    type TEXT
)
"""

# Extra intents handled on top of engine.intent_detector
FREE_ROOM_KEYWORDS = (
    "free room",
    "room free",
    "free classroom",
    "classroom free",
    "free rooms",
    "rooms free",
    "which room is free",
    "which rooms are free",
    "available room",
    "room available",
)

BUSY_KEYWORDS = ("busy", "occupied", "not free", "not available")


# ------------------------------------------------------------------
# 1. PDF Parsing
# ------------------------------------------------------------------

def parse_faculty_pdf(pdf_bytes: bytes) -> dict:
    """
    Parse a faculty-wise timetable PDF into:

        {teacher: {"Monday": [{slot, subject, room, class, group, type}, ...], ...}}

    Raises ValueError when the PDF does not look like a faculty
    timetable (no "Teacher <name>" headers found).
    """
    database = {}
    teacher_pages = 0

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:

        for page in pdf.pages:

            text = page.extract_text()

            if not text:
                continue

            match = TEACHER_HEADER.search(text)

            if not match:
                continue

            teacher = match.group(1).strip()

            if not is_valid_teacher(teacher):
                continue

            teacher_pages += 1

            # One page = one teacher's timetable
            days = {day: [] for day in DAY_MAP.values()}
            database.setdefault(teacher, days)

            tables = page.extract_tables()

            if not tables:
                continue

            table = tables[0]

            for row in table[1:]:

                if not row or not row[0]:
                    continue

                day = str(row[0]).strip()

                if day not in DAY_MAP:
                    continue

                full_day = DAY_MAP[day]

                for slot in SLOTS:

                    if slot >= len(row):
                        continue

                    cell = row[slot]

                    if not cell:
                        continue

                    parsed = parse_cell(cell)

                    if not parsed or not parsed.get("subject", "").strip():
                        continue

                    database[teacher][full_day].append({
                        "slot": slot,
                        **parsed,
                    })

    if not database:
        raise ValueError(
            "Could not find any faculty timetable in this PDF. "
            "Please upload a faculty-wise timetable PDF (one page per "
            "teacher, starting with 'Teacher <Name>' and a Mo-Sa slot "
            "table), like the sample 'Facultywise TT 20 sep.pdf'."
        )

    return database


# ------------------------------------------------------------------
# 2. SQLite Build
# ------------------------------------------------------------------

def build_database(database: dict, db_path: str | None = None):
    """
    Write the parsed timetable into a fresh SQLite database.

    Returns (db_path, stats) where stats contains counts such as
    teachers, records, subjects and classes.
    """
    if db_path is None:
        fd, db_path = tempfile.mkstemp(prefix="faculty_upload_", suffix=".db")
        os.close(fd)

    connection = sqlite3.connect(db_path)
    cursor = connection.cursor()

    cursor.execute("DROP TABLE IF EXISTS timetable")
    cursor.execute(TIMETABLE_SCHEMA)

    count = 0
    subjects = set()
    classes = set()

    for teacher, days in database.items():

        for day, lectures in days.items():

            for lecture in lectures:

                subject = lecture.get("subject", "").strip()
                class_name = lecture.get("class", "").strip()

                cursor.execute(
                    """
                    INSERT INTO timetable(
                        teacher, day, slot, subject,
                        room, class_name, group_name, type
                    )
                    VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        teacher,
                        day,
                        lecture["slot"],
                        subject,
                        lecture.get("room", "").strip(),
                        class_name,
                        lecture.get("group", "").strip(),
                        lecture.get("type", "").strip(),
                    ),
                )

                count += 1

                if subject:
                    subjects.add(subject)
                if class_name:
                    classes.add(class_name)

    connection.commit()
    connection.close()

    stats = {
        "teachers": len(database),
        "records": count,
        "subjects": len(subjects),
        "classes": len(classes),
    }

    return db_path, stats


# ------------------------------------------------------------------
# 3. Activation
# ------------------------------------------------------------------

# The data-driven query engine built from the ACTIVE (uploaded) database.
_SMART_ENGINE = None


def activate(db_path: str) -> None:
    """
    Point every database reader in the project at the new database.

    All repositories go through database.db_manager.execute_query,
    which reads the module-level DB_FILE at call time, so patching
    that one path is enough for the whole NLP engine.
    """
    import database.db_manager as db_manager

    db_manager.DB_FILE = db_path

    # engine.free_slot_engine keeps its own copy of the path
    # (engine.classroom_engine is NOT imported here because it runs
    # interactive code at module level; its query is inlined below)
    try:
        import engine.free_slot_engine as free_slot_engine
        free_slot_engine.DB_FILE = db_path
    except Exception:
        pass

    # Build the data-driven query engine from the very same database, so
    # every answer about the uploaded PDF comes from that PDF's data.
    global _SMART_ENGINE

    try:
        from engine.smart_query import SmartQueryEngine
        from engine.timetable_model import TimetableModel

        _SMART_ENGINE = SmartQueryEngine(
            TimetableModel.from_sqlite(db_path)
        )
    except Exception:
        _SMART_ENGINE = None


# ------------------------------------------------------------------
# 4. Chat Answering
# ------------------------------------------------------------------

def answer_query(query: str, entity_extractor=None) -> dict:
    """
    Run one chat question through the NLP pipeline.

    Returns a dict with:
        query, intent, day, slot, entities, rows, text, detection
    """
    from engine.query_tokenizer import QueryTokenizer
    from engine.stopword_filter import StopWordFilter
    from engine.day_slot_extractor import DaySlotExtractor
    from engine.entity_extractor import EntityExtractor
    from engine.intent_detector import IntentDetector
    from engine.query_planner import QueryPlanner
    from engine.response_generator import ResponseGenerator
    from database.knowledge_loader import KnowledgeLoader
    from database.db_manager import execute_query
    from database.timetable_repository import TimetableRepository

    # --------------------------------------------------------------
    # 1. The data-driven engine answers first (built from the active
    #    database in activate()).  If it understood the question the
    #    result is returned in the same dict shape as before.
    # --------------------------------------------------------------

    if _SMART_ENGINE is not None:

        smart_text = _SMART_ENGINE.answer(query)

        if smart_text is None:
            smart_text = _SMART_ENGINE.fallback_text()
            smart_intent = "UNKNOWN"
        else:
            smart_intent = str(
                _SMART_ENGINE.last_route or "SMART"
            ).upper()

        return {
            "query": query,
            "intent": smart_intent,
            "day": None,
            "slot": None,
            "entities": {},
            "rows": [],
            "text": smart_text,
            "detection": {
                "intent": smart_intent,
                "day": None,
                "slot": None,
                "entities": {},
            },
        }

    tokens = QueryTokenizer.tokenize(query)
    filtered = StopWordFilter.filter(tokens)

    day_slot = DaySlotExtractor.extract(filtered)

    extractor = entity_extractor or EntityExtractor()
    entities = extractor.extract(day_slot["remaining_tokens"])

    lowered = query.lower()

    # --------------------------------------------------------------
    # Extra intents checked BEFORE the base detector, because the
    # base detector would otherwise classify "free room" queries as
    # free-faculty queries (both contain the word "free").
    # --------------------------------------------------------------

    if (
        any(k in lowered for k in BUSY_KEYWORDS)
        and (day_slot["day"] or day_slot["slot"])
    ):
        intent = "FIND_BUSY_FACULTY"

    elif (
        any(k in lowered for k in FREE_ROOM_KEYWORDS)
        and (day_slot["day"] or day_slot["slot"])
    ):
        intent = "FIND_FREE_ROOM"

    else:

        intent = IntentDetector.detect(tokens, entities, day_slot)

        # ----------------------------------------------------------
        # Friendlier fallbacks when nothing was detected
        # ----------------------------------------------------------

        if intent == "UNKNOWN":

            if "timetable" in lowered or "schedule" in lowered:
                intent = "ASK_TIMETABLE_WHOM"

            elif any(k in lowered for k in ("free", "available", "vacant")):
                intent = "ASK_DAY_SLOT"

    # --------------------------------------------------------------
    # FIND FREE ROOM (bonus intent using engine.classroom_engine)
    # --------------------------------------------------------------

    if intent == "FIND_FREE_ROOM":

        if not day_slot["day"] or not day_slot["slot"]:

            text = (
                "Please tell me both the **day** and the **slot**, "
                "for example:\n\n"
                "• Which rooms are free on Tuesday slot 4?"
            )

            rows = []

        else:

            # Same query as engine.classroom_engine.find_busy_rooms
            # (inlined here because that module runs code on import)
            busy = set(
                row[0]
                for row in execute_query(
                    """
                    SELECT DISTINCT room
                    FROM timetable
                    WHERE day = ?
                    AND slot = ?
                    AND room != ''
                    ORDER BY room
                    """,
                    (day_slot["day"], day_slot["slot"]),
                )
            )
            all_rooms = set(KnowledgeLoader.get_rooms())
            rows = sorted(all_rooms - busy)

            text = (
                f"🪑 **Free rooms on {day_slot['day']} slot {day_slot['slot']} "
                f"({len(rows)}):**\n\n"
                + ("\n".join(f"• {room}" for room in rows) if rows else "_None_")
            )

    # --------------------------------------------------------------
    # FIND BUSY FACULTY (bonus intent)
    # --------------------------------------------------------------

    elif intent == "FIND_BUSY_FACULTY":

        if not day_slot["day"] or not day_slot["slot"]:

            text = (
                "Please tell me both the **day** and the **slot**, "
                "for example:\n\n"
                "• Who is busy on Friday slot 2?"
            )

            rows = []

        else:

            rows = sorted({
                row[0]
                for row in TimetableRepository.find({
                    "teacher": None,
                    "subject": None,
                    "class": None,
                    "group": None,
                    "room": None,
                    "day": day_slot["day"],
                    "slot": day_slot["slot"],
                })
            })

            text = (
                f"📌 **Busy faculty on {day_slot['day']} slot {day_slot['slot']} "
                f"({len(rows)}):**\n\n"
                + ("\n".join(f"• {teacher}" for teacher in rows) if rows else "_None_")
            )

    # --------------------------------------------------------------
    # ASK_TIMETABLE_WHOM (friendly prompt instead of silence)
    # --------------------------------------------------------------

    elif intent == "ASK_TIMETABLE_WHOM":

        rows = []

        text = (
            "Whose timetable would you like to see? 😊\n\n"
            "• Show timetable of **3CS-DS-A**\n"
            "• Show timetable of **Dr. Pankaj Dadheech**"
        )

    # --------------------------------------------------------------
    # ASK_DAY_SLOT (free/available mentioned without day+slot)
    # --------------------------------------------------------------

    elif intent == "ASK_DAY_SLOT":

        rows = []

        text = (
            "I need both the **day** and the **slot** for that, "
            "for example:\n\n"
            "• Who is free on **Monday slot 3**?\n"
            "• Available faculty **Tuesday 5**"
        )

    # --------------------------------------------------------------
    # FIND FREE FACULTY without day/slot
    # --------------------------------------------------------------

    elif intent == "FIND_FREE_FACULTY" and (
        not day_slot["day"] or not day_slot["slot"]
    ):

        rows = []

        text = (
            "I need both the **day** and the **slot** to find free "
            "faculty, for example:\n\n"
            "• Who is free on **Monday slot 3**?\n"
            "• Available faculty **Tuesday 5**"
        )

    # --------------------------------------------------------------
    # UNKNOWN
    # --------------------------------------------------------------

    elif intent == "UNKNOWN":

        rows = []

        text = (
            "🤔 I couldn't understand that question. Try asking:\n\n"
            "• Who teaches **Python**?\n"
            "• Who is free on **Monday slot 3**?\n"
            "• Show timetable of **3CS-DS-A**\n"
            "• Where is **Python for DS Lab**?\n"
            "• Subjects of **Dr. Pankaj Dadheech**\n"
            "• Which rooms are free on **Tuesday slot 4**?"
        )

    # --------------------------------------------------------------
    # Core intents -> existing engine pipeline
    # --------------------------------------------------------------

    else:

        rows = QueryPlanner.plan(intent, entities, day_slot)
        text = ResponseGenerator.generate(intent, rows)

    # --------------------------------------------------------------
    # Detection summary (shown in the UI for transparency)
    # --------------------------------------------------------------

    detection = {
        "intent": intent,
        "day": day_slot["day"],
        "slot": day_slot["slot"],
        "entities": {
            entity_type: [item["value"] for item in entity_list]
            for entity_type, entity_list in entities.items()
            if entity_list
        },
    }

    return {
        "query": query,
        "intent": intent,
        "day": day_slot["day"],
        "slot": day_slot["slot"],
        "entities": detection["entities"],
        "rows": rows,
        "text": text,
        "detection": detection,
    }


# ------------------------------------------------------------------
# Quick command-line test: python3 pdf_pipeline.py
# ------------------------------------------------------------------

if __name__ == "__main__":

    sample = os.path.join("data", "Facultywise TT 20 sep.pdf")

    with open(sample, "rb") as file:
        data = parse_faculty_pdf(file.read())

    db_path, stats = build_database(data)
    activate(db_path)

    print("Parsed teachers :", stats["teachers"])
    print("Records         :", stats["records"])
    print("Subjects        :", stats["subjects"])
    print("Classes         :", stats["classes"])
    print("Database        :", db_path)
    print()

    for question in [
        "Who teaches Python?",
        "Who is free on Monday slot 3?",
        "Show timetable of 3CS-DS-A",
        "Where is Python for DS Lab?",
        "Subjects of Dr. Pankaj Dadheech",
        "Which rooms are free on Tuesday slot 4?",
    ]:
        answer = answer_query(question)
        print("=" * 70)
        print("Q:", question)
        print("Intent:", answer["intent"], "| Day:", answer["day"],
              "| Slot:", answer["slot"])
        print(answer["text"][:400])
        print()