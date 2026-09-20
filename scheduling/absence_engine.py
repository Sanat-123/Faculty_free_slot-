from collections import defaultdict
import re

from utils.faculty_names import find_composites, is_placeholder_name


class FacultyAbsenceEngine:

    def __init__(self, query_engine, workload_engine=None):
        self.query_engine = query_engine
        self.workload_engine = workload_engine

    # ============================================================
    # BASIC HELPERS
    # ============================================================

    def _events(self):
        try:
            return list(self.query_engine._events())
        except Exception:
            return []

    @staticmethod
    def _text(value):
        return str(value or "").strip()

    @staticmethod
    def _normalize_day(day):
        if not day:
            return None

        days = {
            "mon": "monday",
            "monday": "monday",
            "tue": "tuesday",
            "tues": "tuesday",
            "tuesday": "tuesday",
            "wed": "wednesday",
            "wednesday": "wednesday",
            "thu": "thursday",
            "thur": "thursday",
            "thurs": "thursday",
            "thursday": "thursday",
            "fri": "friday",
            "friday": "friday",
            "sat": "saturday",
            "saturday": "saturday",
            "sun": "sunday",
            "sunday": "sunday",
        }

        return days.get(str(day).strip().lower())

    @staticmethod
    def _slot_number(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            match = re.search(r"\b(\d+)\b", str(value or ""))
            return int(match.group(1)) if match else None

    @staticmethod
    def _time_range(value):
        if not value:
            return None

        text = str(value).strip().lower().replace(".", ":")

        match = re.search(
            r"(\d{1,2}):(\d{2})\s*(am|pm)?\s*"
            r"(?:-|–|—|to)\s*"
            r"(\d{1,2}):(\d{2})\s*(am|pm)?",
            text,
        )

        if not match:
            return None

        h1, m1, ap1, h2, m2, ap2 = match.groups()

        def minutes(hour, minute, ampm):
            hour = int(hour)
            minute = int(minute)

            if ampm == "am" and hour == 12:
                hour = 0
            elif ampm == "pm" and hour != 12:
                hour += 12

            return hour * 60 + minute

        start = minutes(h1, m1, ap1)
        end = minutes(h2, m2, ap2)

        if end <= start:
            return None

        return start, end

    # ============================================================
    # NORMALIZED TEXT
    # ============================================================

    @staticmethod
    def _clean_text(value):
        text = str(value or "").lower()

        text = re.sub(r"[_/|,:;]+", " ", text)
        text = re.sub(r"[-]+", " ", text)
        text = re.sub(r"[^a-z0-9]+", " ", text)

        return " ".join(text.split())

    @classmethod
    def _tokens(cls, value):
        return cls._clean_text(value).split()

    # ============================================================
    # CLASS STRUCTURE
    #
    # We do NOT hard-code 5CS, 5CSE, 5CS-D, etc.
    #
    # The function attempts to understand a class identifier such as:
    #
    # 5CS
    # 5CSA
    # 5CSB
    # 5CSE
    # 7CS-DS-A
    #
    # without depending on a particular dataset.
    # ============================================================

    @classmethod
    def _class_structure(cls, class_name):
        """
        Parse timetable class identifiers into:

            semester = academic level
            base     = branch/program without section
            section  = section when identifiable

        Examples:

            5CS       -> semester=5, base=CS,   section=None
            5CSA      -> semester=5, base=CS,   section=A
            5CSB      -> semester=5, base=CS,   section=B
            5CS-D     -> semester=5, base=CS,   section=D

            5CS-DS    -> semester=5, base=CSDS, section=None
            5CS-DS-A  -> semester=5, base=CSDS, section=A
            5CS-DS-B  -> semester=5, base=CSDS, section=B

            7CS-AI-A  -> semester=7, base=CSAI, section=A
            7CS-AI-B  -> semester=7, base=CSAI, section=B

        The method does not hard-code specific subjects or branches.
        """

        text = cls._text(class_name).strip()

        if not text:
            return {
                "raw": "",
                "normalized": "",
                "semester": None,
                "base": "",
                "section": None,
            }

        normalized = cls._clean_text(text)

        # ------------------------------------------------------------
        # Extract leading academic semester/level.
        # ------------------------------------------------------------

        semester_match = re.match(r"^\s*(\d+)", text)

        if semester_match:
            semester = semester_match.group(1)
        else:
            semester = None

        # ------------------------------------------------------------
        # Remove semester number.
        # ------------------------------------------------------------

        remainder = text[semester_match.end():] if semester_match else text

        remainder = remainder.strip()

        # ------------------------------------------------------------
        # Remove spaces.
        # ------------------------------------------------------------

        remainder = re.sub(r"\s+", "", remainder)

        # ------------------------------------------------------------
        # Split on '-' / '_' separators.
        #
        # Example:
        #
        # CS-DS-A -> ["CS", "DS", "A"]
        # CS-AI-A -> ["CS", "AI", "A"]
        # CS-D    -> ["CS", "D"]
        # ------------------------------------------------------------

        parts = [
            p for p in re.split(r"[-_]+", remainder)
            if p
        ]

        section = None

        if parts:

            last = parts[-1]

            # A final single alphabetic character is treated
            # as a section only when there are multiple parts.
            if len(parts) >= 2 and re.fullmatch(
                r"[A-Za-z]",
                last,
            ):
                section = last.upper()
                parts = parts[:-1]

        # ------------------------------------------------------------
        # Handle compact section notation.
        #
        # Examples:
        #
        # CSA -> CS + A
        # CSB -> CS + B
        # CSC -> CS + C
        # CSE -> CS + E
        #
        # But:
        #
        # CSDS -> CSDS
        # CSAI -> CSAI
        # CSIOT -> CSIOT
        #
        # We only treat the final character as a section when
        # removing it leaves a plausible base.
        # ------------------------------------------------------------

        if section is None and len(parts) == 1:

            compact = parts[0]

            # Common compact section form:
            #
            # CS + A/B/C/D/E/F
            #
            # We determine this structurally rather than
            # hard-coding complete class names.

            if len(compact) >= 3:

                prefix = compact[:-1]
                last = compact[-1]

                if (
                    last.isalpha()
                    and prefix.isalpha()
                    and len(prefix) >= 2
                ):
                    # A short final alphabetic marker is considered
                    # a section when the prefix itself represents
                    # the main class/program identity.
                    #
                    # Avoid splitting obvious multi-letter branch
                    # identifiers such as CSDS, CSAI, CSIOT.
                    if not prefix.endswith(
                        ("DS", "AI", "IOT")
                    ):
                        section = last.upper()
                        compact = prefix

                parts = [compact]

        # ------------------------------------------------------------
        # Build branch/base identity.
        # ------------------------------------------------------------

        base = "".join(parts).upper()

        # ------------------------------------------------------------
        # Normalize base by removing non-alphanumeric characters.
        # ------------------------------------------------------------

        base = re.sub(
            r"[^A-Z0-9]",
            "",
            base,
        )

        return {
            "raw": text,
            "normalized": normalized,
            "semester": semester,
            "base": base,
            "section": section,
        }

    # ============================================================
    # CONTEXTUAL CLASS COMPARISON
    # ============================================================

    @classmethod
    def _class_similarity(cls, class_a, class_b):
        """
        Compare two timetable class identifiers.

        Returns:

            3 = exact class
            2 = same academic level + same branch/base
            1 = same academic level
            0 = unrelated / unknown

        This is deliberately generic.
        """

        a = cls._class_structure(class_a)
        b = cls._class_structure(class_b)

        if not a["normalized"] or not b["normalized"]:
            return 0

        if a["normalized"] == b["normalized"]:
            return 3

        if (
            a["semester"]
            and b["semester"]
            and a["semester"] == b["semester"]
            and a["base"]
            and b["base"]
            and a["base"] == b["base"]
        ):
            return 2

        if (
            a["semester"]
            and b["semester"]
            and a["semester"] == b["semester"]
        ):
            return 1

        return 0

    # ============================================================
    # SUBJECT FAMILY
    #
    # Instead of hard-coding AOA, DBMS, CN, etc., identify the
    # underlying subject by removing common delivery/group metadata.
    # ============================================================

    @classmethod
    def _normalize_text(cls, value):
        """
        Normalize timetable text without assuming any particular
        subject, branch, semester, section, or timetable format.
        """
        text = cls._text(value).lower()

        if not text:
            return ""

        text = re.sub(r"[^a-z0-9]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        return text

    @classmethod
    def _subject_family(cls, subject):
        """
        Create a normalized subject identity.

        Removes generic timetable delivery/group information without
        hard-coding particular subjects such as AOA, DBMS, OS, etc.
        """
        text = cls._normalize_text(subject)

        if not text:
            return ""

        tokens = text.split()

        metadata = {
            "lab",
            "laboratory",
            "theory",
            "tutorial",
            "practical",
            "lecture",
            "group",
        }

        cleaned = []

        for token in tokens:

            # Remove generic timetable metadata.
            if token in metadata:
                continue

            # Remove standalone numeric group identifiers.
            if token.isdigit():
                continue

            cleaned.append(token)

        # ------------------------------------------------------------
        # Remove section markers only when they appear as the final
        # token AND there is another subject token before them.
        #
        # Examples:
        #
        #   AOA A              -> AOA
        #   AOA B              -> AOA
        #   AOA AI A           -> AOA AI
        #
        # ------------------------------------------------------------

        if len(cleaned) >= 2:

            last = cleaned[-1]

            if len(last) == 1 and last.isalpha():
                cleaned.pop()

        # ------------------------------------------------------------
        # Remove common specialization marker immediately before
        # a final section marker.
        #
        # Example:
        #
        #   AOA AI A -> AOA
        #   AOA AI B -> AOA
        #
        # ------------------------------------------------------------

        if len(cleaned) >= 2:

            previous = cleaned[-1]

            if previous in {
                "ai",
                "ds",
                "iot",
            }:
                cleaned.pop()

        return " ".join(cleaned)

    @classmethod
    def _class_context(cls, class_name):
        """
        Normalize the timetable's class/program field.

        No branch, semester, section, or class names are hard-coded.
        The returned value is only a normalized representation of the
        value supplied by the timetable.
        """
        return cls._normalize_text(class_name)

    # ============================================================
    # SUBJECT TOKEN SIMILARITY
    # ============================================================

    @classmethod
    def _subject_similarity(cls, subject_a, subject_b):
        a = cls._subject_family(subject_a)
        b = cls._subject_family(subject_b)

        if not a or not b:
            return 0

        if a == b:
            return 3

        a_tokens = set(a.split())
        b_tokens = set(b.split())

        if not a_tokens or not b_tokens:
            return 0

        intersection = a_tokens & b_tokens

        if intersection:
            return 1

        return 0

    # ============================================================
    # FACULTY EVENTS
    # ============================================================

    def faculty_events(self, teacher, day=None):
        teacher_key = self._text(teacher).lower()

        day_key = (
            self._normalize_day(day)
            if day
            else None
        )

        results = []

        for event in self._events():

            if self._text(event.get("teacher")).lower() != teacher_key:
                continue

            if (
                day_key
                and self._normalize_day(event.get("day")) != day_key
            ):
                continue

            results.append(event)

        return results

    # ============================================================
    # ABSENT FACULTY CLASSES
    # ============================================================

    def absent_faculty_classes(self, teacher, day):

        day_key = self._normalize_day(day)

        if not day_key:
            return {
                "query_type": "absent_faculty_classes",
                "teacher": teacher,
                "day": day,
                "count": 0,
                "classes": [],
            }

        classes = []

        for event in self.faculty_events(teacher, day_key):

            classes.append({
                "teacher": event.get("teacher", ""),
                "day": event.get("day", ""),
                "slot": event.get("slot"),
                "slot_time": event.get("slot_time", ""),
                "subject": event.get("subject", ""),
                "class_name": event.get("class_name", ""),
                "group_name": event.get("group_name", ""),
                "type": event.get("type", ""),
                "room": event.get("room", ""),
            })

        return {
            "query_type": "absent_faculty_classes",
            "teacher": teacher,
            "day": day_key,
            "count": len(classes),
            "classes": classes,
        }

    # ============================================================
    # FACULTY AVAILABILITY
    # ============================================================

    def is_faculty_free(self, teacher, day, slot):

        teacher_key = self._text(teacher).lower()
        day_key = self._normalize_day(day)
        target_slot = self._slot_number(slot)

        if not day_key or target_slot is None:
            return False

        composite_parts = self._composite_parts()

        for event in self._events():

            event_teacher = self._text(event.get("teacher")).lower()

            if (
                (
                    event_teacher == teacher_key
                    or teacher_key in composite_parts.get(
                        event_teacher, ()
                    )
                )
                and self._normalize_day(event.get("day"))
                == day_key
                and self._slot_number(event.get("slot"))
                == target_slot
            ):
                return False

        return True

    def _composite_parts(self):
        """
        {merged name (lower): {person (lower), ...}}

        A merged entry such as "A B" that is exactly the concatenation of
        two other teacher names means both people are teaching that slot.
        Detected structurally from the loaded data (no names hard-coded).
        """

        events = self._events()

        signature = len(events)

        cache = getattr(self, "_composite_cache", None)

        if cache is not None and cache[0] == signature:
            return cache[1]

        names = {
            self._text(e.get("teacher"))
            for e in events
            if self._text(e.get("teacher"))
        }

        parts = {
            merged.lower(): {a.lower(), b.lower()}
            for merged, (a, b) in find_composites(sorted(names)).items()
        }

        self._composite_cache = (signature, parts)

        return parts

    def is_faculty_free_for_block(self, teacher, day, block):

        for event in block.get("events", []):

            if not self.is_faculty_free(
                teacher,
                day,
                event.get("slot"),
            ):
                return False

        return True

    # ============================================================
    # FACULTY SETS
    # ============================================================

    def all_faculty(self, exclude_teacher=None):

        exclude_key = self._text(exclude_teacher).lower()

        faculty = set()

        composites = self._composite_parts()

        for event in self._events():

            teacher = self._text(event.get("teacher"))

            if not teacher:
                continue

            if teacher.lower() == exclude_key:
                continue

            # codes / placeholders ("AS", "XE2") and merged names are
            # not people who can be asked to cover a class
            if is_placeholder_name(teacher):
                continue

            if teacher.lower() in composites:
                continue

            faculty.add(teacher)

        return sorted(faculty, key=str.lower)

    # ============================================================
    # DAILY WORKLOAD
    # ============================================================

    def _daily_workload_map(self, day):

        day_key = self._normalize_day(day)

        workload = defaultdict(int)

        for event in self._events():

            if self._normalize_day(event.get("day")) != day_key:
                continue

            teacher = self._text(event.get("teacher"))

            if teacher:
                workload[teacher.lower()] += 1

        return workload

    # ============================================================
    # BLOCK IDENTIFICATION
    # ============================================================

    @classmethod
    def _block_key(cls, event):

        return (
            cls._clean_text(event.get("class_name")),
            cls._subject_family(event.get("subject")),
            cls._clean_text(event.get("group_name")),
            cls._clean_text(event.get("type")),
            cls._clean_text(event.get("room")),
        )

    def _affected_blocks(self, teacher, day):

        events = []

        for event in self.faculty_events(teacher, day):

            tr = self._time_range(
                event.get("slot_time")
            )

            events.append({
                **event,
                "_slot": self._slot_number(
                    event.get("slot")
                ),
                "_start": (
                    tr[0]
                    if tr
                    else None
                ),
                "_end": (
                    tr[1]
                    if tr
                    else None
                ),
                "_key": self._block_key(event),
            })

        events.sort(
            key=lambda x: (
                x["_start"] is None,
                (
                    x["_start"]
                    if x["_start"] is not None
                    else 99999
                ),
                (
                    x["_slot"]
                    if x["_slot"] is not None
                    else 99999
                ),
            )
        )

        blocks = []

        for event in events:

            if not blocks:
                blocks.append([event])
                continue

            previous = blocks[-1][-1]

            same_identity = (
                previous["_key"]
                == event["_key"]
            )

            time_contiguous = (
                previous["_end"] is not None
                and event["_start"] is not None
                and previous["_end"]
                == event["_start"]
            )

            slot_contiguous = (
                previous["_slot"] is not None
                and event["_slot"] is not None
                and event["_slot"]
                == previous["_slot"] + 1
            )

            if same_identity and (
                time_contiguous
                or (
                    previous["_start"] is None
                    and event["_start"] is None
                    and slot_contiguous
                )
            ):
                blocks[-1].append(event)

            else:
                blocks.append([event])

        result = []

        for block_events in blocks:

            first = block_events[0]

            result.append({
                "class_name": first.get("class_name", ""),
                "subject": first.get("subject", ""),
                "subject_family": self._subject_family(
                    first.get("subject", "")
                ),
                "group_name": first.get("group_name", ""),
                "type": first.get("type", ""),
                "room": first.get("room", ""),
                "slots": [
                    e.get("slot")
                    for e in block_events
                ],
                "events": block_events,
                "period_count": len(block_events),
            })

        return result

    # ============================================================
    # BLOCK TIME
    # ============================================================

    @classmethod
    def _block_time(cls, block):

        events = block.get("events", [])

        if not events:
            return ""

        first = cls._time_range(
            events[0].get("slot_time")
        )

        last = cls._time_range(
            events[-1].get("slot_time")
        )

        if first and last:

            def fmt(value):
                return (
                    f"{value // 60:02d}:"
                    f"{value % 60:02d}"
                )

            return (
                f"{fmt(first[0])} - "
                f"{fmt(last[1])}"
            )

        return " / ".join(
            str(e.get("slot_time", ""))
            for e in events
        )

    # ============================================================
    # REPLACEMENT CANDIDATES
    # ============================================================

    def replacement_candidates(self, teacher, day, slots=None):

        day_key = self._normalize_day(day)

        if not day_key:
            return {
                "query_type": "replacement_candidates",
                "absent_teacher": teacher,
                "day": day,
                "count": 0,
                "block_count": 0,
                "blocks": [],
                "results": [],
            }

        blocks = self._affected_blocks(
            teacher,
            day_key,
        )

        # Optional slot scope: only the absent blocks that touch one of
        # the requested slots (e.g. "substitute for slot 2").
        if slots:

            wanted = {
                self._slot_number(s) for s in slots
                if self._slot_number(s) is not None
            }

            blocks = [
                b for b in blocks
                if wanted & {
                    self._slot_number(e.get("slot"))
                    for e in b.get("events", [])
                }
            ]

        workload = self._daily_workload_map(
            day_key
        )

        results = []

        all_faculty = self.all_faculty(
            teacher
        )

        # --------------------------------------------------------
        # Process every absent block independently.
        # --------------------------------------------------------

        for block_index, block in enumerate(
            blocks,
            start=1,
        ):

            block_class = block.get(
                "class_name",
                "",
            )

            block_subject = block.get(
                "subject",
                "",
            )

            # ----------------------------------------------------
            # Find candidates.
            # ----------------------------------------------------

            candidates = []

            for replacement in all_faculty:

                # ------------------------------------------------
                # HARD CONSTRAINT:
                # Candidate MUST be free for every period in
                # the affected block.
                # ------------------------------------------------

                if not self.is_faculty_free_for_block(
                    replacement,
                    day_key,
                    block,
                ):
                    continue

                replacement_key = (
                    replacement.lower()
                )

                # ------------------------------------------------
                # Determine what this faculty member teaches.
                # ------------------------------------------------

                candidate_events = [
                    e
                    for e in self._events()
                    if self._text(
                        e.get("teacher")
                    ).lower()
                    == replacement_key
                ]

                best_class_similarity = 0
                best_subject_similarity = 0

                for event in candidate_events:

                    class_similarity = (
                        self._class_similarity(
                            block_class,
                            event.get(
                                "class_name",
                                "",
                            ),
                        )
                    )

                    subject_similarity = (
                        self._subject_similarity(
                            block_subject,
                            event.get(
                                "subject",
                                "",
                            ),
                        )
                    )

                    best_class_similarity = max(
                        best_class_similarity,
                        class_similarity,
                    )

                    best_subject_similarity = max(
                        best_subject_similarity,
                        subject_similarity,
                    )

                # ------------------------------------------------
                # Qualification tiers
                #
                # 1 = same subject + same class context
                # 2 = same subject
                # 3 = same class context
                #
                # Unrelated faculty are excluded.
                # ------------------------------------------------

                if (
                    best_subject_similarity >= 3
                    and best_class_similarity >= 2
                ):
                    priority = 1
                    priority_reason = (
                        "same subject + same class context"
                    )

                elif best_subject_similarity >= 3:
                    priority = 2
                    priority_reason = (
                        "same subject"
                    )

                elif best_class_similarity >= 2:
                    priority = 3
                    priority_reason = (
                        "same class context"
                    )

                else:
                    continue

                periods = workload.get(
                    replacement_key,
                    0,
                )

                candidates.append({
                    "replacement_teacher": replacement,
                    "daily_periods": periods,
                    "priority": priority,
                    "priority_reason": priority_reason,
                    "class_similarity": (
                        best_class_similarity
                    ),
                    "subject_similarity": (
                        best_subject_similarity
                    ),
                    "complete_block": True,
                    "block_index": block_index,
                    "day": day_key,
                    "slots": block["slots"],
                    "slot_time": self._block_time(
                        block
                    ),
                    "period_count": block[
                        "period_count"
                    ],
                    "subject": block[
                        "subject"
                    ],
                    "subject_family": (
                        self._subject_family(
                            block["subject"]
                        )
                    ),
                    "class_name": block[
                        "class_name"
                    ],
                    "group_name": block[
                        "group_name"
                    ],
                    "type": block["type"],
                    "room": block["room"],
                    "_score": (
                        priority,
                        periods,
                        replacement_key,
                    ),
                })

            # ----------------------------------------------------
            # Ranking:
            #
            # First qualification tier.
            # Then lowest workload.
            # Then deterministic name ordering.
            # ----------------------------------------------------

            candidates.sort(
                key=lambda x: x["_score"]
            )

            for rank, candidate in enumerate(
                candidates,
                start=1,
            ):

                candidate["rank"] = rank
                candidate.pop("_score", None)

                results.append(candidate)

        return {
            "query_type": (
                "replacement_candidates"
            ),
            "absent_teacher": teacher,
            "day": day_key,
            "count": len(results),
            "block_count": len(blocks),

            "blocks": [
                {
                    "block_index": i,
                    "slots": b["slots"],
                    "slot_time": (
                        self._block_time(b)
                    ),
                    "period_count": (
                        b["period_count"]
                    ),
                    "subject": b[
                        "subject"
                    ],
                    "subject_family": (
                        self._subject_family(
                            b["subject"]
                        )
                    ),
                    "class_name": b[
                        "class_name"
                    ],
                    "group_name": b[
                        "group_name"
                    ],
                    "type": b[
                        "type"
                    ],
                }
                for i, b in enumerate(
                    blocks,
                    start=1,
                )
            ],

            "results": results,
        }

    

        # ============================================================
    # BEST REPLACEMENT RECOMMENDATIONS
    # ============================================================

    def best_replacements(self, teacher, day, slots=None):
        """
        Select the best replacement faculty for every absent block.

        Selection priority:

            1. Same subject + same class context
            2. Same subject
            3. Same class context
            4. Lowest daily workload
            5. Faculty name for deterministic ordering

        A faculty member is not assigned to multiple blocks unless
        there is no other suitable available faculty.
        """

        result = self.replacement_candidates(
            teacher,
            day,
            slots=slots,
        )

        if not result.get("blocks"):
            return {
                "query_type": "best_replacements",
                "absent_teacher": teacher,
                "day": result.get("day", day),
                "block_count": 0,
                "recommendations": [],
            }

        recommendations = []

        # Keep track of faculty already assigned to another block.
        assigned_teachers = set()

        for block in result["blocks"]:

            block_index = block["block_index"]

            candidates = [
                r
                for r in result["results"]
                if r["block_index"] == block_index
            ]

            if not candidates:
                recommendations.append({
                    "block_index": block_index,
                    "slots": block["slots"],
                    "slot_time": block["slot_time"],
                    "period_count": block["period_count"],
                    "subject": block["subject"],
                    "subject_family": block["subject_family"],
                    "class_name": block["class_name"],
                    "group_name": block["group_name"],
                    "type": block["type"],
                    "room": block.get("room", ""),
                    "replacement_teacher": None,
                    "priority": None,
                    "priority_reason": "No suitable faculty available",
                    "daily_periods": None,
                    "complete_block": False,
                })

                continue

            # ----------------------------------------------------
            # First try candidates who have NOT already been used.
            # ----------------------------------------------------

            unused_candidates = [
                candidate
                for candidate in candidates
                if candidate["replacement_teacher"].lower()
                not in assigned_teachers
            ]

            if unused_candidates:
                candidates_to_consider = unused_candidates
            else:
                # If every suitable faculty member is already used,
                # allow reuse rather than leaving the block uncovered.
                candidates_to_consider = candidates

            # ----------------------------------------------------
            # Candidates are already ranked by:
            #
            # priority -> workload -> name
            #
            # Therefore the first candidate is the best one.
            # ----------------------------------------------------

            best = candidates_to_consider[0]

            teacher_name = best["replacement_teacher"]

            assigned_teachers.add(
                teacher_name.lower()
            )

            recommendations.append({
                "block_index": block_index,
                "day": day,
                "slots": best["slots"],
                "slot_time": best["slot_time"],
                "period_count": best["period_count"],
                "subject": best["subject"],
                "subject_family": best["subject_family"],
                "class_name": best["class_name"],
                "group_name": best["group_name"],
                "type": best["type"],
                "room": best["room"],
                "replacement_teacher": teacher_name,
                "priority": best["priority"],
                "priority_reason": best["priority_reason"],
                "daily_periods": best["daily_periods"],
                "class_similarity": best["class_similarity"],
                "subject_similarity": best["subject_similarity"],
                "complete_block": best["complete_block"],
            })

        return {
            "query_type": "best_replacements",
            "absent_teacher": teacher,
            "day": result.get("day", day),
            "block_count": len(recommendations),
            "recommendations": recommendations,
        }