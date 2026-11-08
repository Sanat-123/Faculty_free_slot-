# exam_management - college-wide, fairness-based examination duty management

Extends the Faculty Free Slot Identification System. Nothing existing was removed:
the old single-day exam-duty flow, free-slot queries and all JSON stores still work.
Single-day requests stay with the old flow; **multi-day / examination-period** requests,
exclusions, reports and approvals are handled here.

## How it fits

```
chatbot (faculty_chatbot.py / pdf_pipeline.py)
   └─ ExamManagementHandler.handle()   -> None for anything it does not own
        ├─ nlu.py          natural language -> structured requests (names only from the roster)
        ├─ service.py      generate / approve / reallocate / validate   (no chatbot needed)
        │    ├─ eligibility.py  who may take which date+shift, and WHY (runs BEFORE fairness)
        │    ├─ restrictions.py exclusions, bulk, restore, admin policy, audit
        │    ├─ fairness.py     targets from history + current load + eligible opportunities
        │    └─ optimizer.py    CP-SAT (+ constructive start + local search), controlled randomness
        ├─ reports.py      reports + explanations built from stored records
        └─ repository.py / db.py   SQLite, versioned additive migrations
providers.py adapts this project's timetable model, department index and duty sheet.
```

## Database
Separate file `<state dir>/exam_management.db` (created automatically; `faculty.db` and the JSON
stores are never modified). Migrations are additive and idempotent (`exm_schema_version`);
run `python -c "from exam_management import db; c=db.connect('path'); print(db.migrate(c))"`
or simply start the chatbot. Tables: `exm_faculty, exm_history, exm_session, exm_session_date,
exm_shift, exm_room, exm_admin_responsibility, exm_restriction, exm_commitment, exm_allocation,
exm_duty, exm_unfilled, exm_audit, exm_settings`.

* Historical = imported duty sheet (replaced per source, never added twice) + `completed` duties.
* Current = duties of the allocation being built; other sessions' approved duties are "committed".
* Draft / superseded / cancelled duties never count as history.

## Fairness in one paragraph
Eligibility is computed first (exclusions, leave, shifts, existing duties, timetable when classes are
running). Each person's capacity = eligible slots (capped by the maximum). Targets come from water-filling
the combined load (historical + committed + new) over those capacities, blended with a share proportional
to eligible slots, with the minimum as a floor. The objective then minimises squared distance to target,
minimum shortfall, consecutive days / short rest, same-day repeats, concentration, changes to kept
assignments and repeats of the previous version. Weights: `config.OptimizerWeights` (override via
`EXAM_MGMT_SETTINGS_FILE` JSON or the `exm_settings` table). Randomness is a tiny per-(person, slot) cost,
so only comparable candidates are reordered; a seed reproduces a result.

## Example messages
* `Allocate examination duties from 10 November to 20 November. We have 15 examination rooms. Assign 2 invigilators per room. Minimum 2 and maximum 4 duties. Morning shift 9 am to 12 pm, afternoon shift 2 pm to 5 pm. Regular classes are suspended.`
* `Exclude Dr. A and Prof. B from the entire examination period because they are handling administrative responsibilities.`
* `Dr. C is unavailable on 12 November, Prof. D is unavailable from 13 November to 15 November.`
* `Dr. E is unavailable during the morning shift on 12 November but is available in the afternoon.`
* `Make Dr. A available again from 16 November.`   `Remove the temporary exclusion for Prof. B.`
* `Dr. F is HOD.` then `Exclude faculty handling administrative responsibilities (only HODs).`
* `Generate another allocation with a different faculty distribution.`   `approve`
* `Remove Dr. G from 13 November because of an urgent meeting.`   (add *allow wider changes* if needed)
* `Show department-wise distribution` / `show faculty with consecutive duties` / `who has no duties` /
  `show unfilled rooms` / `why did X receive more duties than Y` / `show full report`

## Run the tests
```
pip install -r requirements.txt pytest
python -m pytest tests/exam_management -q
```