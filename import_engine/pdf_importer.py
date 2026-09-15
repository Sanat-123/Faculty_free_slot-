"""
==============================================================
UNISCHED AI - UNIVERSAL PDF TIMETABLE IMPORTER
==============================================================

IMPORTANT:
- Preserves EMPTY timetable cells.
- Empty faculty cell = FREE.
- Non-empty faculty cell = BUSY.
- Automatically extracts teacher name.
- Automatically extracts day, slot and slot time.
- Does NOT hard-code teacher names.
- Does NOT hard-code college names.
- Designed for facultywise/classwise/locationwise timetable PDFs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
import re

import pdfplumber


class PDFImporter:

    # ==========================================================
    # CONSTANTS
    # ==========================================================

    DAY_MAP = {
        "mo": "monday",
        "mon": "monday",
        "monday": "monday",

        "tu": "tuesday",
        "tue": "tuesday",
        "tues": "tuesday",
        "tuesday": "tuesday",

        "we": "wednesday",
        "wed": "wednesday",
        "weds": "wednesday",
        "wednesday": "wednesday",

        "th": "thursday",
        "thu": "thursday",
        "thur": "thursday",
        "thurs": "thursday",
        "thursday": "thursday",

        "fr": "friday",
        "fri": "friday",
        "friday": "friday",

        "sa": "saturday",
        "sat": "saturday",
        "saturday": "saturday",

        "su": "sunday",
        "sun": "sunday",
        "sunday": "sunday",
    }

    SLOT_TIMES = {
        1: "08:15 - 09:15",
        2: "09:15 - 10:15",
        3: "10:15 - 11:15",
        4: "11:15 - 12:15",
        5: "12:00 - 13:00",
        6: "13:00 - 14:00",
        7: "14:00 - 15:00",
        8: "15:00 - 15:30",
    }

    # ==========================================================
    # BASIC CLEANING
    # ==========================================================

    @staticmethod
    def clean_text(value: Any) -> str:

        if value is None:
            return ""

        text = str(value)

        text = text.replace("\xa0", " ")

        text = text.replace("\r", "\n")

        return " ".join(
            text.strip().split()
        )

    # ==========================================================
    # FILE VALIDATION
    # ==========================================================

    @classmethod
    def validate_file(
        cls,
        file_path: str | Path
    ) -> Dict[str, Any]:

        path = Path(file_path)

        if not path.exists():

            return {
                "valid": False,
                "reason": "PDF file does not exist.",
            }

        if path.suffix.lower() != ".pdf":

            return {
                "valid": False,
                "reason": "File is not a PDF.",
            }

        size = path.stat().st_size

        if size == 0:

            return {
                "valid": False,
                "reason": "PDF file is empty.",
            }

        return {
            "valid": True,
            "reason": "",
            "size_bytes": size,
            "size_mb": round(
                size / (1024 * 1024),
                2
            ),
        }

    # ==========================================================
    # TEACHER EXTRACTION
    # ==========================================================

    @classmethod
    def detect_teacher_from_text(
        cls,
        text: str
    ) -> str:

        if not text:
            return ""

        # ------------------------------------------------------
        # Normal case:
        #
        # Teacher Ms.Archika Jain
        # Teacher Dr. Aakriti Sharma
        # ------------------------------------------------------

        patterns = [

            r"Teacher\s+(.+?)(?=\n|$)",

            r"TEACHER\s+(.+?)(?=\n|$)",

            r"Teacher:\s*(.+?)(?=\n|$)",

            r"Faculty\s+(.+?)(?=\n|$)",

            r"Faculty:\s*(.+?)(?=\n|$)",
        ]

        for pattern in patterns:

            match = re.search(
                pattern,
                text,
                flags=re.IGNORECASE
            )

            if match:

                teacher = cls.clean_text(
                    match.group(1)
                )

                # Remove accidental timetable information
                teacher = re.sub(
                    r"\s+1\s+2\s+3\s+4\s+5\s+6\s+7\s+8.*$",
                    "",
                    teacher
                ).strip()

                if teacher:
                    return teacher

        return ""

    # ==========================================================
    # TEACHER EXTRACTION FROM PAGE
    # ==========================================================

    @classmethod
    def detect_teacher(
        cls,
        page
    ) -> str:

        # ------------------------------------------------------
        # First use normal text extraction.
        # ------------------------------------------------------

        try:

            text = page.extract_text() or ""

            teacher = cls.detect_teacher_from_text(
                text
            )

            if teacher:
                return teacher

        except Exception:
            pass

        # ------------------------------------------------------
        # Fallback: extract words.
        # ------------------------------------------------------

        try:

            words = page.extract_words(
                x_tolerance=2,
                y_tolerance=2
            )

        except Exception:
            words = []

        if not words:
            return ""

        words = sorted(
            words,
            key=lambda x: (
                round(x.get("top", 0), 1),
                x.get("x0", 0)
            )
        )

        for index, word in enumerate(words):

            value = cls.clean_text(
                word.get("text", "")
            )

            if value.lower() != "teacher":
                continue

            teacher_parts = []

            current_top = word.get(
                "top",
                0
            )

            for next_word in words[index + 1:]:

                next_top = next_word.get(
                    "top",
                    0
                )

                if abs(
                    next_top - current_top
                ) > 5:

                    break

                value = cls.clean_text(
                    next_word.get(
                        "text",
                        ""
                    )
                )

                if value:

                    teacher_parts.append(
                        value
                    )

            if teacher_parts:

                return cls.clean_text(
                    " ".join(
                        teacher_parts
                    )
                )

        return ""

    # ==========================================================
    # GENERIC PAGE LABEL DETECTION (structure-based)
    #
    # Facultywise pages identify themselves with a "Teacher ..."
    # / "Faculty ..." line (handled above). Classwise and
    # location-wise pages instead identify themselves with a
    # bare code (e.g. "3CSA." or "403.") on the line immediately
    # before the numbered slot-header row ("1 2 3 4 5 6 7 8...").
    #
    # This looks for that structural position -- the line right
    # before the slot header -- rather than any specific value,
    # so it works regardless of institution name, class-naming
    # convention, or room-numbering scheme.
    # ==========================================================

    @staticmethod
    def _is_sequential_slot_header_line(line: str) -> bool:

        parts = line.strip().split()

        # A real slot-number header line has several columns --
        # require at least 3 so a stray "1 2" elsewhere in the
        # text doesn't false-positive.
        if len(parts) < 3:
            return False

        if not all(
            part.isdigit()
            for part in parts
        ):
            return False

        numbers = [
            int(part)
            for part in parts
        ]

        return numbers == list(
            range(1, len(numbers) + 1)
        )

    @classmethod
    def detect_page_label_line(
        cls,
        text: str
    ) -> str:

        if not text:
            return ""

        lines = [
            line.strip()
            for line in text.split("\n")
        ]

        for index, line in enumerate(lines):

            if not cls._is_sequential_slot_header_line(
                line
            ):
                continue

            # Walk backwards to the nearest non-empty line --
            # that is the page's identity label.
            for prior_index in range(
                index - 1,
                -1,
                -1
            ):

                candidate = lines[prior_index].strip()

                if candidate:
                    return candidate

            break

        return ""

    # ==========================================================
    # PAGE IDENTITY (teacher / class / room)
    # ==========================================================

    # ==========================================================
    # PER-PAGE FACULTY ABBREVIATION LEGEND
    #
    # Classwise/room-wise pages often print faculty as a short
    # code inside the grid (e.g. "MM", "SSS", "ArS") and put the
    # actual code -> full-name key in a small legend block near
    # the bottom of the SAME page (e.g. "MM Dr. Mehul Mahrishi").
    # This is the timetable's own source-of-truth for what a
    # given code means ON THAT PAGE - nothing here is a fixed
    # abbreviation list; every mapping is read straight out of
    # the page text.
    #
    # A code is intentionally NOT resolved globally across the
    # whole document: the SAME code has been observed to mean
    # different people on different pages, and even to appear
    # twice with two different names on a single page's own
    # legend (a real quirk in the source data) - both cases are
    # handled by scoping every legend to its own page and
    # dropping any code that isn't unambiguous within it.
    # ==========================================================

    _LEGEND_TITLE_WORDS = {
        "dr", "mr", "ms", "mrs", "prof", "professor"
    }

    @classmethod
    def extract_page_legend(
        cls,
        page
    ) -> Dict[str, str]:

        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""

        if not text:
            return {}

        marker = re.search(
            r"Timetable generated:[^\n]*\n?(.*)",
            text,
            re.DOTALL
        )

        if not marker:
            return {}

        legend_text = marker.group(1)

        if not legend_text.strip():
            return {}

        raw_matches = list(
            re.finditer(
                r"\b(?:[A-Z][a-z]{0,2}){1,3}\b(?!\.)",
                legend_text
            )
        )

        code_matches = []

        for match in raw_matches:

            token = match.group(0)

            if token.lower() in cls._LEGEND_TITLE_WORDS:
                continue

            preceding = legend_text[:match.start()].rstrip()

            preceding_word_match = re.search(
                r"([A-Za-z]+\.?)$",
                preceding
            )

            if (
                preceding_word_match
                and preceding_word_match.group(1).rstrip(
                    "."
                ).lower() in cls._LEGEND_TITLE_WORDS
            ):
                continue

            code_matches.append(match)

        candidates: Dict[str, List[str]] = defaultdict(list)

        for index, match in enumerate(code_matches):

            start = match.end()

            end = (
                code_matches[index + 1].start()
                if index + 1 < len(code_matches)
                else len(legend_text)
            )

            name = re.sub(
                r"\s+",
                " ",
                legend_text[start:end]
            ).strip().strip(".").strip()

            if not name:
                continue

            code = match.group(0)

            if name not in candidates[code]:
                candidates[code].append(name)

        # A code that resolves to more than one DIFFERENT name
        # within this same page's own legend is a genuine
        # ambiguity in the source - leave it unresolved rather
        # than guess which one is meant.
        return {
            code: names[0]
            for code, names in candidates.items()
            if len(names) == 1
        }

    # ==========================================================
    # ROOM DETECTION FROM A PAGE TITLE/LABEL
    #
    # detect_room() above is tuned for CELL text, where the
    # digit/dash/colon shape of a room code (e.g. "CL-15",
    # "301", "7F:EE-Lab13") is what distinguishes a room from a
    # subject - and subjects routinely end in a bare word like
    # "Lab" too (e.g. "DSA Lab", "CS Lab"), so that same "ends
    # in a room-type word" shape can NOT safely be added to
    # detect_room() itself without misreading half the lab
    # subjects in a normal cell as rooms.
    #
    # A PAGE TITLE is a much narrower, safer context: it is the
    # page's ENTIRE identity line with nothing else on it (e.g.
    # "IAI Lab." on a Location-wise page, parallel to "Teacher
    # Dr. X" or "7CSA." on the other page types), not a mix of
    # subject/class/room text. Only used as a fallback here,
    # after detect_room() (which still wins whenever the label
    # DOES contain a digit/dash/colon room code) has already
    # failed.
    # ==========================================================

    _ROOM_TYPE_WORDS = {
        "lab", "hall", "lib", "library", "room",
        "block", "building", "centre", "center",
    }

    @classmethod
    def detect_room_from_label(
        cls,
        label: str
    ) -> str:

        text = cls.clean_text(
            label
        ).rstrip(".").strip()

        if not text:
            return ""

        words = text.split()

        if not words or len(words) > 4:
            return ""

        if words[-1].lower() not in cls._ROOM_TYPE_WORDS:
            return ""

        # A page-identity label is otherwise only teacher/class/
        # room text - if it contains a digit at all it is far
        # more likely a room code detect_room() should have
        # already caught (or a stray class fragment), not a
        # bare-word room name.
        if any(ch.isdigit() for ch in text):
            return ""

        return text

    @classmethod
    def detect_page_identity(
        cls,
        page
    ) -> Tuple[str, str]:

        """
        Determine what this page's timetable rows are FOR:

            ("teacher", "<name>")   -- Facultywise page
            ("class", "<class>")    -- Classwise page
            ("room", "<room>")      -- Location/room-wise page
            ("", "")                -- could not determine

        This is the single place that decides page orientation,
        so process_page() doesn't need to assume any one layout.
        """

        teacher = cls.detect_teacher(
            page
        )

        if teacher:
            return "teacher", teacher

        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""

        label = cls.detect_page_label_line(
            text
        )

        if not label:
            return "", ""

        class_name = cls.detect_class(
            label
        )

        if class_name:
            return "class", class_name

        room = cls.detect_room(
            label
        )

        if room:
            return "room", room

        room = cls.detect_room_from_label(
            label
        )

        if room:
            return "room", room

        return "", ""

    @classmethod
    def detect_day(
        cls,
        value: Any
    ) -> Optional[str]:

        text = cls.clean_text(
            value
        ).lower()

        return cls.DAY_MAP.get(
            text
        )

    # ==========================================================
    # SLOT HEADER
    # ==========================================================

    @staticmethod
    def parse_slot_header(
        value: Any
    ) -> Optional[int]:

        if value is None:
            return None

        text = str(value).strip()

        match = re.match(
            r"^\s*(\d+)",
            text
        )

        if not match:
            return None

        try:

            slot = int(
                match.group(1)
            )

            if 1 <= slot <= 8:
                return slot

        except ValueError:
            pass

        return None

    # ==========================================================
    # TIME EXTRACTION
    # ==========================================================

    @classmethod
    def parse_time(
        cls,
        value: Any
    ) -> str:

        text = cls.clean_text(
            value
        )

        match = re.search(
            r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})",
            text
        )

        if not match:

            return ""

        h1, m1 = match.group(1).split(":")
        h2, m2 = match.group(2).split(":")

        return (
            f"{int(h1):02d}:{int(m1):02d} - "
            f"{int(h2):02d}:{int(m2):02d}"
        )

    # ==========================================================
    # SLOT HEADER DETECTION
    # ==========================================================

    @classmethod
    def detect_slot_headers(
        cls,
        row: List[Any]
    ) -> Dict[int, Dict[str, Any]]:

        result = {}

        if not row:
            return result

        for index, cell in enumerate(row):

            if cell is None:
                continue

            text = str(cell).strip()

            slot = cls.parse_slot_header(
                text
            )

            if slot is None:
                continue

            slot_time = cls.parse_time(
                text
            )

            if not slot_time:

                slot_time = cls.SLOT_TIMES.get(
                    slot,
                    ""
                )

            result[index] = {
                "slot": slot,
                "time": slot_time,
            }

        return result

    # ==========================================================
    # CLASS DETECTION
    # ==========================================================

    @staticmethod
    def detect_class(
        text: str
    ) -> str:

        if not text:
            return ""

        patterns = [

            # 3CS-DS-A (tolerates a stray space right after a
            # hyphen too, e.g. "5CS-DS- B" from a PDF text-
            # extraction spacing artifact - normalized below)
            r"\b\d+[A-Za-z]{2,}(?:-\s?[A-Za-z0-9]+)+\b",

            # 7CS-IOT
            r"\b\d+[A-Za-z]{2,}[A-Za-z0-9-]*\b",
        ]

        candidates = []

        for pattern in patterns:

            found = re.findall(
                pattern,
                text,
                flags=re.IGNORECASE
            )

            candidates.extend(
                found
            )

        # Normalize away a stray space directly after a hyphen
        # (e.g. "5CS-DS- B" -> "5CS-DS-B") - the pattern above
        # tolerates it on the way in so the match isn't missed
        # entirely, but the class name itself should never
        # contain that space.
        candidates = [
            re.sub(r"-\s+", "-", x)
            for x in candidates
        ]

        if not candidates:
            return ""

        # Remove obvious room-like values
        candidates = [
            x for x in candidates
            if not re.fullmatch(
                r"\d+",
                x
            )
        ]

        if not candidates:
            return ""

        # Prefer class names containing '-'
        dashed = [
            x for x in candidates
            if "-" in x
        ]

        if dashed:
            return max(
                dashed,
                key=len
            )

        return max(
            candidates,
            key=len
        )

    # ==========================================================
    # ROOM DETECTION
    # ==========================================================

    @staticmethod
    def _looks_like_clock_time(
        value: str
    ) -> bool:

        """
        True if `value` is, in its entirety, a clock-time token
        such as "9:00", "12:45", "08:15" - H:MM or HH:MM, minutes
        00-59. This is a purely FORMAT-based check (never a list
        of specific times) so it generalizes to any uploaded
        timetable: a token is excluded because it IS shaped like
        a time, not because it matches some particular observed
        value.

        A real room code that happens to use a colon (e.g.
        "7F:CP7", "7F:EE-Lab13") always has letters on at least
        one side of the colon, so it never matches this pattern -
        only a genuinely all-digits "H:MM" token does.
        """

        return bool(
            re.fullmatch(
                r"\d{1,2}:[0-5]\d",
                value.strip()
            )
        )

    @classmethod
    def detect_room(
        cls,
        text: str
    ) -> str:

        if not text:
            return ""

        # ------------------------------------------------------
        # Strip clock-time RANGES ("8:00 - 10:00", "12:45-14:45")
        # before looking for a room at all.
        #
        # Several of these PDFs' table cells embed the slot's own
        # time range directly in the cell text alongside the
        # actual room (e.g. "8:00 - 10:00 Group 1 A-JAVA Lab/
        # Spoken-Java CL-13 SN"). The room-candidate regexes below
        # were never meant to look inside a time range at all -
        # removing it at the source is more robust than trying to
        # out-guess it candidate-by-candidate afterwards, and
        # naturally also prevents a lone leftover time FRAGMENT
        # (e.g. just the "00" from "10:00") from ever being
        # extracted as a bare 2-4 digit "room" if no real room is
        # present in the cell.
        # ------------------------------------------------------

        text = re.sub(
            r"\d{1,2}:[0-5]\d\s*-\s*\d{1,2}:[0-5]\d",
            " ",
            text
        )

        # ------------------------------------------------------
        # Also strip any remaining STANDALONE clock-time token
        # (not part of a "-" range, e.g. a cell whose only
        # content is a single time like "9:00"). This has to
        # happen at the TEXT level, before the extraction regexes
        # below run, and not only as a candidate-level filter
        # afterwards - otherwise the bare 2-4 digit pattern could
        # still separately pick up a leftover FRAGMENT of the
        # time (e.g. just the "00" from "9:00") once the full
        # colon-token is gone from the candidate list but its
        # digits are still sitting in the text.
        # ------------------------------------------------------

        text = re.sub(
            r"\b\d{1,2}:[0-5]\d\b",
            " ",
            text
        )

        candidates = []

        # CL-15
        candidates.extend(
            re.findall(
                r"\b[A-Za-z]{1,10}-[A-Za-z0-9:]*\d+[A-Za-z0-9:-]*\b",
                text
            )
        )

        # 7F:EE-Lab13
        candidates.extend(
            re.findall(
                r"\b[A-Za-z0-9]+:+[A-Za-z0-9:-]*\d+[A-Za-z0-9:-]*\b",
                text
            )
        )

        # 301 / 103 / 306
        candidates.extend(
            re.findall(
                r"\b\d{2,4}\b",
                text
            )
        )

        # ------------------------------------------------------
        # Generic, reusable defensive layer: even after stripping
        # time RANGES above, never let a candidate through that is
        # itself, in its entirety, a clock-time token - covers any
        # lone (non-ranged) time value a source file might embed
        # in a cell in some other shape.
        # ------------------------------------------------------

        candidates = [
            x for x in candidates
            if not cls._looks_like_clock_time(x)
        ]

        if not candidates:
            return ""

        class_name = (
            PDFImporter.detect_class(
                text
            )
        )

        filtered = [
            x for x in candidates
            if x.lower() != class_name.lower()
        ]

        if filtered:
            candidates = filtered

        # Prefer CL-/Lab-like identifiers
        structured = [
            x for x in candidates
            if "-" in x or ":" in x
        ]

        if structured:
            return structured[-1]

        return candidates[-1]

    # ==========================================================
    # SUBJECT DETECTION
    # ==========================================================

    # ==========================================================
    # GROUP DETECTION
    # ==========================================================

    @staticmethod
    def detect_group(text: str) -> str:

        """
        Find a "Group N" reference anywhere in the cell text
        (not just as the whole line) and return it in a
        normalized "Group N" form, e.g. from raw text like
        "10:00 - 12:00 Group 1 CS Lab CL-19 MA" this returns
        "Group 1". Purely pattern-based (any digit works),
        so it is not tied to any specific lab/subject name.
        """

        if not text:
            return ""

        match = re.search(
            r"\bgroup\s*(\d+)\b",
            text,
            flags=re.IGNORECASE
        )

        if not match:
            return ""

        return f"Group {match.group(1)}"

    # ==========================================================
    # DIVISION / SECTION SUFFIX DETECTION
    #
    # Some timetables split one numbered class (e.g. "5CS") into
    # lettered sections/divisions (e.g. "AI-A", "AI-B") that
    # appear in the cell text as a SEPARATE trailing token, not
    # glued onto the digit-prefixed class code detect_class()
    # already finds. This is a shape-based pattern (short letter
    # group, hyphen, one or two letters) - not a list of known
    # section names - so it generalizes to any uploaded
    # timetable's own section naming.
    #
    # A genuine division suffix always comes AFTER something else
    # in the cell - the subject, a room, and/or the class digits
    # it refines (e.g. "CGMT 404 5CS AI-A"). It is never the very
    # first token. A subject/course CODE that merely happens to
    # share this same shape (e.g. "DV-R") sits right where a
    # cell's subject always starts - at the very beginning - so
    # rejecting a match at position 0 is what tells the two apart
    # without needing to know either string by name.
    # ==========================================================

    @staticmethod
    def detect_division_suffix(text: str) -> str:

        if not text:
            return ""

        match = re.search(
            r"(?<![\w-])[A-Za-z]{1,6}-[A-Za-z]{1,2}(?![\w-])",
            text
        )

        if not match:
            return ""

        if match.start() == 0:
            return ""

        return match.group(0)

    @classmethod
    def detect_subject(
        cls,
        text: str
    ) -> str:

        text = cls.clean_text(
            text
        )

        if not text:
            return ""

        lines = [
            cls.clean_text(x)
            for x in text.splitlines()
            if cls.clean_text(x)
        ]

        if not lines:
            return ""

        # Remove group information
        #
        # Only strip the "Group N" PREFIX from a line, not the
        # whole line. A cell that has "Group 1" as its own
        # separate line (nothing else on it) correctly loses
        # that line entirely once the prefix is removed - but a
        # single-line cell like "Group 1 CS Lab 5F::CP5 SD" must
        # keep "CS Lab 5F::CP5 SD" rather than being discarded
        # wholesale just because it STARTS WITH "Group 1".
        cleaned_lines = []

        for x in lines:

            stripped = re.sub(
                r"^group\s*\d+\s*",
                "",
                x,
                flags=re.IGNORECASE
            ).strip()

            if stripped:
                cleaned_lines.append(stripped)

        lines = cleaned_lines

        if not lines:
            return ""

        class_name = cls.detect_class(
            text
        )

        room = cls.detect_room(
            text
        )

        subject = lines[0]

        if class_name:

            # class_name itself is already normalized (no
            # stray space after a hyphen - see detect_class()),
            # but the raw subject text it needs to be removed
            # FROM might still have one (e.g. "5CS-DS- B"). The
            # removal pattern tolerates that same optional space
            # after each hyphen so it still finds and strips the
            # match.
            class_removal_pattern = re.escape(
                class_name
            ).replace(r"\-", r"-\s?")

            subject = re.sub(
                class_removal_pattern,
                "",
                subject,
                flags=re.IGNORECASE
            )

        if room:

            subject = re.sub(
                re.escape(room),
                "",
                subject,
                flags=re.IGNORECASE
            )

        # Strip a "Group N" reference that appears WITHIN the
        # line (not just when it is the whole line) - e.g.
        # "10:00 - 12:00 Group 1 CS Lab MA" still has "Group 1"
        # stuck in the middle after class/room removal.
        subject = re.sub(
            r"\bgroup\s*\d+\b",
            "",
            subject,
            flags=re.IGNORECASE
        )

        # Strip an embedded clock time-range, e.g.
        # "10:00 - 12:00 CS Lab" -> "CS Lab". This is the SAME
        # sub-block timing already captured separately as
        # slot_time - keeping a second copy inside subject only
        # adds noise.
        subject = re.sub(
            r"\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}",
            "",
            subject
        )

        # Strip a division/section suffix (e.g. "AI-A", "AI-B")
        # that isn't part of the digit-prefixed class code -
        # detect_class()/create_record() are responsible for
        # attaching it to the class instead.
        division_suffix = cls.detect_division_suffix(
            subject
        )

        if division_suffix:

            subject = re.sub(
                re.escape(division_suffix),
                "",
                subject,
                count=1
            )

        subject = cls.clean_text(
            subject
        )

        return subject

    # ==========================================================
    # TYPE DETECTION
    # ==========================================================

    @staticmethod
    def detect_type(
        subject: str
    ) -> str:

        text = str(
            subject or ""
        ).lower()

        if not text:
            return ""

        if "lab" in text:
            return "Lab"

        if "seminar" in text:
            return "Seminar"

        if "tutorial" in text:
            return "Tutorial"

        return "Theory"

    # ==========================================================
    # CREATE RECORD
    # ==========================================================

    @classmethod
    def create_record(
        cls,
        day: str,
        slot: int,
        slot_time: str,
        cell_text: str,
        source_file: str,
        source_page: int,
        teacher: str = "",
        class_name: str = "",
        room: str = "",
        page_identity_type: str = "",
        legend: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:

        """
        Build one timetable record for one (day, slot) cell.

        Exactly one of `teacher` / `class_name` / `room` is
        expected to be the PAGE-LEVEL identity (who/what this
        entire page's grid is FOR), as determined by
        detect_page_identity(). Whichever one is passed wins
        over whatever the same field's cell-text extractor
        would otherwise guess -- the other fields are still
        extracted from the cell text as before.

        `page_identity_type` records WHICH of the three fields
        (if any) was that page-level identity ("teacher" /
        "class" / "room" / "" for unknown/not applicable). This
        is what lets CanonicalEventMatcher.identify_source()
        classify a record from the ACTUAL PAGE CONTENT that
        produced it, instead of only guessing from the source
        filename -- required because the uploaded file may be
        named anything.
        """

        cell_text = (
            cell_text
            if cell_text is not None
            else ""
        )

        cell_text = str(
            cell_text
        ).replace(
            "\xa0",
            " "
        ).strip()

        # ------------------------------------------------------
        # IMPORTANT:
        #
        # DO NOT remove empty cells.
        #
        # Empty cell is required to determine FREE.
        # ------------------------------------------------------

        subject = cls.detect_subject(
            cell_text
        )

        # ------------------------------------------------------
        # PAGE LEGEND RESOLUTION
        #
        # If this page has its own faculty-abbreviation legend
        # (extract_page_legend()), a trailing code left in the
        # subject text after detect_subject()'s own cleanup
        # (e.g. "Project/Spoken-Latex MM") is resolved using
        # THAT PAGE'S own key - "MM" -> whatever name that same
        # page's legend says it means, not a fixed lookup table.
        # ------------------------------------------------------

        legend_teacher = ""

        if legend and subject:

            subject_tokens = subject.split()

            if subject_tokens and (
                subject_tokens[-1] in legend
            ):

                legend_teacher = legend[subject_tokens[-1]]
                subject = " ".join(
                    subject_tokens[:-1]
                ).strip()

        extracted_room = cls.detect_room(
            cell_text
        )

        extracted_class = cls.detect_class(
            cell_text
        )

        extracted_group = cls.detect_group(
            cell_text
        )

        division_suffix = cls.detect_division_suffix(
            re.sub(
                re.escape(extracted_class),
                "",
                cell_text,
                flags=re.IGNORECASE
            ) if extracted_class else cell_text
        )

        # Page-level identity wins for its own field; the cell
        # text is only used to fill in whichever of class/room
        # ISN'T the page's own known identity.
        final_room = room or extracted_room

        final_class = class_name or extracted_class

        final_teacher = teacher or legend_teacher

        # A division/section suffix found in the cell text
        # (e.g. "AI-A", "AI-B") is not itself a full class code -
        # it refines whichever class was already identified
        # (from the page or from a digit-prefixed token in this
        # same cell), e.g. "5CS" + "AI-A" -> "5CS-AI-A". This
        # preserves the distinction between divisions that would
        # otherwise be lost (and previously ended up glued onto
        # "subject" instead).
        if division_suffix:

            if final_class and not final_class.upper().endswith(
                division_suffix.upper()
            ):
                final_class = f"{final_class}-{division_suffix}"

            elif not final_class:
                final_class = division_suffix

        has_content = bool(
            cell_text
        )

        if teacher:

            record_type = (
                "FACULTY_SCHEDULED"
                if has_content
                else "FACULTY_FREE_SLOT"
            )

        elif class_name:

            record_type = (
                "CLASS_SCHEDULED"
                if has_content
                else "CLASS_FREE_SLOT"
            )

        elif room:

            record_type = (
                "ROOM_SCHEDULED"
                if has_content
                else "ROOM_FREE_SLOT"
            )

        else:

            record_type = (
                "SCHEDULED"
                if has_content
                else "FREE_SLOT"
            )

        return {

            "teacher":
                cls.clean_text(
                    final_teacher
                ),

            "day":
                day,

            "slot":
                slot,

            "slot_time":
                slot_time,

            "subject":
                subject,

            "room":
                final_room,

            "class_name":
                final_class,

            "group_name":
                extracted_group,

            "type":
                cls.detect_type(
                    subject
                ),

            "length":
                "",

            "lessons_per_week":
                "",

            "available_classrooms":
                "",

            "cycle":
                "",

            "source_file":
                source_file,

            "source_type":
                "pdf",

            "source_page":
                source_page,

            "raw_text":
                cell_text,

            "record_type":
                record_type,

            "page_identity_type":
                page_identity_type,
        }

    # ==========================================================
    # TABLE EXTRACTION
    # ==========================================================

    @staticmethod
    def extract_tables_from_page(
        page
    ) -> List[List[List[Any]]]:

        try:

            tables = page.extract_tables()

            if tables:
                return tables

        except Exception:
            pass

        return []

    # ==========================================================
    # PROCESS ONE PAGE
    # ==========================================================

    @classmethod
    def process_page(
        cls,
        page,
        page_number: int,
        source_file: str
    ) -> List[Dict[str, Any]]:

        records = []

        # --------------------------------------------------
        # Detect page identity (teacher / class / room /
        # unknown) from actual page content - NOT filename.
        #
        # Unlike the old teacher-only check this used to be,
        # a page is no longer skipped just because it isn't a
        # facultywise page. Classwise and location/room-wise
        # pages identify themselves differently (a bare class
        # or room code immediately above the slot-header row -
        # see detect_page_label_line()), and a page whose
        # identity can't be determined at all is still parsed
        # if it has genuine day/slot table structure - it will
        # simply have blank teacher/class_name/room identity
        # fields (create_record() never invents a value for a
        # field that wasn't actually present).
        # --------------------------------------------------

        identity_type, identity_value = cls.detect_page_identity(
            page
        )

        legend = cls.extract_page_legend(
            page
        )

        teacher = (
            identity_value
            if identity_type == "teacher"
            else ""
        )

        class_name = (
            identity_value
            if identity_type == "class"
            else ""
        )

        room = (
            identity_value
            if identity_type == "room"
            else ""
        )

        # --------------------------------------------------
        # Extract tables
        # --------------------------------------------------

        tables = cls.extract_tables_from_page(
            page
        )

        for table in tables:

            if not table:
                continue

            slot_headers = {}
            header_index = None

            # --------------------------------------------------
            # Find slot header row
            # --------------------------------------------------

            for row_index, row in enumerate(table):

                if not row:
                    continue

                detected = cls.detect_slot_headers(
                    row
                )

                if detected:

                    slot_headers = detected
                    header_index = row_index

                    break

            if not slot_headers:
                continue

            # --------------------------------------------------
            # Process timetable rows
            # --------------------------------------------------

            for row in table[header_index + 1:]:

                if not row:
                    continue

                # --------------------------------------------------
                # Detect day
                # --------------------------------------------------

                day = cls.detect_day(
                    row[0] if len(row) > 0 else ""
                )

                if not day:
                    continue

                # ==================================================
                # MERGED CELL HANDLING
                # ==================================================
                #
                # pdfplumber represents merged cells as:
                #
                #   Slot 1 = "Python for DS Lab..."
                #   Slot 2 = None
                #   Slot 3 = None
                #
                # while a genuinely free cell is:
                #
                #   Slot 4 = ""
                #
                # Therefore:
                #
                #   None -> inherit previous occupied cell
                #   ""   -> genuinely FREE
                #
                # ==================================================

                previous_cell_text = None

                for column_index, slot_info in slot_headers.items():

                    # --------------------------------------------------
                    # Safety
                    # --------------------------------------------------

                    if column_index >= len(row):
                        continue

                    raw_cell = row[column_index]

                    # --------------------------------------------------
                    # MERGED CONTINUATION
                    # --------------------------------------------------

                    if raw_cell is None:

                        if previous_cell_text:

                            cell = previous_cell_text

                        else:

                            cell = ""

                    # --------------------------------------------------
                    # NORMAL CELL
                    # --------------------------------------------------

                    else:

                        cell = cls.clean_text(
                            raw_cell
                        )

                    # --------------------------------------------------
                    # Create record
                    #
                    # create_record() already computes the correct
                    # record_type ("FACULTY_SCHEDULED" /
                    # "CLASS_SCHEDULED" / "ROOM_SCHEDULED" /
                    # "SCHEDULED", and their *_FREE_SLOT
                    # equivalents) from whichever of
                    # teacher/class_name/room is non-empty - there
                    # is no need to (and this must NOT) overwrite
                    # it afterwards, since doing so would mislabel
                    # every class/room page's records as
                    # FACULTY_SCHEDULED/FACULTY_FREE_SLOT.
                    # --------------------------------------------------

                    record = cls.create_record(
                        teacher=teacher,
                        class_name=class_name,
                        room=room,
                        day=day,
                        slot=slot_info["slot"],
                        slot_time=slot_info["time"],
                        cell_text=cell,
                        source_file=source_file,
                        source_page=page_number,
                        page_identity_type=identity_type,
                        legend=legend
                    )

                    records.append(
                        record
                    )

                    # --------------------------------------------------
                    # Update merged-cell state
                    # --------------------------------------------------
                    #
                    # IMPORTANT:
                    #
                    # Only a real cell can start/end a merged region.
                    #
                    # None must NOT reset previous_cell_text.
                    #
                    # Example:
                    #
                    # Slot 1 = BUSY
                    # Slot 2 = None
                    # Slot 3 = None
                    # Slot 4 = BUSY
                    #
                    # becomes:
                    #
                    # Slot 1 = BUSY
                    # Slot 2 = BUSY
                    # Slot 3 = BUSY
                    # Slot 4 = BUSY
                    #
                    # --------------------------------------------------

                    if raw_cell is not None:

                        if cell:

                            previous_cell_text = cell

                        else:

                            # Genuine empty cell.
                            # This breaks the merged region.

                            previous_cell_text = None

        return records

    # ==========================================================
    # IMPORT FILE
    # ==========================================================

    @classmethod
    def import_file(
        cls,
        file_path: str | Path
    ) -> List[Dict[str, Any]]:

        validation = cls.validate_file(
            file_path
        )

        if not validation["valid"]:

            raise ValueError(
                validation["reason"]
            )

        path = Path(
            file_path
        )

        records = []

        with pdfplumber.open(
            path
        ) as pdf:

            for page_number, page in enumerate(
                pdf.pages,
                start=1
            ):

                page_records = (
                    cls.process_page(
                        page,
                        page_number,
                        path.name
                    )
                )

                records.extend(
                    page_records
                )

        return records

    # ==========================================================
    # INSPECT FILE
    # ==========================================================

    @classmethod
    def inspect_file(
        cls,
        file_path: str | Path
    ) -> Dict[str, Any]:

        """
        Diagnostic inspection of a PDF file: reports, per page,
        the CONTENT-detected identity (teacher / class / room /
        unknown - see detect_page_identity()) and whether that
        page produced any records, plus file-level totals.

        This is a standalone diagnostic utility - it is NOT
        called from ImportManager.import_file()'s normal import
        path, because it re-opens and re-walks every page/table
        a second time, roughly doubling PDF processing time for
        no benefit ImportManager actually uses (see
        import_engine/import_manager.py). Call it directly when
        you need the detailed per-page breakdown, e.g. for
        troubleshooting why a particular file produced fewer
        records than expected.
        """

        validation = cls.validate_file(
            file_path
        )

        if not validation["valid"]:

            raise ValueError(
                validation["reason"]
            )

        path = Path(
            file_path
        )

        pages = 0
        pages_with_teacher = 0
        pages_with_class = 0
        pages_with_room = 0
        pages_with_tables = 0
        pages_with_timetable_structure = 0
        total_records = 0
        has_day = False
        has_slot = False

        page_reports: List[Dict[str, Any]] = []

        with pdfplumber.open(
            path
        ) as pdf:

            pages = len(
                pdf.pages
            )

            for page_number, page in enumerate(
                pdf.pages,
                start=1
            ):

                identity_type, identity_value = (
                    cls.detect_page_identity(
                        page
                    )
                )

                if identity_type == "teacher":
                    pages_with_teacher += 1
                elif identity_type == "class":
                    pages_with_class += 1
                elif identity_type == "room":
                    pages_with_room += 1

                tables = cls.extract_tables_from_page(
                    page
                )

                if tables:
                    pages_with_tables += 1

                page_records = cls.process_page(
                    page,
                    page_number,
                    path.name
                )

                if page_records:
                    pages_with_timetable_structure += 1

                total_records += len(
                    page_records
                )

                page_failure_reason = ""

                if not page_records:

                    if not tables:
                        page_failure_reason = (
                            "No table structure detected "
                            "on this page."
                        )
                    elif not identity_type:
                        page_failure_reason = (
                            "A table was found, but no "
                            "day/slot timetable grid could "
                            "be matched to it (and no "
                            "teacher/class/room identity "
                            "was detected either)."
                        )
                    else:
                        page_failure_reason = (
                            "A page identity was detected, "
                            "but no day/slot timetable rows "
                            "could be matched in its "
                            "table(s)."
                        )

                for record in page_records:

                    if record.get("day"):
                        has_day = True

                    if record.get("slot") is not None:
                        has_slot = True

                page_reports.append(
                    {
                        "page":
                            page_number,

                        "identity_type":
                            identity_type or "unknown",

                        "identity_value":
                            identity_value,

                        "has_table":
                            bool(tables),

                        "records":
                            len(page_records),

                        "reason":
                            page_failure_reason,
                    }
                )

        pages_with_identity = (
            pages_with_teacher
            + pages_with_class
            + pages_with_room
        )

        identity_kinds_present = sum(
            1
            for count in (
                pages_with_teacher,
                pages_with_class,
                pages_with_room,
            )
            if count > 0
        )

        if identity_kinds_present > 1:

            dataset_type = "MIXED"

        elif pages_with_teacher:

            dataset_type = "FACULTYWISE"

        elif pages_with_class:

            dataset_type = "CLASSWISE"

        elif pages_with_room:

            dataset_type = "LOCATIONWISE"

        else:

            dataset_type = "UNKNOWN"

        return {

            "file":
                path.name,

            "size_bytes":
                validation["size_bytes"],

            "size_mb":
                validation["size_mb"],

            "pages":
                pages,

            "pages_with_teacher":
                pages_with_teacher,

            "pages_with_class":
                pages_with_class,

            "pages_with_room":
                pages_with_room,

            "pages_with_identity":
                pages_with_identity,

            "pages_with_tables":
                pages_with_tables,

            "pages_with_timetable_structure":
                pages_with_timetable_structure,

            "records":
                total_records,

            "has_day":
                has_day,

            "has_slot":
                has_slot,

            "dataset_type":
                dataset_type,

            "page_reports":
                page_reports,
        }


# ============================================================
# DIRECT TEST
# ============================================================

if __name__ == "__main__":

    print("=" * 80)

    print(
        "UNISCHED AI - PDF IMPORTER"
    )

    print("=" * 80)

    pdf = (
        "data/Facultywise TT 20 sep.pdf"
    )

    records = (
        PDFImporter.import_file(
            pdf
        )
    )

    print(
        "Imported records:",
        len(records)
    )

    archika = [

        r for r in records

        if (
            "archika"
            in str(
                r.get(
                    "teacher",
                    ""
                )
            ).lower()
        )

        and r.get("day")
        == "monday"
    ]

    print()
    print(
        "ARCHIKA MONDAY"
    )

    print("-" * 80)

    for record in archika:

        print(
            record
        )