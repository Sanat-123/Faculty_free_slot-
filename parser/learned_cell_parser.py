"""
Document-learned timetable cell parser
======================================

Splits a timetable cell such as

    "DSA Lab CL-11\\n3CSAI-A\\nGroup 1"
    "OS V 106\\n5CS-DS-\\nB"

into subject / room / class / group / type WITHOUT any list of rooms,
class-code format, department or college baked into the code.

Everything is learned from the cells of the document being imported:

* layout   - line 1 is "<subject> <room>", the next line(s) are the class,
             and a last line whose first word repeats across the document
             ("Group 1", "Batch 2", ...) is the group.  A line that ends with
             a hyphen continues on the next line ("5CS-DS-" + "B").
* rooms    - a room is the tail of line 1 that, across the whole document, is
             (almost) only ever found at the END of a first line and recurs.
             Subject words ("Lab", "III") also appear in the middle of lines,
             so they are not mistaken for rooms.  A word that is always
             glued directly in front of a room joins it ("Central" + "Lib").
             For very small documents a token that mixes letters and digits
             at the end of line 1 is accepted as a room.
* classes  - the learned class line, with the commonest spelling of each
             class (ignoring spaces / case) used everywhere.

The only language knowledge, the words that mark a "Lab" / "Seminar" /
"Project" session, comes from config/nlu_lexicon.json ("type_keywords").
"""

from __future__ import annotations

import re
from collections import Counter

MIN_ROOM_COUNT = 2        # a room must recur this often to be "learned"
MIN_PURITY = 0.9          # share of its occurrences that are line-final
MAX_ROOM_WORDS = 3

DEFAULT_TYPE_KEYWORDS = {
    "Lab": ["lab"],
    "Seminar": ["seminar"],
    "Project": ["project"],
}


def _key(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def split_lines(cell):
    """Layout only: (first_line, [continuation lines merged on trailing '-'])."""

    lines = [x.strip() for x in str(cell or "").split("\n") if x.strip()]

    if not lines:
        return "", []

    merged = []

    for line in lines[1:]:

        if merged and merged[-1].endswith("-"):
            merged[-1] += line
        else:
            merged.append(line)

    return lines[0], merged


class CellVocabulary:
    """Statistics of one document, used to parse each of its cells."""

    def __init__(self, cells, type_keywords=None):

        self.type_keywords = type_keywords or DEFAULT_TYPE_KEYWORDS

        self.any_count = Counter()
        self.end_count = Counter()
        self.token_count = Counter()
        self.group_words = set()
        self.class_spelling = {}
        self.multi_word_rooms = []

        self._learn(list(cells))

    # ------------------------------------------------------------------
    # learning
    # ------------------------------------------------------------------

    def _learn(self, cells):

        group_starts = Counter()
        multi_line = 0
        spellings = {}

        for cell in cells:

            first, rest = split_lines(cell)

            words = first.split()

            for n in range(1, MAX_ROOM_WORDS + 1):
                for i in range(len(words) - n + 1):
                    gram = " ".join(words[i:i + n])
                    self.any_count[gram] += 1
                    if i + n == len(words):
                        self.end_count[gram] += 1

            for word in words:
                self.token_count[word] += 1

            # a repeated first word on the LAST extra line = the group marker
            if len(rest) >= 2:
                multi_line += 1
                group_starts[rest[-1].split()[0].lower()] += 1

        if multi_line:
            word, count = group_starts.most_common(1)[0]
            if count / multi_line >= 0.6:
                self.group_words.add(word)

        # commonest spelling of every class (ignoring spaces and case)
        for cell in cells:
            for line in self._class_lines(cell):
                spellings.setdefault(_key(line), Counter())[line] += 1

        self.class_spelling = {
            k: c.most_common(1)[0][0] for k, c in spellings.items()
        }

        # rooms found so far; multi-word ones let us recognise a bare first
        # word ("IAI") as the room "IAI Lab" when it appears only once
        found = Counter()

        for cell in cells:
            first, _ = split_lines(cell)
            words = first.split()
            n, room = self._room_of(words)
            if n:
                found[room] += 1

        self.multi_word_rooms = [
            r for r, c in found.items() if len(r.split()) > 1
        ]

    def _is_group_line(self, line):

        first = line.split()[0].lower() if line.split() else ""

        return first in self.group_words

    def _class_lines(self, cell):

        _, rest = split_lines(cell)

        return [ln for ln in rest if not self._is_group_line(ln)][:1]

    def purity(self, gram):

        total = self.any_count[gram]

        return self.end_count[gram] / total if total else 0.0

    # ------------------------------------------------------------------
    # room detection
    # ------------------------------------------------------------------

    def _room_of(self, words):
        """Return (number of trailing words forming the room, room text)."""

        if len(words) < 2:
            return 0, ""

        # shortest line-final n-gram that is "pure" and recurs
        for n in range(1, min(MAX_ROOM_WORDS, len(words) - 1) + 1):

            gram = " ".join(words[-n:])

            if (
                self.end_count[gram] >= MIN_ROOM_COUNT
                and self.purity(gram) >= MIN_PURITY
            ):
                n = self._extend_left(words, n)
                return n, " ".join(words[-n:])

        # a bare first word of a known multi-word room ("IAI" -> "IAI Lab")
        for k in range(1, min(MAX_ROOM_WORDS, len(words) - 1) + 1):

            tail = " ".join(words[-k:])

            for room in self.multi_word_rooms:
                if room.startswith(tail + " "):
                    return k, room

        # small documents: letters+digits at the end ("CL-11", "ECL-08", "303")
        last = words[-1]

        if re.search(r"\d", last) and len(last) <= 12 and not re.fullmatch(
            r"[ivxlcdm]+", last.lower()
        ):
            return 1, last

        return 0, ""

    def _extend_left(self, words, n):
        """Absorb words that are always directly in front of this room."""

        while len(words) - n - 1 >= 1:

            left = words[-n - 1]

            if left.startswith("(") and left.endswith(")"):
                break                       # "(T)" / "(P)" mark the session

            gram = " ".join(words[-n - 1:])

            if (
                self.token_count[left] >= MIN_ROOM_COUNT
                and self.any_count[gram] == self.token_count[left]
            ):
                n += 1
            else:
                break

        return n

    # ------------------------------------------------------------------
    # parsing
    # ------------------------------------------------------------------

    def parse(self, cell):

        if not cell or not str(cell).strip():
            return None

        first, rest = split_lines(cell)

        words = first.split()
        n_room, room = self._room_of(words)

        subject_words = words[:-n_room] if n_room else words

        # "7F:L9" / "5F::CP6": a floor label glued to the room by ':'
        if ":" in room:
            room = room.split(":")[-1]

        # stray labels that end with ':' are not part of the subject
        subject_words = [w for w in subject_words if not w.endswith(":")]

        class_name, group = "", ""

        for line in rest:

            if self._is_group_line(line):
                group = (group + " " + line).strip()

            elif not class_name:
                class_name = self.class_spelling.get(_key(line), line)

            else:
                group = (group + " " + line).strip()

        subject = " ".join(subject_words).strip()

        if not subject:
            return None

        return {
            "subject": subject,
            "room": room,
            "class": class_name,
            "group": group,
            "type": self.detect_type(subject),
        }

    def detect_type(self, subject):

        low = subject.lower()

        for type_name, words in self.type_keywords.items():
            if any(w in low for w in words):
                return type_name

        return "Theory"