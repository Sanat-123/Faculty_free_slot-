# 🎓 Faculty Free Slot Identification

NLP-powered chatbot that identifies free faculty, teachers, subjects, rooms and
timetables from **faculty timetable PDFs**.

## ✨ PDF Chatbot (upload a PDF and chat)

The quickest way to use the project: upload a faculty-wise timetable PDF and ask
questions in plain English.

```bash
pip install -r requirements.txt
streamlit run pdf_chatbot_app.py
```

Then open **http://localhost:8501** in your browser.

- 📄 Upload any faculty-wise timetable PDF (one page per teacher, starting with
  `Teacher <Name>`, with a Mo–Sa / slot 1–8 table — same format as
  `data/Facultywise TT 20 sep.pdf`)
- 💬 Or click **📥 Load sample PDF** to try it instantly with the bundled timetable
- 🤖 Ask things like:
  - *"Who is free on Monday slot 3?"*
  - *"Who teaches Python?"*
  - *"Show timetable of 3CS-DS-A"*
  - *"Where is Python for DS Lab?"*
  - *"Subjects of Dr. Pankaj Dadheech"*
  - *"Which rooms are free on Tuesday slot 4?"*
  - *"Who is busy on Friday slot 2?"*

### How it works

```
Uploaded PDF
   │  pdf_pipeline.parse_faculty_pdf()   (pdfplumber + CellParser)
   ▼
{teacher: {day: [{slot, subject, room, class, group, type}]}}
   │  pdf_pipeline.build_database()      (fresh SQLite DB, same schema as faculty.db)
   ▼
temporary .db  ── pdf_pipeline.activate()  re-points database.db_manager.DB_FILE
   │
   ▼
Your question  ── engine pipeline ──►  QueryTokenizer → StopWordFilter
                                        → DaySlotExtractor → EntityExtractor
                                        → IntentDetector → QueryPlanner
                                        → ResponseGenerator  →  answer
```

The uploaded PDF is parsed into a **temporary** SQLite database, so the
project's own `database/faculty.db` and JSON files are never modified.

## 🧠 Data-driven question answering (SmartQueryEngine)

Every question is first offered to `engine/smart_query.py`. Everything it
understands is **learned from the loaded timetable** - days, slots and their
clock times, faculty, subjects, classes, rooms, groups - so it works for any
college's timetable. The only language knowledge is in
`config/nlu_lexicon.json` (generic English/Hinglish cue words; edit it to add
synonyms or another language).

| File | Role |
|------|------|
| `engine/timetable_model.py` | All indexes built from the events (free/busy per cell, class/room/subject views, sessions, conflicts) |
| `engine/smart_query.py` | Parsing (entities, day, slot, ranges, times, relative days, typos) + routing + follow-up context |
| `engine/smart_handlers_faculty.py` | Free/busy lists, one teacher, comparisons, rankings, meeting / make-up planning |
| `engine/smart_handlers_catalog.py` | Subjects, classes, rooms, labs, conflicts, data-quality report |
| `utils/faculty_names.py` | Structural name rules (placeholders, merged names) |

Questions it does not own (absence & substitutes, exam duty, lab shifts,
what-if, workload dashboards) fall through to the original pipeline.

**Definitions used in every answer**

* *Free* = no scheduled class in that day/slot. A scheduled class always wins
  over a "free" record in a source file.
* *Period* = one distinct slot (parallel sections count once);
  *class entry* = each section counted separately.
* *Lab session* = a continuous run of lab periods on one day.
* Entries that are not people - initials-only codes, or names with a digit
  (e.g. `AS`, `XE2`) - are kept in schedules but never reported as free/busy
  faculty or offered as substitutes; a name that is two other names glued
  together is split back into the two people. Ask *"data quality report"* to
  see what was detected in the loaded data.
* Rankings and "best slot" questions only consider slots in which classes
  actually run.

**Follow-ups** ("What about slot 4?", "Only Tuesday.", "Where is it held?",
"Which of them teach labs?") use the previous question for 30 minutes
(`context_ttl_minutes` in the lexicon).

## 🧪 Tests

```bash
python test_pdf_chatbot_app.py     # end-to-end chatbot test (Streamlit AppTest)
python pdf_pipeline.py             # pipeline smoke test on the sample PDF
python test_query_planner.py       # core NLP engine test
```

### End-to-end question suite

```bash
python3 test_timetable_questions.py     # or: pytest test_timetable_questions.py
```

Asks every kind of coordinator question (free/busy, multi-slot, one teacher,
subject, class, room, lab, rankings, conflicts, follow-ups, messy input) and
checks each answer against ground truth computed independently from the raw
events. It runs on the bundled timetable **and** on a second, completely
different synthetic timetable, and picks every teacher/class/room/day/slot
from the loaded data - nothing is hard-coded.

## 🗂️ Project structure (main pieces)

| Path            | Purpose                                                |
|-----------------|--------------------------------------------------------|
| `pdf_chatbot_app.py` | Streamlit chat UI with PDF upload (the chatbot)   |
| `pdf_pipeline.py`    | PDF → SQLite ingestion + chat answering pipeline   |
| `parser/`           | PDF reading and timetable cell parsing/cleaning      |
| `database/`         | SQLite `faculty.db`, repositories, knowledge loader  |
| `engine/`           | NLP pipeline + the data-driven SmartQueryEngine      |
| `config/`           | `nlu_lexicon.json` - language cue words (no data)    |
| `data/`             | Sample timetable PDFs                                |
| `chatbot/`          | Earlier chatbot experiments (analytics / CLI bots)   |