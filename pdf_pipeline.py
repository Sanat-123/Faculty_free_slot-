"""
=====================================================================
PDF Timetable Ingestion + Chat Answering Pipeline
=====================================================================
Parses a faculty-wise timetable PDF (any file; nothing here knows a file
name) into a fresh SQLite database and activates it for the NLP engine.

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
import sys
import re
import sqlite3
import tempfile

import pdfplumber

from parser.data_cleaner import parse_cell            # legacy rule-based parser
from parser.learned_cell_parser import CellVocabulary  # document-learned parser
from utils.validator import is_valid_teacher

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

def _language_resource(name, default):
    """Read a generic word list from config/nlu_lexicon.json.

    Calendar names and header words are language resources, not timetable
    data: edit the JSON to support another language or PDF layout.
    """

    import json

    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "config", "nlu_lexicon.json",
    )

    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get(name) or default
    except Exception:
        return default


def _teacher_header_regex():
    """\"Teacher <Name>\" (or another configured header word) + the name."""

    words = _language_resource("teacher_header_words", ["Teacher"])

    return re.compile(
        r"(?:" + "|".join(re.escape(w) for w in words) + r")\s*[:\-]?\s*(.+)",
        re.IGNORECASE,
    )


def _day_from_label(label, weekdays):
    """'Mo' / 'Mon' / 'Monday' / 'Thurs' -> 'Monday' (None if not a day).

    A label is a day when it is a prefix of a weekday name and shares the
    weekday's first two letters, so Tu/Th and Sa/Su stay distinct.
    """

    text = re.sub(r"[^a-z]", "", str(label or "").lower())

    if len(text) < 2:
        return None

    for weekday in weekdays:

        name = weekday.lower()

        if name.startswith(text) or (
            text.startswith(name[:2]) and name.startswith(text[:3])
        ):
            return weekday

    return None


CLOCK_RANGE = re.compile(r"\d{1,2}[:.]\d{2}\s*[-\u2013to ]+\s*\d{1,2}[:.]\d{2}")


def _slot_columns(header_row):
    """
    Read the slot numbers (and clock times) from the table's header row.

    Returns {column_index: (slot_number, "8:15 - 9:15" or "")}.  Slot numbers
    come from the header text ("1\n8:15 - 9:15" -> 1); when a header cell
    has no number the column position is used.  Nothing about the number of
    slots is assumed.
    """

    columns = {}

    for index, cell in enumerate(header_row or []):

        if index == 0:
            continue

        text = str(cell or "").strip()

        number = re.match(r"\s*(\d+)", text)
        clock = CLOCK_RANGE.search(text)

        slot = int(number.group(1)) if number else index

        columns[index] = (slot, clock.group(0).strip() if clock else "")

    return columns


TIMETABLE_SCHEMA = """
CREATE TABLE timetable(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    teacher TEXT,
    day TEXT,
    slot INTEGER,
    slot_time TEXT,
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
    pending = []
    teacher_pages = 0

    weekdays = _language_resource(
        "weekdays",
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
         "Saturday", "Sunday"],
    )
    header = _teacher_header_regex()

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:

        for page in pdf.pages:

            text = page.extract_text()

            if not text:
                continue

            match = header.search(text)

            if not match:
                continue

            teacher = match.group(1).strip()

            if not is_valid_teacher(teacher):
                continue

            teacher_pages += 1

            # One page = one teacher's timetable
            teacher_days = database.setdefault(teacher, {})

            tables = page.extract_tables()

            if not tables:
                continue

            table = tables[0]

            slot_columns = _slot_columns(table[0] if table else [])

            for row in table[1:]:

                if not row or not row[0]:
                    continue

                full_day = _day_from_label(row[0], weekdays)

                if full_day is None:
                    continue

                lectures = teacher_days.setdefault(full_day, [])

                for column, (slot, clock) in slot_columns.items():

                    if column >= len(row):
                        continue

                    cell = row[column]

                    if not cell or not str(cell).strip():
                        continue

                    # parsed after the whole PDF is read (see below)
                    pending.append((lectures, slot, clock, cell))

    # Learn the document's own vocabulary (rooms, class spellings, group
    # marker) from ALL its cells, then parse every cell with it.  No room
    # list, class-code format or department is assumed.
    # FACULTY_LEGACY_CELL_PARSER=1 switches back to the old rule-based parser.
    if os.environ.get("FACULTY_LEGACY_CELL_PARSER") == "1":
        parse_one = parse_cell
    else:
        vocabulary = CellVocabulary(
            [cell for _, _, _, cell in pending],
            type_keywords=_language_resource("type_keywords", None),
        )
        parse_one = vocabulary.parse

    for lectures, slot, clock, cell in pending:

        parsed = parse_one(cell)

        if not parsed or not parsed.get("subject", "").strip():
            continue

        lectures.append({
            "slot": slot,
            "slot_time": clock,
            **parsed,
        })

    if not database:
        raise ValueError(
            "Could not find any faculty timetable in this PDF. "
            "Please upload a faculty-wise timetable PDF (one page per "
            "teacher, with a 'Teacher <Name>' heading and a table of "
            "days by slots). "
            "Header words and day names can be extended in "
            "config/nlu_lexicon.json."
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
                        teacher, day, slot, slot_time, subject,
                        room, class_name, group_name, type
                    )
                    VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        teacher,
                        day,
                        lecture["slot"],
                        lecture.get("slot_time", ""),
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

# Optional LLM front end (engine/llm_layer.py) wrapped around _SMART_ENGINE.
# None unless ANTHROPIC_API_KEY is set; the rule-based engine is the fallback.
_LLM = None


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
    global _SMART_ENGINE, _LLM

    _LLM = None

    try:
        from engine.smart_query import SmartQueryEngine
        from engine.timetable_model import TimetableModel

        _SMART_ENGINE = SmartQueryEngine(
            TimetableModel.from_sqlite(db_path)
        )
    except Exception:
        _SMART_ENGINE = None

    # AI conversation layer (optional): understands any wording and answers
    # in natural language, using the engine above as its only data source.
    try:
        from engine.llm_layer import LLMAssistant

        if _SMART_ENGINE is not None and LLMAssistant.available():
            _LLM = LLMAssistant(_SMART_ENGINE)
    except Exception:
        _LLM = None


def find_faculty_pdf(directory=None):
    """
    The PDF to use when none is named: $FACULTY_TIMETABLE_PDF, else the newest
    PDF in the data folder that really IS a faculty-wise timetable (a folder
    usually also holds class-wise / room-wise PDFs, which are skipped).
    """

    from utils.file_discovery import list_timetable_pdfs

    explicit = os.environ.get("FACULTY_TIMETABLE_PDF")

    if explicit and os.path.isfile(explicit):
        return explicit

    for path in list_timetable_pdfs(directory):

        try:
            with open(path, "rb") as fh:
                parse_faculty_pdf(fh.read())
            return str(path)
        except Exception:
            continue

    return None


def example_questions(limit: int = 6) -> list:
    """Example questions built from the CURRENTLY LOADED timetable.

    Real teacher / class / subject names come from the data; before anything
    is loaded generic placeholders are returned.
    """

    if _SMART_ENGINE is not None:
        try:
            found = _SMART_ENGINE.examples()
            if found:
                return found[:limit]
        except Exception:
            pass

    return [
        "Who is free on <day> slot <n>?",
        "Is <teacher> free on <day> slot <n>?",
        "Show timetable of <teacher or class>",
        "Who teaches <subject>?",
        "Which rooms are free on <day> slot <n>?",
    ][:limit]


def _examples_bullets(limit: int = 6) -> str:

    return "\n".join(f"• {q}" for q in example_questions(limit))


def ai_enabled() -> bool:
    """True when answers go through the AI conversation layer."""

    return _LLM is not None


# ------------------------------------------------------------------
# 4. Chat Answering
# ------------------------------------------------------------------

def answer_query(query: str, entity_extractor=None, history=None) -> dict:
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

    if _LLM is not None:

        # Conversational path. Any failure (network, quota, bad reply)
        # silently falls through to the rule-based engine below.
        try:

            reply = _LLM.answer(query, history)

            return {
                "query": query,
                "intent": "AI_ASSISTANT",
                "day": None,
                "slot": None,
                "entities": {"Asked the timetable": reply["queries"]}
                if reply["queries"] else {},
                "rows": [],
                "text": reply["text"],
                "detection": {
                    "intent": "AI_ASSISTANT",
                    "day": None,
                    "slot": None,
                    "entities": {"Asked the timetable": reply["queries"]}
                    if reply["queries"] else {},
                },
            }

        except Exception:
            pass

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
            "Try, for example:\n" + _examples_bullets(4)
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
            + _examples_bullets()
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

    # PDF = first command-line argument, else $FACULTY_TIMETABLE_PDF, else
    # the newest faculty-wise PDF in the data folder; no file name is
    # built into the code
    sample = sys.argv[1] if len(sys.argv) > 1 else find_faculty_pdf()

    if not sample:
        sys.exit(
            "No timetable PDF found. Usage: python pdf_pipeline.py "
            "<timetable.pdf>   (or put a PDF in the data folder)"
        )

    print("Using:", sample)

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

    for question in example_questions():
        answer = answer_query(question)
        print("=" * 70)
        print("Q:", question)
        print("Intent:", answer["intent"], "| Day:", answer["day"],
              "| Slot:", answer["slot"])
        print(answer["text"][:400])
        print()