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

## New semester = new files, no code changes

No file name is written anywhere in the code. Files are found by looking in a folder:

| Setting (environment variable) | Meaning | Default |
|---|---|---|
| `FACULTY_DATA_DIR` | folder holding the timetable files (PDF / Excel / CSV) | `data` |
| `FACULTY_STATE_DIR` | folder for saved exam duties, room shifts, assignments | same as the data folder |
| `FACULTY_TIMETABLE_PDF` | use exactly this PDF (optional) | newest faculty-wise PDF in the data folder |

Typical semester change: create a folder for the new semester, drop the new
files in it (any names), and start the app with
`FACULTY_DATA_DIR=data/2027_odd` (Windows PowerShell:
`$env:FACULTY_DATA_DIR="data/2027_odd"`). Saved duties and shifts then stay
with that semester too. In the Streamlit PDF app you can also just upload the
PDF, or pick it from the data folder; the newest faculty-wise PDF is
preselected and class-wise / room-wise PDFs are skipped automatically.

## Assigning exam duty (in the chat)

Duties are tied to a **real calendar date** and a **time range**; the bot
proposes, you confirm, and only then is anything saved.

1. *See who can take it:* `Who is available for exam duty on 2026-10-12 from 9 to 11?`
2. *Let the bot choose N people* (lightest teaching workload first, fewest earlier duties):
   `Assign 3 faculty for exam duty on 2026-10-12 from 9 to 11 in room 303`
   (also: `need 4 invigilators on 12/10/2026 from 09:00 to 11:00 in hall CL-1`)
3. *Or name the people yourself* (typos and missing titles are fine):
   `Assign exam duty to Dr. Kiran Rathi and Dr. Kirti Bala on 2026-10-12 from 9 to 11`
   Anyone with a class in that window, or already on duty, is refused with the reason.
4. Reply `confirm` in the same chat to save. Saved duties go to
   `exam_duties.json` in the state folder (`FACULTY_STATE_DIR`, default `data`).
5. Review: `Show exam duties`, `Show exam duties on 2026-10-12`,
   `How many exam duties does each faculty member have?`

Dates: `2026-10-12`, `12/10/2026`, `12 October 2026`, `Oct 12, 2026`.
Times: `9 to 11`, `9:00-11:00`, `9am to 11am`, `14:00 to 16:00`; a bare `2 to 4`
is read from the timetable's own working day (14:00-16:00).
A weekday instead of a date (`exam duty on Monday from 9 to 11`) only lists
candidates; nothing can be saved without a date.

## What is data-driven (no hard-coded college data)

Everything the chatbot knows about a college comes from the PDF you upload:

| Learned from your PDF | How |
|---|---|
| Teachers | the `Teacher <Name>` heading of each page |
| Days and their order of use | the day column of each table (`Mo`, `Mon`, `Monday`, `Sun` ... any weekday, any number of days) |
| Slots and clock times | the table's header row (`1\n8:15 - 9:15`), any number of slots |
| Subject, room, class, group | `parser/learned_cell_parser.py` learns them from all cells of the document (no room list, no class-code format) |
| Example questions in the sidebar | built from the loaded teachers / classes / subjects |

Only generic **language** resources live in one editable file,
`config/nlu_lexicon.json`: weekday names, honorifics (Dr., Prof.), words such
as "free", "busy", "after", header words (`Teacher`, `Faculty Name`), and the
words that mark a Lab / Seminar / Project session. Add synonyms or another
language there; never edit code for a new college.

Assumed PDF layout: one page per teacher, a heading with the teacher's name,
a table whose first column is the day and whose header row numbers the slots,
and cells with the subject and room on the first line, the class on the next
line(s) and an optional group line (`Group 1`, `Batch 2`, ...).
Set `FACULTY_LEGACY_CELL_PARSER=1` to use the old rule-based cell parser.

## Chat in any wording (optional AI mode)

By default the app uses the built-in rule-based NLP engine. To let people
ask in *any* wording (typos, Hinglish, follow-ups such as "what about
tomorrow?", multi-part questions) set an API key before starting:

```
export ANTHROPIC_API_KEY=sk-...        # Windows: set ANTHROPIC_API_KEY=sk-...
streamlit run pdf_chatbot_app.py
```

The AI only *understands and phrases*; every fact (who is free, which room,
which subject) is fetched from your timetable through the data-driven engine,
so it cannot invent teachers or slots. If the API is unreachable the app
silently falls back to the rule-based engine.

Optional: `FACULTY_LLM_MODEL` (default `claude-sonnet-5-5`),
`FACULTY_LLM_OFF=1` to force rule-based mode.

## Question bank and self-check

```
python generate_all_questions.py --pdf "data/Facultywise TT 20 sep.pdf" --out question_bank
```

Expands ~150 question patterns over every teacher, class, room, subject, day
and slot of the loaded timetable, runs them all, and checks the main answer
types against the raw data. Works on any timetable you load.

## Tests

```
python test_timetable_questions.py   # engine vs raw data, two different timetables
python test_universal_parser.py      # imports a generated PDF of an imaginary other college (pip install reportlab)
python test_llm_layer.py             # AI layer with a scripted fake model (no API key)
python test_typo_robustness.py    # every teacher x ~16 spelling mistakes, classes, rooms, ordinary-word typos
python test_exam_duty_requests.py   # propose -> confirm -> saved, named teachers, refusals, dates vs times
python test_time_questions.py     # thousands of time questions (9 to 11, 8am to 2pm, after 12, morning ...) vs the raw data; add --app to use the web app's data path
```