"""
EXAM DUTY MANAGEMENT

Helps identify, rank, and confirm faculty invigilators for an
exam session, and reports on already-confirmed exam duties.

This module does not implement its own faculty-lookup, free/busy,
or persistence primitives from scratch. It is a thin coordination
layer on top of the EXISTING engines:

    QueryEngine
        faculty_free_for_period()/faculty_status_for_period() -
        the existing clock-time/weekday free-busy logic already
        used throughout the project - reused here unchanged so a
        faculty member's regular-timetable availability is
        determined identically everywhere in the app.

    FacultyWorkloadEngine
        exam_duty_candidates() - the existing read-only candidate
        ranking (free faculty for a day/time range, ordered by
        regular teaching workload). Reused unchanged; this module
        only ADDS an exam-duty-specific fairness tiebreak and
        exam-duty-specific conflict filtering on top of it - it
        never reimplements the free/busy or workload logic.

    ExamDutyStore
        The smallest new persistence overlay: a JSON-file-backed
        list of CONFIRMED exam-duty records. Canonical timetable
        events, AssignmentStore, and RoomShiftStore are never
        touched by this module.

Nothing here hard-codes any faculty name, date, time, hall, or
duty count/limit. Every decision is derived from whatever the
query/workload engines report for the timetable data currently
loaded, plus whatever exam-duty records have actually been
confirmed.

============================================================
DATE-AWARE DESIGN
============================================================

Regular timetable data uses a recurring weekday/slot concept,
which cannot distinguish different exam weeks. An exam duty is
therefore always identified by a REAL CALENDAR DATE (exam_date,
expected as an ISO "YYYY-MM-DD" string) plus a clock start/end
time. The weekday used to check regular-timetable availability is
always DERIVED from that date (via Python's own calendar/date
handling) - it is never accepted or hard-coded separately, so it
can never disagree with the actual date.

============================================================
CONFLICTS
============================================================

A candidate is excluded from a proposal/confirmation when EITHER:

    - the regular timetable reports them busy for the derived
      weekday/time range (via FacultyWorkloadEngine.
      exam_duty_candidates(), which itself is powered by
      QueryEngine.faculty_free_for_period()), or

    - ExamDutyStore reports an already-CONFIRMED exam duty for
      that exact person overlapping the same date/time range.

A faculty member can be free from the regular timetable but still
unavailable because of an already-confirmed exam duty - both
checks are always applied together.

============================================================
FAIRNESS
============================================================

FacultyWorkloadEngine.exam_duty_candidates() already ranks
candidates by regular teaching workload (fewer daily periods =
better candidate). That ranking is preserved as the PRIMARY sort
key. This module extends it with the candidate's own accumulated
CONFIRMED exam-duty count (from ExamDutyStore) as a secondary
tiebreak - among otherwise similarly-loaded candidates, the one
with fewer existing confirmed exam duties is preferred. No
fairness threshold or faculty limit is hard-coded anywhere; the
scoring is a simple, deterministic sort.

============================================================
PLAN / CONFIRM
============================================================

plan_duty() is completely side-effect free: it never writes to
ExamDutyStore. confirm_duty() re-resolves candidate availability
FROM SCRATCH (rather than trusting the plan's earlier snapshot),
so any change to the timetable or exam-duty state between planning
and confirming - including a stale/no-longer-valid plan - is
caught rather than silently applied. Confirmation is the ONLY
operation that persists an exam-duty record.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from scheduling.exam_duty_store import ExamDutyStore


class ExamDutyCoordinator:

    def __init__(
        self,
        query_engine,
        workload_engine,
        store=None,
    ):
        self.query_engine = query_engine
        self.workload_engine = workload_engine
        self.store = store or ExamDutyStore()

    # ============================================================
    # BASIC HELPERS
    # ============================================================

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    # ============================================================
    # DATE PARSING / VALIDATION
    #
    # Accepts a real calendar date as an ISO "YYYY-MM-DD" string
    # (or an already-parsed datetime.date). Natural-language date
    # extraction from a chat message is the caller's job (see
    # faculty_chatbot.py's _extract_exam_date) - this coordinator
    # only validates and derives the weekday from a resolved date,
    # so date parsing never happens in more than one place with
    # more than one notion of what "valid" means.
    # ============================================================

    @staticmethod
    def _parse_date(value: Any):

        if value is None:
            return None

        if isinstance(value, date):
            return value

        text = str(value).strip()

        if not text:
            return None

        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            return None

    @classmethod
    def _derive_weekday(cls, exam_date) -> str:
        """
        Derive the recurring weekday name (lowercase, matching
        QueryEngine._day()'s canonical form) from an actual
        calendar date. Never hard-coded.
        """
        return exam_date.strftime("%A").lower()

    # ============================================================
    # TIME VALIDATION
    #
    # Reuses QueryEngine's own HH:MM parsing (the same primitive
    # faculty_free_for_period()/faculty_status_for_period() use)
    # so "what counts as a valid time" is never redefined here.
    # ============================================================

    def _validate_time_range(self, start_time, end_time):

        start = self.query_engine._time_to_minutes(start_time)
        end = self.query_engine._time_to_minutes(end_time)

        if start is None or end is None or end <= start:
            return None, None

        return start, end

    # ============================================================
    # CANDIDATE DISCOVERY (side-effect free)
    # ============================================================

    def candidates(
        self,
        exam_date,
        start_time,
        end_time,
    ) -> dict[str, Any]:
        """
        Return exam-duty candidates for a specific calendar date
        and clock time range, combining:

            - FacultyWorkloadEngine.exam_duty_candidates() for
              regular-timetable-based free/busy + workload rank
              (weekday derived from exam_date), and

            - ExamDutyStore for already-CONFIRMED exam-duty
              conflicts and accumulated duty counts (fairness).

        Side-effect free: never writes to ExamDutyStore.
        """

        parsed_date = self._parse_date(exam_date)

        if exam_date in (None, ""):
            return {
                "success": False,
                "reason": "missing_date",
            }

        if parsed_date is None:
            return {
                "success": False,
                "reason": "invalid_date",
            }

        if not start_time or not end_time:
            return {
                "success": False,
                "reason": "missing_time",
            }

        start_minutes, end_minutes = self._validate_time_range(
            start_time, end_time
        )

        if start_minutes is None:
            return {
                "success": False,
                "reason": "invalid_time_range",
            }

        weekday = self._derive_weekday(parsed_date)
        exam_date_key = parsed_date.isoformat()

        base_result = self.workload_engine.exam_duty_candidates(
            weekday,
            start_time,
            end_time,
        )

        duty_counts = self.store.duty_counts()

        results = []
        conflicts = []

        for candidate in base_result.get("results", []):

            teacher = self._text(candidate.get("teacher"))

            if not teacher:
                continue

            if self.store.teacher_has_overlap(
                teacher,
                exam_date_key,
                start_time,
                end_time,
            ):
                conflicts.append({
                    "teacher": teacher,
                    "reason": "existing_confirmed_exam_duty",
                })
                continue

            existing_exam_duty_count = duty_counts.get(
                teacher,
                duty_counts.get(teacher.lower(), 0),
            )

            # duty_counts() keys are display-cased, not
            # necessarily matching `teacher`'s exact case - do a
            # case-insensitive lookup instead of relying on exact
            # key equality.
            if not existing_exam_duty_count:
                existing_exam_duty_count = next(
                    (
                        count
                        for name, count in duty_counts.items()
                        if name.strip().lower()
                        == teacher.strip().lower()
                    ),
                    0,
                )

            results.append({
                "teacher": teacher,
                "daily_periods": candidate.get("daily_periods", 0),
                "priority": candidate.get("priority", ""),
                "existing_exam_duty_count": existing_exam_duty_count,
            })

        # Preserve the existing daily-workload ranking as the
        # PRIMARY key; use the candidate's own accumulated
        # confirmed exam-duty count as a secondary fairness
        # tiebreak, then name for determinism.
        results.sort(
            key=lambda item: (
                item["daily_periods"],
                item["existing_exam_duty_count"],
                item["teacher"].lower(),
            )
        )

        return {
            "success": True,
            "exam_date": exam_date_key,
            "day": weekday,
            "start_time": start_time,
            "end_time": end_time,
            "count": len(results),
            "results": results,
            "conflicts": conflicts,
        }

    # ============================================================
    # PLAN (side-effect free)
    # ============================================================

    def plan_duty(
        self,
        exam_date,
        start_time,
        end_time,
        required_faculty,
        hall=None,
        session_id=None,
        teachers=None,
    ) -> dict[str, Any]:
        """
        Produces a structured, side-effect-free exam-duty
        proposal. Never writes to ExamDutyStore.

        `teachers` (optional): the specific faculty to assign instead of
        the top-ranked ones. Each named teacher is checked against the
        SAME availability rules as every other candidate (free for the
        whole time range, not already on duty in this session); if any
        cannot be assigned the proposal is rejected with the reason for
        each, and nothing is proposed.
        """

        try:
            required_count = int(required_faculty)
        except (TypeError, ValueError):
            return {
                "success": False,
                "status": "rejected",
                "reason": "invalid_required_faculty_count",
            }

        if required_count <= 0:
            return {
                "success": False,
                "status": "rejected",
                "reason": "invalid_required_faculty_count",
            }

        candidate_result = self.candidates(
            exam_date, start_time, end_time
        )

        if not candidate_result.get("success"):
            return {
                "success": False,
                "status": "rejected",
                "reason": candidate_result.get("reason"),
            }

        hall_text = self._text(hall) or None
        session_id_text = self._text(session_id) or None

        session_key = ExamDutyStore.session_key(
            candidate_result["exam_date"],
            start_time,
            end_time,
            hall_text,
            session_id_text,
        )

        available = [
            candidate
            for candidate in candidate_result["results"]
            if not self.store.teacher_already_in_session(
                candidate["teacher"], session_key
            )
        ]

        if not available and not teachers:
            return {
                "success": False,
                "status": "rejected",
                "reason": "no_eligible_faculty",
                "exam_date": candidate_result["exam_date"],
                "day": candidate_result["day"],
                "start_time": start_time,
                "end_time": end_time,
            }

        if not teachers and len(available) < required_count:
            return {
                "success": False,
                "status": "rejected",
                "reason": "insufficient_available_faculty",
                "exam_date": candidate_result["exam_date"],
                "day": candidate_result["day"],
                "start_time": start_time,
                "end_time": end_time,
                "available_count": len(available),
                "required_count": required_count,
                "results": available,
            }

        selected = available[:required_count]

        if teachers:

            by_name = {c["teacher"]: c for c in available}

            all_names = {
                c["teacher"] for c in candidate_result["results"]
            }

            unavailable = {}

            selected = []

            for name in teachers:

                if name in by_name:
                    if by_name[name] not in selected:
                        selected.append(by_name[name])
                elif name in all_names:
                    unavailable[name] = "already_on_duty_in_this_session"
                else:
                    unavailable[name] = "not_free_for_the_whole_time_range"

            if unavailable:
                return {
                    "success": False,
                    "status": "rejected",
                    "reason": "requested_teachers_unavailable",
                    "exam_date": candidate_result["exam_date"],
                    "day": candidate_result["day"],
                    "start_time": start_time,
                    "end_time": end_time,
                    "unavailable": unavailable,
                    "available_requested": [c["teacher"] for c in selected],
                }

            required_count = len(selected)

        return {
            "success": True,
            "status": "proposed",
            "exam_date": candidate_result["exam_date"],
            "day": candidate_result["day"],
            "start_time": start_time,
            "end_time": end_time,
            "hall": hall_text or "",
            "session_id": session_id_text or "",
            "session_key": session_key,
            "required_count": required_count,
            "requested_teachers": (
                [c["teacher"] for c in selected] if teachers else []
            ),
            "recommended": selected,
            "existing_duty_counts": {
                candidate["teacher"]: candidate[
                    "existing_exam_duty_count"
                ]
                for candidate in selected
            },
            "conflicts": candidate_result.get("conflicts", []),
            "outcome": "proposed",
        }

    # ============================================================
    # CONFIRM
    #
    # Re-resolves candidates and re-validates availability FROM
    # SCRATCH using the plan's own identifying fields, rather than
    # trusting the earlier plan snapshot. This is what detects a
    # stale plan: if the exam-duty/timetable state has changed
    # since planning (e.g. another confirm has since claimed one
    # of the recommended faculty, or a recommended faculty member
    # is no longer available), re-planning now surfaces that and
    # confirm_duty() rejects rather than applying a now-invalid
    # assignment.
    # ============================================================

    def confirm_duty(self, plan: dict[str, Any]) -> dict[str, Any]:

        if not isinstance(plan, dict) or not plan.get("success"):
            return {
                "success": False,
                "status": "rejected",
                "reason": "invalid_plan",
            }

        exam_date = plan.get("exam_date")
        start_time = plan.get("start_time")
        end_time = plan.get("end_time")
        required_count = plan.get("required_count")
        hall = plan.get("hall") or None
        session_id = plan.get("session_id") or None
        recommended = plan.get("recommended") or []

        if not exam_date or not start_time or not end_time or not (
            recommended
        ):
            return {
                "success": False,
                "status": "rejected",
                "reason": "invalid_plan",
            }

        # A plan made for SPECIFIC people is re-checked for those same
        # people (still free, not on duty); a ranked plan is re-ranked.
        fresh_plan = self.plan_duty(
            exam_date=exam_date,
            start_time=start_time,
            end_time=end_time,
            required_faculty=required_count,
            hall=hall,
            session_id=session_id,
            teachers=plan.get("requested_teachers") or None,
        )

        if not fresh_plan.get("success"):
            return {
                "success": False,
                "status": "rejected",
                "reason": "stale_plan",
                "revalidation": fresh_plan,
            }

        # ------------------------------------------------------
        # The freshly re-resolved recommendation must still
        # contain every faculty member named in the plan being
        # confirmed - otherwise the exam-duty/timetable state has
        # changed in a way that makes the original plan unsafe to
        # apply as described (e.g. one of them picked up a
        # conflicting confirmed duty, or a regular timetable
        # change, since planning).
        # ------------------------------------------------------

        fresh_teachers = {
            candidate["teacher"].strip().lower()
            for candidate in fresh_plan["recommended"]
        }

        planned_teachers = [
            candidate["teacher"]
            for candidate in recommended
        ]

        if not all(
            teacher.strip().lower() in fresh_teachers
            for teacher in planned_teachers
        ):
            return {
                "success": False,
                "status": "rejected",
                "reason": "stale_plan",
                "revalidation": fresh_plan,
            }

        # ------------------------------------------------------
        # Duplicate-within-this-confirmation guard: the same
        # faculty member must never be persisted twice for one
        # confirm() call.
        # ------------------------------------------------------

        seen = set()
        for teacher in planned_teachers:
            key = teacher.strip().lower()
            if key in seen:
                return {
                    "success": False,
                    "status": "rejected",
                    "reason": "duplicate_duty",
                }
            seen.add(key)

        duty_group_id = (
            f"examdutygrp-{datetime.now(timezone.utc).timestamp():.6f}"
        )

        session_key = fresh_plan["session_key"]

        records = []

        for index, teacher in enumerate(planned_teachers):

            duty_id = f"{duty_group_id}-{index}"

            record = {
                "duty_id": duty_id,
                "duty_group_id": duty_group_id,
                "session_key": session_key,
                "exam_date": fresh_plan["exam_date"],
                "day": fresh_plan["day"],
                "start_time": fresh_plan["start_time"],
                "end_time": fresh_plan["end_time"],
                "hall": fresh_plan.get("hall", ""),
                "session_id": fresh_plan.get("session_id", ""),
                "teacher": teacher,
                "status": "confirmed",
                "confirmed_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }

            self.store.add(record)
            records.append(record)

        return {
            "success": True,
            "status": "confirmed",
            "duty_group_id": duty_group_id,
            "duties": records,
        }

    # ============================================================
    # QUERYING CONFIRMED DUTIES (delegates to the store)
    # ============================================================

    def duties_for_date(self, exam_date) -> list[dict[str, Any]]:
        parsed_date = self._parse_date(exam_date)
        key = parsed_date.isoformat() if parsed_date else exam_date
        return self.store.for_date(key)

    def duties_for_teacher(self, teacher: str) -> list[dict[str, Any]]:
        return self.store.for_teacher(teacher)

    def all_duties(self) -> list[dict[str, Any]]:
        return self.store.all()

    def duty_counts(self) -> dict[str, int]:
        return self.store.duty_counts()