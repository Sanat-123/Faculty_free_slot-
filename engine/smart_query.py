"""
SmartQueryEngine
================

A data-driven natural-language layer for timetable questions.

* Every entity it recognises (teachers, subjects, classes, rooms, groups,
  days, slots and their clock times) is learned from the TimetableModel,
  i.e. from whatever timetable was loaded.  Nothing about a particular
  college is hard-coded.
* The only language knowledge lives in config/nlu_lexicon.json (generic
  English / Hinglish cue words such as "free", "busy", "who", "how many").
* One question -> one Frame (entities + day/slot/time + cues) -> one
  handler.  Handlers only read the model, so answers are always consistent
  with the loaded data.

`answer(query)` returns Markdown text, or None when the question belongs
to another part of the system (absence planning, exam duty, lab shifts ...)
so the caller can hand it on.
"""

import calendar
import datetime
import re
from collections import defaultdict

from rapidfuzz import fuzz, process

from engine.smart_format import title_day
from engine.smart_handlers_catalog import CatalogHandlers
from engine.smart_handlers_faculty import FacultyHandlers
from engine.timetable_model import DAY_ORDER, fmt_minutes
from utils.faculty_names import (
    alnum_key,
    load_lexicon,
    name_tokens,
    strip_title,
)

KIND_PRIORITY = ("teacher", "class", "room", "subject", "group")

# Cue groups that express WHAT is being asked (as opposed to structural
# words such as slot / day).  A follow-up such as "What about slot 4?"
# has none of these, so it re-runs the previous question.
INTENT_CUES = {
    "free", "busy", "who", "how_many", "most", "least", "timetable",
    "where", "teaches", "doing", "compare", "meeting", "common",
    "makeup", "conflict", "data_quality", "consecutive", "gap",
    "completely", "first_last_class", "works", "lab_words",
    "span", "nosession", "subject_words", "room_words", "listing",
    "class_words", "teacher_words", "group_words", "when",
}


def _phrase_re(phrases):

    ordered = sorted({p for p in phrases if p}, key=len, reverse=True)

    if not ordered:
        return re.compile(r"(?!x)x")

    body = "|".join(re.escape(p) for p in ordered)

    return re.compile(r"(?<![a-z0-9§])(?:" + body + r")(?![a-z0-9])")


class Frame:
    """Everything understood about one question."""

    def __init__(self, raw):

        self.raw = raw
        self.text = ""
        self.words = []

        self.days = []
        self.unknown_days = []
        self.all_days = False
        self.rel_word = None

        self.slots = []
        self.slot_mode = "all"
        self.slot_ref = None
        self.bad_slots = []
        self.time_range = None
        self.time_point = None

        self.teachers = []
        self.subjects = []
        self.classes = []
        self.rooms = []
        self.groups = []

        self.unresolved = []
        self.ambiguous = []

        self.cues = set()
        self.polarity = None

        self.number = None
        self.block_size = None

    def copy(self):

        clone = Frame(self.raw)

        for key, value in self.__dict__.items():

            if isinstance(value, list):
                setattr(clone, key, list(value))
            elif isinstance(value, set):
                setattr(clone, key, set(value))
            else:
                setattr(clone, key, value)

        return clone


class SmartQueryEngine(FacultyHandlers, CatalogHandlers):

    def __init__(self, model, lexicon=None, now_fn=None, debug=False):

        self.model = model
        self.lex = lexicon or load_lexicon()
        self.now_fn = now_fn or datetime.datetime.now
        self.debug = debug

        self.titles = set(self.lex.get("titles", []))
        self.stop = set(self.lex.get("stopwords", []))
        self.cutoff = int(self.lex.get("spell_cutoff", 84))
        self.short_max = int(self.lex.get("short_entity_max_len", 3))

        phrases = self.lex.get("phrases", {})

        self.cue_re = {
            group: _phrase_re(items) for group, items in phrases.items()
        }

        self.rel_days = dict(self.lex.get("relative_days", {}))
        self.ordinals = dict(self.lex.get("ordinal_words", {}))
        self.number_words = dict(self.lex.get("number_words", {}))

        self._build_day_words()
        self._build_entity_index()
        self._build_vocab()

        self.last = None
        self.last_route = None

    # ==================================================================
    # construction
    # ==================================================================

    @classmethod
    def from_matcher(cls, matcher, **kwargs):

        from engine.timetable_model import TimetableModel

        return cls(TimetableModel.from_matcher(matcher), **kwargs)

    def _build_day_words(self):

        self.day_map = {}

        for name, abbr in zip(calendar.day_name, calendar.day_abbr):
            self.day_map[name.lower()] = name.lower()
            self.day_map[abbr.lower()] = name.lower()

        for alias, full in self.lex.get("day_aliases", {}).items():
            self.day_map[alias.lower()] = full.lower()

        body = "|".join(
            re.escape(k) for k in sorted(self.day_map, key=len, reverse=True)
        )

        self.day_re = re.compile(
            r"(?<![a-z0-9§])(" + body + r")(?![a-z0-9])"
        )

        rel = "|".join(
            re.escape(k) for k in sorted(self.rel_days, key=len, reverse=True)
        )

        self.rel_re = re.compile(
            r"(?<![a-z0-9§])(" + rel + r")(?![a-z0-9])"
        ) if rel else re.compile(r"(?!x)x")

    def _build_entity_index(self):

        m = self.model

        self.entity_index = defaultdict(list)
        self.numeric_rooms = {}

        def add(kind, canonical, key, forms):

            if not key:
                return

            for existing in self.entity_index[key]:
                if existing[0] == kind and existing[1] == canonical:
                    existing[2].update(forms)
                    return

            self.entity_index[key].append((kind, canonical, set(forms)))

        for teacher in list(m.faculty) + list(m.codes):

            forms = {teacher, strip_title(teacher)}

            add("teacher", teacher, alnum_key(teacher), forms)
            add("teacher", teacher, alnum_key(strip_title(teacher)), forms)

        for subject in m.subjects:
            add("subject", subject, alnum_key(subject), {subject})

        for class_name in m.classes:
            add("class", class_name, alnum_key(class_name), {class_name})

        for room in m.rooms:

            key = alnum_key(room)

            if key.isdigit():
                self.numeric_rooms[key] = room
            else:
                add("room", room, key, {room})

        for group in m.groups:
            add("group", group, alnum_key(group), {group})

        self.teacher_token_vocab = defaultdict(set)

        for teacher in m.faculty:
            for token in name_tokens(teacher, self.lex):
                if len(token) >= 3:
                    self.teacher_token_vocab[token].add(teacher)

        self.subject_token_vocab = defaultdict(set)

        for subject in m.subjects:
            for token in re.findall(r"[a-z0-9]+", subject.lower()):
                if len(token) >= 3:
                    self.subject_token_vocab[token].add(subject)

        self.class_keys = sorted(
            alnum_key(c) for c in m.classes
        )

    def _build_vocab(self):

        vocab = set()

        for items in self.lex.get("phrases", {}).values():
            for phrase in items:
                for word in re.findall(r"[a-z]+", phrase.lower()):
                    vocab.add(word)

        vocab |= set(self.stop)
        vocab |= set(self.titles)
        vocab |= set(self.day_map)
        vocab |= set(self.rel_days)
        vocab |= set(self.ordinals)
        vocab |= set(self.number_words)

        self.vocab = {w for w in vocab if w}

        self.vocab_list = sorted(w for w in self.vocab if len(w) >= 3)

        self.entity_tokens = (
            set(self.teacher_token_vocab)
            | set(self.subject_token_vocab)
        )

    # ==================================================================
    # public API
    # ==================================================================

    def answer(self, query):
        """Markdown answer, or None when this layer does not own the question."""

        query = str(query or "").strip()

        self.last_route = None

        if not query:
            return None

        try:

            frame = self.parse(query)

            return self._respond(frame)

        except Exception:

            if self.debug:
                raise

            return None

    def examples(self):
        """Example questions built from the loaded data (nothing fixed)."""

        m = self.model

        if not m.faculty or not m.days or not m.teaching_slots:
            return []

        teacher = max(m.faculty, key=lambda t: len(m.by_teacher[t]))

        class_key = max(m.by_class, key=lambda k: len(m.by_class[k]))
        class_name = m.by_class[class_key][0]["class_name"]

        subject_key = max(m.by_subject, key=lambda k: len(m.by_subject[k]))
        subject = m.by_subject[subject_key][0]["subject"]

        day = title_day(m.days[0])
        slot = m.teaching_slots[min(2, len(m.teaching_slots) - 1)]

        return [
            f"Who is free on {day} slot {slot}?",
            f"Who is busy on {title_day(m.days[-1])} slot {m.teaching_slots[0]}?",
            f"Is {teacher} free on {day} slot {slot}?",
            f"Show timetable of {teacher}",
            f"Show timetable of {class_name}",
            f"Who teaches {subject}?",
            f"Where is {subject} held?",
            f"Which rooms are free on {day} slot {slot}?",
            f"Who is free in slots {slot} and {slot + 1} on {day}?",
            f"Who has the heaviest workload?",
            f"Is any teacher scheduled in two places at the same time?",
        ]

    def help_text(self):

        lines = [self.lex.get("help_intro", "You can ask:"), ""]

        for example in self.examples():
            lines.append(f"- {example}")

        lines.append("")
        lines.append(
            "I also handle staff absence / substitutes, exam duty and lab "
            "room shifts."
        )

        return "\n".join(lines)

    def fallback_text(self):

        base = self.lex.get("messages", {}).get(
            "fallback", "I couldn't map that to a timetable question."
        )

        samples = self.examples()[:4]

        if not samples:
            return base

        return base + " Try, for example:\n\n" + "\n".join(
            f"- {s}" for s in samples
        )

    # ==================================================================
    # tokenising / entity resolution
    # ==================================================================

    @staticmethod
    def _tokenize(query):

        words = []

        for match in re.finditer(r"\S+", query):

            raw = match.group(0)

            clean = raw.strip(" ,;?!\"'()[]{}")
            clean = re.sub(r"['’]s$", "", clean)
            clean = clean.rstrip(".")

            if not clean:
                continue

            words.append({
                "raw": raw,
                "clean": clean,
                "low": clean.lower(),
                "kind": None,
                "eid": None,
                "entity": None,
            })

        return words

    def _case_ok(self, hit, text, key):

        if len(key) > self.short_max:
            return True

        return text in hit[2]

    def _mark(self, words, i, length, kind, entity, eid):

        for k in range(i, i + length):
            words[k]["kind"] = kind
            words[k]["entity"] = entity
            words[k]["eid"] = eid

    def _resolve_exact(self, words, f):

        n = len(words)
        eid = 0
        found = []

        for length in range(min(8, n), 0, -1):

            for i in range(0, n - length + 1):

                if any(words[k]["kind"] for k in range(i, i + length)):
                    continue

                text = " ".join(w["clean"] for w in words[i:i + length])
                key = alnum_key(text)

                hits = self.entity_index.get(key)

                if not hits:
                    continue

                hits = [h for h in hits if self._case_ok(h, text, key)]

                if not hits:
                    continue

                hits.sort(key=lambda h: KIND_PRIORITY.index(h[0]))

                kind, canonical, _ = hits[0]

                eid += 1

                self._mark(words, i, length, kind, canonical, eid)

                found.append((i, kind, canonical))

        found.sort()

        for _, kind, canonical in found:
            self._attach(f, kind, canonical)

        return eid

    @staticmethod
    def _attach(f, kind, canonical):

        target = {
            "teacher": f.teachers,
            "subject": f.subjects,
            "class": f.classes,
            "room": f.rooms,
            "group": f.groups,
        }[kind]

        if canonical not in target:
            target.append(canonical)

    def _eligible(self, word):

        low = word["low"]

        return (
            word["kind"] is None
            and re.fullmatch(r"[a-z]{3,}", low) is not None
            and low not in self.vocab
            and low not in self.day_map
        )

    def _runs(self, words):
        """Maximal runs of consecutive eligible words -> [(start, end)]"""

        runs = []
        start = None

        for i, w in enumerate(words):

            if self._eligible(w):
                if start is None:
                    start = i
            else:
                if start is not None:
                    runs.append((start, i))
                    start = None

        if start is not None:
            runs.append((start, len(words)))

        return runs

    def _match_token(self, token, vocab):
        """Exact, else a close typo (length >= 5)."""

        if token in vocab:
            return token

        if len(token) >= 5:

            hit = process.extractOne(
                token, list(vocab), scorer=fuzz.ratio, score_cutoff=88
            )

            if hit:
                return hit[0]

        return None

    def _candidates(self, tokens, vocab_map, fuzzy):

        sets = []

        for token in tokens:

            matched = token if token in vocab_map else (
                self._match_token(token, vocab_map) if fuzzy else None
            )

            if matched is None:
                return set()

            sets.append(vocab_map[matched])

        result = set(sets[0])

        for s in sets[1:]:
            result &= s

        return result

    def _resolve_partial_teachers(self, words, f):

        eid_base = 1000

        for start, end in self._runs(words):

            has_title = (
                start > 0 and words[start - 1]["low"] in self.titles
            )

            tokens = [words[i]["low"] for i in range(start, end)]

            best = None

            for length in range(len(tokens), 0, -1):

                for offset in range(0, len(tokens) - length + 1):

                    sub = tokens[offset:offset + length]

                    fuzzy = has_title or length >= 2

                    cands = self._candidates(
                        sub, self.teacher_token_vocab, fuzzy
                    )

                    if cands:
                        best = (offset, length, cands)
                        break

                if best:
                    break

            if best is None:

                if has_title:
                    self._unresolved_teacher(words, f, start, end)

                continue

            offset, length, cands = best

            first, last = start + offset, start + offset + length

            single = length == 1
            token = tokens[offset]

            weak_single = (
                single
                and not has_title
                and (
                    token in self.subject_token_vocab
                    or len(token) < 4
                )
            )

            if weak_single:
                continue

            if len(cands) == 1:

                teacher = next(iter(cands))

                eid_base += 1

                self._mark(words, first, last - first, "teacher",
                           teacher, eid_base)

                self._attach(f, "teacher", teacher)

            else:

                if has_title or length >= 2 or "teacher_words" in \
                        self._peek_cues(words):

                    eid_base += 1

                    text = " ".join(
                        words[i]["clean"] for i in range(first, last)
                    )

                    self._mark(words, first, last - first, "teacher",
                               None, eid_base)

                    f.ambiguous.append(
                        ("teacher", text, sorted(cands, key=str.lower))
                    )

    def _peek_cues(self, words):

        text = " ".join(w["low"] for w in words if not w["kind"])

        return {
            g for g, rx in self.cue_re.items() if rx.search(text)
        }

    def _unresolved_teacher(self, words, f, start, end):

        title_index = start - 1

        text = " ".join(
            words[i]["clean"] for i in range(title_index, end)
        )

        typed = " ".join(words[i]["low"] for i in range(start, end))

        names = list(self.model.faculty) + list(self.model.codes)

        hits = process.extract(
            typed,
            [strip_title(n).lower() for n in names],
            scorer=fuzz.WRatio,
            limit=3,
            score_cutoff=70,
        )

        suggestions = [names[h[2]] for h in hits]

        for i in range(title_index, end):
            words[i]["kind"] = "unresolved"
            words[i]["eid"] = 5000 + title_index

        f.unresolved.append(("teacher", text, suggestions))

    def _spell_fix(self, words):

        for w in words:

            low = w["low"]

            if (
                w["kind"] is not None
                or len(low) < 3
                or not re.fullmatch(r"[a-z]+", low)
                or low in self.vocab
                or low in self.entity_tokens
            ):
                continue

            hit = process.extractOne(
                low, self.vocab_list, scorer=fuzz.ratio,
                score_cutoff=self.cutoff,
            )

            if hit:
                w["low"] = hit[0]

    def _resolve_partial_subjects(self, words, f):

        cues = self._peek_cues(words)

        wants_subject = bool(
            cues & {"teaches", "subject_words", "where", "when",
                    "how_many", "timetable", "who"}
        )

        if not wants_subject:
            return

        eid = 2000

        for start, end in self._runs(words):

            tokens = [words[i]["low"] for i in range(start, end)]

            cands = self._candidates(
                tokens, self.subject_token_vocab, fuzzy=False
            )

            if not cands:
                continue

            eid += 1

            self._mark(words, start, end - start, "subject",
                       sorted(cands)[0], eid)

            for subject in sorted(cands):
                self._attach(f, "subject", subject)

    def _mark_leftover_subject(self, words, f):
        """'Who teaches Xyz?' where Xyz matches nothing at all."""

        cues = self._peek_cues(words)

        if not ({"teaches"} & cues and "who" in cues):
            return

        if f.teachers or f.subjects or f.classes or f.rooms:
            return

        runs = self._runs(words)

        if not runs:
            return

        start, end = runs[0]

        text = " ".join(words[i]["clean"] for i in range(start, end))

        subjects = list(self.model.subjects)

        hits = process.extract(
            text.lower(), [s.lower() for s in subjects],
            scorer=fuzz.WRatio, limit=3, score_cutoff=70,
        )

        f.unresolved.append(
            ("subject", text, [subjects[h[2]] for h in hits])
        )

        for i in range(start, end):
            words[i]["kind"] = "unresolved"
            words[i]["eid"] = 6000

    # ==================================================================
    # parse
    # ==================================================================

    def parse(self, query):

        f = Frame(query)

        words = self._tokenize(query)

        self._resolve_exact(words, f)
        self._resolve_partial_teachers(words, f)
        self._spell_fix(words)
        self._resolve_partial_subjects(words, f)
        self._mark_leftover_subject(words, f)

        f.words = words

        text = self._build_text(words)

        self._extract_block_size(f, text)
        self._extract_threshold(f, text)

        text = self._extract_days(f, text)

        residual = self._extract_slots(f, text)

        self._resolve_numeric_rooms(f, words, residual)
        self._detect_unresolved_class(f, words)

        f.text = text

        self._detect_cues(f, text)

        return f

    @staticmethod
    def _build_text(words):

        out = []
        last_eid = None

        for w in words:

            if w["kind"]:

                if w["eid"] != last_eid:
                    out.append("§" + w["kind"][0])

                last_eid = w["eid"]

            else:
                out.append(w["low"])
                last_eid = None

        text = " ".join(out)

        text = re.sub(r"(?<=\d),(?=\d)", " , ", text)

        return text

    # ------------------------------------------------------------------

    def _to_number(self, token):

        token = token.strip().lower()

        if token.isdigit():
            return int(token)

        return self.number_words.get(token)

    def _extract_block_size(self, f, text):

        words = "|".join(re.escape(w) for w in self.number_words)

        patterns = [
            rf"\b(\d+|{words})[\s-]*(?:slots?|periods?)[\s-]*"
            rf"(?:block|window|stretch)",
            rf"\b(?:block|window)s?\s+of\s+(\d+|{words})\s+"
            rf"(?:slots?|periods?)",
        ]

        for pattern in patterns:

            found = re.search(pattern, text)

            if found:
                f.block_size = self._to_number(found.group(1))
                return

    def _extract_threshold(self, f, text):

        words = "|".join(re.escape(w) for w in self.number_words)

        found = re.search(
            rf"(?:more than|over|above|greater than|exceed(?:s|ing)?|"
            rf"at least|>)\s*(\d+|{words})\b",
            text,
        )

        if found:
            f.number = self._to_number(found.group(1))

    def _extract_days(self, f, text):

        model = self.model

        for match in self.day_re.finditer(text):

            name = self.day_map[match.group(1)]

            if name in model.days:
                if name not in f.days:
                    f.days.append(name)
            elif name not in f.unknown_days:
                f.unknown_days.append(name)

        rel = self.rel_re.search(text)

        if rel:

            f.rel_word = rel.group(1)

            today = self.now_fn()

            name = DAY_ORDER[
                (today.weekday() + self.rel_days[rel.group(1)]) % 7
            ]

            if name in model.days:
                if name not in f.days:
                    f.days.append(name)
            elif name not in f.unknown_days:
                f.unknown_days.append(name)

        if self.cue_re.get("all_days") and \
                self.cue_re["all_days"].search(text):
            f.all_days = True

        return text

    # ------------------------------------------------------------------

    def _expand_list(self, chunk):
        """'4 and 5' / '6 to 8' / '3, 4, 5' / '2 or 3' -> (numbers, mode)"""

        tokens = re.findall(
            r"\d+|,|&|and|or|to|-|through|till|until", chunk
        )

        numbers = []
        mode = "all"
        pending_range = False

        for token in tokens:

            if token.isdigit():

                n = int(token)

                if pending_range and numbers:

                    a = numbers[-1]

                    if a < n and n - a <= 30:
                        numbers.extend(range(a + 1, n + 1))
                    else:
                        numbers.append(n)

                    pending_range = False

                else:
                    numbers.append(n)

            elif token in ("to", "-", "through", "till", "until"):
                pending_range = True

            elif token == "or":
                mode = "any"

        return numbers, mode

    def _extract_slots(self, f, text):

        model = self.model

        work = text

        # ---- clock times -------------------------------------------
        clock = r"(\d{1,2})[:.](\d{2})\s*(am|pm)?"

        def to_min(h, mi, ap):

            h, mi = int(h), int(mi)

            if ap == "pm" and h < 12:
                h += 12
            if ap == "am" and h == 12:
                h = 0

            return h * 60 + mi

        rng = re.search(
            rf"{clock}\s*(?:-|–|to|and|till|until)\s*{clock}", work
        )

        if rng:

            a = to_min(rng.group(1), rng.group(2), rng.group(3))
            b = to_min(rng.group(4), rng.group(5), rng.group(6))

            if b < 12 * 60 and b < a:
                b += 12 * 60

            f.time_range = (a, b) if a <= b else (b, a)

            work = work[:rng.start()] + " " + work[rng.end():]

        else:

            point = re.search(clock, work)

            if point:

                f.time_point = to_min(
                    point.group(1), point.group(2), point.group(3)
                )

                work = work[:point.start()] + " " + work[point.end():]

        # ---- number joiners ----------------------------------------
        work = re.sub(r"(?<=\d)\s*-\s*(?=\d)", " to ", work)

        found = []

        slot_word = r"(?:slots?|periods?|lectures?|hours?)"
        num = r"\d{1,2}"
        conn = r"(?:\s*(?:,|&|and|or|to|through|till|until)\s*)"

        patterns = [
            rf"{slot_word}\s*(?:no\.?\s*|number\s*|#\s*)?"
            rf"(?P<list>{num}(?:{conn}(?:{slot_word}\s*)?{num})*)",
            rf"(?P<list>{num})(?:st|nd|rd|th)\s*{slot_word}",
        ]

        day_or_rel = "|".join(
            re.escape(k)
            for k in sorted(
                list(self.day_map) + list(self.rel_days),
                key=len, reverse=True,
            )
        )

        patterns.append(
            rf"(?<![a-z0-9§])(?:{day_or_rel})\s+"
            rf"(?P<list>{num}(?:{conn}{num})*)(?![\d:.]|\s*"
            rf"(?:st|nd|rd|th|am|pm))"
        )

        ordinal_words = "|".join(
            re.escape(w) for w in self.ordinals
            if w not in ("first",)
        )

        if ordinal_words:
            patterns.append(
                rf"(?P<word>{ordinal_words})\s+{slot_word}"
            )

        for pattern in patterns:

            for match in list(re.finditer(pattern, work)):

                if "word" in match.groupdict() and match.group("word"):
                    found.append(([self.ordinals[match.group("word")]],
                                  "all"))
                else:
                    found.append(self._expand_list(match.group("list")))

            work = re.sub(pattern, " ", work)

        numbers = []

        for nums, mode in found:

            numbers.extend(nums)

            if mode == "any":
                f.slot_mode = "any"

        f.slots = []

        for n in numbers:

            if n in model.slots:
                if n not in f.slots:
                    f.slots.append(n)
            elif n not in f.bad_slots:
                f.bad_slots.append(n)

        # ---- slot references ---------------------------------------
        def has(group):

            rx = self.cue_re.get(group)

            return bool(rx and rx.search(text))

        if has("first_slot"):
            f.slot_ref = "first"
        elif has("last_slot"):
            f.slot_ref = "last"
        elif has("all_slots"):
            f.slot_ref = "all"
        elif has("next"):
            f.slot_ref = "next"
        elif has("now") and not (
            f.slots or f.days or f.time_range or f.time_point
        ):
            f.slot_ref = "current"

        return work

    def _resolve_numeric_rooms(self, f, words, residual):

        has_room_word = bool(
            self.cue_re.get("room_words")
            and self.cue_re["room_words"].search(residual)
        )

        for match in re.finditer(r"(?<![:\d.])(\d{2,4})(?![:\d])", residual):

            key = match.group(1)

            if key in self.numeric_rooms and (
                len(key) >= 3 or has_room_word
            ):

                room = self.numeric_rooms[key]

                if room not in f.rooms:
                    f.rooms.append(room)

    def _detect_unresolved_class(self, f, words):

        for w in words:

            if w["kind"]:
                continue

            token = w["clean"]

            # class-like: letters AND digits (e.g. "5CS-XX-Z", "Y1-A-9")
            if not (re.search(r"\d", token) and re.search(r"[A-Za-z]", token)):
                continue

            if re.fullmatch(r"\d+(st|nd|rd|th|am|pm)", token, re.I):
                continue

            if re.fullmatch(r"\d{1,2}[:.]\d{2}(am|pm)?", token, re.I):
                continue

            key = alnum_key(token)

            if len(key) < 3:
                continue

            # ... and it must look like a class that EXISTS in this data:
            # share a prefix of >= 3 characters with a known class key
            if any(
                key[:3] == k[:3] and len(k) >= 3
                for k in self.class_keys
            ):

                classes = self.model.classes

                hits = process.extract(
                    token.lower(),
                    [c.lower() for c in classes],
                    scorer=fuzz.WRatio, limit=4, score_cutoff=60,
                )

                f.unresolved.append(
                    ("class", token, [classes[h[2]] for h in hits])
                )

                w["kind"] = "unresolved"
                w["eid"] = 7000

    def _detect_cues(self, f, text):

        cues = set()

        neg_re = self.cue_re.get("negated_free")

        neg_text = text

        if neg_re and neg_re.search(text):
            cues.add("negated")
            neg_text = neg_re.sub(" ", text)

        for group, rx in self.cue_re.items():

            target = neg_text if group == "free" else text

            if rx.search(target):
                cues.add(group)

        if "negated" in cues:
            cues.add("busy")

        if "free" in cues and "busy" not in cues:
            f.polarity = "free"
        elif "busy" in cues and "free" not in cues:
            f.polarity = "busy"
        elif "free" in cues and "busy" in cues:
            f.polarity = "free"

        f.cues = cues

    # ==================================================================
    # cell resolution (days x slots)
    # ==================================================================

    def resolve_cells(self, f):
        """
        Turn a frame's day / slot / time information into concrete cells.

        Returns (days, slots, message).  `message` is a ready-made reply
        when the request cannot be resolved (e.g. nothing is in progress
        right now); otherwise it is None.
        """

        m = self.model

        now = self.now_fn()
        today = DAY_ORDER[now.weekday()]
        minute = now.hour * 60 + now.minute

        days = list(f.days)

        if f.all_days:
            days = list(m.days)

        slots = list(f.slots)

        span = (
            f"{title_day(m.days[0])}–{title_day(m.days[-1])}"
            if m.days else "the loaded days"
        )

        ref = f.slot_ref

        if ref == "current":

            if today not in m.days:
                return None, None, (
                    f"Today is {title_day(today)}, which has no classes in "
                    f"this timetable (it covers {span})."
                )

            current = m.slots_at_minute(minute)

            if not current:

                first = m.teaching_slots[0]
                last = m.teaching_slots[-1]

                return None, None, (
                    f"No class period is in progress right now "
                    f"({title_day(today)}, {fmt_minutes(minute)}). Classes "
                    f"run {m.range_label(first, last) or 'during the day'} "
                    f"({span})."
                )

            days, slots = [today], current

        elif ref == "next":

            nxt = m.next_slot_after(minute) if today in m.days else None

            if nxt is not None:
                days, slots = [today], [nxt]

            else:

                for ahead in range(1, 8):

                    day = DAY_ORDER[(now.weekday() + ahead) % 7]

                    if day in m.days:
                        days, slots = [day], [m.teaching_slots[0]]
                        break

        else:

            if f.time_range:

                found = m.slots_overlapping(*f.time_range)

                if not found:
                    return None, None, (
                        f"No slot overlaps {fmt_minutes(f.time_range[0])}"
                        f"–{fmt_minutes(f.time_range[1])}. Slots run "
                        f"{m.range_label(m.slots[0], m.slots[-1]) or 'at other times'}."
                    )

                slots = sorted(set(slots) | set(found))

            if f.time_point is not None:

                found = m.slots_at_minute(f.time_point)

                if not found:
                    return None, None, (
                        f"No slot is in progress at "
                        f"{fmt_minutes(f.time_point)}. Slots run "
                        f"{m.range_label(m.slots[0], m.slots[-1]) or 'at other times'}."
                    )

                slots = sorted(set(slots) | set(found))

            if ref == "first":
                slots = [m.teaching_slots[0]]

            elif ref == "last":
                slots = [m.teaching_slots[-1]]

            elif ref == "all":
                slots = list(m.slots)

        return days, sorted(set(slots)), None

    # ==================================================================
    # routing
    # ==================================================================

    def _is_legacy_owned(self, f):

        low = f.raw.lower()

        rx = self.cue_re.get("legacy_delegate")

        if rx and rx.search(low):
            return True

        rx = self.cue_re.get("absence")

        if rx and rx.search(low):
            return True

        # "Which rooms are available for <teacher>'s lab ...": a room
        # question about a specific teacher's lab is a lab-shift request.
        c = f.cues

        if (
            f.teachers
            and "room_words" in c
            and ("free" in c or "lab_words" in c)
        ):
            return True

        return False

    def _guard(self, f):

        if f.ambiguous:

            kind, text, options = f.ambiguous[0]

            lines = [
                f"“{text}” matches several {kind}s - which one do you mean?",
                "",
            ]

            lines += [f"{i}. {o}" for i, o in enumerate(options[:8], 1)]

            return "\n".join(lines)

        if f.unresolved:

            kind, text, options = f.unresolved[0]

            reply = f"I couldn't find a {kind} matching “{text}” in the loaded timetable."

            if options:
                reply += " Closest matches:\n\n" + "\n".join(
                    f"{i}. {o}" for i, o in enumerate(options, 1)
                )

            return reply

        if f.bad_slots:

            m = self.model

            times = ", ".join(
                f"{s}{m.slot_paren(s)}" for s in m.slots
            )

            bad = ", ".join(str(s) for s in f.bad_slots)

            return (
                f"There is no slot {bad} in this timetable. "
                f"Valid slots are: {times}."
            )

        if f.unknown_days and not f.days and not f.all_days:

            m = self.model

            span = f"{title_day(m.days[0])}–{title_day(m.days[-1])}"

            day = f.unknown_days[0]

            prefix = (
                f"{f.rel_word.capitalize()} is {title_day(day)}"
                if f.rel_word else title_day(day)
            )

            extra = (
                " so nobody is scheduled to teach - everyone is free."
                if "free" in f.cues else " so nothing is scheduled."
            )

            return (
                f"{prefix} is not part of this timetable (it covers "
                f"{span}),{extra}"
            ) if not f.rel_word else (
                f"{prefix}, which is not part of this timetable (it covers "
                f"{span}),{extra}"
            )

        return None

    def _meta(self, f):

        c = f.cues

        if (
            f.teachers or f.subjects or f.classes or f.rooms
            or f.days or f.slots or f.slot_ref or f.all_days
        ):
            return None

        if c & (INTENT_CUES - {"listing"}):
            if not (c == {"help"} or "help" in c and len(c) <= 2):
                return None

        words = [w["low"] for w in f.words]

        if not words:
            return None

        if "help" in c:
            return self.help_text()

        if "thanks" in c and len(words) <= 5:
            return "You're welcome! Ask me anything else about the timetable."

        if "greeting" in c and len(words) <= 6:
            return (
                "Hello! I can tell you who is free or busy, show timetables "
                "of teachers, classes and rooms, and plan substitutes.\n\n"
                + self.help_text()
            )

        return None

    def _route(self, f):

        c = f.cues

        nT, nS = len(f.teachers), len(f.subjects)
        nC, nR = len(f.classes), len(f.rooms)

        free = "free" in c
        busy = "busy" in c

        slot_given = bool(
            f.slots or f.slot_ref or f.time_range
            or f.time_point is not None
        )
        day_given = bool(f.days or f.all_days)

        text = f.text

        def has(rx):
            return re.search(rx, text) is not None

        which_day = has(r"\b(?:which|what|on which)\s+days?\b")
        which_slot = has(r"\b(?:which|what)\s+slots?\b")

        # ---------------- data quality / conflicts -------------------
        if "data_quality" in c:
            return "data_quality"

        if "conflict" in c and not (nT and free):
            return "conflicts"

        if "no_classes_at_all" in c and not (nT or nS or nC or nR):
            return "faculty_no_classes"

        if "any_section" in c and free and not (nT or nS or nC or nR):
            return "empty_slots"

        # ---------------- coordinator ---------------------------------
        if "makeup" in c or "extra" in c:

            if nC == 1 and nT == 1:
                return "common_free_class_teacher"

            if nS >= 1 and nT == 0:
                return "subject_free_teachers"

            if nC == 1:
                return "makeup_class"

        if nT >= 2:

            if "most" in c or "least" in c or "compare" in c:
                return "compare_teachers"

            if free or "common" in c or "meeting" in c or "both" in c:
                return "common_free_teachers"

        if "meeting" in c and nT == 0 and nC == 0:

            if f.block_size or has(r"\bblocks?\b"):
                return "meeting_blocks"

            return "meeting_best"

        # ---------------- one teacher --------------------------------
        if nT == 1:

            if nS == 1 and "group_words" in c:
                return "teacher_subject_group"

            if "who" in c and ("common" in c) and free:
                return "free_same_time"

            rank = (
                "completely" in c or "most" in c or "least" in c
            )

            if which_day and rank:
                return "teacher_day_rank"

            if "first_last_class" in c:
                return "teacher_first_last"

            if "consecutive" in c:
                return "teacher_back_to_back"

            if "gap" in c:
                return "teacher_gap"

            if "how_many" in c:
                return "teacher_count"

            if "lab_words" in c and not (free or busy):
                return "teacher_lab_check"

            if ("doing" in c or "where" in c) and (slot_given or day_given):
                return "teacher_at"

            if free or busy:
                return "teacher_status"

            if "subject_words" in c:
                return "teacher_subjects"

            if "room_words" in c:
                return "teacher_rooms"

            if "class_words" in c and "timetable" not in c:
                return "teacher_classes"

            return "teacher_timetable"

        # ---------------- subject focused ------------------------------
        if nS >= 1 and nT == 0:

            if nC >= 1 and ("where" in c or "room_words" in c):
                return "class_subject_room"

            if free and ("who" in c or "teaches" in c):
                return "subject_free_teachers"

            if "how_many" in c and "teacher_words" in c:
                return "subject_teacher_count"

            if "how_many" in c:
                return "subject_count"

            if "type_of" in c or (
                "lab_words" in c and "theory_words" in c
            ):
                return "subject_type"

            if "teaches" in c or "who" in c:
                return "subject_teachers"

            if "where" in c or "room_words" in c:
                return "subject_where"

            if "class_words" in c:
                return "subject_classes"

            return "subject_schedule"

        # ---------------- class focused --------------------------------
        if nC >= 1 and nT == 0:

            if "group_words" in c and "lab_words" in c:
                return "class_lab_groups"

            if "lab_words" in c:
                return "class_lab_check"

            if "how_many" in c:
                return "class_count"

            if which_day and ("most" in c or "least" in c):
                return "class_day_rank"

            if "subject_words" in c:
                return "class_subjects"

            if "who" in c or "teaches" in c:
                return "class_teacher_at" if slot_given else "class_teachers"

            if free or busy:
                return "class_status"

            if ("doing" in c or slot_given) and "timetable" not in c:
                return "class_at"

            return "class_timetable"

        # ---------------- room focused ---------------------------------
        if nR >= 1 and nT == 0:

            if free or busy:

                if "when" in c or not slot_given:
                    return "room_free_slots"

                return "room_status"

            if slot_given:
                return "room_at"

            return "room_schedule"

        # ---------------- catalogue questions (no entity) --------------
        if "subject_words" in c:

            if "lab_words" in c and has(r"\bonly\b"):
                return "lab_only_subjects"

            if slot_given or day_given:
                return "subjects_at_cell"

            return "list_subjects"

        if "room_words" in c:

            if "lab_words" in c:
                return "lab_rooms"

            if "never_used" in c:
                return "unused_rooms"

            if "most" in c or "least" in c:
                return "room_rank"

            if free or busy:
                return "free_rooms" if free else "busy_rooms"

            return "list_rooms"

        if "lab_words" in c:

            if "span" in c:
                return "lab_multislot"

            if "nosession" in c:
                return "lab_no_session"

            if "who" in c:
                return "lab_at"

            return "labs_on_day"

        if (
            "class_words" in c
            and "who" not in c
            and "teacher_words" not in c
        ):

            if (free or busy) and (slot_given or day_given):
                return "free_classes" if free else "busy_classes"

            return "list_classes"

        # ---------------- aggregates over faculty ---------------------
        if "no_classes_at_all" in c:
            return "faculty_no_classes"

        if "any_section" in c and free:
            return "empty_slots"

        if "consecutive" in c:
            return "faculty_consecutive"

        if which_day and ("most" in c or "least" in c):
            return "day_rank_faculty"

        if which_slot and ("most" in c or "least" in c):
            return "slot_rank_faculty"

        if (
            "works" in c and f.all_days and not slot_given
            and ("who" in c or "teacher_words" in c)
        ):
            return "faculty_all_days"

        if ("most" in c or "least" in c) and (
            "who" in c or "teacher_words" in c
        ) and (free or "workload" in c or "how_many" in c or busy):
            return "faculty_rank"

        # ---------------- free / busy lists ---------------------------
        if free and busy and self._split_free_busy(f):
            return "free_but_busy"

        if free:
            return "free_list"

        if busy or (
            "works" in c and ("who" in c or "teacher_words" in c)
        ):
            return "busy_list"

        # ---------------- bare day/slot ("Mon 3") ---------------------
        if (day_given or slot_given) and not (c & INTENT_CUES):
            return "free_list"

        if "who" in c and (day_given or slot_given):
            return "free_list"

        return None

    # ------------------------------------------------------------------

    def _split_free_busy(self, f):
        """'free at A but busy at B' -> (free_part, busy_part) or None."""

        text = f.text

        found = re.search(
            r"^(?P<a>.*?)\b(?:but|and|yet|while|whereas)\b\s*"
            r"(?:is|are|also|then|being|not)?\s*(?P<b>.*)$",
            text,
        )

        if not found:
            return None

        a, b = found.group("a"), found.group("b")

        def polarity(part):

            neg = self.cue_re.get("negated_free")

            has_neg = bool(neg and neg.search(part))

            base = neg.sub(" ", part) if neg else part

            if has_neg or self.cue_re["busy"].search(part):
                return "busy"

            if self.cue_re["free"].search(base):
                return "free"

            return None

        pa, pb = polarity(a), polarity(b)

        if {pa, pb} == {"free", "busy"}:
            return (a, pa), (b, pb)

        return None

    # ==================================================================
    # respond
    # ==================================================================

    def _respond(self, f):

        if self._is_legacy_owned(f):
            return None

        meta = self._meta(f)

        if meta is not None:
            self.last_route = "meta"
            return meta

        guard = self._guard(f)

        if guard is not None:
            self.last_route = "guard"
            return guard

        # "Which of them ...": filter the previous answer
        if self.last and self._context_alive() and \
                self.last.get("names") and \
                "pronouns" in f.cues and self._is_filter_request(f):

            return self.h_filter_last(f)

        route = None
        merged = f

        bare = not (f.cues & INTENT_CUES) and not (
            f.teachers or f.subjects or f.classes or f.rooms
        )

        hint = bool(
            f.days or f.slots or f.slot_ref or f.time_range or f.all_days
            or f.time_point is not None
            or "pronouns" in f.cues or "followup_starters" in f.cues
        )

        if self.last and self._context_alive() and bare and hint:

            merged, route = self._follow_up(f)

        if route is None:

            merged = f

            route = self._route(f)

            if route is None and self.last and self._context_alive():

                merged, route = self._follow_up(f)

        if route is None:
            return None

        handler = getattr(self, "h_" + route, None)

        if handler is None:
            return None

        response = handler(merged)

        if response is None:
            return None

        self._remember(route, merged)

        self.last_route = route

        return response

    def reset_context(self):
        """Forget the previous question (start a fresh conversation)."""

        self.last = None
        self._last_names = None

    def _context_alive(self):

        if not self.last:
            return False

        ttl = self.lex.get("context_ttl_minutes")

        if not ttl:
            return True

        age = self.now_fn() - self.last["time"]

        return age.total_seconds() <= float(ttl) * 60

    def _is_filter_request(self, f):

        c = f.cues

        return bool(c & {"lab_words", "theory_words", "free", "busy"}) or \
            "teaches" in c

    def _remember(self, route, f, names=None):

        previous = self.last or {}

        self.last = {
            "route": route,
            "frame": f,
            "time": self.now_fn(),
            "names": names if names is not None else getattr(
                self, "_last_names", None
            ),
        }

        self._last_names = None

        return previous

    def _follow_up(self, f):
        """Complete an elliptical question from the previous one."""

        last = self.last

        prev = last["frame"]

        c = f.cues

        has_entity = bool(
            f.teachers or f.subjects or f.classes or f.rooms
        )

        contextual = bool(
            f.days or f.slots or f.slot_ref or f.time_range
            or f.time_point is not None or has_entity
            or "pronouns" in c or "followup_starters" in c
            or c & {"who", "where", "when", "how_many"}
        )

        if not contextual:
            return f, None

        g = f.copy()

        for attr in ("teachers", "subjects", "classes", "rooms", "groups"):

            if not getattr(g, attr) and getattr(prev, attr):

                pronoun = "pronouns" in c

                if pronoun or not has_entity:
                    setattr(g, attr, list(getattr(prev, attr)))

        if not (g.days or g.all_days or g.unknown_days):
            g.days = list(prev.days)
            g.all_days = prev.all_days

        if not (g.slots or g.slot_ref or g.time_range
                or g.time_point is not None):
            g.slots = list(prev.slots)
            g.slot_ref = prev.slot_ref
            g.time_range = prev.time_range
            g.time_point = prev.time_point

        if not (("free" in c) or ("busy" in c)) and prev.polarity:
            g.cues.add(prev.polarity)
            g.polarity = prev.polarity

        intent = c & INTENT_CUES

        if not intent:
            return g, last["route"]

        route = self._route(g)

        return g, route